CREATE OR REFRESH MATERIALIZED VIEW ref_stations
COMMENT 'Owned stations: call letters, market (DMA), DMA rank, network affiliation.'
AS SELECT * FROM read_files(
  '${mg_source_root}/stations/',
  format => 'csv',
  header => true,
  schema => 'station_id STRING, call_letters STRING, market STRING, dma_rank INT, network_affiliation STRING, timezone STRING'
);
