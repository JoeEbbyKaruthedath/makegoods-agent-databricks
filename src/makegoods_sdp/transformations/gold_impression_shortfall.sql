CREATE OR REFRESH MATERIALIZED VIEW gold_impression_shortfall
COMMENT 'Per guaranteed contract: contracted vs expected-to-date vs delivered impressions (pacing index), plus preempted spots that are made good, refunded or still unresolved, with owed impressions and revenue at risk. Row filter mg_row_visible.'
WITH ROW FILTER ${mg_catalog}.${mg_schema}_security.mg_row_visible ON (seller_id, market)
AS WITH cfg AS (SELECT CAST(str_value AS DATE) AS as_of FROM ${mg_catalog}.${mg_schema}.mg_rules_config WHERE rule_key = 'as_of_date'),
deliv AS (SELECT order_id, sum(impressions) AS delivered FROM silver_delivery_metrics GROUP BY order_id),
pre AS (
  SELECT s.order_id,
    count(*) AS preempted_spots,
    sum(CASE WHEN o.made_good THEN 1 ELSE 0 END) AS made_good_spots,
    sum(CASE WHEN o.refunded THEN 1 ELSE 0 END) AS refunded_spots,
    sum(CASE WHEN NOT coalesce(o.made_good, false) AND NOT coalesce(o.refunded, false) THEN 1 ELSE 0 END) AS unresolved_spots,
    sum(CASE WHEN NOT coalesce(o.made_good, false) AND NOT coalesce(o.refunded, false) THEN s.unit_rate_usd ELSE 0 END) AS revenue_at_risk_usd,
    sum(coalesce(o.refund_amount_usd, 0)) AS refunded_usd
  FROM silver_ad_schedule_clean s LEFT JOIN spot_offer_status o ON o.spot_id = s.spot_id
  WHERE s.status <> 'Aired' GROUP BY s.order_id
),
base AS (
  SELECT c.*, a.advertiser_name, cfg.as_of,
    least(greatest(datediff(cfg.as_of, c.flight_start) + 1, 0) / (datediff(c.flight_end, c.flight_start) + 1), 1.0) AS elapsed_pct,
    coalesce(d.delivered, 0) AS delivered_to_date,
    coalesce(p.preempted_spots, 0) AS preempted_spots, coalesce(p.made_good_spots, 0) AS made_good_spots,
    coalesce(p.refunded_spots, 0) AS refunded_spots, coalesce(p.unresolved_spots, 0) AS unresolved_spots,
    coalesce(p.revenue_at_risk_usd, 0) AS revenue_at_risk_usd, coalesce(p.refunded_usd, 0) AS refunded_usd
  FROM silver_contract_obligations c
  JOIN ref_advertisers a ON a.advertiser_id = c.advertiser_id
  CROSS JOIN cfg
  LEFT JOIN deliv d ON d.order_id = c.order_id
  LEFT JOIN pre p ON p.order_id = c.order_id
  WHERE c.flight_start <= cfg.as_of
)
SELECT
  order_id, advertiser_id, advertiser_name, station_id, market, campaign_name, daypart,
  audience_segment_id, segment_code, flight_start, flight_end, as_of AS as_of_date,
  contracted_impressions, planned_spots, round(elapsed_pct, 3) AS elapsed_pct,
  CAST(round(contracted_impressions * elapsed_pct) AS BIGINT) AS expected_to_date_impressions,
  delivered_to_date AS delivered_impressions,
  round(delivered_to_date / nullif(contracted_impressions * elapsed_pct, 0), 3) AS pacing_index,
  greatest(CAST(round(contracted_impressions * elapsed_pct) AS BIGINT) - delivered_to_date, 0) AS shortfall_to_date_impressions,
  preempted_spots, made_good_spots, refunded_spots, unresolved_spots,
  unresolved_spots * owed_impressions_per_spot AS owed_impressions_unresolved,
  revenue_at_risk_usd, refunded_usd, makegood_eligible, seller_id
FROM base;
