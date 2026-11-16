-- Audience taxonomy export: canonical segments, aliases, rating indices and universe estimates.
CREATE OR REFRESH STREAMING TABLE bronze_audience_segments
COMMENT 'Raw audience taxonomy export: one record per canonical segment with aliases, daypart/genre rating indices and DMA universe estimates.'
AS SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingest_ts
FROM STREAM read_files(
  '${mg_source_root}/audience_segments/',
  format => 'json',
  schema => 'audience_segment_id STRING, segment_code STRING, segment_name STRING, gender STRING, age_min INT, age_max INT, aliases ARRAY<STRING>, daypart_index ARRAY<STRUCT<daypart: STRING, `index`: DOUBLE>>, genre_index ARRAY<STRUCT<genre: STRING, `index`: DOUBLE>>, universe_estimates ARRAY<STRUCT<market: STRING, dma_rank: INT, universe_000: INT>>'
);
