-- Guaranteed-audience orders from the order management system.
CREATE OR REFRESH STREAMING TABLE bronze_contracts
COMMENT 'Raw guaranteed-audience contracts/orders: flight, daypart, free-text audience target, guaranteed rating, contracted impressions and make-good eligibility.'
AS SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingest_ts
FROM STREAM read_files(
  '${mg_source_root}/contracts/',
  format => 'json',
  schema => 'order_id STRING, advertiser_id STRING, station_id STRING, campaign_name STRING, daypart STRING, spot_length_sec INT, unit_rate_usd DOUBLE, spots_per_week INT, flight_start DATE, flight_end DATE, audience_segment_text STRING, guaranteed_rating DOUBLE, contracted_impressions BIGINT, makegood_window_days INT, makegood_eligible BOOLEAN, competitive_category STRING, preemptible BOOLEAN'
);
