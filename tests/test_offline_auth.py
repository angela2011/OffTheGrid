import pytest

from charger.local_auth.voucher_verify import AuthError, VoucherError
from mobile_app.wallet import WalletRefused


def test_valid_voucher_authorizes_offline(rig):
    c, w = rig.charger(), rig.wallet()
    v = rig.voucher(w)
    assert rig.start(c, w, v)


def test_forged_voucher_rejected(rig):
    c, w = rig.charger(), rig.wallet()
    v = dict(rig.voucher(w), max_cents=999999)       # tamper with cap
    with pytest.raises(VoucherError, match="bad_issuer_signature"):
        rig.start(c, w, v)


def test_expired_voucher_rejected(rig):
    c, w = rig.charger(), rig.wallet()
    v = rig.voucher(w, ttl_s=60)
    rig.clock.advance(120)
    with pytest.raises(VoucherError, match="expired"):
        rig.start(c, w, v)


def test_wrong_station_rejected(rig):
    c, w = rig.charger("CHG-001"), rig.wallet()
    v = rig.voucher(w, allowed_stations=["CHG-777"])
    with pytest.raises(VoucherError, match="station_not_allowed"):
        rig.start(c, w, v)


def test_stolen_voucher_fails_proof_of_possession(rig):
    c, owner, thief = rig.charger(), rig.wallet(), rig.wallet()
    v = rig.voucher(owner)
    ch = c.new_challenge()
    with pytest.raises(AuthError, match="bad_proof"):
        c.start_session(v, ch, thief.sign_challenge(ch))


def test_challenge_cannot_be_replayed(rig):
    c, w = rig.charger(), rig.wallet()
    v = rig.voucher(w)
    ch = c.new_challenge()
    sig = w.sign_challenge(ch)
    tx = c.start_session(v, ch, sig)
    c.stop_session(tx, w)
    with pytest.raises(AuthError):
        c.start_session(v, ch, sig)


def test_session_stops_at_voucher_cap_and_blocks_reuse(rig):
    c, w = rig.charger(), rig.wallet()
    v = rig.voucher(w, max_cents=100)
    tx = rig.start(c, w, v)
    capped = False
    for _ in range(20):
        s = c.tick(tx, 600)
        capped = s["cap_reached"]
        if capped:
            break
    assert capped and s["cents"] <= 100
    c.stop_session(tx, w)
    with pytest.raises(VoucherError):
        rig.start(c, w, v)


def test_wallet_refuses_inflated_receipt(rig):
    c, w = rig.charger(), rig.wallet()
    v = rig.voucher(w)
    tx = rig.start(c, w, v)
    rig.charge(c, tx, 30)
    receipt = c.stop_session(tx, None)["receipt"]
    with pytest.raises(WalletRefused):
        w.cosign(dict(receipt, wh=receipt["wh"] * 2, meter_end_wh=receipt["meter_end_wh"] * 2))
    with pytest.raises(WalletRefused):
        w.cosign(dict(receipt, rate_cents_per_kwh=500, cents=receipt["cents"] * 20))
