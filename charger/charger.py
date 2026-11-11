"""The charge point. Works with no network: authorizes from a signed voucher,
meters energy, enforces the voucher cap, and queues everything in the outbox."""
import secrets
import uuid

from common.clock import Clock
from common.crypto import sign, verify
from common.protocol import auth_message, price_cents, receipt_bytes
from common.sqlite_tx import transaction

from .local_auth.voucher_verify import AuthError, VoucherError, VoucherStore, verify_voucher
from .offline_queue.db import get_meta, set_meta


class Charger:
    def __init__(self, charger_id, signing_key, issuer_pub, outbox, clock: Clock = None,
                 rate_cents_per_kwh=25, power_w=7400):
        self.charger_id = charger_id
        self.sk = signing_key
        self.issuer_pub = issuer_pub
        self.outbox = outbox
        self.conn = outbox.conn
        self.clock = clock or Clock()
        self.rate = rate_cents_per_kwh
        self.power_w = power_w
        self.store = VoucherStore(self.conn)
        self._challenges = set()

    # ---- authorization ----
    def new_challenge(self) -> str:
        c = secrets.token_hex(16)
        self._challenges.add(c)
        return c

    def start_session(self, voucher, challenge, challenge_sig) -> str:
        if challenge not in self._challenges:
            raise AuthError("unknown_or_reused_challenge")
        self._challenges.discard(challenge)
        verify_voucher(voucher, self.issuer_pub, self.clock.now_ms(), self.charger_id)
        # proof of possession: whoever holds the voucher must also hold its key
        if not verify(voucher["user_pub"], auth_message(challenge), challenge_sig):
            raise AuthError("bad_proof_of_possession")
        with transaction(self.conn):
            self.store.register(voucher)
            if self.store.remaining(voucher["voucher_id"]) <= 0:
                raise VoucherError("exhausted")
            if self.store.has_active(voucher["voucher_id"]):
                raise VoucherError("session_already_active")
            tx_id = str(uuid.uuid4())
            meter = int(get_meta(self.conn, "meter_wh", 0))
            now = self.clock.now_ms()
            self.conn.execute(
                "INSERT INTO local_tx(tx_id,voucher_id,user_pub,state,start_meter_wh,started_at) "
                "VALUES(?,?,?,?,?,?)",
                (tx_id, voucher["voucher_id"], voucher["user_pub"], "ACTIVE", meter, now))
            self.outbox.enqueue("START_TX", {"tx_id": tx_id, "voucher_id": voucher["voucher_id"],
                                             "user_pub": voucher["user_pub"],
                                             "meter_start_wh": meter, "ts": now},
                                tx_id=tx_id, priority=0)
        return tx_id

    # ---- charging ----
    def tick(self, tx_id, seconds) -> dict:
        tx = self._active_tx(tx_id)
        remaining = self.store.remaining(tx["voucher_id"])
        max_wh = remaining * 1000 // self.rate             # what the voucher can still pay for
        session_wh = tx["session_wh"] + int(self.power_w * seconds / 3600)
        cap_reached = session_wh >= max_wh
        session_wh = min(session_wh, max_wh)
        meter = tx["start_meter_wh"] + session_wh
        with transaction(self.conn):
            self.conn.execute("UPDATE local_tx SET session_wh=? WHERE tx_id=?", (session_wh, tx_id))
            set_meta(self.conn, "meter_wh", meter)
            self.outbox.enqueue("METER", {"tx_id": tx_id, "meter_wh": meter,
                                          "session_wh": session_wh,
                                          "cents": price_cents(session_wh, self.rate),
                                          "ts": self.clock.now_ms()},
                                tx_id=tx_id, priority=2)
        return {"session_wh": session_wh, "cents": price_cents(session_wh, self.rate),
                "cap_reached": cap_reached}

    def stop_session(self, tx_id, wallet=None, reason="Local") -> dict:
        tx = self._active_tx(tx_id)
        now = self.clock.now_ms()
        wh = tx["session_wh"]
        receipt = {"tx_id": tx_id, "charger_id": self.charger_id,
                   "voucher_id": tx["voucher_id"], "user_pub": tx["user_pub"],
                   "meter_start_wh": tx["start_meter_wh"],
                   "meter_end_wh": tx["start_meter_wh"] + wh, "wh": wh,
                   "rate_cents_per_kwh": self.rate, "cents": price_cents(wh, self.rate),
                   "start_ts": tx["started_at"], "stop_ts": now}
        charger_sig = sign(self.sk, receipt_bytes(receipt))
        user_sig = None
        if wallet is not None:
            try:
                user_sig = wallet.cosign(receipt)       # phone countersigns over BLE/NFC/QR
            except Exception:                           # WalletRefused or phone unreachable
                user_sig = None
        with transaction(self.conn):
            self.conn.execute("UPDATE local_tx SET state='STOPPED', stopped_at=? WHERE tx_id=?",
                              (now, tx_id))
            self.store.add_spent(tx["voucher_id"], receipt["cents"])
            self.outbox.enqueue("STOP_TX", {"tx_id": tx_id, "meter_stop_wh": receipt["meter_end_wh"],
                                            "reason": reason, "ts": now}, tx_id=tx_id, priority=0)
            self.outbox.enqueue("RECEIPT", {"receipt": receipt, "charger_sig": charger_sig,
                                            "user_sig": user_sig}, tx_id=tx_id, priority=0)
        return {"receipt": receipt, "user_sig": user_sig}

    def _active_tx(self, tx_id):
        tx = self.conn.execute("SELECT * FROM local_tx WHERE tx_id=?", (tx_id,)).fetchone()
        if not tx or tx["state"] != "ACTIVE":
            raise VoucherError("no_active_session")
        return tx
