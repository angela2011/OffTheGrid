"""End-to-end demo:  python -m simulation.demo

1. Online: user gets a signed voucher (funds locked in escrow)
2. Network drops. Charger still authorizes, charges, enforces the cap, signs receipts
3. A thief with a copied voucher is rejected offline
4. Network returns: queued events sync, backend dedupes, receipts settle
"""
import json
import urllib.request

from backend.ledger import Ledger
from backend.server import make_server, start_in_thread
from charger.charger import Charger
from charger.local_auth.voucher_verify import AuthError, VoucherError
from charger.offline_queue import db
from charger.offline_queue.outbox import Outbox
from charger.offline_queue.sender import Sender
from charger.transport.http_transport import HttpTransport, SimulatedLink
from common.clock import FakeClock
from common.crypto import generate_keypair
from mobile_app.wallet import Wallet


def step(msg):
    print(f"\n=== {msg}")


def main():
    clock = FakeClock()

    # --- backend + charger provisioning ---
    ledger = Ledger(clock=clock)
    server = make_server(ledger)
    start_in_thread(server)
    url = f"http://127.0.0.1:{server.server_address[1]}"

    ch_sk, ch_pk = generate_keypair()
    ledger.register_charger("CHG-001", ch_pk)
    outbox = Outbox(db.connect(":memory:"), "CHG-001", ch_sk, clock)
    charger = Charger("CHG-001", ch_sk, ledger.issuer_pk, outbox, clock)
    link = SimulatedLink(HttpTransport(url))
    sender = Sender(outbox, link)

    u_sk, u_pk = generate_keypair()
    wallet = Wallet(u_sk, u_pk)

    # --- 1. ONLINE: voucher issuance ---
    step("ONLINE: user buys a 500-cent voucher (funds locked in escrow)")
    req = urllib.request.Request(url + "/vouchers/issue", method="POST",
                                 data=json.dumps({"user_pub": u_pk, "max_cents": 500}).encode(),
                                 headers={"Content-Type": "application/json"})
    voucher = json.loads(urllib.request.urlopen(req).read())
    wallet.add_voucher(voucher)
    print("voucher", voucher["voucher_id"][:8], "cap", voucher["max_cents"], "cents")

    # --- 2. OFFLINE ---
    step("NETWORK DOWN (user is in a remote area)")
    link.online = False

    ch = charger.new_challenge()
    tx1 = charger.start_session(voucher, ch, wallet.sign_challenge(ch))
    print("session 1 authorized offline:", tx1[:8])
    for _ in range(3):
        clock.advance(600)
        s = charger.tick(tx1, 600)
    r1 = charger.stop_session(tx1, wallet)
    print(f"session 1: {r1['receipt']['wh']} Wh = {r1['receipt']['cents']} cents, countersigned={bool(r1['user_sig'])}")
    print("sender while offline:", sender.drain_once())
    print("queue:", outbox.counts())

    step("THIEF copies the voucher but doesn't hold the user's key")
    thief_sk, thief_pk = generate_keypair()
    thief = Wallet(thief_sk, thief_pk)
    ch = charger.new_challenge()
    try:
        charger.start_session(voucher, ch, thief.sign_challenge(ch))
    except AuthError as e:
        print("rejected offline:", e)

    step("Session 2: runs until the voucher cap is hit")
    ch = charger.new_challenge()
    tx2 = charger.start_session(voucher, ch, wallet.sign_challenge(ch))
    while True:
        clock.advance(600)
        s = charger.tick(tx2, 600)
        if s["cap_reached"]:
            break
    r2 = charger.stop_session(tx2, wallet)
    print(f"session 2 stopped at cap: {r2['receipt']['wh']} Wh = {r2['receipt']['cents']} cents")
    ch = charger.new_challenge()
    try:
        charger.start_session(voucher, ch, wallet.sign_challenge(ch))
    except VoucherError as e:
        print("session 3 refused:", e)

    print("queue:", outbox.counts(), "| chain intact:", outbox.verify_chain())

    # --- 3. ONLINE AGAIN ---
    step("NETWORK RESTORED")
    link.online = True
    sender.on_link_up()
    print("drain:", sender.drain_all())
    print("queue:", outbox.counts())

    step("Replay the same events again (e.g. lost ack) -> backend dedupes")
    rows = outbox.conn.execute("SELECT * FROM outbox").fetchall()
    batch = [dict(outbox.envelope(r), charger_id="CHG-001") for r in rows[:5]]
    print("replay result acked:", len(ledger.ingest(batch)["acked"]), "| events stored:",
          ledger.summary()["events_stored"])

    step("LEDGER")
    print(json.dumps(ledger.summary(), indent=2))
    print("audit CHG-001:", ledger.audit_chain("CHG-001") or "chain OK, no gaps")
    server.shutdown()


if __name__ == "__main__":
    main()
