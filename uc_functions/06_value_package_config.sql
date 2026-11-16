-- Dollar matching, make-good packages and the offer lifecycle: tunable parameters (run BEFORE the pipeline, after 05).
-- The value of a make-good is compared with the value of the spot it replaces; a package of up to
-- package_max_lines avails can replace one spot. The rule functions read these at call time.
USE CATALOG YOUR_CATALOG;
USE SCHEMA YOUR_SCHEMA;

MERGE INTO mg_rules_config t
USING (SELECT * FROM VALUES
  ('dollar_basis', NULL, 'rate_card', 'must_match', 'How a make-good’s value is measured against the missed spot’s unit rate: rate_card (sum of the replacement avails’ rate-card value) or cpm_equivalent (impressions delivered priced at the contract’s CPM).', 'Which value do clients and finance reconcile make-goods on: rate card, or delivered impressions at the contract CPM?'),
  ('dollar_tolerance_under_pct', 0.05, NULL, 'must_match', 'A make-good may be worth at most this share less than the missed spot before it counts as under-delivering (the gap can be refunded).', 'How far under the missed spot’s value can a make-good be before a partial refund is owed?'),
  ('dollar_tolerance_over_pct', 0.50, NULL, 'must_match', 'A make-good may be worth at most this share more than the missed spot before it counts as giving away inventory.', 'How much extra value can a make-good give away (e.g. Prime for a Daytime spot) without a manager’s approval?'),
  ('package_max_lines', 3, NULL, 'soft_match', 'Most avails one make-good package may combine to replace a single missed spot.', 'Will clients take two or three smaller spots in place of one? Is there a limit?'),
  ('package_candidate_pool', 12, NULL, 'system', 'How many top-ranked options the package builder combines.', NULL),
  ('package_line_penalty', 0.5, NULL, 'soft_match', 'Package score points deducted per extra avail (one-spot replacements are simpler to sell and traffic).', NULL),
  ('gap_loop_max_iterations', 2, NULL, 'system', 'How many times the agent widens the search when no package fully covers the impressions and dollars owed.', NULL),
  ('gap_widen_horizon_days', 14, NULL, 'system', 'Days added to the search horizon on each widening pass.', NULL),
  ('gap_widen_max_results', 60, NULL, 'system', 'Candidate avails kept on a widening pass.', NULL),
  ('w_day_pref', 0.10, NULL, 'soft_match', 'Score weight: avail airs on a day of the week the client prefers (client rule).', NULL),
  ('hold_hours', 72, NULL, 'system', 'How long avails in an offer sent to a client stay held for that client.', 'How long can traffic hold inventory while a client decides?'),
  ('approval_required_over_usd', 0, NULL, 'system', 'Offers above this value need a sales manager’s sign-off before the seller can send them (0 = every offer).', 'Which offers need a manager’s sign-off: all of them, or above a dollar value?')
  AS v(rule_key, num_value, str_value, rule_type, description, question_for_ad_ops)) s
ON t.rule_key = s.rule_key
WHEN NOT MATCHED THEN INSERT *;
