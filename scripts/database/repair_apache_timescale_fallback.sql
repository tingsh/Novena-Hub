-- Repair the Ubuntu Apache-only fallback after installing TimescaleDB Community.
-- Back up the database first. Run with psql -X -v ON_ERROR_STOP=1 -f this-file.
-- Does not reset Django migration records or modify raw telemetry rows.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
DO $$
BEGIN
    IF current_setting('timescaledb.license', true) IS DISTINCT FROM 'timescale' THEN
        RAISE EXCEPTION 'Install TimescaleDB Community before repairing telemetry';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.hypertables
        WHERE hypertable_schema = 'public' AND hypertable_name = 'telemetry_telemetrydata'
    ) THEN
        RAISE EXCEPTION 'Expected public.telemetry_telemetrydata hypertable is missing';
    END IF;
    IF EXISTS (
        SELECT 1 FROM timescaledb_information.continuous_aggregates
        WHERE view_schema = 'public' AND view_name = 'hourly_telemetry_stats'
    ) THEN
        RAISE EXCEPTION 'A real continuous aggregate already exists; inspect readiness instead of repeating this repair';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_matviews
        WHERE schemaname = 'public' AND matviewname = 'hourly_telemetry_stats' AND NOT ispopulated
    ) THEN
        RAISE EXCEPTION 'Expected unpopulated Apache fallback is missing; inspect database before proceeding';
    END IF;
END $$;

-- No CASCADE: unexpected dependent objects must stop the repair.
DROP MATERIALIZED VIEW public.hourly_telemetry_stats;
CREATE MATERIALIZED VIEW public.hourly_telemetry_stats
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', timestamp) AS bucket,
       device_id,
       key,
       AVG(value_numeric) AS avg_value,
       MAX(value_numeric) AS max_value,
       MIN(value_numeric) AS min_value,
       AVG(CASE WHEN value_bool IS TRUE THEN 1.0 WHEN value_bool IS FALSE THEN 0.0 ELSE NULL END) AS true_percentage
FROM public.telemetry_telemetrydata
GROUP BY bucket, device_id, key
WITH NO DATA;

ALTER TABLE public.telemetry_telemetrydata SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'device_id'
);
SELECT add_compression_policy('public.telemetry_telemetrydata', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('public.hourly_telemetry_stats',
    start_offset => INTERVAL '3 hours', end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour', if_not_exists => TRUE);
SELECT add_retention_policy('public.telemetry_telemetrydata', INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_retention_policy('public.hourly_telemetry_stats', INTERVAL '90 days', if_not_exists => TRUE);
COMMIT;

-- Populate all existing closed hourly buckets; the scheduled policy handles new data.
CALL refresh_continuous_aggregate('public.hourly_telemetry_stats', NULL, date_trunc('hour', now()));
SELECT count(*) AS readable_hourly_buckets FROM public.hourly_telemetry_stats;
