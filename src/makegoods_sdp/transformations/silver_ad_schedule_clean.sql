CREATE OR REFRESH MATERIALIZED VIEW silver_ad_schedule_clean (
  CONSTRAINT valid_spot_id EXPECT (spot_id IS NOT NULL) ON VIOLATION DROP ROW,
  CONSTRAINT non_negative_rate EXPECT (unit_rate_usd >= 0) ON VIOLATION DROP ROW,
  CONSTRAINT known_station EXPECT (station_known) ON VIOLATION DROP ROW,
  CONSTRAINT valid_status EXPECT (status IN ('Aired', 'Preempted', 'Missed'))
)
COMMENT 'Clean as-run log: one row per spot (deduplicated), invalid rows dropped by expectations, broadcast date (5am day boundary) and policy daypart rank added.'
AS SELECT
  s.spot_id, s.order_id, s.station_id, s.scheduled_ts,
  to_date(s.scheduled_ts - INTERVAL 5 HOURS) AS broadcast_date,
  s.daypart, ${mg_catalog}.${mg_schema}.daypart_rank(s.daypart) AS daypart_rank,
  s.program_name, s.program_genre, s.spot_length_sec, s.unit_rate_usd,
  s.status, s.preemption_reason, s.rating_actual,
  st.station_id IS NOT NULL AS station_known,
  s._ingest_ts
FROM bronze_ad_schedule s
LEFT JOIN ref_stations st ON s.station_id = st.station_id
QUALIFY s.spot_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY s.spot_id ORDER BY s._ingest_ts DESC) = 1;
