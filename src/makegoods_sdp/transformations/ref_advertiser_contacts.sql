CREATE OR REFRESH MATERIALIZED VIEW ref_advertiser_contacts
COMMENT 'Client contacts: who receives make-good offers for each advertiser.'
AS SELECT * FROM read_files(
  '${mg_source_root}/advertiser_contacts/',
  format => 'csv',
  header => true,
  schema => 'advertiser_id STRING, contact_name STRING, contact_email STRING, contact_title STRING'
);
