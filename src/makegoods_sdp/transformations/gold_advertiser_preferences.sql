CREATE OR REFRESH MATERIALIZED VIEW gold_advertiser_preferences
COMMENT 'Advertiser adjacency preferences learned from like-for-like (non-downgrade) make-good decisions before mg_rules_config.pref_cutoff_date. weight = (accepts - rejects) / (accepts + rejects + 1), in (-1, 1). Seeds the agent''s Lakebase preference memory.'
AS WITH cfg AS (SELECT CAST(str_value AS DATE) AS cutoff FROM ${mg_catalog}.${mg_schema}.mg_rules_config WHERE rule_key = 'pref_cutoff_date'),
d AS (
  SELECT h.advertiser_id, tag,
    CASE WHEN h.offer_status = 'Accepted' THEN 1 ELSE 0 END AS accepted,
    CASE WHEN h.offer_status IN ('Rejected', 'Expired') THEN 1 ELSE 0 END AS rejected,
    cfg.cutoff
  FROM gold_make_good_history h
  CROSS JOIN cfg
  LATERAL VIEW explode(h.offered_adjacency_tags) t AS tag
  WHERE h.offer_status IN ('Accepted', 'Rejected', 'Expired')
    AND NOT h.is_downgrade
    AND to_date(h.decision_ts) < cfg.cutoff
)
SELECT advertiser_id, tag AS adjacency_tag,
  sum(accepted) AS accepts, sum(rejected) AS rejects, count(*) AS evidence_count,
  round((sum(accepted) - sum(rejected)) / (sum(accepted) + sum(rejected) + 1), 3) AS weight,
  max(cutoff) AS learned_through
FROM d GROUP BY advertiser_id, tag;
