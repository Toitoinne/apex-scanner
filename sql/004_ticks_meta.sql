-- Prix post-migration (PumpSwap / DexScreener) : nécessaires pour reprendre le suivi 24 h après redémarrage
CREATE TABLE IF NOT EXISTS price_ticks (
    ts      TIMESTAMPTZ NOT NULL,
    mint    TEXT NOT NULL,
    price   DOUBLE PRECISION NOT NULL,
    source  TEXT
);
SELECT create_hypertable('price_ticks', 'ts', chunk_time_interval => INTERVAL '6 hours', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS price_ticks_mint_ts ON price_ticks (mint, ts);
SELECT add_retention_policy('price_ticks', INTERVAL '3 days', if_not_exists => TRUE);

-- Métadonnées des tokens (réseaux sociaux, description) récupérées à la création
ALTER TABLE tokens ADD COLUMN IF NOT EXISTS meta JSONB;
