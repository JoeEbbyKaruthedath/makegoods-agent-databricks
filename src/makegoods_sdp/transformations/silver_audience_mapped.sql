CREATE OR REFRESH MATERIALIZED VIEW silver_audience_mapped
COMMENT 'Audience alias dictionary: every published alias (upper-cased) resolved to its canonical audience_segment_id. Used for exact-match audience mapping.'
AS WITH seg AS (
  SELECT * FROM bronze_audience_segments
  QUALIFY ROW_NUMBER() OVER (PARTITION BY audience_segment_id ORDER BY _ingest_ts DESC) = 1
)
SELECT DISTINCT
  upper(trim(alias)) AS alias_upper, alias AS alias_text,
  audience_segment_id, segment_code, segment_name, gender, age_min, age_max
FROM seg LATERAL VIEW explode(aliases) a AS alias;
