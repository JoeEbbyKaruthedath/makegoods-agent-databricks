CREATE OR REFRESH MATERIALIZED VIEW silver_inventory_enriched (
  CONSTRAINT valid_avail EXPECT (avail_id IS NOT NULL AND station_id IS NOT NULL) ON VIOLATION DROP ROW,
  CONSTRAINT known_daypart EXPECT (daypart_rank IS NOT NULL)
)
COMMENT 'Avails enriched with market, policy daypart rank, parsed adjacency tags / competitive blocks, and per-segment rating and impression estimates (maps keyed by audience_segment_id).'
AS WITH a AS (
  SELECT * FROM bronze_inventory_avails
  QUALIFY ROW_NUMBER() OVER (PARTITION BY avail_id ORDER BY _ingest_ts DESC) = 1
)
SELECT
  a.avail_id, a.station_id, st.call_letters, st.market, a.air_date, a.daypart,
  ${mg_catalog}.${mg_schema}.daypart_rank(a.daypart) AS daypart_rank,
  a.program_name, a.program_genre,
  split(a.adjacency_tags, '\\|') AS adjacency_tags,
  coalesce(split(a.competitive_category_blocked, '\\|'), array()) AS competitive_categories_blocked,
  a.spot_length_sec, a.est_rating_hh, a.rate_card_usd, a.avail_status,
  map_from_entries(collect_list(struct(r.audience_segment_id, round(a.est_rating_hh * r.rating_index, 2)))) AS segment_ratings,
  map_from_entries(collect_list(struct(r.audience_segment_id,
    CAST(round(a.est_rating_hh * r.rating_index / 100 * u.universe_000 * 1000) AS BIGINT)))) AS segment_impressions
FROM a
JOIN ref_stations st ON st.station_id = a.station_id
JOIN silver_segment_rating_index r ON r.daypart = a.daypart AND r.genre = a.program_genre
JOIN silver_segment_universe u ON u.market = st.market AND u.audience_segment_id = r.audience_segment_id
GROUP BY ALL;
