-- PagedOut ingestion job.
--
-- Two INSERTs submitted as one STATEMENT SET, so they share a single job
-- graph and a single set of task slots:
--
--   1. normalize   three raw topics -> one incident-signal schema
--   2. correlate   windowed aggregation -> one incident per burst
--
-- The correlation step is the point of the whole job. Ten error logs plus a
-- metric sample plus an alert, all describing the same failure, must become
-- ONE incident rather than twelve pages.

SET 'pipeline.name' = 'pagedout-incident-pipeline';
SET 'execution.checkpointing.interval' = '10s';
SET 'execution.checkpointing.mode' = 'EXACTLY_ONCE';
SET 'state.backend.type' = 'rocksdb';
SET 'state.backend.incremental' = 'true';
SET 'table.exec.source.idle-timeout' = '5s';

-- ── Sources ──────────────────────────────────────────────────────────────────
-- Watermarks are generous (15s) because the load harness bursts events, and a
-- tight watermark would drop late records and understate throughput.

CREATE TABLE logs_raw (
    event_id       STRING,
    `timestamp`    TIMESTAMP_LTZ(3),
    service        STRING,
    incident_type  STRING,
    severity       STRING,
    message        STRING,
    pod            STRING,
    namespace      STRING,
    emitted_at_ms  BIGINT,
    WATERMARK FOR `timestamp` AS `timestamp` - INTERVAL '15' SECOND
) WITH (
    'connector'                          = 'kafka',
    'topic'                              = 'logs.raw',
    'properties.bootstrap.servers'       = 'kafka:9092',
    'properties.group.id'                = 'pagedout-flink',
    'scan.startup.mode'                  = 'latest-offset',
    'format'                             = 'json',
    'json.timestamp-format.standard'     = 'ISO-8601',
    'json.ignore-parse-errors'           = 'true'
);

CREATE TABLE metrics_raw (
    event_id       STRING,
    `timestamp`    TIMESTAMP_LTZ(3),
    service        STRING,
    incident_type  STRING,
    severity       STRING,
    metrics        MAP<STRING, DOUBLE>,
    emitted_at_ms  BIGINT,
    WATERMARK FOR `timestamp` AS `timestamp` - INTERVAL '15' SECOND
) WITH (
    'connector'                          = 'kafka',
    'topic'                              = 'metrics.raw',
    'properties.bootstrap.servers'       = 'kafka:9092',
    'properties.group.id'                = 'pagedout-flink',
    'scan.startup.mode'                  = 'latest-offset',
    'format'                             = 'json',
    'json.timestamp-format.standard'     = 'ISO-8601',
    'json.ignore-parse-errors'           = 'true'
);

CREATE TABLE alerts_raw (
    event_id       STRING,
    `timestamp`    TIMESTAMP_LTZ(3),
    service        STRING,
    incident_type  STRING,
    severity       STRING,
    title          STRING,
    description    STRING,
    runbook_hint   STRING,
    emitted_at_ms  BIGINT,
    WATERMARK FOR `timestamp` AS `timestamp` - INTERVAL '15' SECOND
) WITH (
    'connector'                          = 'kafka',
    'topic'                              = 'alerts.raw',
    'properties.bootstrap.servers'       = 'kafka:9092',
    'properties.group.id'                = 'pagedout-flink',
    'scan.startup.mode'                  = 'latest-offset',
    'format'                             = 'json',
    'json.timestamp-format.standard'     = 'ISO-8601',
    'json.ignore-parse-errors'           = 'true'
);

-- ── Sinks ────────────────────────────────────────────────────────────────────

CREATE TABLE signals_normalized (
    event_id       STRING,
    event_time     TIMESTAMP_LTZ(3),
    service        STRING,
    signal_type    STRING,
    incident_type  STRING,
    severity       STRING,
    message        STRING,
    metrics        MAP<STRING, DOUBLE>,
    emitted_at_ms  BIGINT
) WITH (
    'connector'                    = 'kafka',
    'topic'                        = 'signals.normalized',
    'properties.bootstrap.servers' = 'kafka:9092',
    'format'                       = 'json',
    'json.timestamp-format.standard' = 'ISO-8601'
);

