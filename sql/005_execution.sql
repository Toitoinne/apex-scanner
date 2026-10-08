-- Système d'ordres : ordres (simulés ou réels) et positions exécutées
CREATE TABLE IF NOT EXISTS exec_orders (
    id             BIGSERIAL PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    decision_id    TEXT NOT NULL,
    mint           TEXT NOT NULL,
    side           TEXT NOT NULL,            -- buy | sell
    mode           TEXT NOT NULL,            -- simulation | reel
    reason         TEXT,                     -- alerte, PALIER, STOP, DANGER…
    sol            DOUBLE PRECISION,
    tokens         DOUBLE PRECISION,
    expected_price DOUBLE PRECISION,
    fill_price     DOUBLE PRECISION,
    slippage       DOUBLE PRECISION,
    fees_sol       DOUBLE PRECISION,
    status         TEXT NOT NULL,            -- filled | failed | skipped
    error          TEXT,
    tx_signature   TEXT,
    latency_s      DOUBLE PRECISION,
    builder_ok     BOOLEAN                    -- transaction PumpPortal construite et valide
);
CREATE INDEX IF NOT EXISTS exec_orders_ts ON exec_orders (ts DESC);

CREATE TABLE IF NOT EXISTS exec_positions (
    decision_id    TEXT PRIMARY KEY,
    mint           TEXT NOT NULL,
    symbol         TEXT,
    mode           TEXT NOT NULL,
    policy         TEXT,
    opened_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sol_in         DOUBLE PRECISION NOT NULL DEFAULT 0,
    sol_out        DOUBLE PRECISION NOT NULL DEFAULT 0,
    tokens_initial DOUBLE PRECISION NOT NULL DEFAULT 0,
    tokens         DOUBLE PRECISION NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'open',   -- open | closed | failed
    pnl_sol        DOUBLE PRECISION,
    pnl            DOUBLE PRECISION,
    closed_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS exec_positions_status ON exec_positions (status, opened_at DESC);
