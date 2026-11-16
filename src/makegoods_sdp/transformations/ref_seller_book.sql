CREATE OR REFRESH MATERIALIZED VIEW ref_seller_book
COMMENT 'Local account ownership: the seller who owns each advertiser on each station. Drives who sees which make-goods.'
AS SELECT * FROM read_files(
  '${mg_source_root}/seller_book/',
  format => 'csv',
  header => true,
  schema => 'advertiser_id STRING, station_id STRING, market STRING, seller_id STRING'
);
