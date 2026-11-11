import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  seq           INTEGER PRIMARY KEY,            -- monotonic per charger (from meta)
  event_id      TEXT NOT NULL UNIQUE,           -- idempotency key
  tx_id         TEXT,
  event_type    TEXT NOT NULL,                  -- START_TX | METER | STOP_TX | RECEIPT
  priority      INTEGER NOT NULL DEFAULT 1,     -- 0 critical .. 2 droppable
  payload       TEXT NOT NULL,                  -- canonical JSON
  created_at    INTEGER NOT NULL,
  time_trusted  INTEGER NOT NULL DEFAULT 1,
  status        TEXT NOT NULL DEFAULT 'PENDING',-- PENDING|INFLIGHT|ACKED|DEAD
  attempts      INTEGER NOT NULL DEFAULT 0,
  next_retry_at INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  prev_hash     TEXT NOT NULL,
  hash          TEXT NOT NULL,
  sig           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, seq);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS local_tx (
  tx_id          TEXT PRIMARY KEY,
  voucher_id     TEXT NOT NULL,
  user_pub       TEXT NOT NULL,
  state          TEXT NOT NULL,                 -- ACTIVE | STOPPED
  start_meter_wh INTEGER NOT NULL,
  session_wh     INTEGER NOT NULL DEFAULT 0,
  started_at     INTEGER NOT NULL,
  stopped_at     INTEGER
);

CREATE TABLE IF NOT EXISTS voucher_usage (    -- what THIS charger has handed out
  voucher_id  TEXT PRIMARY KEY,
  max_cents   INTEGER NOT NULL,
  spent_cents INTEGER NOT NULL DEFAULT 0
);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")   # power loss is expected on chargers
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, str(value)))
