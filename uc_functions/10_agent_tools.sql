-- Make-Goods: deterministic "commensurate" rules + the agent's tools (run AFTER the pipeline).
-- Every business rule executes here, in SQL governed by Unity Catalog. The LLM only orchestrates
-- and explains; it never evaluates a rule itself. Thresholds/weights come from mg_rules_config.

USE CATALOG YOUR_CATALOG;
USE SCHEMA YOUR_SCHEMA;

-- Rule engine: evaluates every must-match rule for every avail on the spot's station within the
-- search horizon and names each rule an avail fails.
CREATE OR REPLACE FUNCTION evaluate_inventory(
  p_spot_id STRING COMMENT 'Preempted or missed spot id, e.g. SPT-0058222',
  p_as_of DATE DEFAULT NULL COMMENT 'Decision date; avails on or before it are in the past. NULL = mg_rules_config.as_of_date',
  p_horizon_days INT DEFAULT NULL COMMENT 'Days after the spot to search. NULL = mg_rules_config.search_horizon_days (the gap-closing loop widens it)'
)
RETURNS TABLE (
  avail_id STRING, air_date DATE, daypart STRING, program_name STRING, program_genre STRING,
  adjacency_tags ARRAY<STRING>, spot_length_sec INT, avail_status STRING, booked_for_spot_id STRING,
  rate_card_usd DOUBLE, segment_rating DOUBLE, est_impressions BIGINT, owed_impressions BIGINT,
  impression_equivalence DOUBLE, competitive_conflict BOOLEAN, is_commensurate BOOLEAN, failed_rules ARRAY<STRING>,
  rating_floor DOUBLE, hard_fail BOOLEAN, rule_penalty DOUBLE, misses ARRAY<STRING>
)
COMMENT 'Make-good rules engine. For a preempted spot, evaluates every must-match commensurate rule (same station, same-or-better daypart, segment rating >= floor, same length, within make-good window and flight, Open and unbooked, in the future) against every avail on the station. Lists failed rules, separates hard failures (already aired, already booked, contract not make-good eligible) from soft misses, and grades each soft miss by how close it is (rule_penalty, misses) for tiered near-miss scoring.'
RETURN
  WITH sp AS (
    SELECT s.spot_id, s.station_id, s.broadcast_date, s.daypart_rank, s.daypart AS spot_daypart, s.spot_length_sec,
      c.audience_segment_id, c.guaranteed_rating, c.owed_impressions_per_spot, c.flight_end,
      c.makegood_eligible, c.competitive_category, c.makegood_window_days AS window_days
    FROM silver_ad_schedule_clean s JOIN silver_contract_obligations c ON c.order_id = s.order_id
    JOIN gold_spot_access g ON g.spot_id = s.spot_id  -- only spots of clients the caller may see
    WHERE s.spot_id = p_spot_id
  ),
  cfg AS (
    SELECT
      coalesce(p_as_of, CAST(max(CASE WHEN rule_key = 'as_of_date' THEN str_value END) AS DATE)) AS as_of,
      max(CASE WHEN rule_key = 'rating_floor_pct' THEN num_value END) AS floor_pct,
      coalesce(p_horizon_days, CAST(max(CASE WHEN rule_key = 'search_horizon_days' THEN num_value END) AS INT)) AS horizon,
      max(CASE WHEN rule_key = 'pen_rating_base' THEN num_value END) AS pen_rating_base,
      max(CASE WHEN rule_key = 'pen_rating_per_pct' THEN num_value END) AS pen_rating_per_pct,
      max(CASE WHEN rule_key = 'pen_daypart_step' THEN num_value END) AS pen_daypart_step,
      max(CASE WHEN rule_key = 'pen_length_shorter' THEN num_value END) AS pen_length_shorter,
      max(CASE WHEN rule_key = 'pen_length_longer' THEN num_value END) AS pen_length_longer,
      max(CASE WHEN rule_key = 'pen_window_base' THEN num_value END) AS pen_window_base,
      max(CASE WHEN rule_key = 'pen_window_per_day' THEN num_value END) AS pen_window_per_day,
      max(CASE WHEN rule_key = 'pen_flight_base' THEN num_value END) AS pen_flight_base,
      max(CASE WHEN rule_key = 'pen_flight_per_day' THEN num_value END) AS pen_flight_per_day,
      max(CASE WHEN rule_key = 'pen_avail_held' THEN num_value END) AS pen_avail_held
    FROM mg_rules_config
  ),
  ev AS (
    SELECT a.*, sp.spot_id AS p_spot, sp.broadcast_date, sp.daypart_rank AS spot_rank, sp.spot_daypart, sp.spot_length_sec AS spot_len,
      sp.guaranteed_rating, sp.owed_impressions_per_spot, sp.flight_end, sp.makegood_eligible,
      sp.competitive_category, sp.window_days, sp.audience_segment_id, cfg.*,
      a.segment_ratings[sp.audience_segment_id] AS seg_rating,
      cfg.floor_pct * sp.guaranteed_rating AS floor_rating
    FROM sp CROSS JOIN cfg
    JOIN gold_available_inventory a
      ON a.station_id = sp.station_id
     AND a.air_date > sp.broadcast_date
     AND a.air_date <= date_add(sp.broadcast_date, cfg.horizon)
  ),
  m AS (  -- how far each soft rule is missed
    SELECT ev.*,
      greatest(0.0, (floor_rating - seg_rating) / floor_rating) AS rating_short,
      greatest(0, spot_rank - daypart_rank) AS dp_steps,
      greatest(0, datediff(air_date, date_add(broadcast_date, window_days))) AS days_late,
      greatest(0, datediff(air_date, flight_end)) AS days_after_flight
    FROM ev
  ),
  rules AS (
    SELECT m.*, filter(array(
        CASE WHEN NOT makegood_eligible THEN 'contract_not_makegood_eligible' END,
        CASE WHEN daypart_rank < spot_rank THEN 'daypart_lower_value' END,
        CASE WHEN seg_rating < floor_rating THEN 'rating_below_floor' END,
        CASE WHEN spot_length_sec <> spot_len THEN 'spot_length_mismatch' END,
        CASE WHEN days_late > 0 THEN 'outside_makegood_window' END,
        CASE WHEN days_after_flight > 0 THEN 'after_flight_end' END,
        CASE WHEN air_date <= as_of THEN 'already_aired' END,
        CASE WHEN avail_status <> 'Open' THEN 'avail_held' END,
        CASE WHEN booked_for_spot_id IS NOT NULL AND booked_for_spot_id <> p_spot THEN 'already_booked' END
      ), x -> x IS NOT NULL) AS failed,
      (NOT makegood_eligible) OR air_date <= as_of OR (booked_for_spot_id IS NOT NULL AND booked_for_spot_id <> p_spot) AS hard,
      CASE WHEN rating_short > 0 THEN pen_rating_base + pen_rating_per_pct * rating_short * 100 ELSE 0 END
        + pen_daypart_step * dp_steps
        + CASE WHEN spot_length_sec < spot_len THEN pen_length_shorter WHEN spot_length_sec > spot_len THEN pen_length_longer ELSE 0 END
        + greatest(  -- one timing problem, charged once: the larger of window and flight lateness
            CASE WHEN days_late > 0 THEN pen_window_base + pen_window_per_day * days_late ELSE 0 END,
            CASE WHEN days_after_flight > 0 THEN pen_flight_base + pen_flight_per_day * days_after_flight ELSE 0 END)
        + CASE WHEN avail_status <> 'Open' THEN pen_avail_held ELSE 0 END AS penalty,
      filter(array(
        CASE WHEN rating_short > 0 THEN concat('rating ', format_number(seg_rating, 2), ' vs ', format_number(floor_rating, 2),
          ' floor (-', format_number(rating_short * 100, 1), '%)') END,
        CASE WHEN dp_steps > 0 THEN concat(daypart, ' is ', dp_steps, ' daypart step', CASE WHEN dp_steps > 1 THEN 's' ELSE '' END, ' below ', spot_daypart) END,
        CASE WHEN spot_length_sec <> spot_len THEN concat(':', spot_length_sec, ' avail for a :', spot_len, ' spot') END,
        CASE WHEN days_late > 0 THEN concat(days_late, ' day', CASE WHEN days_late > 1 THEN 's' ELSE '' END, ' past the make-good window') END,
        CASE WHEN days_after_flight > 0 THEN concat(days_after_flight, ' day', CASE WHEN days_after_flight > 1 THEN 's' ELSE '' END, ' after flight end') END,
        CASE WHEN avail_status <> 'Open' THEN 'avail on hold (traffic must release it)' END
      ), x -> x IS NOT NULL) AS soft_misses
    FROM m
  )
  SELECT avail_id, air_date, daypart, program_name, program_genre, adjacency_tags, spot_length_sec,
    avail_status, booked_for_spot_id, rate_card_usd, seg_rating,
    CAST(segment_impressions[audience_segment_id] AS BIGINT),
    owed_impressions_per_spot,
    round(seg_rating / guaranteed_rating, 3),
    array_contains(competitive_categories_blocked, competitive_category),
    size(failed) = 0,
    failed,
    round(floor_rating, 2),
    hard,
    round(penalty, 2),
    soft_misses
  FROM rules;

