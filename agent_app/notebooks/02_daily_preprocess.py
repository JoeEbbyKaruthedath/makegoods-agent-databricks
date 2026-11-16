# Databricks notebook source
# MAGIC %md
# MAGIC # Daily make-good preparation
# MAGIC
# MAGIC Runs before sellers start their day. For every open spot in the work queue it runs the same rule functions the agent
# MAGIC uses (no AI) and stores the make-good options in `mg_proposals`, so the app's queue shows each spot's best option
# MAGIC with its rule checklist and dollar match, and the morning digest can list them.
# MAGIC
# MAGIC | Step | What happens |
# MAGIC |---|---|
# MAGIC | 1 | Read the open queue and each client's saved rules (`mg_advertiser_rules`) |
# MAGIC | 2 | For each spot: `inventory_search` with the client's rules, then `package_builder` (options A/B/C) |
# MAGIC | 3 | Replace today's rows in `mg_proposals` |

# COMMAND ----------

# MAGIC %pip install -q "mlflow==3.17.0" "databricks-sdk==0.151.0"
# MAGIC %restart_python

# COMMAND ----------

dbutils.widgets.text("catalog", "YOUR_CATALOG", "Catalog")
dbutils.widgets.text("schema", "makegoods", "Schema")
dbutils.widgets.text("max_spots", "0", "Spots to prepare (0 = all)")
dbutils.widgets.text("parallel", "8", "Spots prepared at once")
CATALOG, SCHEMA = dbutils.widgets.get("catalog").strip(), dbutils.widgets.get("schema").strip()
MAX_SPOTS, PARALLEL = int(dbutils.widgets.get("max_spots") or 0), int(dbutils.widgets.get("parallel") or 8)

import os
import sys
from concurrent.futures import ThreadPoolExecutor

os.environ["MG_CATALOG"], os.environ["MG_SCHEMA"] = CATALOG, SCHEMA
sys.path.insert(0, os.path.abspath(".."))  # agent_app: the agent's rule helpers
from agent_server import rules as R  # noqa: E402
from agent_server.uc_tools import sql_literal  # noqa: E402

fq = lambda n: f"{CATALOG}.{SCHEMA}.{n}"  # noqa: E731

# COMMAND ----------

queue = spark.sql(f"""SELECT spot_id, advertiser_id, market, seller_id FROM {fq('gold_make_good_candidates')}
                      ORDER BY days_to_deadline, revenue_at_risk_usd DESC""").collect()
if MAX_SPOTS:
    queue = queue[:MAX_SPOTS]
rule_rows = spark.sql(f"""SELECT * FROM {fq('mg_advertiser_rules')}
                          QUALIFY ROW_NUMBER() OVER (PARTITION BY rule_id ORDER BY created_at DESC) = 1""").collect()
rules_by_adv: dict = {}
for r in rule_rows:
    if r.active:
        rules_by_adv.setdefault(r.advertiser_id, []).append({"type": r.rule_type, "values": list(r.rule_values or []),
                                                             "label": r.label, "active": True})
as_of = spark.sql(f"SELECT str_value FROM {fq('mg_rules_config')} WHERE rule_key = 'as_of_date'").first()[0]
print(f"{len(queue)} open spots; client rules for {len(rules_by_adv)} clients; as of {as_of}")


