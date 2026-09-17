-- Read-only policy verification beyond the basic manage.py verify_timescale check.
\set ON_ERROR_STOP on
BEGIN READ ONLY;
DO $$
DECLARE
    aggregate_schema text;
    aggregate_table text;
BEGIN
    IF current_setting('timescaledb.license', true) IS DISTINCT FROM 'timescale' THEN
        RAISE EXCEPTION 'TimescaleDB Community features are required';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.hypertables
        WHERE hypertable_schema='public' AND hypertable_name='telemetry_telemetrydata' AND compression_enabled) THEN
        RAISE EXCEPTION 'Telemetry hypertable/compression is missing';
    END IF;
    SELECT view_schema, view_name
    INTO aggregate_schema, aggregate_table
    FROM timescaledb_information.continuous_aggregates
    WHERE view_schema='public' AND view_name='hourly_telemetry_stats';
    IF aggregate_table IS NULL THEN
        RAISE EXCEPTION 'Hourly telemetry must be a real continuous aggregate';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.jobs
        WHERE hypertable_schema='public' AND hypertable_name='telemetry_telemetrydata'
        AND proc_name='policy_compression' AND scheduled
        AND (config->>'compress_after')::interval=INTERVAL '7 days') THEN
        RAISE EXCEPTION 'Scheduled 7-day telemetry compression is missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.jobs
        WHERE hypertable_schema=aggregate_schema AND hypertable_name=aggregate_table
        AND proc_name='policy_refresh_continuous_aggregate' AND scheduled
        AND schedule_interval=INTERVAL '1 hour'
        AND (config->>'start_offset')::interval=INTERVAL '3 hours'
        AND (config->>'end_offset')::interval=INTERVAL '1 hour') THEN
        RAISE EXCEPTION 'Hourly aggregate refresh policy is missing or incorrect';
    END IF;
    IF (SELECT count(*) FROM timescaledb_information.jobs
        WHERE ((hypertable_schema='public' AND hypertable_name='telemetry_telemetrydata')
            OR (hypertable_schema=aggregate_schema AND hypertable_name=aggregate_table))
        AND proc_name='policy_retention' AND scheduled
        AND (config->>'drop_after')::interval=INTERVAL '90 days') != 2 THEN
        RAISE EXCEPTION 'Both raw and aggregate telemetry require scheduled 90-day retention';
    END IF;
END $$;
SELECT count(*) AS readable_hourly_buckets FROM public.hourly_telemetry_stats;
COMMIT;
\echo 'PASS: Community edition, compression, continuous aggregation, refresh and retention policies'