CREATE TABLE incidents_correlated (
    window_start        TIMESTAMP(3),
    window_end          TIMESTAMP(3),
    service             STRING,
    incident_type       STRING,
    severity            STRING,
    signal_count        BIGINT,
    log_count           BIGINT,
    metric_count        BIGINT,
    alert_count         BIGINT,
    distinct_pods       BIGINT,
    sample_message      STRING,
    first_emitted_at_ms BIGINT,
    last_emitted_at_ms  BIGINT
) WITH (
    'connector'                    = 'kafka',
    'topic'                        = 'incidents.correlated',
    'properties.bootstrap.servers' = 'kafka:9092',
    'format'                       = 'json',
    'json.timestamp-format.standard' = 'ISO-8601'
);

-- ── Normalized view over all three sources ───────────────────────────────────

CREATE TEMPORARY VIEW all_signals AS
SELECT
    event_id,
    `timestamp`  AS event_time,
    service,
    'log'        AS signal_type,
    incident_type,
    severity,
    message,
    CAST(NULL AS MAP<STRING, DOUBLE>) AS metrics,
    pod,
    emitted_at_ms
FROM logs_raw
UNION ALL
SELECT
    event_id,
    `timestamp`  AS event_time,
    service,
    'metric'     AS signal_type,
    incident_type,
    severity,
    CONCAT('metric sample for ', incident_type) AS message,
    metrics,
    CAST(NULL AS STRING) AS pod,
    emitted_at_ms
FROM metrics_raw
UNION ALL
SELECT
    event_id,
    `timestamp`  AS event_time,
    service,
    'alert'      AS signal_type,
    incident_type,
    severity,
    title        AS message,
    CAST(NULL AS MAP<STRING, DOUBLE>) AS metrics,
    CAST(NULL AS STRING) AS pod,
    emitted_at_ms
FROM alerts_raw;

-- ── Statement set ────────────────────────────────────────────────────────────

EXECUTE STATEMENT SET
BEGIN

    -- 1. Every raw event, projected onto one schema.
    INSERT INTO signals_normalized
    SELECT
        event_id, event_time, service, signal_type,
        incident_type, severity, message, metrics, emitted_at_ms
    FROM all_signals;

    -- 2. Collapse a burst of signals into a single incident.
    --
    -- Grouping key is (service, incident_type) inside a 30s tumbling window.
    -- An incident is only emitted if the burst is real: either a genuine
    -- alert fired, or enough correlated logs accumulated to matter. A single
    -- stray ERROR line does not page anyone.
    INSERT INTO incidents_correlated
    SELECT
        window_start,
        window_end,
        service,
        incident_type,
        MIN(severity)                                     AS severity,
        COUNT(*)                                          AS signal_count,
        COUNT(*) FILTER (WHERE signal_type = 'log')       AS log_count,
        COUNT(*) FILTER (WHERE signal_type = 'metric')    AS metric_count,
        COUNT(*) FILTER (WHERE signal_type = 'alert')     AS alert_count,
        COUNT(DISTINCT pod)                               AS distinct_pods,
        MIN(message)                                      AS sample_message,
        MIN(emitted_at_ms)                                AS first_emitted_at_ms,
        MAX(emitted_at_ms)                                AS last_emitted_at_ms
    FROM TABLE(
        TUMBLE(TABLE all_signals, DESCRIPTOR(event_time), INTERVAL '30' SECOND)
    )
    GROUP BY window_start, window_end, service, incident_type
    HAVING COUNT(*) FILTER (WHERE signal_type = 'alert') >= 1
        OR COUNT(*) FILTER (WHERE signal_type = 'log')   >= 5;

END;
