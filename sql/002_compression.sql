-- Compression TimescaleDB des données d'apprentissage (gain typique x5–x10) et rétention des labels.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['trades','predictions','evaluations','labels','curve_points','learning_states','market_context','system_events'] LOOP
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = t AND compression_enabled) THEN
      EXECUTE format('ALTER TABLE %I SET (timescaledb.compress, timescaledb.compress_orderby = ''ts DESC'')', t);
    END IF;
    PERFORM add_compression_policy(t::regclass, INTERVAL '1 day', if_not_exists => TRUE);
  END LOOP;
END $$;
SELECT add_retention_policy('labels', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_retention_policy('errors', INTERVAL '60 days', if_not_exists => TRUE);
