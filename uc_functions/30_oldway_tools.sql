-- "Old way" baseline tools for the evaluation comparison: raw data dumps with NO rule evaluation.
-- An LLM given these plus the policy text must apply every commensurate rule itself, which is how
-- an earlier GPT-based attempt worked (and why it burned tokens).

USE CATALOG YOUR_CATALOG;
USE SCHEMA YOUR_SCHEMA;

CREATE OR REPLACE FUNCTION raw_spot_and_contract(p_spot_id STRING COMMENT 'Preempted or missed spot id')
RETURNS TABLE (
  spot_id STRING, station_id STRING, call_letters STRING, scheduled_ts TIMESTAMP, broadcast_date DATE, daypart STRING,
  program_name STRING, program_genre STRING, spot_length_sec INT, unit_rate_usd DOUBLE, status STRING,
  preemption_reason STRING, advertiser_name STRING, advertiser_category STRING, audience_segment_text STRING,
  guaranteed_rating DOUBLE, makegood_window_days INT, flight_end DATE, makegood_eligible BOOLEAN
)
COMMENT 'Baseline tool: the preempted spot and its contract exactly as the source systems store them (free-text audience, no resolution).'
RETURN
  SELECT s.spot_id, s.station_id, st.call_letters, s.scheduled_ts, s.broadcast_date, s.daypart, s.program_name, s.program_genre,
    s.spot_length_sec, s.unit_rate_usd, s.status, s.preemption_reason, a.advertiser_name, a.category,
    c.audience_segment_text, c.guaranteed_rating, c.makegood_window_days, c.flight_end, c.makegood_eligible
  FROM silver_ad_schedule_clean s
  JOIN silver_contract_obligations c ON c.order_id = s.order_id
  JOIN ref_advertisers a ON a.advertiser_id = c.advertiser_id
  JOIN ref_stations st ON st.station_id = s.station_id
  WHERE s.spot_id = p_spot_id;

CREATE OR REPLACE FUNCTION raw_station_inventory(
  p_station_id STRING COMMENT 'Station id, e.g. STN-004',
  p_from DATE COMMENT 'First air date (inclusive)',
  p_to DATE COMMENT 'Last air date (inclusive)'
)
RETURNS TABLE (
  avail_id STRING, air_date DATE, daypart STRING, program_name STRING, program_genre STRING, adjacency_tags STRING,
  spot_length_sec INT, est_rating_hh DOUBLE, rate_card_usd DOUBLE, avail_status STRING, booked BOOLEAN,
  competitive_category_blocked STRING
)
COMMENT 'Baseline tool: every avail on a station between two dates, raw (household rating only, no segment ratings, no rule checks).'
RETURN
  SELECT avail_id, air_date, daypart, program_name, program_genre, array_join(adjacency_tags, '|'), spot_length_sec,
    est_rating_hh, rate_card_usd, avail_status, booked_for_spot_id IS NOT NULL, array_join(competitive_categories_blocked, '|')
  FROM gold_available_inventory
  WHERE station_id = p_station_id AND air_date BETWEEN p_from AND p_to;

CREATE OR REPLACE FUNCTION raw_audience_indices()
RETURNS TABLE (segment_code STRING, aliases ARRAY<STRING>, daypart STRING, genre STRING, rating_index DOUBLE)
COMMENT 'Baseline tool: the audience taxonomy rating indices (segment rating = household rating x index).'
RETURN
  SELECT r.segment_code, a.aliases, r.daypart, r.genre, r.rating_index
  FROM silver_segment_rating_index r
  JOIN (SELECT segment_code, collect_set(alias_text) AS aliases FROM silver_audience_mapped GROUP BY segment_code) a
    ON a.segment_code = r.segment_code;
