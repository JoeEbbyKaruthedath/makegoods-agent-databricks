USE CATALOG YOUR_CATALOG;
USE SCHEMA YOUR_SCHEMA;
SELECT 'bronze_ad_schedule' t, count(*) n FROM bronze_ad_schedule UNION ALL SELECT 'silver_ad_schedule_clean', count(*) FROM silver_ad_schedule_clean
UNION ALL SELECT 'bronze_inventory_avails', count(*) FROM bronze_inventory_avails UNION ALL SELECT 'gold_available_inventory', count(*) FROM gold_available_inventory
UNION ALL SELECT 'silver_contract_obligations', count(*) FROM silver_contract_obligations UNION ALL SELECT 'silver_delivery_metrics', count(*) FROM silver_delivery_metrics
UNION ALL SELECT 'gold_make_good_history', count(*) FROM gold_make_good_history UNION ALL SELECT 'gold_make_good_candidates', count(*) FROM gold_make_good_candidates
UNION ALL SELECT 'gold_impression_shortfall', count(*) FROM gold_impression_shortfall UNION ALL SELECT 'gold_advertiser_preferences', count(*) FROM gold_advertiser_preferences;
SELECT audience_match_type, count(*) n, round(avg(audience_confidence), 2) conf FROM silver_contract_obligations GROUP BY 1 ORDER BY 2 DESC;
SELECT original_daypart, count(*) offers, sum(CASE WHEN is_downgrade THEN 1 ELSE 0 END) downgrades, round(sum(refund_amount_usd)) refund_usd FROM gold_make_good_history GROUP BY 1 ORDER BY refund_usd DESC;
SELECT matched_by, count(*) offers, round(avg(CASE WHEN offer_status = 'Accepted' THEN 1.0 ELSE 0 END), 2) accept_rate, round(sum(refund_amount_usd)) refund_usd, percentile(gpt_total_tokens, 0.5) median_gpt_tokens FROM gold_make_good_history GROUP BY 1;
SELECT preemption_reason, count(*) spots FROM silver_ad_schedule_clean WHERE status <> 'Aired' GROUP BY 1 ORDER BY 2 DESC;
SELECT st.market, count(*) breaking_news FROM silver_ad_schedule_clean s JOIN ref_stations st USING (station_id) WHERE preemption_reason = 'Breaking News' AND broadcast_date BETWEEN '2026-10-05' AND '2026-10-09' GROUP BY 1 ORDER BY 2 DESC LIMIT 5;
SELECT queue_status, count(*) spots, round(sum(revenue_at_risk_usd)) usd_at_risk FROM gold_make_good_candidates GROUP BY 1;
SELECT spot_id, advertiser_name, call_letters, market, broadcast_date, daypart, preemption_reason, revenue_at_risk_usd, segment_code, owed_impressions_per_spot, days_to_deadline FROM gold_make_good_candidates WHERE advertiser_id = 'ADV-0151';
SELECT round(avg(pacing_index), 3) avg_pacing, sum(unresolved_spots) unresolved, round(sum(revenue_at_risk_usd)) at_risk_usd, round(sum(refunded_usd)) refunded_usd FROM gold_impression_shortfall;
