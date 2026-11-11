"""Offline authorization: everything here works with zero connectivity."""
from common.crypto import verify
from common.protocol import voucher_body


class VoucherError(Exception):
    pass


class AuthError(Exception):
    pass


def verify_voucher(voucher: dict, issuer_pub: str, now_ms: int, charger_id: str):
    if not verify(issuer_pub, voucher_body(voucher), voucher.get("issuer_sig")):
        raise VoucherError("bad_issuer_signature")
    if now_ms >= voucher["expiry_ms"]:
        raise VoucherError("expired")
    allowed = voucher.get("allowed_stations")
    if allowed and charger_id not in allowed:
        raise VoucherError("station_not_allowed")


class VoucherStore:
    """Tracks how much of each voucher THIS charger has already used."""

    def __init__(self, conn):
        self.conn = conn

    def register(self, voucher):
        self.conn.execute("INSERT OR IGNORE INTO voucher_usage(voucher_id, max_cents) VALUES(?,?)",
                          (voucher["voucher_id"], voucher["max_cents"]))

    def remaining(self, voucher_id) -> int:
        r = self.conn.execute("SELECT max_cents - spent_cents AS rem FROM voucher_usage "
                              "WHERE voucher_id=?", (voucher_id,)).fetchone()
        return r["rem"] if r else 0

    def add_spent(self, voucher_id, cents):
        self.conn.execute("UPDATE voucher_usage SET spent_cents = spent_cents + ? WHERE voucher_id=?",
                          (cents, voucher_id))

    def has_active(self, voucher_id) -> bool:
        return self.conn.execute("SELECT 1 FROM local_tx WHERE voucher_id=? AND state='ACTIVE'",
                                 (voucher_id,)).fetchone() is not None
