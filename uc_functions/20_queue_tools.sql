-- Read-only tools for open-ended questions about the make-good workload (used by the agent's chat node).

USE CATALOG YOUR_CATALOG;
USE SCHEMA YOUR_SCHEMA;

CREATE OR REPLACE FUNCTION make_good_queue(
  p_market STRING DEFAULT NULL COMMENT 'Filter by market (DMA), e.g. Nashville',
  p_advertiser STRING DEFAULT NULL COMMENT 'Filter by advertiser name (case-insensitive substring)',
  p_limit INT DEFAULT 10 COMMENT 'Maximum rows to return'
)
RETURNS TABLE (
  spot_id STRING, advertiser_name STRING, call_letters STRING, market STRING, broadcast_date DATE, daypart STRING,
  preemption_reason STRING, revenue_at_risk_usd DOUBLE, segment_code STRING, makegood_deadline DATE,
  days_to_deadline INT, queue_status STRING
)
COMMENT 'Open make-good work queue: preempted or missed spots that still need a make-good, most urgent first (days to deadline, then revenue at risk).'
RETURN
  SELECT spot_id, advertiser_name, call_letters, market, broadcast_date, daypart, preemption_reason,
    revenue_at_risk_usd, segment_code, makegood_deadline, days_to_deadline, queue_status
  FROM (
    SELECT *, ROW_NUMBER() OVER (ORDER BY days_to_deadline, revenue_at_risk_usd DESC, spot_id) AS rn
    FROM gold_make_good_candidates
    WHERE (p_market IS NULL OR lower(market) = lower(p_market))
      AND (p_advertiser IS NULL OR lower(advertiser_name) LIKE concat('%', lower(p_advertiser), '%'))
  )
  WHERE rn <= p_limit;

CREATE OR REPLACE FUNCTION make_good_exposure_summary()
RETURNS TABLE (
  market STRING, open_spots BIGINT, revenue_at_risk_usd DOUBLE, due_within_3_days BIGINT,
  refunded_usd_to_date DOUBLE, top_preemption_reason STRING
)
COMMENT 'Make-good exposure by market: open spots and revenue at risk in the work queue, how many are due within 3 days, refunds issued to date, and the most common preemption reason.'
RETURN
  WITH q AS (
    SELECT market, count(*) AS open_spots, sum(revenue_at_risk_usd) AS revenue_at_risk_usd,
      count_if(days_to_deadline <= 3) AS due_within_3_days, mode(preemption_reason) AS top_preemption_reason
    FROM gold_make_good_candidates GROUP BY market
  ),
  r AS (SELECT market, sum(refund_amount_usd) AS refunded FROM gold_make_good_history GROUP BY market)
  SELECT q.market, q.open_spots, round(q.revenue_at_risk_usd, 0), q.due_within_3_days,
    round(coalesce(r.refunded, 0), 0), q.top_preemption_reason
  FROM q LEFT JOIN r ON r.market = q.market;

CREATE OR REPLACE FUNCTION make_good_rules()
RETURNS TABLE (rule STRING, rule_type STRING, current_value STRING, description STRING)
COMMENT 'The make-good policy as the rules engine applies it: every must-match rule (with its failure code) and every soft-match ranking weight, with current values from mg_rules_config.'
RETURN
  SELECT * FROM VALUES
    ('same_station', 'must_match', 'required', 'The make-good airs on the same station (same DMA) as the preempted spot.'),
    ('daypart_lower_value', 'must_match', 'Daytime < Early Morning < Early Fringe < Late Fringe < Prime Access < Prime', 'The make-good daypart must be the same or more valuable than the preempted daypart.'),
    ('rating_below_floor', 'must_match', 'see rating_floor_pct', 'The avail rating in the contract audience segment must reach the floor share of the guaranteed rating.'),
    ('spot_length_mismatch', 'must_match', 'required', 'Same spot length (15 / 30 / 60 seconds).'),
    ('outside_makegood_window', 'must_match', 'contract make-good window', 'Airs after the preempted spot and within the contract make-good window.'),
    ('after_flight_end', 'must_match', 'required', 'Airs no later than the contract flight end.'),
    ('avail_held / already_booked', 'must_match', 'required', 'The avail is Open and not already booked for another make-good.'),
    ('contract_not_makegood_eligible', 'must_match', 'contract flag', 'The contract allows make-goods (otherwise the advertiser is refunded).'),
    ('match_score', 'tiered_scoring', '0-10 (10 = perfect)', 'Every option is scored out of 10: 10 minus graded penalties for each rule it misses, minus up to fit_penalty_max for weak soft-match fit. Commensurate options (no misses) rank first; near-misses (soft misses only, score >= near_miss_min_score) are offered for reviewer approval as exceptions instead of a refund. Already aired, already booked and ineligible contracts are never offered.')
    AS v(rule, rule_type, current_value, description)
  UNION ALL
  SELECT rule_key, rule_type, coalesce(CAST(num_value AS STRING), str_value), description
  FROM mg_rules_config WHERE rule_type IN ('must_match', 'soft_match', 'near_miss');