CREATE OR REPLACE FUNCTION is_commensurate(
  p_spot_id STRING COMMENT 'Preempted or missed spot id',
  p_avail_id STRING COMMENT 'Candidate replacement avail id',
  p_as_of DATE DEFAULT NULL COMMENT 'Decision date (NULL = simulation clock)'
)
RETURNS STRUCT<is_commensurate: BOOLEAN, failed_rules: ARRAY<STRING>>
COMMENT 'TRUE when the avail satisfies every must-match make-good rule for the spot; otherwise lists the failed rules. Avails on a different station fail "different_station".'
RETURN coalesce(
  (SELECT first(named_struct('is_commensurate', e.is_commensurate, 'failed_rules', e.failed_rules))
   FROM evaluate_inventory(p_spot_id, p_as_of) e WHERE e.avail_id = p_avail_id),
  named_struct('is_commensurate', false, 'failed_rules', array('different_station_or_outside_horizon')));

CREATE OR REPLACE FUNCTION spot_context(
  p_spot_id STRING COMMENT 'Preempted or missed spot id'
)
RETURNS TABLE (
  spot_id STRING, order_id STRING, advertiser_id STRING, advertiser_name STRING, advertiser_category STRING,
  station_id STRING, call_letters STRING, market STRING, network_affiliation STRING,
  scheduled_ts TIMESTAMP, broadcast_date DATE, daypart STRING, program_name STRING, program_genre STRING,
  spot_length_sec INT, status STRING, preemption_reason STRING, unit_rate_usd DOUBLE,
  campaign_name STRING, audience_segment_text STRING, guaranteed_rating DOUBLE, owed_impressions BIGINT,
  makegood_window_days INT, flight_end DATE, makegood_deadline DATE, makegood_eligible BOOLEAN,
  latest_offer_status STRING, seller_id STRING, seller_name STRING, seller_email STRING, manager_id STRING,
  contact_name STRING, contact_email STRING
)
COMMENT 'Everything the agent needs to know about a preempted/missed spot: schedule, program, contract (free-text audience target, guaranteed rating, make-good window, flight), advertiser and station, the latest make-good offer status, the seller who owns the account and the client contact. Returns nothing for a spot the caller may not see.'
RETURN
  SELECT s.spot_id, s.order_id, c.advertiser_id, a.advertiser_name, a.category,
    s.station_id, st.call_letters, st.market, st.network_affiliation,
    s.scheduled_ts, s.broadcast_date, s.daypart, s.program_name, s.program_genre,
    s.spot_length_sec, s.status, s.preemption_reason, s.unit_rate_usd,
    c.campaign_name, c.audience_segment_text, c.guaranteed_rating, c.owed_impressions_per_spot,
    c.makegood_window_days, c.flight_end,
    least(date_add(s.broadcast_date, c.makegood_window_days), c.flight_end),
    c.makegood_eligible,
    coalesce((SELECT max_by(h.offer_status, h.offered_ts) FROM gold_make_good_outcomes h WHERE h.spot_id = p_spot_id), 'No offer'),
    c.seller_id, sl.seller_name, sl.seller_email, sl.manager_id, ac.contact_name, ac.contact_email
  FROM silver_ad_schedule_clean s
  JOIN gold_spot_access g ON g.spot_id = s.spot_id  -- only spots of clients the caller may see
  JOIN silver_contract_obligations c ON c.order_id = s.order_id
  JOIN ref_advertisers a ON a.advertiser_id = c.advertiser_id
  JOIN ref_stations st ON st.station_id = s.station_id
  LEFT JOIN ref_sellers sl ON sl.seller_id = c.seller_id
  LEFT JOIN ref_advertiser_contacts ac ON ac.advertiser_id = c.advertiser_id
  WHERE s.spot_id = p_spot_id;

