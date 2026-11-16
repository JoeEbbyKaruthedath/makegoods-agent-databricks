CREATE OR REFRESH MATERIALIZED VIEW ref_sellers
COMMENT 'Sales org: sellers (account executives) and the sales managers they report to, with the markets each region covers.'
AS SELECT seller_id, seller_name, seller_email, role, region,
  from_json(markets, 'ARRAY<STRING>') AS markets, manager_id
FROM read_files(
  '${mg_source_root}/sellers/',
  format => 'csv',
  header => true,
  schema => 'seller_id STRING, seller_name STRING, seller_email STRING, role STRING, region STRING, markets STRING, manager_id STRING'
);
