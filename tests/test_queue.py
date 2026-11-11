import sqlite3

from charger.offline_queue import db
from charger.offline_queue.outbox import Outbox
from charger.offline_queue.sender import Sender
from charger.transport.base import Transport, TransportError
from common.crypto import generate_keypair


def make_outbox(path=":memory:"):
    sk, _ = generate_keypair()
    return Outbox(db.connect(path), "C1", sk)


class FakeServer(Transport):
    def __init__(self):
        self.up, self.seen = True, []

    def send(self, batch):
        if not self.up:
            raise TransportError("down")
        self.seen += [e["seq"] for e in batch]
        return {"acked": [e["event_id"] for e in batch], "rejected": {}}


def test_enqueue_chains_and_orders():
    ob = make_outbox()
    for i in range(5):
        ob.enqueue("METER", {"i": i}, tx_id="t")
    assert ob.verify_chain() == (True, None)
    assert [e["seq"] for e in ob.claim_batch(10)] == [1, 2, 3, 4, 5]


def test_offline_then_online_delivers_all_in_order_once():
    ob, srv = make_outbox(), FakeServer()
    sender = Sender(ob, srv)
    srv.up = False
    for i in range(1000):
        ob.enqueue("METER", {"i": i}, tx_id="t")
    assert sender.drain_once()["error"]
    srv.up = True
    sender.on_link_up()
    sender.drain_all()
    assert srv.seen == list(range(1, 1001))
    assert ob.pending() == 0


def test_backoff_blocks_until_link_up():
    ob, srv = make_outbox(), FakeServer()
    sender = Sender(ob, srv)
    ob.enqueue("START_TX", {}, tx_id="t")
    srv.up = False
    sender.drain_once()
    srv.up = True
    assert sender.drain_once()["sent"] == 0          # still backing off
    sender.on_link_up()
    assert sender.drain_once()["acked"] == 1


def test_crash_mid_send_recovers(tmp_path):
    path = str(tmp_path / "c.db")
    ob = make_outbox(path)
    ob.enqueue("START_TX", {}, tx_id="t")
    ob.claim_batch()                                  # INFLIGHT, then "crash"
    assert ob.counts() == {"INFLIGHT": 1}
    ob2 = Outbox(db.connect(path), "C1", ob.sk)       # reboot
    ob2.recover()
    assert ob2.counts() == {"PENDING": 1}


def test_tamper_detected():
    ob = make_outbox()
    for i in range(4):
        ob.enqueue("METER", {"kwh": i}, tx_id="t")
    ob.conn.execute("UPDATE outbox SET payload='{\"kwh\":999}' WHERE seq=2")
    assert ob.verify_chain() == (False, 2)


def test_deleted_row_detected():
    ob = make_outbox()
    for i in range(4):
        ob.enqueue("METER", {"i": i}, tx_id="t")
    ob.conn.execute("DELETE FROM outbox WHERE seq=3")
    assert ob.verify_chain() == (False, 4)


def test_prune_keeps_chain_valid_and_seq_monotonic():
    ob, srv = make_outbox(), FakeServer()
    for i in range(3):
        ob.enqueue("METER", {"i": i})
    Sender(ob, srv).drain_all()
    assert ob.prune_acked() == 3
    ob.enqueue("METER", {"i": 3})
    assert ob.verify_chain() == (True, None)
    assert ob.conn.execute("SELECT seq FROM outbox").fetchone()["seq"] == 4


def test_state_and_event_commit_atomically():
    ob = make_outbox()
    try:
        with __import__("common.sqlite_tx", fromlist=["x"]).transaction(ob.conn):
            ob.conn.execute("INSERT INTO local_tx(tx_id,voucher_id,user_pub,state,start_meter_wh,started_at)"
                            " VALUES('t','v','u','ACTIVE',0,0)")
            ob.enqueue("START_TX", {}, tx_id="t")
            raise RuntimeError("power cut")
    except RuntimeError:
        pass
    assert ob.conn.execute("SELECT COUNT(*) c FROM local_tx").fetchone()["c"] == 0
    assert ob.conn.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"] == 0
