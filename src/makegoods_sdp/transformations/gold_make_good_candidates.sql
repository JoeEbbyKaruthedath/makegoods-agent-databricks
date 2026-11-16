CREATE OR REFRESH MATERIALIZED VIEW gold_make_good_candidates
COMMENT 'The agent''s work queue: preempted/missed spots on make-good-eligible contracts with no accepted make-good and no refund, whose make-good deadline has not passed as of the simulation clock (mg_rules_config.as_of_date). Each person sees only the clients they may see (row filter mg_row_visible).'
WITH ROW FILTER ${mg_catalog}.${mg_schema}_security.mg_row_visible ON (seller_id, market)
AS WITH cfg AS (SELECT CAST(str_value AS DATE) AS as_of FROM ${mg_catalog}.${mg_schema}.mg_rules_config WHERE rule_key = 'as_of_date')
SELECT
  s.spot_id, s.order_id, c.advertiser_id, a.advertiser_name, a.category AS advertiser_category,
  s.station_id, st.call_letters, st.market, st.network_affiliation,
  s.scheduled_ts, s.broadcast_date, s.daypart, s.daypart_rank, s.program_name, s.program_genre,
  s.spot_length_sec, s.status, s.preemption_reason,
  s.unit_rate_usd AS revenue_at_risk_usd,
  c.audience_segment_text, c.audience_segment_id, c.segment_code, c.guaranteed_rating, c.owed_impressions_per_spot,
  least(date_add(s.broadcast_date, c.makegood_window_days), c.flight_end) AS makegood_deadline,
  datediff(least(date_add(s.broadcast_date, c.makegood_window_days), c.flight_end), cfg.as_of) AS days_to_deadline,
  coalesce(o.latest_offer_status, 'No offer') AS latest_offer_status,
  o.latest_offer_id,
  CASE WHEN o.latest_offer_status = 'Pending' THEN 'Offer pending' ELSE 'Needs make-good' END AS queue_status,
  cfg.as_of AS as_of_date,
  c.seller_id, sl.seller_name, sl.manager_id
FROM silver_ad_schedule_clean s
JOIN silver_contract_obligations c ON c.order_id = s.order_id
JOIN ref_advertisers a ON a.advertiser_id = c.advertiser_id
JOIN ref_stations st ON st.station_id = s.station_id
LEFT JOIN spot_offer_status o ON o.spot_id = s.spot_id
LEFT JOIN ref_sellers sl ON sl.seller_id = c.seller_id
CROSS JOIN cfg
WHERE s.status <> 'Aired'
  AND c.makegood_eligible
  AND NOT coalesce(o.made_good, false)
  AND NOT coalesce(o.refunded, false)
  AND s.broadcast_date <= cfg.as_of
  AND least(date_add(s.broadcast_date, c.makegood_window_days), c.flight_end) > cfg.as_of;
