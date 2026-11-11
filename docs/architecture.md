# Architecture notes

- **One code path:** charging logic only ever writes to the outbox. Online or offline looks identical.
- **Atomicity:** state change + event enqueue share one SQLite transaction (`transaction()` joins nested calls).
- **Ordering:** drained strictly by `seq`; a backed-off event blocks later ones so per-tx order is preserved.
- **Idempotency:** backend dedupes on `event_id`, rejects `(charger_id, seq)` conflicts.
- **Integrity:** digest = H(prev_hash, event_id, charger_id, type, tx_id, payload, created_at, seq);
  charger signs the digest. Backend recomputes and verifies, and can audit gaps/breaks per charger.
- **Payment:** voucher (issuer-signed, capped, expiring) + receipt (signed by charger AND phone).
