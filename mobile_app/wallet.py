"""Simulated phone wallet: holds vouchers, proves ownership, countersigns receipts.
It refuses receipts that don't match what it expects, so a dishonest charger
can't invent kWh or over-bill."""
from common.crypto import sign
from common.protocol import auth_message, price_cents, receipt_bytes


class WalletRefused(Exception):
    pass


class Wallet:
    def __init__(self, sk, pk, max_rate_cents_per_kwh=50):
        self.sk, self.pk = sk, pk
        self.max_rate = max_rate_cents_per_kwh
        self.vouchers = {}
        self.spent = {}

    def add_voucher(self, voucher):
        self.vouchers[voucher["voucher_id"]] = voucher
        self.spent.setdefault(voucher["voucher_id"], 0)

    def sign_challenge(self, challenge: str) -> str:
        return sign(self.sk, auth_message(challenge))

    def cosign(self, receipt: dict) -> str:
        v = self.vouchers.get(receipt["voucher_id"])
        if v is None or receipt["user_pub"] != self.pk:
            raise WalletRefused("unknown_voucher")
        if receipt["wh"] != receipt["meter_end_wh"] - receipt["meter_start_wh"]:
            raise WalletRefused("meter_math")
        if receipt["rate_cents_per_kwh"] > self.max_rate:
            raise WalletRefused("rate_too_high")
        if receipt["cents"] != price_cents(receipt["wh"], receipt["rate_cents_per_kwh"]):
            raise WalletRefused("price_mismatch")
        if self.spent[v["voucher_id"]] + receipt["cents"] > v["max_cents"]:
            raise WalletRefused("exceeds_voucher")
        if receipt["stop_ts"] > v["expiry_ms"]:
            raise WalletRefused("expired")
        self.spent[v["voucher_id"]] += receipt["cents"]
        return sign(self.sk, receipt_bytes(receipt))
