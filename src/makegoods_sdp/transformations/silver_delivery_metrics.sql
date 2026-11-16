CREATE OR REFRESH MATERIALIZED VIEW silver_delivery_metrics (
  CONSTRAINT non_negative_impressions EXPECT (impressions >= 0)
)
COMMENT 'Daily delivered impressions per order using the best available measurement per spot (final C3 when reported, else ACR preliminary / Nielsen overnight), with cumulative delivery and first-report latency.'
AS WITH p AS (
  SELECT * FROM bronze_delivery_pacing
  WHERE order_id IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY spot_id, measurement_stage ORDER BY reported_ts DESC) = 1
),
best AS (
  SELECT * FROM p
  QUALIFY ROW_NUMBER() OVER (PARTITION BY spot_id
    ORDER BY CASE measurement_stage WHEN 'final_c3' THEN 1 ELSE 2 END, reported_ts DESC) = 1
),
first_report AS (
  SELECT spot_id, min(reported_ts) AS first_reported_ts FROM p GROUP BY spot_id
)
SELECT
  b.order_id, b.station_id,
  to_date(b.event_hour - INTERVAL 5 HOURS) AS delivery_date,
  sum(b.impressions) AS impressions,
  count(*) AS spots_measured,
  sum(CASE WHEN b.measurement_stage = 'final_c3' THEN 1 ELSE 0 END) AS spots_final,
  round(avg((unix_timestamp(f.first_reported_ts) - unix_timestamp(b.event_hour)) / 3600), 1) AS avg_first_report_latency_hours,
  sum(sum(b.impressions)) OVER (PARTITION BY b.order_id ORDER BY to_date(b.event_hour - INTERVAL 5 HOURS)) AS cumulative_impressions
FROM best b JOIN first_report f ON f.spot_id = b.spot_id
GROUP BY b.order_id, b.station_id, to_date(b.event_hour - INTERVAL 5 HOURS);
