-- v2 hero checks: dollars, packages and the rule checklist for SPT-0058222 (Ironwood Motors, Prime, $1,566).
--   python tools/run_sql.py tests/v2_dollars_check.sql --catalog <catalog> --schema makegoods_v2 --show
USE CATALOG YOUR_CATALOG;
USE SCHEMA YOUR_SCHEMA;

-- the dollar rule on its own: matches, under-delivers, gives away value
SELECT value_reconciliation(1566, 1566, 51240, 65268).label AS exact,
       value_reconciliation(1566, 900, 51240, 30000).label AS under,
       value_reconciliation(1566, 900, 51240, 30000).partial_refund_usd AS refund_gap,
       value_reconciliation(1566, 3432, 51240, 135537).label AS over;

-- the best avail's dollars and impressions (AVL-026683: Mon Nov 2, Network Prime Drama)
SELECT spot_id, owed_value_usd, proposed_value_usd, value_gap_usd, value_ratio, dollar_match, dollar_label,
       owed_impressions, proposed_impressions, spot_coverage_pct
FROM shortfall_calculator('SPT-0058222', array('AVL-026683'));

-- options A/B/C with their checklists, from the hero's commensurate avails
SELECT package_label, strategy, avail_ids, coverage_pct, proposed_value_usd, dollar_label, rules_passed, rules_total, is_full,
       transform(filter(checklist, c -> NOT c.passed), c -> c.label) AS failed_rules
FROM package_builder('SPT-0058222', array('AVL-025122', 'AVL-025928', 'AVL-026683', 'AVL-027449', 'AVL-027451', 'AVL-028215',
                                            'AVL-029002', 'AVL-030493'))
ORDER BY package_label;

-- a client rule removes the program from every option
SELECT count(*) AS options_with_excluded_program
FROM inventory_search('SPT-0058222', 25, NULL, NULL, NULL, NULL, TRUE, array('Network Prime Drama'))
WHERE program_name = 'Network Prime Drama';
