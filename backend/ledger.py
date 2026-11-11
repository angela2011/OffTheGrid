"""Cloud side: issues vouchers (escrow lock), ingests queued events idempotently,
and settles receipts. The escrow here is simulated in SQLite; swap the marked
spots for calls to your Solidity escrow contract."""
import json
import sqlite3
import threading
import uuid

from common.clock import Clock
from common.crypto import canonical, generate_keypair, sign, verify
from common.protocol import event_digest, price_cents, receipt_bytes, voucher_body
from common.sqlite_tx import transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS chargers (charger_id TEXT PRIMARY KEY, pubkey TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY, charger_id TEXT, seq INTEGER, tx_id TEXT, event_type TEXT,
  payload TEXT, created_at INTEGER, time_trusted INTEGER, prev_hash TEXT, hash TEXT,
  UNIQUE(charger_id, seq));
CREATE TABLE IF NOT EXISTS vouchers (
  voucher_id TEXT PRIMARY KEY, user_pub TEXT, max_cents INTEGER, expiry_ms INTEGER,
  allowed_stations TEXT, locked_cents INTEGER, spent_cents INTEGER DEFAULT 0,
  refunded_cents INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS receipts (
  tx_id TEXT PRIMARY KEY, voucher_id TEXT, charger_id TEXT, wh INTEGER, cents INTEGER,
  paid_cents INTEGER, status TEXT, reason TEXT, event_id TEXT);
"""


class Rejected(Exception):
    pass


class Ledger:
    def __init__(self, path=":memory:", clock=None, issuer_keys=None, grace_ms=7 * 24 * 3600 * 1000):
        self.clock = clock or Clock()
        self.issuer_sk, self.issuer_pk = issuer_keys or generate_keypair()
        self.grace_ms = grace_ms
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    def register_charger(self, charger_id, pubkey):
        with self.lock, transaction(self.conn):
            self.conn.execute("INSERT OR REPLACE INTO chargers VALUES(?,?)", (charger_id, pubkey))

    # ---- vouchers / escrow ----
    def issue_voucher(self, user_pub, max_cents, ttl_s=24 * 3600, allowed_stations=None) -> dict:
        now = self.clock.now_ms()
        v = {"voucher_id": str(uuid.uuid4()), "user_pub": user_pub, "max_cents": max_cents,
             "issued_at": now, "expiry_ms": now + ttl_s * 1000,
             "nonce": uuid.uuid4().hex, "allowed_stations": allowed_stations}
        v["issuer_sig"] = sign(self.issuer_sk, voucher_body(v))
        with self.lock, transaction(self.conn):
            # >>> ESCROW HOOK: lock `max_cents` in the escrow contract here <<<
            self.conn.execute(
                "INSERT INTO vouchers(voucher_id,user_pub,max_cents,expiry_ms,allowed_stations,locked_cents)"
                " VALUES(?,?,?,?,?,?)",
                (v["voucher_id"], user_pub, max_cents, v["expiry_ms"],
                 json.dumps(allowed_stations), max_cents))
        return v

    def refund_expired(self) -> list:
        """After expiry + grace (time for late receipts to arrive), return unused funds."""
        now = self.clock.now_ms()
        out = []
        with self.lock, transaction(self.conn):
            rows = self.conn.execute(
                "SELECT * FROM vouchers WHERE refunded_cents=0 AND expiry_ms + ? < ?",
                (self.grace_ms, now)).fetchall()
            for r in rows:
                refund = r["locked_cents"] - r["spent_cents"]
                # >>> ESCROW HOOK: release `refund` back to the user <<<
                self.conn.execute("UPDATE vouchers SET refunded_cents=? WHERE voucher_id=?",
                                  (refund, r["voucher_id"]))
                out.append((r["voucher_id"], refund))
        return out

    # ---- ingest ----
    def ingest(self, events: list) -> dict:
        acked, rejected = [], {}
        with self.lock:
            for e in events:
                try:
                    self._ingest_one(e)
                    acked.append(e["event_id"])
                except Rejected as r:
                    rejected[e.get("event_id", "?")] = str(r)
                except (KeyError, TypeError, ValueError) as ex:
                    rejected[e.get("event_id", "?") if isinstance(e, dict) else "?"] = f"malformed:{ex}"
        return {"acked": acked, "rejected": rejected}

    def _ingest_one(self, e):
        with transaction(self.conn):
            ch = self.conn.execute("SELECT pubkey FROM chargers WHERE charger_id=?",
                                   (e["charger_id"],)).fetchone()
            if ch is None:
                raise Rejected("unknown_charger")
            digest = event_digest(e["prev_hash"], e["event_id"], e["charger_id"], e["event_type"],
                                  e["tx_id"], e["payload"], e["created_at"], e["seq"])
            if digest.hex() != e["hash"]:
                raise Rejected("bad_hash")
            if not verify(ch["pubkey"], digest, e["sig"]):
                raise Rejected("bad_signature")
            dup = self.conn.execute("SELECT event_id FROM events WHERE charger_id=? AND seq=?",
                                    (e["charger_id"], e["seq"])).fetchone()
            if dup:
                if dup["event_id"] == e["event_id"]:
                    return                                # replay: already have it -> idempotent ack
                raise Rejected("seq_conflict")
            if self.conn.execute("SELECT 1 FROM events WHERE event_id=?", (e["event_id"],)).fetchone():
                raise Rejected("duplicate_event_id")
            self.conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)",
                (e["event_id"], e["charger_id"], e["seq"], e["tx_id"], e["event_type"], e["payload"],
                 e["created_at"], e.get("time_trusted", 1), e["prev_hash"], e["hash"]))
            if e["event_type"] == "RECEIPT":
                self._settle(e, ch["pubkey"])

    def _settle(self, e, charger_pub):
        p = json.loads(e["payload"])
        r = p["receipt"]

        def record(status, reason="", paid=0):
            self.conn.execute("INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?,?,?,?,?)",
                              (r["tx_id"], r["voucher_id"], e["charger_id"], r["wh"], r["cents"],
                               paid, status, reason, e["event_id"]))

        if self.conn.execute("SELECT 1 FROM receipts WHERE tx_id=?", (r["tx_id"],)).fetchone():
            return                                        # same tx settled once only
        v = self.conn.execute("SELECT * FROM vouchers WHERE voucher_id=?", (r["voucher_id"],)).fetchone()
        if v is None:
            return record("REJECTED", "unknown_voucher")
        if r["charger_id"] != e["charger_id"] or not verify(charger_pub, receipt_bytes(r), p.get("charger_sig")):
            return record("REJECTED", "bad_charger_sig")
        if r["user_pub"] != v["user_pub"]:
            return record("REJECTED", "user_mismatch")
        allowed = json.loads(v["allowed_stations"] or "null")
        if allowed and e["charger_id"] not in allowed:
            return record("REJECTED", "station_not_allowed")
        if r["stop_ts"] > v["expiry_ms"]:
            return record("REJECTED", "expired")
        if r["wh"] != r["meter_end_wh"] - r["meter_start_wh"] or \
                r["cents"] != price_cents(r["wh"], r["rate_cents_per_kwh"]):
            return record("REJECTED", "math_mismatch")
        if not p.get("user_sig"):
            return record("HELD", "no_user_countersignature")   # needs evidence/dispute process
        if not verify(v["user_pub"], receipt_bytes(r), p["user_sig"]):
            return record("REJECTED", "bad_user_sig")
        remaining = v["max_cents"] - v["spent_cents"]
        paid = min(r["cents"], remaining)
        # >>> ESCROW HOOK: pay `paid` from escrow to the station operator <<<
        self.conn.execute("UPDATE vouchers SET spent_cents = spent_cents + ? WHERE voucher_id=?",
                          (paid, v["voucher_id"]))
        if paid < r["cents"]:
            record("PARTIAL", f"overspend_shortfall_{r['cents'] - paid}", paid)  # double-spend detected
        else:
            record("SETTLED", "", paid)

    # ---- reporting ----
    def audit_chain(self, charger_id) -> list:
        """Missing sequence numbers or broken hash links per charger."""
        rows = self.conn.execute("SELECT seq, prev_hash, hash FROM events WHERE charger_id=? ORDER BY seq",
                                 (charger_id,)).fetchall()
        problems, prev = [], None
        for r in rows:
            if prev is not None:
                if r["seq"] != prev["seq"] + 1:
                    problems.append(f"gap between seq {prev['seq']} and {r['seq']}")
                elif r["prev_hash"] != prev["hash"]:
                    problems.append(f"chain break at seq {r['seq']}")
            prev = r
        return problems

    def summary(self) -> dict:
        with self.lock:
            q = lambda s: [dict(r) for r in self.conn.execute(s).fetchall()]
            return {
                "vouchers": q("SELECT voucher_id, max_cents, locked_cents, spent_cents, refunded_cents FROM vouchers"),
                "receipts": q("SELECT tx_id, charger_id, wh, cents, paid_cents, status, reason FROM receipts"),
                "operator_payouts": q("SELECT charger_id, SUM(paid_cents) AS paid_cents FROM receipts GROUP BY charger_id"),
                "events_stored": self.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"],
            }
