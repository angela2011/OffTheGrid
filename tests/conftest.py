import pytest

from backend.ledger import Ledger
from charger.charger import Charger
from charger.offline_queue import db
from charger.offline_queue.outbox import Outbox
from common.clock import FakeClock
from common.crypto import generate_keypair
from mobile_app.wallet import Wallet


class Rig:
    """A ledger plus helpers to create chargers and wallets sharing one clock."""

    def __init__(self):
        self.clock = FakeClock()
        self.ledger = Ledger(clock=self.clock)

    def charger(self, cid="CHG-001", db_path=":memory:", **kw):
        sk, pk = generate_keypair()
        self.ledger.register_charger(cid, pk)
        ob = Outbox(db.connect(db_path), cid, sk, self.clock)
        return Charger(cid, sk, self.ledger.issuer_pk, ob, self.clock, **kw)

    def wallet(self, **kw):
        sk, pk = generate_keypair()
        return Wallet(sk, pk, **kw)

    def voucher(self, wallet, max_cents=500, **kw):
        v = self.ledger.issue_voucher(wallet.pk, max_cents, **kw)
        wallet.add_voucher(v)
        return v

    @staticmethod
    def start(charger, wallet, voucher):
        c = charger.new_challenge()
        return charger.start_session(voucher, c, wallet.sign_challenge(c))

    @staticmethod
    def charge(charger, tx, minutes):
        for _ in range(minutes // 10):
            charger.clock.advance(600)
            charger.tick(tx, 600)


@pytest.fixture
def rig():
    return Rig()
