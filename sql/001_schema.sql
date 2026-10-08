-- APEX SCANNER — schéma PostgreSQL + TimescaleDB
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============ Données de marché ============
CREATE TABLE IF NOT EXISTS tokens (
    mint            TEXT PRIMARY KEY,
    chain           TEXT NOT NULL DEFAULT 'solana',
    name            TEXT, symbol TEXT, uri TEXT,
    creator         TEXT,
    bonding_curve   TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    created_slot    BIGINT,
    migrated_at     TIMESTAMPTZ,
    closed_at       TIMESTAMPTZ,
    peak_mc_sol     DOUBLE PRECISION,
    rugged          BOOLEAN,
    outcome         JSONB
);
CREATE INDEX IF NOT EXISTS tokens_created_idx ON tokens (created_at DESC);
CREATE INDEX IF NOT EXISTS tokens_creator_idx ON tokens (creator);

-- Trades bruts : purgés après agrégation (rétention 3 jours)
CREATE TABLE IF NOT EXISTS trades (
    ts          TIMESTAMPTZ NOT NULL,
    mint        TEXT NOT NULL,
    signature   TEXT NOT NULL,
    slot        BIGINT,
    trader      TEXT,
    is_buy      BOOLEAN,
    sol         DOUBLE PRECISION,
    tokens      DOUBLE PRECISION,
    v_sol       DOUBLE PRECISION,
    v_tokens    DOUBLE PRECISION
);
SELECT create_hypertable('trades', 'ts', chunk_time_interval => INTERVAL '6 hours', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS trades_mint_ts ON trades (mint, ts);
SELECT add_retention_policy('trades', INTERVAL '2 days', if_not_exists => TRUE);

-- Agrégat minute conservé (bougies)
CREATE MATERIALIZED VIEW IF NOT EXISTS candles_1m
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 minute', ts) AS bucket, mint,
       first(v_sol / NULLIF(v_tokens, 0), ts) AS open,
       max(v_sol / NULLIF(v_tokens, 0))       AS high,
       min(v_sol / NULLIF(v_tokens, 0))       AS low,
       last(v_sol / NULLIF(v_tokens, 0), ts)  AS close,
       sum(sol) AS volume_sol,
       count(*) FILTER (WHERE is_buy) AS n_buys,
       count(*) FILTER (WHERE NOT is_buy) AS n_sells
FROM trades GROUP BY bucket, mint WITH NO DATA;
SELECT add_continuous_aggregate_policy('candles_1m',
    start_offset => INTERVAL '2 hours', end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS market_context (
    ts              TIMESTAMPTZ NOT NULL,
    temperature     DOUBLE PRECISION,
    launches_per_h  DOUBLE PRECISION,
    migrations_per_h DOUBLE PRECISION,
    sol_price_usd   DOUBLE PRECISION,
    data            JSONB
);
SELECT create_hypertable('market_context', 'ts', if_not_exists => TRUE);

-- ============ Boucle 1 : décisions, prédictions, labels ============
CREATE TABLE IF NOT EXISTS decisions (
    ts              TIMESTAMPTZ NOT NULL,
    decision_id     TEXT NOT NULL,
    mint            TEXT NOT NULL,
    point           TEXT NOT NULL,
    features        JSONB NOT NULL,
    feature_versions JSONB,
    entry_price     DOUBLE PRECISION,
    mc_sol          DOUBLE PRECISION,
    blocked         BOOLEAN DEFAULT FALSE,
    safety_flags    TEXT[],
    config_version  TEXT,
    PRIMARY KEY (decision_id, ts)
);
SELECT create_hypertable('decisions', 'ts', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS decisions_mint ON decisions (mint);

CREATE TABLE IF NOT EXISTS predictions (
    ts              TIMESTAMPTZ NOT NULL,
    decision_id     TEXT NOT NULL,
    mint            TEXT NOT NULL,
    point           TEXT NOT NULL,
    horizon         TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    p_raw           DOUBLE PRECISION,
    p_cal           DOUBLE PRECISION,
    is_champion     BOOLEAN DEFAULT FALSE,
    config_version  TEXT
);
SELECT create_hypertable('predictions', 'ts', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS predictions_dec ON predictions (decision_id, horizon);
CREATE INDEX IF NOT EXISTS predictions_model_ts ON predictions (model_id, ts DESC);

CREATE TABLE IF NOT EXISTS labels (
    ts              TIMESTAMPTZ NOT NULL,
    decision_id     TEXT NOT NULL,
    mint            TEXT NOT NULL,
    point           TEXT NOT NULL,
    horizon         TEXT NOT NULL,
    y               SMALLINT NOT NULL,
    max_return      DOUBLE PRECISION,
    max_drawdown    DOUBLE PRECISION,
    time_to_peak_s  DOUBLE PRECISION,
    rug             BOOLEAN,
    final_return    DOUBLE PRECISION,
    sim_pnl         DOUBLE PRECISION
);
SELECT create_hypertable('labels', 'ts', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS labels_dec ON labels (decision_id, horizon);

-- Évaluation prequential (une ligne par modèle × label) : base de toutes les courbes
CREATE TABLE IF NOT EXISTS evaluations (
    ts              TIMESTAMPTZ NOT NULL,
    decision_id     TEXT NOT NULL,
    horizon         TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    is_champion     BOOLEAN,
    p               DOUBLE PRECISION,
    y               SMALLINT,
    logloss         DOUBLE PRECISION,
    error_type      TEXT,
    cost            DOUBLE PRECISION
);
SELECT create_hypertable('evaluations', 'ts', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS evaluations_model_ts ON evaluations (model_id, horizon, ts DESC);

-- ============ Journal des erreurs ============
CREATE TABLE IF NOT EXISTS error_types (
    name        TEXT PRIMARY KEY,
    description TEXT,
    rule        JSONB,             -- règle DSL (types créés par Claude)
    created_by  TEXT DEFAULT 'system',
    created_at  TIMESTAMPTZ DEFAULT now(),
    active      BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS errors (
    id          BIGSERIAL,
    ts          TIMESTAMPTZ NOT NULL,
    decision_id TEXT NOT NULL,
    mint        TEXT NOT NULL,
    point       TEXT,
    horizon     TEXT,
    error_type  TEXT NOT NULL,
    model_id    TEXT,
    p           DOUBLE PRECISION,
    alerted     BOOLEAN,
    cost        DOUBLE PRECISION,
    features    JSONB,
    market      JSONB,
    outcome     JSONB,
    PRIMARY KEY (id, ts)
);
SELECT create_hypertable('errors', 'ts', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS errors_type_ts ON errors (error_type, ts DESC);

-- ============ Alertes ============
CREATE TABLE IF NOT EXISTS alerts (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    decision_id     TEXT UNIQUE NOT NULL,
    mint            TEXT NOT NULL,
    point           TEXT,
    p               DOUBLE PRECISION,
    model_id        TEXT,
    arm             TEXT,
    payload         JSONB,
    tg_message_id   BIGINT,
    result_15       JSONB,
    result_60       JSONB,
    sim_pnl         DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts (ts DESC);

-- ============ Bases wallets / devs ============
CREATE TABLE IF NOT EXISTS wallets (
    address     TEXT PRIMARY KEY,
    n_trades    INT DEFAULT 0,
    n_wins      INT DEFAULT 0,
    pnl_sol     DOUBLE PRECISION DEFAULT 0,
    first_seen  TIMESTAMPTZ,
    funder      TEXT,
    funder2     TEXT,
    is_smart    BOOLEAN DEFAULT FALSE,
    is_bot      BOOLEAN DEFAULT FALSE,
    updated_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS wallets_smart ON wallets (is_smart) WHERE is_smart;

CREATE TABLE IF NOT EXISTS devs (
    address     TEXT PRIMARY KEY,
    n_tokens    INT DEFAULT 0,
    n_rugs      INT DEFAULT 0,
    n_winners   INT DEFAULT 0,
    last_token  TEXT,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- ============ Boucle 2 : auto-supervision ============
CREATE TABLE IF NOT EXISTS curve_points (
    ts          TIMESTAMPTZ NOT NULL,
    curve       TEXT NOT NULL,       -- ex. error_rate, error_rate:RUG_ALERTE, cost, precision, ece...
    model_id    TEXT NOT NULL,
    win         TEXT NOT NULL,       -- 1h, 6h, 24h, 7d
    value       DOUBLE PRECISION,
    n           INT
);
SELECT create_hypertable('curve_points', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS curve_points_idx ON curve_points (curve, model_id, win, ts DESC);

CREATE TABLE IF NOT EXISTS reference_curves (
    curve       TEXT PRIMARY KEY,
    value       DOUBLE PRECISION,
    n           INT,
    source      TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS learning_states (
    ts          TIMESTAMPTZ NOT NULL,
    curve       TEXT NOT NULL,
    state       TEXT NOT NULL,
    slope       DOUBLE PRECISION,
    p_value     DOUBLE PRECISION,
    details     JSONB
);
SELECT create_hypertable('learning_states', 'ts', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS diagnoses (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    state       TEXT NOT NULL,
    curve       TEXT,
    causes      JSONB,
    confidence  DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS corrections (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    component       TEXT NOT NULL,
    problem_key     TEXT NOT NULL,
    state           TEXT NOT NULL,
    context         TEXT,
    action          TEXT NOT NULL,
    params_before   JSONB,
    params_after    JSONB,
    diagnosis_id    BIGINT REFERENCES diagnoses(id),
    curves_snapshot JSONB,
    shadow_id       TEXT,
    target_curve    TEXT,
    status          TEXT NOT NULL DEFAULT 'applied',
    effect          JSONB,
    evaluated_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS corrections_status ON corrections (status, ts);

CREATE TABLE IF NOT EXISTS efficacy (
    state       TEXT NOT NULL,
    context     TEXT NOT NULL,
    action      TEXT NOT NULL,
    n           INT DEFAULT 0,
    n_success   INT DEFAULT 0,
    n_fail      INT DEFAULT 0,
    gain_sum    DOUBLE PRECISION DEFAULT 0,
    PRIMARY KEY (state, context, action)
);

CREATE TABLE IF NOT EXISTS frozen_components (
    component   TEXT PRIMARY KEY,
    problem_key TEXT,
    since       TIMESTAMPTZ NOT NULL,
    reason      TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    path        TEXT NOT NULL,
    stable      BOOLEAN DEFAULT FALSE,
    config_version TEXT,
    meta        JSONB
);

-- ============ Amélioration par Claude ============
CREATE TABLE IF NOT EXISTS claude_proposals (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    trigger     TEXT,
    problem_type TEXT,
    hypothesis  TEXT,
    n_features  INT,
    n_error_types INT,
    status      TEXT,
    reject_reason TEXT,
    raw         JSONB,
    usage       JSONB
);

CREATE TABLE IF NOT EXISTS claude_features (
    feature_id  TEXT PRIMARY KEY,
    version     INT NOT NULL DEFAULT 1,
    name        TEXT,
    description TEXT,
    code        TEXT NOT NULL,
    test_code   TEXT,
    proposal_id BIGINT REFERENCES claude_proposals(id),
    problem_type TEXT,
    status      TEXT NOT NULL,      -- sandbox_failed | shadow | champion | rejected | disabled
    reason      TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    decided_at  TIMESTAMPTZ
);

-- ============ Journal système ============
CREATE TABLE IF NOT EXISTS system_events (
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    level   TEXT,
    kind    TEXT,
    message TEXT,
    data    JSONB
);
SELECT create_hypertable('system_events', 'ts', if_not_exists => TRUE);

-- Purge des données d'apprentissage détaillées après 30 jours (agrégats conservés)
SELECT add_retention_policy('predictions', INTERVAL '14 days', if_not_exists => TRUE);
SELECT add_retention_policy('evaluations', INTERVAL '14 days', if_not_exists => TRUE);
SELECT add_retention_policy('decisions', INTERVAL '14 days', if_not_exists => TRUE);
SELECT add_retention_policy('curve_points', INTERVAL '60 days', if_not_exists => TRUE);
