CREATE OR REFRESH MATERIALIZED VIEW silver_segment_rating_index
COMMENT 'Segment rating index by daypart and program genre: segment rating = household rating x daypart index x genre index.'
AS WITH seg AS (
  SELECT * FROM bronze_audience_segments
  QUALIFY ROW_NUMBER() OVER (PARTITION BY audience_segment_id ORDER BY _ingest_ts DESC) = 1
)
SELECT s.audience_segment_id, s.segment_code, d.daypart, g.genre,
  d.`index` AS daypart_index, g.`index` AS genre_index,
  round(d.`index` * g.`index`, 4) AS rating_index
FROM seg s
LATERAL VIEW explode(s.daypart_index) dt AS d
LATERAL VIEW explode(s.genre_index) gt AS g;