CREATE OR REPLACE FUNCTION audience_mapper(
  p_advertiser_id STRING COMMENT 'Advertiser id, used for the fallback to the advertiser''s usual segment',
  p_audience_text STRING COMMENT 'Contracted audience as written on the order, e.g. "Adults 25 to 54"'
)
RETURNS TABLE (audience_segment_id STRING, segment_code STRING, segment_name STRING, match_type STRING, confidence DOUBLE)
COMMENT 'Translates a contracted audience description into the canonical audience_segment_id: exact published alias (confidence 1.0) -> normalized text such as "women 25 to 54" (0.9) -> the advertiser''s most common resolved segment (0.6) -> unmapped (0.0).'
RETURN
  WITH r AS (
    SELECT
      (SELECT max(audience_segment_id) FROM silver_audience_mapped WHERE alias_upper = upper(trim(p_audience_text))) AS seg_exact,
      (SELECT max(audience_segment_id) FROM silver_audience_mapped WHERE segment_code = normalize_audience_key(p_audience_text)) AS seg_norm,
      (SELECT max_by(audience_segment_id, n) FROM (
         SELECT audience_segment_id, count(*) AS n FROM silver_contract_obligations
         WHERE advertiser_id = p_advertiser_id AND audience_match_type IN ('exact_alias', 'normalized')
         GROUP BY audience_segment_id)) AS seg_default
  ),
  pick AS (
    SELECT coalesce(seg_exact, seg_norm, seg_default) AS seg_id,
      CASE WHEN seg_exact IS NOT NULL THEN 'exact_alias' WHEN seg_norm IS NOT NULL THEN 'normalized'
           WHEN seg_default IS NOT NULL THEN 'advertiser_default' ELSE 'unmapped' END AS match_type,
      CASE WHEN seg_exact IS NOT NULL THEN 1.0 WHEN seg_norm IS NOT NULL THEN 0.9
           WHEN seg_default IS NOT NULL THEN 0.6 ELSE 0.0 END AS confidence
    FROM r
  )
  SELECT pick.seg_id, m.segment_code, m.segment_name, pick.match_type, CAST(pick.confidence AS DOUBLE)
  FROM pick
  LEFT JOIN (SELECT DISTINCT audience_segment_id, segment_code, segment_name FROM silver_audience_mapped) m
    ON m.audience_segment_id = pick.seg_id;

