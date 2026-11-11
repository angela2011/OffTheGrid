"""Write-ahead outbox: every outgoing event is persisted, hash-chained and signed
BEFORE any network attempt. Charging logic never talks to the network directly."""
import random
import uuid

from common.clock import Clock
from common.crypto import canonical, sign
from common.protocol import event_digest
from common.sqlite_tx import transaction

from .db import get_meta, set_meta

GENESIS = "00" * 32
BACKOFF_BASE_MS = 5_000
BACKOFF_CAP_MS = 15 * 60_000


class Outbox:
    def __init__(self, conn, charger_id: str, signing_key: str, clock: Clock = None):
        self.conn = conn
        self.charger_id = charger_id
        self.sk = signing_key
        self.clock = clock or Clock()

    # ---- write side ----
    def enqueue(self, event_type: str, payload: dict, tx_id=None, priority=1) -> str:
        """Safe to call inside a caller's transaction (joins it) so that state
        updates and the event they produce commit atomically."""
        with transaction(self.conn):
            prev = get_meta(self.conn, "last_hash", GENESIS)
            seq = int(get_meta(self.conn, "next_seq", 1))
            event_id = str(uuid.uuid4())
            payload_s = canonical(payload).decode()
            created = self.clock.now_ms()
            digest = event_digest(prev, event_id, self.charger_id, event_type,
                                  tx_id, payload_s, created, seq)
            self.conn.execute(
                "INSERT INTO outbox(seq,event_id,tx_id,event_type,priority,payload,"
                "created_at,time_trusted,prev_hash,hash,sig) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (seq, event_id, tx_id, event_type, priority, payload_s, created,
                 1 if self.clock.trusted else 0, prev, digest.hex(), sign(self.sk, digest)))
            set_meta(self.conn, "last_hash", digest.hex())
            set_meta(self.conn, "next_seq", seq + 1)
        return event_id

    # ---- read / drain side ----
    def claim_batch(self, limit=50):
        """Oldest PENDING events in strict seq order (keeps per-tx ordering).
        Stops at the first event still in backoff so order is never skipped."""
        now = self.clock.now_ms()
        rows = self.conn.execute(
            "SELECT * FROM outbox WHERE status='PENDING' ORDER BY seq LIMIT ?", (limit,)).fetchall()
        batch = []
        for r in rows:
            if r["next_retry_at"] > now:
                break
            batch.append(r)
        if batch:
            with transaction(self.conn):
                self.conn.executemany("UPDATE outbox SET status='INFLIGHT' WHERE event_id=?",
                                      [(r["event_id"],) for r in batch])
        return [self.envelope(r) for r in batch]

    @staticmethod
    def envelope(r) -> dict:
        return {"event_id": r["event_id"], "seq": r["seq"], "charger_id": None,
                "event_type": r["event_type"], "tx_id": r["tx_id"], "payload": r["payload"],
                "created_at": r["created_at"], "time_trusted": r["time_trusted"],
                "prev_hash": r["prev_hash"], "hash": r["hash"], "sig": r["sig"]}

    def mark_acked(self, event_ids):
        with transaction(self.conn):
            self.conn.executemany("UPDATE outbox SET status='ACKED', last_error=NULL WHERE event_id=?",
                                  [(i,) for i in event_ids])

    def mark_rejected(self, event_id, reason):
        """Permanent rejection by the backend -> DEAD (needs human review)."""
        with transaction(self.conn):
            self.conn.execute("UPDATE outbox SET status='DEAD', last_error=? WHERE event_id=?",
                              (reason, event_id))

    def release_failed(self, event_ids, error):
        """Network/transient failure: back to PENDING with exponential backoff + jitter."""
        now = self.clock.now_ms()
        with transaction(self.conn):
            for eid in event_ids:
                attempts = self.conn.execute("SELECT attempts FROM outbox WHERE event_id=?",
                                             (eid,)).fetchone()["attempts"] + 1
                delay = min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** attempts) * random.uniform(0.5, 1.0)
                self.conn.execute(
                    "UPDATE outbox SET status='PENDING', attempts=?, next_retry_at=?, last_error=? "
                    "WHERE event_id=?", (attempts, now + int(delay), error, eid))

    def clear_backoff(self):
        """Call when a link comes up: retry immediately instead of waiting."""
        with transaction(self.conn):
            self.conn.execute("UPDATE outbox SET next_retry_at=0 WHERE status='PENDING'")

    # ---- housekeeping ----
    def counts(self) -> dict:
        rows = self.conn.execute("SELECT status, COUNT(*) c FROM outbox GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}

    def pending(self) -> int:
        c = self.counts()
        return c.get("PENDING", 0) + c.get("INFLIGHT", 0)

    def prune_acked(self) -> int:
        with transaction(self.conn):
            cur = self.conn.execute("DELETE FROM outbox WHERE status='ACKED'")
        return cur.rowcount

    def recover(self):
        """Boot-time: a crash mid-send must not strand events."""
        with transaction(self.conn):
            self.conn.execute("UPDATE outbox SET status='PENDING' WHERE status='INFLIGHT'")

    def verify_chain(self):
        """Returns (ok, bad_seq). Detects edited or deleted rows (tamper evidence).
        Rows pruned from the front are fine: the first row is the anchor."""
        rows = self.conn.execute("SELECT * FROM outbox ORDER BY seq").fetchall()
        prev_hash, prev_seq = None, None
        for r in rows:
            digest = event_digest(r["prev_hash"], r["event_id"], self.charger_id, r["event_type"],
                                  r["tx_id"], r["payload"], r["created_at"], r["seq"])
            if digest.hex() != r["hash"]:
                return False, r["seq"]
            if prev_hash is not None and (r["prev_hash"] != prev_hash or r["seq"] != prev_seq + 1):
                return False, r["seq"]
            prev_hash, prev_seq = r["hash"], r["seq"]
        return True, None
