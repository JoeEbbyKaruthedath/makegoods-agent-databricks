-- As-run log from the traffic system: one row per scheduled spot (aired / preempted / missed).
CREATE OR REFRESH STREAMING TABLE bronze_ad_schedule
COMMENT 'Raw as-run log from the traffic system, ingested incrementally with Auto Loader. Contains known data-quality issues (null spot ids, negative rates, unknown stations, duplicates) that silver cleans.'
AS SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingest_ts
FROM STREAM read_files(
  '${mg_source_root}/as_run_logs/',
  format => 'csv',
  header => true,
  schema => 'spot_id STRING, order_id STRING, station_id STRING, scheduled_ts TIMESTAMP, daypart STRING, program_name STRING, program_genre STRING, spot_length_sec INT, unit_rate_usd DOUBLE, status STRING, preemption_reason STRING, rating_actual DOUBLE'
);