CREATE OR REPLACE FUNCTION inventory_search(
  p_spot_id STRING COMMENT 'Preempted or missed spot id',
  p_max_results INT DEFAULT 25 COMMENT 'Maximum candidates to return (commensurate first, then closest near-misses)',
  p_exclude_avail_ids ARRAY<STRING> DEFAULT NULL COMMENT 'Avails to skip (already proposed, rejected, or booked in this session)',
  p_exclude_tags ARRAY<STRING> DEFAULT NULL COMMENT 'Adjacency tags to avoid, e.g. array(''news'')',
  p_only_dayparts ARRAY<STRING> DEFAULT NULL COMMENT 'Restrict to these dayparts, e.g. array(''Prime'')',
  p_as_of DATE DEFAULT NULL COMMENT 'Decision date (NULL = simulation clock)',
  p_include_near_misses BOOLEAN DEFAULT FALSE COMMENT 'Also return every possible non-commensurate avail (soft rule misses only; never already aired, booked or ineligible), closest first',
  p_exclude_programs ARRAY<STRING> DEFAULT NULL COMMENT 'Client rule: programs never to offer, e.g. array("Judge Judy") (case-insensitive)',
  p_exclude_genres ARRAY<STRING> DEFAULT NULL COMMENT 'Client rule: program genres never to offer, e.g. array("Court")',
  p_only_dows ARRAY<STRING> DEFAULT NULL COMMENT 'Client rule: only these days of the week, e.g. array("Tue", "Thu")',
  p_avoid_dows ARRAY<STRING> DEFAULT NULL COMMENT 'Client rule: never these days of the week, e.g. array("Sat", "Sun")',
  p_horizon_days INT DEFAULT NULL COMMENT 'Days after the spot to search. NULL = mg_rules_config.search_horizon_days'
)
RETURNS TABLE (
  avail_id STRING, air_date DATE, daypart STRING, program_name STRING, program_genre STRING,
  adjacency_tags ARRAY<STRING>, spot_length_sec INT, rate_card_usd DOUBLE, segment_rating DOUBLE,
  est_impressions BIGINT, owed_impressions BIGINT, impression_equivalence DOUBLE, competitive_conflict BOOLEAN,
  is_commensurate BOOLEAN, rule_penalty DOUBLE, misses ARRAY<STRING>
)
COMMENT 'Returns the avails that pass every must-match commensurate rule and, optionally, every other possible avail (soft rule misses only), filtered by reviewer constraints (exclude avails / adjacency tags, restrict dayparts) and the client’s own rules (programs, genres and days of the week to avoid or require). Commensurate first (earliest first), then the rest by smallest rule penalty.'
RETURN
  SELECT avail_id, air_date, daypart, program_name, program_genre, adjacency_tags, spot_length_sec,
    rate_card_usd, segment_rating, est_impressions, owed_impressions, impression_equivalence, competitive_conflict,
    is_commensurate, rule_penalty, misses
  FROM (
    SELECT e.*, ROW_NUMBER() OVER (ORDER BY CASE WHEN e.is_commensurate THEN 0 ELSE 1 END, e.rule_penalty, e.air_date, e.avail_id) AS rn
    FROM evaluate_inventory(p_spot_id, p_as_of, p_horizon_days) e
    WHERE (e.is_commensurate OR (coalesce(p_include_near_misses, false) AND NOT e.hard_fail))
      AND (p_exclude_avail_ids IS NULL OR NOT array_contains(p_exclude_avail_ids, e.avail_id))
      AND (p_exclude_tags IS NULL OR size(array_intersect(e.adjacency_tags, p_exclude_tags)) = 0)
      AND (p_only_dayparts IS NULL OR size(p_only_dayparts) = 0 OR array_contains(p_only_dayparts, e.daypart))
      AND (p_exclude_programs IS NULL OR NOT exists(p_exclude_programs, x -> lower(trim(x)) = lower(e.program_name)))
      AND (p_exclude_genres IS NULL OR NOT exists(p_exclude_genres, x -> lower(trim(x)) = lower(e.program_genre)))
      AND (p_only_dows IS NULL OR size(p_only_dows) = 0 OR array_contains(p_only_dows, date_format(e.air_date, 'EEE')))
      AND (p_avoid_dows IS NULL OR NOT array_contains(p_avoid_dows, date_format(e.air_date, 'EEE')))
  )
  WHERE rn <= p_max_results;

