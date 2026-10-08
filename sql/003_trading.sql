-- Résultats par stratégie de sortie (récompenses du bandit) et paper trading.
CREATE TABLE IF NOT EXISTS outcomes (
    decision_id TEXT PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    mint        TEXT NOT NULL,
    point       TEXT,
    pnl         JSONB NOT NULL,          -- stratégie -> PnL (fraction de la mise)
    max_return  DOUBLE PRECISION,
    reached     JSONB                    -- "x2"/"x5"/"x10"/"x20" -> secondes
);
CREATE INDEX IF NOT EXISTS outcomes_ts ON outcomes (ts DESC);

CREATE TABLE IF NOT EXISTS paper_positions (
    decision_id  TEXT PRIMARY KEY,
    mint         TEXT NOT NULL,
    symbol       TEXT,
    policy       TEXT NOT NULL,
    opened_at    TIMESTAMPTZ NOT NULL,
    notional_sol DOUBLE PRECISION NOT NULL,
    entry_price  DOUBLE PRECISION NOT NULL,
    state        JSONB NOT NULL,
    fills        JSONB NOT NULL DEFAULT '[]'::jsonb,
    status       TEXT NOT NULL DEFAULT 'open',      -- open | closed
    pnl          DOUBLE PRECISION,                   -- fraction de la mise (réalisé + latent)
    pnl_sol      DOUBLE PRECISION,
    max_multiple DOUBLE PRECISION,
    closed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS paper_status ON paper_positions (status, opened_at DESC);
