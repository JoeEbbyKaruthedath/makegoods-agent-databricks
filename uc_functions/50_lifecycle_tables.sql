-- Offer lifecycle in Unity Catalog (run AFTER the pipeline): what the app decided and the daily job prepared, for
-- business results, analytics and the daily digest. Lakebase holds the live workflow (offers, mail, holds); these
-- tables are the governed record. Each is row-filtered like the serving tables, so sellers and managers see their own.
USE CATALOG YOUR_CATALOG;
USE SCHEMA YOUR_SCHEMA;

CREATE TABLE IF NOT EXISTS mg_decision_events (
  event_at TIMESTAMP NOT NULL,
  thread_id STRING,
  offer_id STRING,
  spot_id STRING NOT NULL,
  advertiser_id STRING,
  market STRING,
  seller_id STRING,
  outcome STRING COMMENT 'make_good | make_good_exception | refund | escalated',
  final_avail_ids ARRAY<STRING>,
  refund_usd DOUBLE,
  owed_value_usd DOUBLE COMMENT 'Value of the missed spot (unit rate)',
  booked_value_usd DOUBLE COMMENT 'Rate-card value of the avails booked as the make-good',
  actor STRING COMMENT 'Who made the final decision',
  trace_id STRING COMMENT 'MLflow trace of the recommendation',
  preempted_on DATE,
  days_to_resolve INT COMMENT 'Days from the missed spot to the decision'
)
COMMENT 'Every make-good decision made in the app: booked, refunded or escalated, with its value against the missed spot. Written by the app; row filter mg_row_visible.'
WITH ROW FILTER YOUR_CATALOG.YOUR_SCHEMA_security.mg_row_visible ON (seller_id, market);

CREATE TABLE IF NOT EXISTS mg_advertiser_rules (
  rule_id STRING NOT NULL,
  advertiser_id STRING NOT NULL,
  advertiser_name STRING,
  rule_type STRING COMMENT 'exclude_program | exclude_genre | exclude_tag | only_days | avoid_days | prefer_days | prefer_tag',
  rule_values ARRAY<STRING>,
  strength STRING COMMENT 'hard (never offered) | soft (ranks higher)',
  label STRING,
  source_text STRING COMMENT 'What the seller said',
  created_by STRING,
  created_at TIMESTAMP,
  active BOOLEAN,
  market STRING,
  seller_id STRING
)
COMMENT 'Client rules sellers stated in conversation (copy of the agent’s Lakebase rule memory), so batch jobs and analytics apply the same rules. Row filter mg_row_visible.'
WITH ROW FILTER YOUR_CATALOG.YOUR_SCHEMA_security.mg_row_visible ON (seller_id, market);

CREATE TABLE IF NOT EXISTS mg_proposals (
  spot_id STRING NOT NULL,
  computed_at TIMESTAMP NOT NULL,
  as_of DATE,
  package_label STRING,
  strategy STRING,
  avail_ids ARRAY<STRING>,
  est_impressions BIGINT,
  owed_impressions BIGINT,
  coverage_pct DOUBLE,
  owed_value_usd DOUBLE,
  proposed_value_usd DOUBLE,
  dollar_match BOOLEAN,
  dollar_label STRING,
  rules_passed INT,
  rules_total INT,
  is_full BOOLEAN,
  client_rules ARRAY<STRING>,
  market STRING,
  seller_id STRING
)
COMMENT 'Make-good options prepared overnight by the daily job for every open spot (rules engine only, no AI), so sellers start the day with options ready. Row filter mg_row_visible.'
WITH ROW FILTER YOUR_CATALOG.YOUR_SCHEMA_security.mg_row_visible ON (seller_id, market);

CREATE TABLE IF NOT EXISTS mg_digest_log (
  sent_at TIMESTAMP NOT NULL,
  seller_id STRING,
  to_addr STRING,
  spots INT,
  at_risk_usd DOUBLE,
  message_id STRING,
  mail_mode STRING
)
COMMENT 'Daily make-good digests sent to sellers.';
