-- Make-good packages (run AFTER 10_agent_tools.sql): combine up to package_max_lines avails into options A/B/C,
-- each with a rule-by-rule checklist and its dollar reconciliation; fill the gap in a seller's own selection;
-- look up program names for client rules. Deterministic SQL: the LLM never assembles or checks a package.
USE CATALOG YOUR_CATALOG;
USE SCHEMA YOUR_SCHEMA;

CREATE OR REPLACE FUNCTION package_builder(
  p_spot_id STRING COMMENT 'Preempted or missed spot id',
  p_avail_ids ARRAY<STRING> COMMENT 'Ranked candidate avail ids (the inventory_search results)',
  p_prefs MAP<STRING, DOUBLE> DEFAULT NULL COMMENT 'Advertiser adjacency preferences used for scoring',
  p_as_of DATE DEFAULT NULL COMMENT 'Decision date (NULL = simulation clock)',
  p_prefer_dows ARRAY<STRING> DEFAULT NULL COMMENT 'Client rule: preferred days of the week',
  p_horizon_days INT DEFAULT NULL COMMENT 'Days after the spot that were searched',
  p_fixed_avail_ids ARRAY<STRING> DEFAULT NULL COMMENT 'The seller’s own selection: returned as package S instead of A/B/C',
  p_client_rules ARRAY<STRING> DEFAULT NULL COMMENT 'Client rules applied to the search, listed on the checklist'
)
RETURNS TABLE (
  package_label STRING, strategy STRING, avail_ids ARRAY<STRING>,
  lines ARRAY<STRUCT<avail_id: STRING, rank: INT, air_date: DATE, daypart: STRING, program_name: STRING, program_genre: STRING,
                     adjacency_tags: ARRAY<STRING>, rate_card_usd: DOUBLE, est_impressions: BIGINT, match_score: INT, tier: STRING,
                     failed_rules: ARRAY<STRING>, misses: ARRAY<STRING>, competitive_conflict: BOOLEAN>>,
  n_lines INT, est_impressions BIGINT, owed_impressions BIGINT, coverage_pct DOUBLE,
  owed_value_usd DOUBLE, proposed_value_usd DOUBLE, compared_value_usd DOUBLE, value_gap_usd DOUBLE, value_ratio DOUBLE,
  dollar_match BOOLEAN, partial_refund_usd DOUBLE, dollar_label STRING,
  checklist ARRAY<STRUCT<rule: STRING, label: STRING, passed: BOOLEAN, detail: STRING>>,
  rules_passed INT, rules_total INT, package_score DOUBLE, is_full BOOLEAN
)
COMMENT 'Builds make-good options from the ranked candidates: singles, pairs and triples of the top package_candidate_pool avails (no avail that has already aired, is booked or is ineligible). Each package carries totals (impressions, rate-card value), its dollar reconciliation against the missed spot (value_reconciliation) and an 11-rule checklist: station, daypart, rating floor, length, make-good window, flight, open and unbooked, competitive separation, client rules, impressions covered, value matched. A = a 10/10 package if there is one (preferring the top-ranked avail), else the best package starting with the top-ranked avail; B = best multi-spot package (when it is 10/10 or A falls short); C = best alternative on a different daypart or week. is_full (10/10) = every checklist rule passes. With p_fixed_avail_ids, returns the seller’s selection as package S.'
RETURN
  WITH cfg AS (
    SELECT coalesce(CAST(max(CASE WHEN rule_key = 'package_max_lines' THEN num_value END) AS INT), 3) AS max_lines,
           coalesce(CAST(max(CASE WHEN rule_key = 'package_candidate_pool' THEN num_value END) AS INT), 12) AS pool_size,
           coalesce(max(CASE WHEN rule_key = 'package_line_penalty' THEN num_value END), 0.5) AS line_pen
    FROM mg_rules_config
  ),
  sp AS (
    SELECT s.unit_rate_usd, c.owed_impressions_per_spot AS owed_imps
    FROM silver_ad_schedule_clean s JOIN silver_contract_obligations c ON c.order_id = s.order_id
    JOIN gold_spot_access g ON g.spot_id = s.spot_id  -- only spots of clients the caller may see
    WHERE s.spot_id = p_spot_id
  ),
  cand AS (
    SELECT rank, named_struct('avail_id', avail_id, 'rank', rank, 'air_date', air_date, 'daypart', daypart,
             'program_name', program_name, 'program_genre', program_genre, 'adjacency_tags', adjacency_tags,
             'rate_card_usd', rate_card_usd, 'est_impressions', est_impressions, 'match_score', match_score, 'tier', tier,
             'failed_rules', failed_rules, 'misses', misses, 'competitive_conflict', competitive_conflict) AS l,
      coalesce(array_contains(p_fixed_avail_ids, avail_id), false) AS is_fixed
    FROM commensurate_scorer(p_spot_id, p_avail_ids, p_prefs, p_as_of, p_prefer_dows, p_horizon_days)
    WHERE tier <> 'not_possible'
  ),
  -- score once; combinations are built from one array (every reference to the scorer would re-run it)
  agg AS (
    SELECT array_sort(collect_list(CASE WHEN rank <= cfg.pool_size THEN l END),
                      (x, y) -> CASE WHEN x.rank < y.rank THEN -1 WHEN x.rank > y.rank THEN 1 ELSE 0 END) AS arr,
           array_sort(collect_list(CASE WHEN is_fixed THEN l END),
                      (x, y) -> CASE WHEN x.rank < y.rank THEN -1 WHEN x.rank > y.rank THEN 1 ELSE 0 END) AS fixed,
           max(cfg.max_lines) AS max_lines
    FROM cand CROSS JOIN cfg
  ),
  combos AS (
    SELECT explode(CASE
      WHEN p_fixed_avail_ids IS NOT NULL THEN CASE WHEN size(fixed) > 0 THEN array(fixed) ELSE array() END
      ELSE concat(
        transform(arr, x -> array(x)),
        CASE WHEN size(arr) >= 2 AND max_lines >= 2 THEN
          flatten(transform(sequence(0, size(arr) - 2), i -> transform(sequence(i + 1, size(arr) - 1), j -> array(arr[i], arr[j]))))
          ELSE array() END,
        CASE WHEN size(arr) >= 3 AND max_lines >= 3 THEN
          flatten(flatten(transform(sequence(0, size(arr) - 3), i -> transform(sequence(i + 1, size(arr) - 2), j ->
            transform(sequence(j + 1, size(arr) - 1), k -> array(arr[i], arr[j], arr[k]))))))
          ELSE array() END)
      END) AS lines
    FROM agg
  ),
  m AS (
    SELECT lines, size(lines) AS n_lines, transform(lines, x -> x.avail_id) AS ids,
      aggregate(lines, CAST(0 AS BIGINT), (acc, x) -> acc + x.est_impressions) AS imps,
      aggregate(lines, CAST(0 AS DOUBLE), (acc, x) -> acc + x.rate_card_usd) AS usd,
      aggregate(lines, CAST(0 AS DOUBLE), (acc, x) -> acc + x.match_score * x.est_impressions) AS w_score
    FROM combos
  ),
  r AS (
    SELECT m.*, sp.owed_imps, cfg.line_pen, m.imps / sp.owed_imps AS coverage,
      value_reconciliation(sp.unit_rate_usd, m.usd, sp.owed_imps, m.imps) AS v
    FROM m CROSS JOIN sp CROSS JOIN cfg
  ),
  k AS (
    SELECT r.*, transform(array(
        named_struct('rule', 'station', 'label', 'Same station', 'bad', CAST(array() AS ARRAY<STRING>), 'ok', 'every avail is on the missed spot’s station'),
        named_struct('rule', 'daypart', 'label', 'Same or better daypart', 'bad', transform(filter(lines, x -> array_contains(x.failed_rules, 'daypart_lower_value')), x -> x.avail_id),
                     'ok', array_join(array_distinct(transform(lines, x -> x.daypart)), ', ')),
        named_struct('rule', 'rating', 'label', 'Audience rating at or above the floor', 'bad', transform(filter(lines, x -> array_contains(x.failed_rules, 'rating_below_floor')), x -> x.avail_id),
                     'ok', 'every avail meets the guaranteed rating floor'),
        named_struct('rule', 'length', 'label', 'Same spot length', 'bad', transform(filter(lines, x -> array_contains(x.failed_rules, 'spot_length_mismatch')), x -> x.avail_id),
                     'ok', 'same length as the missed spot'),
        named_struct('rule', 'window', 'label', 'Inside the make-good window', 'bad', transform(filter(lines, x -> array_contains(x.failed_rules, 'outside_makegood_window')), x -> x.avail_id),
                     'ok', concat('airs ', array_join(transform(lines, x -> date_format(x.air_date, 'EEE MMM d')), ', '))),
        named_struct('rule', 'flight', 'label', 'Before the flight ends', 'bad', transform(filter(lines, x -> array_contains(x.failed_rules, 'after_flight_end')), x -> x.avail_id),
                     'ok', 'inside the campaign flight'),
        named_struct('rule', 'available', 'label', 'Open and not booked', 'bad', transform(filter(lines, x -> array_contains(x.failed_rules, 'avail_held') OR array_contains(x.failed_rules, 'already_booked')), x -> x.avail_id),
                     'ok', 'open inventory'),
        named_struct('rule', 'competitive', 'label', 'No competitor in the same break', 'bad', transform(filter(lines, x -> x.competitive_conflict), x -> x.avail_id),
                     'ok', 'no competing advertiser in the pod'),
        named_struct('rule', 'client_rules', 'label', 'Client rules respected', 'bad', CAST(array() AS ARRAY<STRING>),
                     'ok', coalesce(nullif(array_join(p_client_rules, '; '), ''), 'no client rules on file')),
        named_struct('rule', 'impressions', 'label', 'Impressions owed are covered', 'bad', CASE WHEN r.coverage < 1 THEN array('package') ELSE CAST(array() AS ARRAY<STRING>) END,
                     'ok', ''),
        named_struct('rule', 'dollars', 'label', 'Value matches the missed spot', 'bad', CASE WHEN r.v.dollar_match THEN CAST(array() AS ARRAY<STRING>) ELSE array('package') END,
                     'ok', r.v.label)
      ), c -> named_struct('rule', c.rule, 'label', c.label, 'passed', size(c.bad) = 0,
        'detail', CASE
          WHEN c.rule = 'impressions' THEN concat(format_number(r.imps, 0), ' of ', format_number(r.owed_imps, 0), ' owed (', CAST(round(r.coverage * 100) AS INT), '%)')
          WHEN c.rule = 'dollars' THEN r.v.label
          WHEN size(c.bad) = 0 THEN c.ok
          ELSE concat(array_join(c.bad, ', '), ' miss', CASE WHEN size(c.bad) = 1 THEN 'es' ELSE '' END, ' this rule') END)) AS checklist
    FROM r
  ),
  s AS (
    SELECT k.*,
      size(filter(checklist, x -> x.passed)) AS rules_passed,
      size(filter(checklist, x -> NOT x.passed)) = 0 AS is_full,
      round(greatest(0.0, least(10.0, w_score / greatest(imps, 1) - 10 * greatest(0.0, 1 - coverage)
        - CASE WHEN v.dollar_match THEN 0 ELSE 2 END - line_pen * (n_lines - 1))), 2) AS package_score,
      lines[0].rank AS lead_rank, lines[0].daypart AS lead_daypart, weekofyear(lines[0].air_date) AS lead_week,
      array_join(array_sort(ids), ',') AS set_key
    FROM k
  ),
  -- pick A, B, C in one pass (window functions, no repeated subqueries)
  s1 AS (SELECT s.*, min(lead_rank) OVER () AS min_lead FROM s),
  -- A: a 10/10 package if there is one (preferring the top-ranked avail), else the best one starting with it
  s2 AS (SELECT s1.*, ROW_NUMBER() OVER (ORDER BY CASE WHEN is_full THEN 0 ELSE 1 END,
                                                 CASE WHEN lead_rank = min_lead THEN 0 ELSE 1 END, package_score DESC, n_lines, set_key) AS a_rn
         FROM s1),
  s3 AS (SELECT s2.*, max(CASE WHEN a_rn = 1 THEN set_key END) OVER () AS a_key,
                      max(CASE WHEN a_rn = 1 THEN lead_daypart END) OVER () AS a_daypart,
                      max(CASE WHEN a_rn = 1 THEN lead_week END) OVER () AS a_week,
                      coalesce(max(CASE WHEN a_rn = 1 THEN is_full END) OVER (), false) AS a_full
         FROM s2),
  -- B: a multi-spot package, offered when it is 10/10 or when A cannot cover the make-good on its own
  s4 AS (SELECT s3.*, n_lines >= 2 AND set_key <> a_key AND (is_full OR NOT a_full) AS b_ok,
                      ROW_NUMBER() OVER (ORDER BY CASE WHEN n_lines >= 2 AND set_key <> a_key AND (is_full OR NOT a_full) THEN 0 ELSE 1 END,
                                                  CASE WHEN is_full THEN 0 ELSE 1 END, package_score DESC, n_lines, set_key) AS b_rn
         FROM s3),
  s5 AS (SELECT s4.*, max(CASE WHEN b_rn = 1 AND b_ok THEN set_key END) OVER () AS b_key FROM s4),
  s6 AS (SELECT s5.*, set_key <> a_key AND set_key <> coalesce(b_key, '') AND (lead_daypart <> a_daypart OR lead_week <> a_week) AS c_ok
         FROM s5),
  s7 AS (SELECT s6.*, ROW_NUMBER() OVER (ORDER BY CASE WHEN c_ok THEN 0 ELSE 1 END, CASE WHEN is_full THEN 0 ELSE 1 END,
                                                  package_score DESC, n_lines, set_key) AS c_rn
         FROM s6),
  picked AS (
    SELECT s7.*, CASE WHEN p_fixed_avail_ids IS NOT NULL THEN 'S'
                      WHEN a_rn = 1 THEN 'A'
                      WHEN b_rn = 1 AND b_ok THEN 'B'
                      WHEN c_rn = 1 AND c_ok THEN 'C' END AS package_label
    FROM s7
  )
  SELECT package_label,
    CASE package_label
      WHEN 'S' THEN 'Your selection'
      WHEN 'A' THEN CASE WHEN lead_rank <> min_lead THEN 'Best option that meets every rule'
                         WHEN n_lines = 1 THEN 'Best match: the top-ranked avail'
                         ELSE concat('The top-ranked avail plus ', n_lines - 1, ' more to close the gap') END
      WHEN 'B' THEN concat(CASE n_lines WHEN 2 THEN 'Two' ELSE 'Three' END, ' spots that together replace the missed spot')
      ELSE CASE WHEN lead_daypart <> a_daypart THEN concat('Alternative in ', lead_daypart) ELSE 'Alternative in a different week' END
    END,
    ids, lines, n_lines, imps, owed_imps, round(coverage, 3),
    v.owed_value_usd, v.proposed_value_usd, v.compared_value_usd, v.value_gap_usd, v.value_ratio, v.dollar_match,
    v.partial_refund_usd, v.label,
    checklist, rules_passed, size(checklist), package_score, is_full
  FROM picked
  WHERE package_label IS NOT NULL;

