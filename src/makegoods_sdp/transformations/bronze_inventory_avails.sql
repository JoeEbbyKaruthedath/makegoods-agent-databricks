-- Unsold inventory (avails) published by the traffic system: candidate make-good slots.
CREATE OR REFRESH STREAMING TABLE bronze_inventory_avails
COMMENT 'Raw inventory avails (unsold spot slots) by station, date and daypart, with program genre, adjacency tags and competitive blocks.'
AS SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingest_ts
FROM STREAM read_files(
  '${mg_source_root}/inventory_avails/',
  format => 'csv',
  header => true,
  schema => 'avail_id STRING, station_id STRING, air_date DATE, daypart STRING, program_name STRING, program_genre STRING, adjacency_tags STRING, spot_length_sec INT, est_rating_hh DOUBLE, rate_card_usd DOUBLE, competitive_category_blocked STRING, avail_status STRING'
);