CREATE OR REPLACE FUNCTION inventory_rule_summary(
  p_spot_id STRING COMMENT 'Preempted or missed spot id',
  p_as_of DATE DEFAULT NULL COMMENT 'Decision date (NULL = simulation clock)'
)
RETURNS TABLE (rule STRING, avails BIGINT)
COMMENT 'Explains the search: how many avails were evaluated, how many are commensurate, and how many failed each must-match rule (an avail can fail several).'
RETURN
  WITH s AS (
    SELECT count(*) AS evaluated, count_if(is_commensurate) AS commensurate,
      flatten(collect_list(failed_rules)) AS failures
    FROM evaluate_inventory(p_spot_id, p_as_of)
  )
  SELECT rule, CAST(avails AS BIGINT) FROM (
    SELECT inline(array(named_struct('rule', 'evaluated', 'avails', evaluated),
                        named_struct('rule', 'commensurate', 'avails', commensurate))) FROM s
    UNION ALL
    SELECT r AS rule, count(*) AS avails FROM s LATERAL VIEW explode(failures) t AS r GROUP BY r
  );

CREATE OR REPLACE FUNCTION commensurate_scorer(
  p_spot_id STRING COMMENT 'Preempted or missed spot id',
  p_avail_ids ARRAY<STRING> COMMENT 'Candidate avail ids to score (usually the inventory_search results)',
  p_prefs MAP<STRING, DOUBLE> DEFAULT NULL COMMENT 'Advertiser adjacency preferences, tag -> weight in [-1, 1], from preference memory',
  p_as_of DATE DEFAULT NULL COMMENT 'Decision date (NULL = simulation clock)',
  p_prefer_dows ARRAY<STRING> DEFAULT NULL COMMENT 'Client rule: preferred days of the week, e.g. array("Tue", "Thu")',
  p_horizon_days INT DEFAULT NULL COMMENT 'Days after the spot to search (must match the inventory_search call)'
)
RETURNS TABLE (
  rank INT, avail_id STRING, air_date DATE, daypart STRING, program_name STRING, program_genre STRING,
  adjacency_tags ARRAY<STRING>, est_impressions BIGINT, impression_equivalence DOUBLE, score DOUBLE,
  score_breakdown STRUCT<impressions: DOUBLE, genre: DOUBLE, preference: DOUBLE, yield: DOUBLE, timeliness: DOUBLE, competitive: DOUBLE, day_pref: DOUBLE>,
  reasons ARRAY<STRING>, is_commensurate BOOLEAN, failed_rules ARRAY<STRING>,
  match_score INT, tier STRING, misses ARRAY<STRING>,
  rate_card_usd DOUBLE, competitive_conflict BOOLEAN, hard_fail BOOLEAN, spot_length_sec INT
)
COMMENT 'Ranks candidate avails with a tiered match score out of 10, rounded to a whole number (10 = perfect): 10 minus graded penalties for every rule an avail misses, minus up to fit_penalty_max for a weak soft-match fit (impression equivalence, genre continuity, advertiser adjacency preference, yield, timeliness, competitive separation, preferred days of the week). Tiers: commensurate (passes every rule) ranks first; near_miss (soft misses only, score >= near_miss_min_score) next; poor_fit (below it: shown, but a refund is recommended); not_possible (already aired / booked / ineligible) last. Ordered by the unrounded score within a tier. Weights and penalties from mg_rules_config.'
RETURN
  WITH sp AS (
    SELECT s.program_genre, s.unit_rate_usd, s.broadcast_date, c.makegood_window_days AS window_days, c.competitive_category
    FROM silver_ad_schedule_clean s JOIN silver_contract_obligations c ON c.order_id = s.order_id
    JOIN gold_spot_access g ON g.spot_id = s.spot_id  -- only spots of clients the caller may see
    WHERE s.spot_id = p_spot_id
  ),
  w AS (
    SELECT
      max(CASE WHEN rule_key = 'ie_cap' THEN num_value END) AS ie_cap,
      max(CASE WHEN rule_key = 'w_impressions' THEN num_value END) AS w_imp,
      max(CASE WHEN rule_key = 'w_genre' THEN num_value END) AS w_genre,
      max(CASE WHEN rule_key = 'w_preference' THEN num_value END) AS w_pref,
      max(CASE WHEN rule_key = 'w_yield' THEN num_value END) AS w_yield,
      max(CASE WHEN rule_key = 'w_timeliness' THEN num_value END) AS w_time,
      max(CASE WHEN rule_key = 'w_competitive' THEN num_value END) AS w_comp,
      max(CASE WHEN rule_key = 'fit_penalty_max' THEN num_value END) AS fit_pen,
      max(CASE WHEN rule_key = 'near_miss_min_score' THEN num_value END) AS nm_min,
      coalesce(max(CASE WHEN rule_key = 'w_day_pref' THEN num_value END), 0) AS w_dow
    FROM mg_rules_config
  ),
  f AS (
    SELECT e.*, sp.competitive_category, sp.unit_rate_usd, w.*,
      least(e.impression_equivalence, w.ie_cap) / w.ie_cap AS f_imp,
      CASE WHEN e.program_genre = sp.program_genre THEN 1.0 ELSE 0.0 END AS f_genre,
      greatest(-1.0, least(1.0, aggregate(e.adjacency_tags, CAST(0 AS DOUBLE), (acc, t) -> acc + coalesce(p_prefs[t], CAST(0 AS DOUBLE))))) AS f_pref,
      greatest(0.0, 1 - abs(e.rate_card_usd / sp.unit_rate_usd - 1)) AS f_yield,
      greatest(0.0, 1 - datediff(e.air_date, sp.broadcast_date) / sp.window_days) AS f_time,
      CASE WHEN e.competitive_conflict THEN 1.0 ELSE 0.0 END AS f_comp,
      CASE WHEN array_contains(p_prefer_dows, date_format(e.air_date, 'EEE')) THEN 1.0 ELSE 0.0 END AS f_dow,
      w.w_imp + w.w_genre + w.w_yield + w.w_time + CASE WHEN p_prefs IS NULL OR size(p_prefs) = 0 THEN 0 ELSE w.w_pref END
        + CASE WHEN p_prefer_dows IS NULL OR size(p_prefer_dows) = 0 THEN 0 ELSE w.w_dow END AS max_fit
    FROM evaluate_inventory(p_spot_id, p_as_of, p_horizon_days) e CROSS JOIN sp CROSS JOIN w
    WHERE array_contains(p_avail_ids, e.avail_id)
  ),
  s AS (
    SELECT f.*,
      round(w_imp * f_imp, 4) AS c_imp, round(w_genre * f_genre, 4) AS c_genre, round(w_pref * f_pref, 4) AS c_pref,
      round(w_yield * f_yield, 4) AS c_yield, round(w_time * f_time, 4) AS c_time, round(-w_comp * f_comp, 4) AS c_comp,
      round(w_dow * f_dow, 4) AS c_dow
    FROM f
  ),
  t AS (
    SELECT s.*,
      c_imp + c_genre + c_pref + c_yield + c_time + c_comp + c_dow AS soft,
      greatest(0.0, least(10.0, 10 - rule_penalty
        - fit_pen * (1 - greatest(0.0, least(1.0, (c_imp + c_genre + c_pref + c_yield + c_time + c_comp + c_dow) / max_fit))))) AS m_score
    FROM s
  ),
  u AS (
    SELECT t.*,
      CASE WHEN is_commensurate THEN 'commensurate' WHEN hard_fail THEN 'not_possible'
           WHEN round(m_score) >= nm_min THEN 'near_miss' ELSE 'poor_fit' END AS tier_
    FROM t
  )
  SELECT
    CAST(ROW_NUMBER() OVER (ORDER BY CASE tier_ WHEN 'commensurate' THEN 0 WHEN 'near_miss' THEN 1 WHEN 'poor_fit' THEN 2 ELSE 3 END,
                            m_score DESC, soft DESC, air_date, avail_id) AS INT),
    avail_id, air_date, daypart, program_name, program_genre, adjacency_tags, est_impressions, impression_equivalence,
    round(soft, 4),
    named_struct('impressions', c_imp, 'genre', c_genre, 'preference', c_pref, 'yield', c_yield, 'timeliness', c_time, 'competitive', c_comp, 'day_pref', c_dow),
    filter(array(
      CASE WHEN tier_ = 'near_miss' THEN concat('NEAR MISS: ', array_join(misses, '; ')) END,
      CASE WHEN tier_ = 'not_possible' THEN concat('NOT POSSIBLE: ', array_join(failed_rules, ', ')) END,
      CASE WHEN tier_ = 'poor_fit' THEN concat('LOW MATCH: ', array_join(misses, '; ')) END,
      concat('delivers ', CAST(round(impression_equivalence * 100) AS INT), '% of owed impressions'),
      CASE WHEN f_genre = 1 THEN concat('same genre as the preempted program (', program_genre, ')') END,
      CASE WHEN f_pref > 0 THEN concat('fits advertiser preference: ', array_join(filter(adjacency_tags, t -> p_prefs[t] > 0), ', ')) END,
      CASE WHEN f_pref < 0 THEN concat('advertiser tends to reject: ', array_join(filter(adjacency_tags, t -> p_prefs[t] < 0), ', ')) END,
      CASE WHEN rate_card_usd > 1.5 * unit_rate_usd THEN concat('gives away higher-value inventory (rate card $', CAST(round(rate_card_usd) AS INT), ' vs $', CAST(round(unit_rate_usd) AS INT), ' sold)') END,
      CASE WHEN f_comp = 1 THEN concat('competitor (', competitive_category, ') already in this pod') END,
      CASE WHEN f_dow = 1 THEN concat('airs on a day the client prefers (', date_format(air_date, 'EEE'), ')') END
    ), x -> x IS NOT NULL),
    is_commensurate, failed_rules,
    CAST(round(m_score) AS INT), tier_, misses,
    rate_card_usd, competitive_conflict, hard_fail, spot_length_sec
  FROM u;

