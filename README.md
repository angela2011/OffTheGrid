# OffTheGrid using OCPP framework

Offline-first EV charging. A charger with **no network** should still be able to authorize a driver,
meter energy, enforce a spending cap, and produce signed receipts. When any link comes
back, everything syncs and settles against an escrow.

## Run it
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q              
python -m simulation.demo        
```

## Basic workflow
```
Driver phone (Wallet)                 Charger (offline)                     Backend (Ledger)
  holds voucher + key  --challenge-->  verify voucher locally
  signs challenge      <--------------  (issuer sig, expiry, station, cap)
                                        meter ticks -> METER events
  countersigns receipt <--receipt----   stop -> signed RECEIPT
                                        everything -> SQLite outbox (hash-chained, signed)
                                              |  link up (Wi-Fi / LoRa / phone relay ...)
                                              +--------------------------------> ingest (dedupe, verify)
                                                                                 settle vs escrow
```

| Path | What it does |
|---|---|
| `charger/offline_queue/outbox.py` | Write-ahead outbox: hash chain, per-event Ed25519 signature, backoff, crash recovery, prune |
| `charger/offline_queue/sender.py` | Drains the outbox over any `Transport` |
| `charger/transport/` | `Transport` interface, HTTP transport, `SimulatedLink` (on/off switch) |
| `charger/local_auth/` | Offline voucher verification + per-charger spend tracking |
| `charger/charger.py` | Session logic: start (proof of possession), tick, cap enforcement, stop + receipt |
| `mobile_app/wallet.py` | Simulated phone: signs challenges, countersigns only honest receipts |
| `backend/ledger.py` | Voucher issuance, idempotent ingest, settlement, refund, chain audit |
| `backend/server.py` | stdlib HTTP API (`/sync`, `/vouchers/issue`, `/ledger`) |
| `common/` | Crypto, shared formats, clock, SQLite transaction helper |
| `tests/` | Queue, offline auth, and end-to-end tests |

## Security properties covered by tests
- Forged or tampered vouchers rejected offline
- Stolen voucher useless without the owner's key 
- Session automatically stops at the voucher cap, so exhausted voucher is refused
- Phone refuses inflated kWh / price / rate
- Queue tampering or deleted events detected by hash chain and forged events rejected by backend
- Double spend across two offline chargers: backend never pays above the escrow lock and flags `PARTIAL`
- No user countersignature -> receipt `HELD`, not paid

## Known gaps / where to build next
1. **Escrow:** `backend/ledger.py` has `>>> ESCROW HOOK <<<` markers where your GridGarrison
   escrow contract calls go (lock on issue, pay on settle, refund after expiry + grace).
2. **OCPP:** the charger speaks its own event format. Map `START_TX/METER/STOP_TX` to OCPP 1.6
   `StartTransaction/MeterValues/StopTransaction` or 2.0.1 `TransactionEvent` (`offline`, `seqNo`)
   in a new transport adapter (`ocpp` library / MicroOcpp).
3. **More transports:** add LoRa, BLE/NFC to phone (delay-tolerant relay), satellite as new `Transport` classes.
4. **Trusted time:** `common/clock.py` is a stub; back it with GPS/RTC and set `trusted=False` when unsure.
5. **Queue cap policy:** only `prune_acked()` exists; add dropping of low-priority METER rows
   (needs a chain-compatible approach, e.g. summarising meter events).
6. **Charger key storage / provisioning**, golden-hash firmware attestation, DB encryption at rest.
7. **HELD receipts:** needs a dispute/evidence flow.
8. **Backend auth** on `/sync` and `/vouchers/issue` (currently open prototype endpoints).
9. **Cross-charger double spend** is detected after the fact, not prevented. Tune voucher size,
   expiry, and `allowed_stations`.

