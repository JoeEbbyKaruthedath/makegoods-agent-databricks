-- Measurement feed: delivered impressions per aired spot, hourly grain, with reporting latency.
CREATE OR REFRESH STREAMING TABLE bronze_delivery_pacing
COMMENT 'Raw delivery/pacing measurements (ACR preliminary, Nielsen overnight, final C3) per aired spot at hourly grain, with reported_ts for latency analysis.'
AS SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingest_ts
FROM STREAM read_files(
  '${mg_source_root}/delivery_pacing/',
  format => 'csv',
  header => true,
  schema => 'order_id STRING, station_id STRING, spot_id STRING, event_hour TIMESTAMP, audience_segment_code STRING, measurement_stage STRING, impressions BIGINT, reported_ts TIMESTAMP, source_system STRING'
);