-- The one place the dollar rule lives: how a make-good's value compares with the value of the spot it replaces.
CREATE OR REPLACE FUNCTION value_reconciliation(
  p_owed_usd DOUBLE COMMENT 'Value of the missed spot (its unit rate)',
  p_proposed_usd DOUBLE COMMENT 'Rate-card value of the replacement avails',
  p_owed_impressions BIGINT COMMENT 'Impressions owed for the missed spot in the contracted audience',
  p_proposed_impressions BIGINT COMMENT 'Impressions the replacement avails deliver in that audience'
)
RETURNS STRUCT<basis: STRING, owed_value_usd: DOUBLE, proposed_value_usd: DOUBLE, cpm_equivalent_usd: DOUBLE,
               compared_value_usd: DOUBLE, value_gap_usd: DOUBLE, value_ratio: DOUBLE, dollar_match: BOOLEAN,
               partial_refund_usd: DOUBLE, label: STRING>
COMMENT 'Dollar match for a make-good. Compares the replacement value (rate card, or delivered impressions priced at the contract CPM, per mg_rules_config.dollar_basis) with the missed spot’s unit rate. value_gap_usd > 0 means the make-good is worth less (the gap can be refunded); < 0 means it gives away extra value. dollar_match is TRUE within dollar_tolerance_under_pct / dollar_tolerance_over_pct.'
RETURN (
  WITH cfg AS (
    SELECT coalesce(max(CASE WHEN rule_key = 'dollar_basis' THEN str_value END), 'rate_card') AS basis,
           coalesce(max(CASE WHEN rule_key = 'dollar_tolerance_under_pct' THEN num_value END), 0.05) AS under_pct,
           coalesce(max(CASE WHEN rule_key = 'dollar_tolerance_over_pct' THEN num_value END), 0.50) AS over_pct
    FROM mg_rules_config
  ),
  v AS (
    SELECT cfg.*, round(p_owed_usd, 2) AS owed, round(coalesce(p_proposed_usd, 0), 2) AS proposed,
      round(p_owed_usd * coalesce(p_proposed_impressions, 0) / nullif(p_owed_impressions, 0), 2) AS cpm_equiv
    FROM cfg
  ),
  c AS (SELECT v.*, CASE WHEN basis = 'cpm_equivalent' THEN cpm_equiv ELSE proposed END AS compared FROM v)
  SELECT named_struct(
    'basis', basis, 'owed_value_usd', owed, 'proposed_value_usd', proposed, 'cpm_equivalent_usd', cpm_equiv,
    'compared_value_usd', compared, 'value_gap_usd', round(owed - compared, 2),
    'value_ratio', round(compared / nullif(owed, 0), 3),
    'dollar_match', compared >= owed * (1 - under_pct) AND compared <= owed * (1 + over_pct),
    'partial_refund_usd', CASE WHEN compared < owed * (1 - under_pct) THEN round(owed - compared, 2) ELSE 0.0 END,
    'label', CASE
      WHEN compared < owed * (1 - under_pct) THEN concat('worth $', format_number(owed - compared, 0), ' less than the missed spot’s $', format_number(owed, 0))
      WHEN compared > owed * (1 + over_pct) THEN concat('gives away $', format_number(compared - owed, 0), ' more than the missed spot’s $', format_number(owed, 0))
      WHEN abs(compared - owed) <= owed * 0.05 THEN concat('matches the missed spot’s $', format_number(owed, 0))
      WHEN compared > owed THEN concat('$', format_number(compared - owed, 0), ' more than the missed spot’s $', format_number(owed, 0),
                                       ' (', format_number(compared / nullif(owed, 0) * 100, 0), '%), within the allowed range')
      ELSE concat('$', format_number(owed - compared, 0), ' less than the missed spot’s $', format_number(owed, 0), ', within the allowed range') END)
  FROM c
);

