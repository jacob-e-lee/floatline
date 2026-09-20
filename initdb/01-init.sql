-- Floatline TimescaleDB initialization (runs once on first container init)
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- NOTE: the partitioning column (`timestamp`) must be part of the primary key,
-- otherwise create_hypertable() fails with:
--   "cannot create a unique index without the column timestamp (used in partitioning)"
CREATE TABLE IF NOT EXISTS balance_snapshots (
    id                 BIGSERIAL,
    timestamp          TIMESTAMPTZ NOT NULL,
    checking_balance   DOUBLE PRECISION NOT NULL,
    savings_balance    DOUBLE PRECISION NOT NULL,
    brokerage_balance  DOUBLE PRECISION NOT NULL,
    setpoint           DOUBLE PRECISION NOT NULL,
    pid_output         DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    PRIMARY KEY (id, timestamp)
);

SELECT create_hypertable('balance_snapshots', 'timestamp', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS transfer_logs (
    id                 BIGSERIAL,
    timestamp          TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_account_id  TEXT NOT NULL,
    destination        TEXT NOT NULL,
    amount             DOUBLE PRECISION NOT NULL,
    reason             TEXT NOT NULL,
    status             TEXT NOT NULL,
    api_response       TEXT,
    request_id         TEXT,
    PRIMARY KEY (id, timestamp)
);

SELECT create_hypertable('transfer_logs', 'timestamp', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_balance_time  ON balance_snapshots (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_transfer_time ON transfer_logs (timestamp DESC);