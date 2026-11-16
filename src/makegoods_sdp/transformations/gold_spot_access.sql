CREATE OR REFRESH MATERIALIZED VIEW gold_spot_access
COMMENT 'One row per scheduled spot with its schedule, the market and the seller who owns the account. The agent''s rule functions join it, so a person can only work on spots of clients they may see, and the preemption analytics read it, so each person sees their own clients'' preemptions (row filter mg_row_visible).'
WITH ROW FILTER ${mg_catalog}.${mg_schema}_security.mg_row_visible ON (seller_id, market)
AS SELECT s.spot_id, s.order_id, c.advertiser_id, s.station_id, c.market, c.seller_id,
  s.scheduled_ts, s.broadcast_date, s.daypart, s.program_genre, s.status, s.preemption_reason, s.unit_rate_usd
FROM silver_ad_schedule_clean s
JOIN silver_contract_obligations c ON c.order_id = s.order_id;
