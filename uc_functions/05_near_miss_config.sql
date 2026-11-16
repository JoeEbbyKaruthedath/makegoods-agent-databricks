-- Tiered scoring: penalties for near-miss avails (graded by how close they are) and the display threshold.
-- Match score (0-10) = 10 - sum(rule penalties) - fit_penalty_max x (1 - soft-match fit). 10/10 = perfect match.
USE CATALOG YOUR_CATALOG;
USE SCHEMA YOUR_SCHEMA;

MERGE INTO mg_rules_config t
USING (SELECT * FROM VALUES
  ('near_miss_min_score', 6.0, NULL, 'near_miss', 'Lowest match score (out of 10) shown to the reviewer as a near-miss make-good.', 'Below what score should we refund rather than ask the advertiser?'),
  ('fit_penalty_max', 2.0, NULL, 'near_miss', 'Points deducted from 10 for a weak soft-match fit (impressions, genre, preference, yield, timeliness).', NULL),
  ('pen_rating_base', 1.0, NULL, 'near_miss', 'Penalty for any rating below the floor.', 'Is a rating slightly under the floor acceptable with a bonus spot?'),
  ('pen_rating_per_pct', 0.2, NULL, 'near_miss', 'Additional penalty per 1% the rating falls short of the floor (5.40 vs 5.58 = 3.2% -> 1.0 + 0.64).', NULL),
  ('pen_daypart_step', 2.0, NULL, 'near_miss', 'Penalty per daypart step below the preempted daypart.', 'Will advertisers accept a one-step downgrade with extra weight?'),
  ('pen_length_shorter', 2.5, NULL, 'near_miss', 'Penalty when the avail is shorter than the spot (e.g. :15 for a :30).', NULL),
  ('pen_length_longer', 1.0, NULL, 'near_miss', 'Penalty when the avail is longer than the spot (gives away time).', NULL),
  ('pen_window_base', 1.0, NULL, 'near_miss', 'Penalty for airing after the make-good window.', 'How many days late can a make-good air with advertiser consent?'),
  ('pen_window_per_day', 0.25, NULL, 'near_miss', 'Additional penalty per day past the make-good window.', NULL),
  ('pen_flight_base', 1.5, NULL, 'near_miss', 'Penalty for airing after the flight end.', 'Can a make-good extend past the flight?'),
  ('pen_flight_per_day', 0.25, NULL, 'near_miss', 'Additional penalty per day after the flight end.', NULL),
  ('pen_avail_held', 1.5, NULL, 'near_miss', 'Penalty for an avail on hold (traffic must release it).', 'Who can release held inventory for a make-good?')
  AS v(rule_key, num_value, str_value, rule_type, description, question_for_ad_ops)) s
ON t.rule_key = s.rule_key
WHEN NOT MATCHED THEN INSERT *;

-- Whole-number scores; every possible option is shown. Below near_miss_min_score the agent still shows the options
-- but recommends a refund.
UPDATE mg_rules_config SET num_value = 4.0,
  description = 'Match score (out of 10, rounded) below which the agent still shows the best options but recommends a refund.',
  question_for_ad_ops = 'Below what score should the agent recommend a refund rather than an exception?'
WHERE rule_key = 'near_miss_min_score';
