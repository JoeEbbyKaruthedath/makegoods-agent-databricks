CREATE OR REFRESH MATERIALIZED VIEW gold_make_good_history
COMMENT 'Every historical make-good offer with spot, contract, advertiser and offered-avail context: outcome, refund, downgrade flag, matching method and GPT token usage. Source of the evaluation dataset and the GPT cost baseline.'
AS SELECT
  o.offer_id, o.spot_id, o.order_id, c.advertiser_id, a.advertiser_name, a.category AS advertiser_category,
  o.station_id, st.call_letters, st.market,
  s.scheduled_ts AS spot_scheduled_ts, s.broadcast_date AS spot_date, s.preemption_reason, s.status AS spot_status,
  s.program_genre AS spot_program_genre, s.unit_rate_usd,
  o.original_daypart, ${mg_catalog}.${mg_schema}.daypart_rank(o.original_daypart) AS original_daypart_rank,
  o.offered_avail_id, o.offered_daypart, ${mg_catalog}.${mg_schema}.daypart_rank(o.offered_daypart) AS offered_daypart_rank,
  ${mg_catalog}.${mg_schema}.daypart_rank(o.offered_daypart) < ${mg_catalog}.${mg_schema}.daypart_rank(o.original_daypart) AS is_downgrade,
  o.offered_air_date, o.offered_program, i.program_genre AS offered_program_genre, i.adjacency_tags AS offered_adjacency_tags,
  o.offered_ts, o.decision_ts, datediff(to_date(o.offered_ts), s.broadcast_date) AS days_to_offer,
  o.offer_status, o.refund_issued, o.refund_amount_usd,
  o.matched_by, o.reviewer,
  o.gpt_prompt_tokens, o.gpt_completion_tokens, o.gpt_prompt_tokens + o.gpt_completion_tokens AS gpt_total_tokens,
  c.audience_segment_id, c.segment_code
FROM silver_make_good_offers o
LEFT JOIN silver_ad_schedule_clean s ON s.spot_id = o.spot_id
LEFT JOIN silver_contract_obligations c ON c.order_id = o.order_id
LEFT JOIN ref_advertisers a ON a.advertiser_id = c.advertiser_id
LEFT JOIN ref_stations st ON st.station_id = o.station_id
LEFT JOIN silver_inventory_enriched i ON i.avail_id = o.offered_avail_id;
