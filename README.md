# OffGrid-OCPP

Offline-first EV charging. A charger with **no network** can still authorize a driver,
meter energy, enforce a spending cap, and produce signed receipts. When any link comes
back, everything syncs idempotently and settles against an escrow.

## Run it
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q              # 24 tests
python -m simulation.demo        # full offline -> reconnect -> settle walkthrough
```

## How it works
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
- Stolen voucher useless without the owner's key (challenge-response)
- Session auto-stops at the voucher cap; exhausted voucher refused
- Phone refuses inflated kWh / price / rate
- Queue tampering or deleted events detected by hash chain; forged events rejected by backend
- Replayed batches are idempotent
- Double-spend across two offline chargers: backend never pays above the escrow lock and flags `PARTIAL`
- No user countersignature -> receipt `HELD`, not paid

