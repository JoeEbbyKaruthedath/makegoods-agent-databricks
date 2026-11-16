-- Make-Goods: rule configuration + core deterministic rules (run BEFORE the pipeline).
-- Business rules live here as data and UC functions, not in LLM prompts.

USE CATALOG YOUR_CATALOG;
USE SCHEMA YOUR_SCHEMA;

CREATE TABLE IF NOT EXISTS mg_rules_config (
  rule_key STRING NOT NULL,
  num_value DOUBLE,
  str_value STRING,
  rule_type STRING COMMENT 'must_match | soft_match | system',
  description STRING,
  question_for_ad_ops STRING COMMENT 'What ad ops must confirm before production'
)
COMMENT 'Tunable make-good rule parameters. Ad ops change thresholds and weights here; the UC rule functions read them at call time, so no code change or redeploy is needed.';

MERGE INTO mg_rules_config t
USING (SELECT * FROM VALUES
  ('as_of_date', NULL, '2026-10-30', 'system', 'Simulation clock: "today" for the live make-good work queue.', NULL),
  ('pref_cutoff_date', NULL, '2026-10-15', 'system', 'Advertiser preferences are learned from decisions before this date; evaluation replays decisions on/after it.', NULL),
  ('search_horizon_days', 30, NULL, 'system', 'How many days past the preempted spot inventory_search evaluates avails.', NULL),
  ('rating_floor_pct', 0.90, NULL, 'must_match', 'Avail rating in the contract audience segment must be >= this share of the guaranteed rating.', 'Is 90% the right floor? Does it vary by advertiser tier or agency?'),
  ('default_makegood_window_days', 14, NULL, 'must_match', 'Make-good must air within this many days of the preempted spot when the contract does not specify a window.', 'Same week, same flight, or 14 days? Who can approve exceptions?'),
  ('ie_cap', 1.2, NULL, 'soft_match', 'Impression-equivalence ratio above which extra delivery earns no extra score.', 'Above what ratio is extra delivery worthless to the advertiser?'),
  ('w_impressions', 0.35, NULL, 'soft_match', 'Score weight: replacement impressions vs impressions owed (capped at ie_cap).', 'Is 1:1 impression equivalence the goal, or is a quality multiplier needed?'),
  ('w_genre', 0.15, NULL, 'soft_match', 'Score weight: avail program genre matches the preempted program genre.', 'How much does genre continuity matter to advertisers?'),
  ('w_preference', 0.20, NULL, 'soft_match', 'Score weight: adjacency tags vs advertiser preferences learned from accept/reject history.', 'Which advertisers have written adjacency requirements?'),
  ('w_yield', 0.15, NULL, 'soft_match', 'Score weight: rate-card value of the avail close to the spot unit rate (do not give away Prime for Daytime).', 'Is upgrading the daypart ever a revenue concern?'),
  ('w_timeliness', 0.10, NULL, 'soft_match', 'Score weight: earlier make-goods preferred.', NULL),
  ('w_competitive', 0.30, NULL, 'soft_match', 'Score penalty: pod already carries a competitor in the advertiser category.', 'Is competitive separation a hard rule for some categories (auto, legal)?')
  AS v(rule_key, num_value, str_value, rule_type, description, question_for_ad_ops)) s
ON t.rule_key = s.rule_key
WHEN NOT MATCHED THEN INSERT *;

CREATE OR REPLACE FUNCTION daypart_rank(daypart STRING)
RETURNS INT
COMMENT 'Make-good policy daypart value order (higher = more valuable): Daytime 1 < Early Morning 2 < Early Fringe 3 < Late Fringe 4 < Prime Access 5 < Prime 6.'
RETURN CASE daypart
  WHEN 'Daytime' THEN 1 WHEN 'Early Morning' THEN 2 WHEN 'Early Fringe' THEN 3
  WHEN 'Late Fringe' THEN 4 WHEN 'Prime Access' THEN 5 WHEN 'Prime' THEN 6 END;

CREATE OR REPLACE FUNCTION normalize_audience_key(audience_text STRING)
RETURNS STRING
COMMENT 'Normalizes free-text audience descriptions ("Adults 25 to 54", "women 25-54", "P25-54", "Households") to a canonical segment code (A25-54, W25-54, HH, ...). Returns NULL when no age range or household keyword is present.'
RETURN CASE
  WHEN upper(audience_text) RLIKE '\\b(HH|HOUSEHOLDS?|HOMES)\\b' THEN 'HH'
  WHEN regexp_extract(upper(regexp_replace(audience_text, '[–—]', '-')), '(\\d{2})\\s*(-|TO)\\s*(\\d{2})', 1) = '' THEN NULL
  ELSE concat(
    CASE WHEN upper(audience_text) RLIKE '(WOMEN|FEMALE|\\bW\\s*\\d|\\bF\\s*\\d)' THEN 'W'
         WHEN upper(audience_text) RLIKE '(\\bMEN\\b|\\bMALES?\\b|\\bM\\s*\\d)' THEN 'M'
         ELSE 'A' END,
    regexp_extract(upper(regexp_replace(audience_text, '[–—]', '-')), '(\\d{2})\\s*(-|TO)\\s*(\\d{2})', 1), '-',
    regexp_extract(upper(regexp_replace(audience_text, '[–—]', '-')), '(\\d{2})\\s*(-|TO)\\s*(\\d{2})', 3))
END;
