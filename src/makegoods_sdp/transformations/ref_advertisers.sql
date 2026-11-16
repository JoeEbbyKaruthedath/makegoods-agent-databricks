CREATE OR REFRESH MATERIALIZED VIEW ref_advertisers
COMMENT 'Advertisers: name, category (used for competitive separation), agency and tier.'
AS SELECT * FROM read_files(
  '${mg_source_root}/advertisers/',
  format => 'csv',
  header => true,
  schema => 'advertiser_id STRING, advertiser_name STRING, category STRING, agency STRING, tier STRING'
);