def prepare(spot) -> list[dict]:
    rp = R.to_params(rules_by_adv.get(spot.advertiser_id, []))
    args = {"p_spot_id": spot.spot_id, "p_max_results": 25, "p_include_near_misses": True,
            "p_exclude_tags": rp["exclude_tags"] or None, "p_exclude_programs": rp["exclude_programs"] or None,
            "p_exclude_genres": rp["exclude_genres"] or None, "p_only_dows": rp["only_dows"] or None,
            "p_avoid_dows": rp["avoid_dows"] or None}
    named = ", ".join(f"{k} => {sql_literal(v)}" for k, v in args.items() if v is not None)
    ids = [r.avail_id for r in spark.sql(f"SELECT avail_id FROM {fq('inventory_search')}({named})").collect()]
    if not ids:
        return []
    pk_args = {"p_spot_id": spot.spot_id, "p_avail_ids": ids, "p_prefer_dows": rp["prefer_dows"] or None,
               "p_client_rules": rp["labels"] or None}
    named = ", ".join(f"{k} => {sql_literal(v)}" for k, v in pk_args.items() if v is not None)
    rows = spark.sql(f"SELECT * FROM {fq('package_builder')}({named})").collect()
    return [{"spot_id": spot.spot_id, "as_of": as_of, "package_label": p.package_label, "strategy": p.strategy,
             "avail_ids": list(p.avail_ids), "est_impressions": p.est_impressions, "owed_impressions": p.owed_impressions,
             "coverage_pct": p.coverage_pct, "owed_value_usd": p.owed_value_usd, "proposed_value_usd": p.proposed_value_usd,
             "dollar_match": p.dollar_match, "dollar_label": p.dollar_label, "rules_passed": p.rules_passed,
             "rules_total": p.rules_total, "is_full": p.is_full, "client_rules": rp["labels"], "market": spot.market,
             "seller_id": spot.seller_id} for p in rows]


results, failed = [], []
with ThreadPoolExecutor(PARALLEL) as pool:
    for spot, fut in [(s, pool.submit(prepare, s)) for s in queue]:
        try:
            results += fut.result()
        except Exception as e:  # one spot must not stop the batch
            failed.append((spot.spot_id, str(e)[:200]))
print(f"options for {len({r['spot_id'] for r in results})} spots ({len(results)} options); {len(failed)} failed")
for f in failed[:10]:
    print("  failed:", f)

# COMMAND ----------

from pyspark.sql import functions as F  # noqa: E402

# explicit types: a column that is empty or null for every spot (no client rules yet) cannot be inferred
COLUMNS = [("spot_id", str), ("as_of", str), ("package_label", str), ("strategy", str), ("avail_ids", list),
           ("est_impressions", int), ("owed_impressions", int), ("coverage_pct", float), ("owed_value_usd", float),
           ("proposed_value_usd", float), ("dollar_match", bool), ("dollar_label", str), ("rules_passed", int),
           ("rules_total", int), ("is_full", bool), ("client_rules", list), ("market", str), ("seller_id", str)]
DDL = ", ".join(f"{c} {'ARRAY<STRING>' if t is list else {str: 'STRING', int: 'BIGINT', float: 'DOUBLE', bool: 'BOOLEAN'}[t]}"
                for c, t in COLUMNS)


def typed(r: dict) -> tuple:
    return tuple(None if r.get(c) is None else [str(x) for x in r[c]] if t is list else t(r[c]) for c, t in COLUMNS)


if results:
    df = spark.createDataFrame([typed(r) for r in results], schema=DDL).withColumn("computed_at", F.current_timestamp()) \
        .withColumn("as_of", F.to_date("as_of"))
    df.createOrReplaceTempView("new_proposals")
    spark.sql(f"DELETE FROM {fq('mg_proposals')} WHERE spot_id IN (SELECT DISTINCT spot_id FROM new_proposals) "
              f"OR computed_at < current_timestamp() - INTERVAL 7 DAYS")
    spark.sql(f"""INSERT INTO {fq('mg_proposals')} (spot_id, computed_at, as_of, package_label, strategy, avail_ids,
                  est_impressions, owed_impressions, coverage_pct, owed_value_usd, proposed_value_usd, dollar_match,
                  dollar_label, rules_passed, rules_total, is_full, client_rules, market, seller_id)
                  SELECT spot_id, computed_at, as_of, package_label, strategy, avail_ids, est_impressions, owed_impressions,
                  coverage_pct, owed_value_usd, proposed_value_usd, dollar_match, dollar_label, CAST(rules_passed AS INT),
                  CAST(rules_total AS INT), is_full, client_rules, market, seller_id FROM new_proposals""")
full = sum(1 for r in results if r["package_label"] == "A" and r["is_full"])
print(f"stored; {full} spots have a 10/10 option A")
dbutils.jobs.taskValues.set("spots_prepared", len({r["spot_id"] for r in results}))
