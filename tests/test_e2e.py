import json

import urllib.request

from backend.server import make_server, start_in_thread
from charger.offline_queue.sender import Sender
from charger.transport.http_transport import HttpTransport, SimulatedLink
from charger.transport.base import Transport


class Direct(Transport):
    """Calls the ledger in-process (no HTTP) for fast tests."""

    def __init__(self, ledger):
        self.ledger = ledger

    def send(self, batch):
        return self.ledger.ingest(batch)


def sync(charger, ledger):
    s = Sender(charger.outbox, Direct(ledger))
    s.on_link_up()
    return s.drain_all()


def test_offline_session_settles_after_reconnect(rig):
    c, w = rig.charger(), rig.wallet()
    v = rig.voucher(w, 500)
    tx = rig.start(c, w, v)
    rig.charge(c, tx, 30)
    receipt = c.stop_session(tx, w)["receipt"]
    assert rig.ledger.summary()["receipts"] == []        # nothing reached backend yet
    res = sync(c, rig.ledger)
    assert res["rejected"] == 0 and c.outbox.pending() == 0
    rec = rig.ledger.summary()["receipts"][0]
    assert rec["status"] == "SETTLED" and rec["paid_cents"] == receipt["cents"]
    assert rig.ledger.audit_chain("CHG-001") == []


def test_replayed_batch_is_idempotent(rig):
    c, w = rig.charger(), rig.wallet()
    tx = rig.start(c, w, rig.voucher(w))
    rig.charge(c, tx, 20)
    c.stop_session(tx, w)
    batch = c.outbox.claim_batch(100)
    for e in batch:
        e["charger_id"] = "CHG-001"
    first = rig.ledger.ingest(batch)
    second = rig.ledger.ingest(batch)
    assert first == second and not second["rejected"]
    s = rig.ledger.summary()
    assert s["events_stored"] == len(batch) and len(s["receipts"]) == 1
    assert s["vouchers"][0]["spent_cents"] == s["receipts"][0]["paid_cents"]


def test_double_spend_across_two_offline_chargers_is_capped_and_flagged(rig):
    a, b, w = rig.charger("CHG-A"), rig.charger("CHG-B"), rig.wallet()
    v = rig.voucher(w, 500)
    for ch in (a, b):                                   # both offline, both think cap is untouched
        tx = rig.start(ch, w, v)
        for _ in range(12):
            ch.tick(tx, 600)                            # ~400 cents each
        # wallet would refuse the 2nd cosign (exceeds_voucher) -> that charger is left unsigned
        ch.stop_session(tx, w)
    sync(a, rig.ledger)
    sync(b, rig.ledger)
    rows = {r["charger_id"]: r for r in rig.ledger.summary()["receipts"]}
    assert rows["CHG-A"]["status"] == "SETTLED"
    assert rows["CHG-B"]["status"] == "HELD"            # wallet refused to sign over the cap
    assert rig.ledger.summary()["vouchers"][0]["spent_cents"] <= 500


def test_ledger_caps_payout_when_phone_cosigns_over_the_cap(rig):
    """Worst case: the phone's own spend tracking is bypassed, so both offline
    chargers get valid user signatures. The ledger must still never pay > cap."""
    a, b, w = rig.charger("CHG-A"), rig.charger("CHG-B"), rig.wallet()
    v = rig.voucher(w, 500)
    for ch in (a, b):
        tx = rig.start(ch, w, v)
        for _ in range(12):
            ch.tick(tx, 600)                       # ~400 cents each
        ch.stop_session(tx, w)
        w.spent[v["voucher_id"]] = 0               # simulate compromised / reset wallet
    sync(a, rig.ledger)
    sync(b, rig.ledger)
    s = rig.ledger.summary()
    status = {r["charger_id"]: r for r in s["receipts"]}
    assert status["CHG-A"]["status"] == "SETTLED"
    assert status["CHG-B"]["status"] == "PARTIAL"            # double-spend detected
    assert "shortfall" in status["CHG-B"]["reason"]
    assert s["vouchers"][0]["spent_cents"] == 500            # never exceeds the escrow lock


def test_forged_event_rejected_and_marked_dead(rig):
    c, w = rig.charger(), rig.wallet()
    tx = rig.start(c, w, rig.voucher(w))
    c.outbox.conn.execute("UPDATE outbox SET payload='{\"tx_id\":\"x\"}' WHERE seq=1")  # tamper after signing
    res = sync(c, rig.ledger)
    assert res["rejected"] == 1
    assert c.outbox.counts().get("DEAD") == 1


def test_http_roundtrip_with_link_toggle(rig):
    c, w = rig.charger(), rig.wallet()
    server = make_server(rig.ledger)
    start_in_thread(server)
    link = SimulatedLink(HttpTransport(f"http://127.0.0.1:{server.server_address[1]}"), online=False)
    sender = Sender(c.outbox, link)
    try:
        tx = rig.start(c, w, rig.voucher(w))
        rig.charge(c, tx, 20)
        c.stop_session(tx, w)
        assert sender.drain_once()["error"]
        link.online = True
        sender.on_link_up()
        assert sender.drain_all()["error"] is None
        ledger = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_address[1]}/ledger").read())
        assert ledger["receipts"][0]["status"] == "SETTLED"
    finally:
        server.shutdown()


def test_unsigned_receipt_is_held_not_paid(rig):
    c, w = rig.charger(), rig.wallet()
    tx = rig.start(c, w, rig.voucher(w))
    rig.charge(c, tx, 20)
    c.stop_session(tx, None)                             # user walked away, no countersignature
    sync(c, rig.ledger)
    rec = rig.ledger.summary()["receipts"][0]
    assert rec["status"] == "HELD" and rec["paid_cents"] == 0


def test_unused_funds_refunded_after_grace(rig):
    c, w = rig.charger(), rig.wallet()
    tx = rig.start(c, w, rig.voucher(w, 500, ttl_s=3600))
    rig.charge(c, tx, 20)
    spent = c.stop_session(tx, w)["receipt"]["cents"]
    sync(c, rig.ledger)
    rig.clock.advance(3600 + 8 * 24 * 3600)
    (vid, refund), = rig.ledger.refund_expired()
    assert refund == 500 - spent