CREATE OR REPLACE FUNCTION gap_fillers(
  p_spot_id STRING COMMENT 'Preempted or missed spot id',
  p_selected_ids ARRAY<STRING> COMMENT 'Avails the seller has picked so far',
  p_candidate_ids ARRAY<STRING> COMMENT 'Other candidate avails to consider (the inventory_search results)',
  p_prefs MAP<STRING, DOUBLE> DEFAULT NULL COMMENT 'Advertiser adjacency preferences used for scoring',
  p_as_of DATE DEFAULT NULL COMMENT 'Decision date (NULL = simulation clock)',
  p_limit INT DEFAULT 3 COMMENT 'How many suggestions to return'
)
RETURNS TABLE (
  avail_id STRING, air_date DATE, daypart STRING, program_name STRING, rate_card_usd DOUBLE, est_impressions BIGINT,
  match_score INT, tier STRING, coverage_before DOUBLE, coverage_after DOUBLE, value_ratio_before DOUBLE,
  value_ratio_after DOUBLE, closes_gap BOOLEAN, reason STRING
)
COMMENT 'Reviews a seller’s own selection for a make-good: how far it falls short of the impressions and value owed, and the avails that would close the gap, best first (closes the gap with the least extra value given away, then highest match score).'
RETURN
  WITH sel AS (SELECT * FROM shortfall_calculator(p_spot_id, p_selected_ids)),
  sc AS (
    SELECT * FROM commensurate_scorer(p_spot_id, p_candidate_ids, p_prefs, p_as_of)
    WHERE tier <> 'not_possible' AND NOT array_contains(coalesce(p_selected_ids, CAST(array() AS ARRAY<STRING>)), avail_id)
  ),
  x AS (
    SELECT sc.*, sel.spot_coverage_pct AS cov_before, sel.value_ratio AS ratio_before,
      (sel.proposed_impressions + sc.est_impressions) / sel.owed_impressions AS cov_after,
      value_reconciliation(sel.owed_value_usd, sel.proposed_value_usd + sc.rate_card_usd, sel.owed_impressions,
                           sel.proposed_impressions + sc.est_impressions) AS v_after
    FROM sc CROSS JOIN sel
  ),
  o AS (
    SELECT x.*, x.cov_after >= 1 AND x.v_after.dollar_match AS closes,
      ROW_NUMBER() OVER (ORDER BY x.cov_after >= 1 AND x.v_after.dollar_match DESC,
                                  CASE WHEN x.cov_after >= 1 AND x.v_after.dollar_match THEN abs(x.v_after.value_ratio - 1) ELSE -x.cov_after END,
                                  x.match_score DESC, x.avail_id) AS rn
    FROM x
  )
  SELECT avail_id, air_date, daypart, program_name, rate_card_usd, est_impressions, match_score, tier,
    round(cov_before, 3), round(cov_after, 3), ratio_before, v_after.value_ratio, closes,
    concat('adds ', format_number(est_impressions, 0), ' impressions (', CAST(round(cov_before * 100) AS INT), '% -> ',
           CAST(round(cov_after * 100) AS INT), '% of owed); value ', v_after.label)
  FROM o WHERE rn <= coalesce(p_limit, 3);

