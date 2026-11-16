CREATE OR REFRESH MATERIALIZED VIEW silver_contract_obligations (
  CONSTRAINT audience_resolved EXPECT (audience_segment_id IS NOT NULL),
  CONSTRAINT valid_flight EXPECT (flight_start <= flight_end) ON VIOLATION DROP ROW
)
COMMENT 'Contracts with the free-text audience target resolved to a canonical segment (exact alias -> normalized text -> advertiser default), market universe, impressions owed per spot, and the seller who owns the account.'
AS WITH c AS (
  SELECT * FROM bronze_contracts
  QUALIFY ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY _ingest_ts DESC) = 1
),
seg AS (SELECT DISTINCT audience_segment_id, segment_code FROM silver_audience_mapped),
resolved AS (
  SELECT c.*, ex.audience_segment_id AS seg_exact, nm.audience_segment_id AS seg_norm
  FROM c
  LEFT JOIN silver_audience_mapped ex ON ex.alias_upper = upper(trim(c.audience_segment_text))
  LEFT JOIN seg nm ON nm.segment_code = ${mg_catalog}.${mg_schema}.normalize_audience_key(c.audience_segment_text)
),
adv_default AS (
  SELECT advertiser_id, max_by(seg_id, n) AS seg_default
  FROM (SELECT advertiser_id, coalesce(seg_exact, seg_norm) AS seg_id, count(*) AS n
        FROM resolved WHERE coalesce(seg_exact, seg_norm) IS NOT NULL GROUP BY ALL)
  GROUP BY advertiser_id
),
final AS (
  SELECT r.*,
    coalesce(r.seg_exact, r.seg_norm, d.seg_default) AS audience_segment_id,
    CASE WHEN r.seg_exact IS NOT NULL THEN 'exact_alias' WHEN r.seg_norm IS NOT NULL THEN 'normalized'
         WHEN d.seg_default IS NOT NULL THEN 'advertiser_default' ELSE 'unmapped' END AS audience_match_type,
    CASE WHEN r.seg_exact IS NOT NULL THEN 1.0 WHEN r.seg_norm IS NOT NULL THEN 0.9
         WHEN d.seg_default IS NOT NULL THEN 0.6 ELSE 0.0 END AS audience_confidence
  FROM resolved r LEFT JOIN adv_default d ON d.advertiser_id = r.advertiser_id
)
SELECT
  f.order_id, f.advertiser_id, f.station_id, st.market, f.campaign_name, f.daypart,
  ${mg_catalog}.${mg_schema}.daypart_rank(f.daypart) AS daypart_rank,
  f.spot_length_sec, f.unit_rate_usd, f.spots_per_week, f.flight_start, f.flight_end,
  CAST(round(f.spots_per_week * (datediff(f.flight_end, f.flight_start) + 1) / 7) AS INT) AS planned_spots,
  f.audience_segment_text, f.audience_segment_id, s.segment_code, f.audience_match_type, f.audience_confidence,
  f.guaranteed_rating, f.contracted_impressions, u.universe_000,
  CAST(round(f.guaranteed_rating / 100 * u.universe_000 * 1000) AS BIGINT) AS owed_impressions_per_spot,
  f.makegood_window_days, f.makegood_eligible, f.competitive_category, f.preemptible,
  b.seller_id
FROM final f
JOIN ref_stations st ON st.station_id = f.station_id
LEFT JOIN ref_seller_book b ON b.advertiser_id = f.advertiser_id AND b.station_id = f.station_id
LEFT JOIN seg s ON s.audience_segment_id = f.audience_segment_id
LEFT JOIN silver_segment_universe u ON u.market = st.market AND u.audience_segment_id = f.audience_segment_id;