CREATE OR REPLACE FUNCTION shortfall_calculator(
  p_spot_id STRING COMMENT 'Preempted or missed spot id',
  p_avail_ids ARRAY<STRING> COMMENT 'Proposed make-good avail ids'
)
RETURNS TABLE (
  spot_id STRING, order_id STRING, segment_code STRING, owed_impressions BIGINT, proposed_impressions BIGINT,
  spot_gap_impressions BIGINT, spot_coverage_pct DOUBLE, revenue_at_risk_usd DOUBLE,
  order_other_unresolved_spots BIGINT, order_owed_impressions_after BIGINT, order_pacing_index DOUBLE,
  owed_value_usd DOUBLE, proposed_value_usd DOUBLE, value_gap_usd DOUBLE, value_ratio DOUBLE, dollar_match BOOLEAN,
  partial_refund_usd DOUBLE, dollar_basis STRING, dollar_label STRING
)
COMMENT 'Impression and dollar math for a proposed make-good: impressions owed for the spot in the contracted segment vs impressions the proposed avails deliver, the remaining gap, the value of the proposal vs the missed spot (value_reconciliation), and the order-level picture (other unresolved preemptions, owed impressions after this make-good, pacing index).'
RETURN
  WITH sp AS (
    SELECT s.spot_id, s.order_id, s.unit_rate_usd, c.audience_segment_id, c.segment_code, c.owed_impressions_per_spot
    FROM silver_ad_schedule_clean s JOIN silver_contract_obligations c ON c.order_id = s.order_id
    JOIN gold_spot_access g ON g.spot_id = s.spot_id  -- only spots of clients the caller may see
    WHERE s.spot_id = p_spot_id
  ),
  prop AS (
    SELECT coalesce(sum(a.segment_impressions[sp.audience_segment_id]), 0) AS proposed,
           coalesce(sum(a.rate_card_usd), 0) AS proposed_usd
    FROM gold_available_inventory a CROSS JOIN sp
    WHERE array_contains(p_avail_ids, a.avail_id)
  ),
  r AS (
    SELECT sp.*, prop.*,
      value_reconciliation(sp.unit_rate_usd, prop.proposed_usd, sp.owed_impressions_per_spot, CAST(prop.proposed AS BIGINT)) AS v
    FROM sp CROSS JOIN prop
  )
  SELECT r.spot_id, r.order_id, r.segment_code, r.owed_impressions_per_spot, CAST(r.proposed AS BIGINT),
    CAST(greatest(r.owed_impressions_per_spot - r.proposed, 0) AS BIGINT),
    round(r.proposed / r.owed_impressions_per_spot, 3),
    r.unit_rate_usd,
    greatest(g.unresolved_spots - 1, 0),
    CAST(greatest(g.unresolved_spots - 1, 0) * r.owed_impressions_per_spot
         + greatest(r.owed_impressions_per_spot - r.proposed, 0) AS BIGINT),
    g.pacing_index,
    r.v.owed_value_usd, r.v.proposed_value_usd, r.v.value_gap_usd, r.v.value_ratio, r.v.dollar_match,
    r.v.partial_refund_usd, r.v.basis, r.v.label
  FROM r
  LEFT JOIN gold_impression_shortfall g ON g.order_id = r.order_id;