CREATE OR REPLACE FUNCTION program_lookup(
  p_text STRING COMMENT 'Program name as a seller typed it, e.g. "judge judy"',
  p_station_id STRING DEFAULT NULL COMMENT 'Limit to one station’s inventory'
)
RETURNS TABLE (program_name STRING, program_genre STRING, avails BIGINT, stations BIGINT, match_type STRING)
COMMENT 'Finds the program names in the inventory that a client rule refers to (exact, contains, or within 3 edits), so a rule like "no Judge Judy" is saved against real program names.'
RETURN
  SELECT program_name, max(program_genre), count(*), count(DISTINCT station_id),
    CASE WHEN lower(program_name) = lower(trim(p_text)) THEN 'exact'
         WHEN lower(program_name) LIKE concat('%', lower(trim(p_text)), '%') THEN 'contains' ELSE 'similar' END
  FROM gold_available_inventory
  WHERE (p_station_id IS NULL OR station_id = p_station_id)
    AND length(trim(p_text)) >= 3
    AND (lower(program_name) LIKE concat('%', lower(trim(p_text)), '%') OR levenshtein(lower(program_name), lower(trim(p_text))) <= 3)
  GROUP BY program_name;

CREATE OR REPLACE FUNCTION advertiser_lookup(
  p_text STRING COMMENT 'Client name or id as a seller typed it, e.g. "Ironwood"'
)
RETURNS TABLE (advertiser_id STRING, advertiser_name STRING, category STRING, match_type STRING)
COMMENT 'Finds the clients a name refers to, among the clients the caller may see (accounts in gold_spot_access), for client rules stated in conversation.'
RETURN
  SELECT a.advertiser_id, a.advertiser_name, a.category,
    CASE WHEN a.advertiser_id = upper(trim(p_text)) OR lower(a.advertiser_name) = lower(trim(p_text)) THEN 'exact' ELSE 'contains' END
  FROM ref_advertisers a
  WHERE a.advertiser_id IN (SELECT DISTINCT advertiser_id FROM gold_spot_access)
    AND length(trim(p_text)) >= 3
    AND (a.advertiser_id = upper(trim(p_text)) OR lower(a.advertiser_name) LIKE concat('%', lower(trim(p_text)), '%'));
