CREATE OR REFRESH MATERIALIZED VIEW silver_segment_universe
COMMENT 'Audience universe estimates (persons/households, thousands) by market and segment. Impressions = rating / 100 x universe.'
AS WITH seg AS (
  SELECT * FROM bronze_audience_segments
  QUALIFY ROW_NUMBER() OVER (PARTITION BY audience_segment_id ORDER BY _ingest_ts DESC) = 1
)
SELECT s.audience_segment_id, s.segment_code, u.market, u.dma_rank, u.universe_000
FROM seg s LATERAL VIEW explode(s.universe_estimates) ut AS u;