CREATE OR REPLACE FUNCTION recommendation_formatter(
  p_spot_id STRING COMMENT 'Preempted or missed spot id',
  p_avail_ids ARRAY<STRING> COMMENT 'Ranked candidate avail ids',
  p_prefs MAP<STRING, DOUBLE> DEFAULT NULL COMMENT 'Advertiser adjacency preferences used for scoring',
  p_as_of DATE DEFAULT NULL COMMENT 'Decision date (NULL = simulation clock)'
)
RETURNS TABLE (markdown STRING)
COMMENT 'Formats the ranked make-good options as one compact markdown table for the ad-ops reviewer: match score out of 10, tier (commensurate / near-miss), impressions and reasons, so the LLM never rewrites tables (saves tokens and prevents transcription errors).'
RETURN
  SELECT concat(
    '| # | Match | Tier | Avail | Air date | Daypart | Program | Est. impressions | % of owed | Why |\n',
    '|---|---|---|---|---|---|---|---|---|---|\n',
    array_join(transform(array_sort(collect_list(struct(rank, line))), x -> x.line), '\n'))
  FROM (
    SELECT rank, concat('| ', rank, ' | ', match_score, '/10 | ',
      CASE tier WHEN 'commensurate' THEN 'commensurate' WHEN 'near_miss' THEN 'near-miss' WHEN 'poor_fit' THEN 'low match' ELSE tier END, ' | ',
      avail_id, ' | ', date_format(air_date, 'EEE MMM d'), ' | ', daypart, ' | ',
      program_name, ' | ', format_number(est_impressions, 0), ' | ', CAST(round(impression_equivalence * 100) AS INT), '% | ',
      array_join(reasons, '; '), ' |') AS line
    FROM commensurate_scorer(p_spot_id, p_avail_ids, p_prefs, p_as_of)
    WHERE tier IN ('commensurate', 'near_miss', 'poor_fit')
  );
