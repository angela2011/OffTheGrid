"""Formats shared by charger, wallet and backend. Everyone must agree on these."""
from .crypto import canonical, sha256


def price_cents(wh: int, rate_cents_per_kwh: int) -> int:
    """Integer pricing, rounded up. Used by charger, wallet and backend alike."""
    return -(-wh * rate_cents_per_kwh // 1000)


def event_digest(prev_hash, event_id, charger_id, event_type, tx_id, payload, created_at, seq) -> bytes:
    """Hash that chains events together. The charger signs this digest."""
    return sha256(canonical([prev_hash, event_id, charger_id, event_type,
                             tx_id, payload, created_at, seq]))


def voucher_body(voucher: dict) -> bytes:
    return canonical({k: v for k, v in voucher.items() if k != "issuer_sig"})


def auth_message(challenge: str) -> bytes:
    return b"offgrid-auth:" + challenge.encode()


def receipt_bytes(receipt: dict) -> bytes:
    return canonical(receipt)
