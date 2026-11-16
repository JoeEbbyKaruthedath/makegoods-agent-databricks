# Databricks notebook source
# MAGIC %md
# MAGIC # Production monitoring: score live traffic
# MAGIC
# MAGIC Registers scorers on the app's MLflow experiment and starts them on production traces, so the quality bars from
# MAGIC the release gate keep being measured after release. Assessments appear on each scored trace and in the
# MAGIC experiment's monitoring view; `eval/production_report.py` summarizes them by day.
# MAGIC
# MAGIC Every app request is one trace: a new make-good, or one step of its lifecycle (the seller's pick, the manager's
# MAGIC sign-off, sending, the client's reply, the seller's confirmation). Each scorer applies to the steps it names and
# MAGIC skips the rest.
# MAGIC
# MAGIC | Scorer | Step | Checks |
# MAGIC |---|---|---|
# MAGIC | `grounded_ids` | recommendation | every avail id in the response is one the rules engine ranked |
# MAGIC | `dollar_math` | recommendation | every dollar figure in the response is one the rules engine computed |
# MAGIC | `option_a_meets_all_rules` | recommendation | Option A passes all 11 make-good rules |
# MAGIC | `option_a_dollars_match` | recommendation | Option A's value is within the allowed range of the missed spot's |
# MAGIC | `token_budget` | recommendation | AI tokens stay under 10% of the inherited GPT-assist matcher's median |
# MAGIC | `business_rationale` | recommendation | AI judge: the reason is in ad-ops business terms |
# MAGIC | `seller_took_recommendation` | seller's pick | the seller offered Option A (or approved the recommended refund) |
# MAGIC | `offer_grounded` | offer draft | the email's numbers all come from the options offered |
# MAGIC | `offer_email_quality` | offer draft | AI judge: a warm, brief, professional note that asks for the client's pick |
# MAGIC | `manager_approved` | manager sign-off | the manager approved the offer without changes |
# MAGIC | `reply_read_confident` | client reply | the reply was read with confidence, from the right sender, with no instructions in it |
# MAGIC | `seller_confirmed_reading` | seller's confirmation | the seller's action matches how the reply was read |
# MAGIC | `guardrails_passed` | any | every guardrail in the trace passed |
# MAGIC | `response_time` | any | within budget: 60 s for a new request, 30 s for a lifecycle step |
# MAGIC
# MAGIC Code scorers read only the trace and score every trace; the two AI judges score a sample (`judge_sample_rate`).
# MAGIC Monitoring scorers run on their own, outside this repo: each keeps its imports and helpers inside the function.

# COMMAND ----------

# MAGIC %pip install -q "mlflow==3.17.0" "databricks-agents>=1.12"
# MAGIC %restart_python

# COMMAND ----------

dbutils.widgets.text("experiment_id", "YOUR_EXPERIMENT_ID", "MLflow experiment")
dbutils.widgets.text("warehouse_id", "YOUR_WAREHOUSE_ID", "SQL warehouse (reads the trace tables)")
dbutils.widgets.text("sample_rate", "1.0", "Share of traces for the code scorers")
dbutils.widgets.text("judge_sample_rate", "0.1", "Share of traces for the AI judges")
EXPERIMENT_ID, WAREHOUSE_ID = dbutils.widgets.get("experiment_id").strip(), dbutils.widgets.get("warehouse_id").strip()
SAMPLE = float(dbutils.widgets.get("sample_rate") or 1.0)
JUDGE_SAMPLE = float(dbutils.widgets.get("judge_sample_rate") or 0.1)

import mlflow
from mlflow.genai.scorers import ScorerSamplingConfig, delete_scorer, list_scorers, scorer
from mlflow.tracing import set_databricks_monitoring_sql_warehouse_id

mlflow.set_experiment(experiment_id=EXPERIMENT_ID)
set_databricks_monitoring_sql_warehouse_id(WAREHOUSE_ID, experiment_id=EXPERIMENT_ID)

# COMMAND ----------

# MAGIC %md
# MAGIC ## The recommendation

# COMMAND ----------


@scorer
def grounded_ids(trace):
    """Every AVL id in the response is one of the avails the rules engine ranked in this trace."""
    import json
    import re

    io = lambda v: json.loads(v) if isinstance(v, str) else (v or {})  # span data is stored as JSON text
    root = next((s for s in trace.data.spans if s.name == "makegoods_agent"), trace.data.spans[0])
    text = str(io(root.outputs).get("response") or "")
    mentioned = set(re.findall(r"AVL-\d{6}", text))
    if not mentioned:
        return None
    ranked = set()
    for s in trace.data.spans:
        if s.name == "uc.commensurate_scorer" and s.outputs:
            out = io(s.outputs)
            rows = out if isinstance(out, list) else out.get("first_25", [])
            ranked |= {r.get("avail_id") for r in rows if isinstance(r, dict)}
    return not (mentioned - ranked) if ranked else None


@scorer
def dollar_math(trace):
    """Every dollar figure in the response is within $1 of one the rules engine computed in this trace."""
    import json
    import re

    io = lambda v: json.loads(v) if isinstance(v, str) else (v or {})  # span data is stored as JSON text
    root = next((s for s in trace.data.spans if s.name == "makegoods_agent"), trace.data.spans[0])
    text = str(io(root.outputs).get("response") or "")
    stated = {int(m.replace(",", "")) for m in re.findall(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)", text)}
    if not stated:
        return None
    facts = set()
    for s in trace.data.spans:
        if s.name in ("uc.package_builder", "uc.shortfall_calculator", "uc.spot_context") and s.outputs:
            blob = json.dumps(io(s.outputs), default=str)
            facts |= {round(float(x)) for x in re.findall(r'"(?:owed_value_usd|proposed_value_usd|value_gap_usd|partial_refund_usd|'
                                                         r'unit_rate_usd|revenue_at_risk_usd|compared_value_usd)": (-?[\d.]+)', blob)}
    facts |= {abs(f) for f in facts}
    return all(any(abs(v - f) <= 1 for f in facts) for v in stated) if facts else None


@scorer
def option_a_meets_all_rules(trace):
    """Option A passes all 11 make-good rules (the recommendation tags its rule count)."""
    from mlflow.entities import Feedback

    rules = (trace.info.tags or {}).get("best_package_rules")
    if not rules:
        return None
    passed, total = (int(x) for x in rules.split("/"))
    return Feedback(value=passed == total, rationale=f"Option A passes {rules} rules")


@scorer
def option_a_dollars_match(trace):
    """Option A's value is within the allowed range of the missed spot's value (the first priority for ad sales)."""
    match = (trace.info.tags or {}).get("dollar_match")
    return None if match is None else match == "true"


@scorer
def token_budget(trace):
    """AI tokens for a new recommendation stay under 10% of the inherited GPT-assist matcher's median per make-good."""
    from mlflow.entities import Feedback

    gpt_assist_median = 27928  # median GPT tokens per make-good in gold_make_good_history (matched_by = 'gpt_assist')
    if not any(s.name == "explain_recommendation" for s in trace.data.spans):
        return None
    tokens = int((trace.info.token_usage or {}).get("total_tokens") or 0)
    return Feedback(value=tokens < 0.10 * gpt_assist_median,
                    rationale=f"{tokens:,} tokens ({tokens / gpt_assist_median:.1%} of the GPT-assist median {gpt_assist_median:,})")


@scorer
def business_rationale(trace):
    """AI judge on new recommendations only: the reason is in ad-ops business terms."""
    import json

    from mlflow.genai.judges import meets_guidelines

    io = lambda v: json.loads(v) if isinstance(v, str) else (v or {})  # span data is stored as JSON text
    if not any(s.name == "explain_recommendation" for s in trace.data.spans):
        return None
    root = next((s for s in trace.data.spans if s.name == "makegoods_agent"), trace.data.spans[0])
    return meets_guidelines(name="business_rationale", context={
        "request": str(io(root.inputs).get("message") or ""), "response": str(io(root.outputs).get("response") or "")},
        guidelines=["The response explains, in plain business language for an ad-ops reviewer, why the recommendation is "
                    "appropriate, giving at least one concrete business reason (any of: impressions delivered vs owed, value vs "
                    "the missed spot, program or genre fit, advertiser preference or client rules, or why no available inventory "
                    "qualifies). It is not required to mention every kind of reason. Naming the rules an option misses, and what the seller "
                    "should do about them, is part of a complete explanation, not a reason to fail."],
        model="databricks:/databricks-gpt-5-5")  # a different model family from the agent's


# COMMAND ----------

# MAGIC %md
# MAGIC ## The offer, the client's reply, and what people did with them

# COMMAND ----------


@scorer
def seller_took_recommendation(trace):
    """The seller's pick: they offered Option A, or approved the refund the agent recommended."""
    import json

    from mlflow.entities import Feedback

    io = lambda v: json.loads(v) if isinstance(v, str) else (v or {})  # span data is stored as JSON text
    root = next((s for s in trace.data.spans if s.name == "makegoods_agent"), trace.data.spans[0])
    inp = io(root.inputs)
    review = (inp.get("custom_inputs") or {}).get("review") or {}
    action = review.get("action")
    if inp.get("resumed_from") != "make_good_review" or action not in ("offer", "book", "accept", "modify", "reject", "refund"):
        return None  # questions, refinements, rule edits and "check my selection" are not a decision
    labels = review.get("labels") or ([review["label"]] if review.get("label") else [])
    took = action == "accept" or (action in ("offer", "book") and labels == ["A"])
    return Feedback(value=took, rationale=f"seller chose {action}" + (f" Option {', '.join(labels)}" if labels else ""))


@scorer
def offer_grounded(trace):
    """An offer email's numbers all come from the options offered (the offer-facts guardrail passed)."""
    import json

    io = lambda v: json.loads(v) if isinstance(v, str) else (v or {})  # span data is stored as JSON text
    for s in trace.data.spans:
        if s.name == "guardrail.offer_facts" and s.outputs:
            return bool(io(s.outputs).get("passed"))
    return None


@scorer
def offer_email_quality(trace):
    """AI judge on the drafted client email (the same guidelines as the release evaluation)."""
    import json

    from mlflow.genai.judges import meets_guidelines

    io = lambda v: json.loads(v) if isinstance(v, str) else (v or {})  # span data is stored as JSON text
    email = next((str(io(s.inputs).get("email") or "") for s in trace.data.spans if s.name == "guardrail.offer_facts"), "")
    if not email:
        return None
    return meets_guidelines(name="offer_email_quality", context={"request": "Write a make-good offer email to the client.",
                                                                  "response": email}, guidelines=[
        "The email is a warm, brief, professional note from a TV station seller to a client's media buyer.",
        "It explains that the client's spot did not air as planned and offers replacement options.",
        "It asks the client to reply with the option they prefer and makes no promises beyond the options listed."])


@scorer
def manager_approved(trace):
    """The manager's sign-off: approved as drafted (no changes asked, not rejected)."""
    import json

    from mlflow.entities import Feedback

    io = lambda v: json.loads(v) if isinstance(v, str) else (v or {})  # span data is stored as JSON text
    root = next((s for s in trace.data.spans if s.name == "makegoods_agent"), trace.data.spans[0])
    inp = io(root.inputs)
    review = (inp.get("custom_inputs") or {}).get("review") or {}
    if inp.get("resumed_from") != "manager_review" or not review.get("action"):
        return None
    return Feedback(value=review["action"] == "approve", rationale=f"manager chose {review['action']}"
                    + (f": {review['note']}" if review.get("note") else ""))


@scorer
def reply_read_confident(trace):
    """The client's reply was read with confidence, from the address the offer went to, with no instructions in it."""
    import json

    from mlflow.entities import Feedback

    io = lambda v: json.loads(v) if isinstance(v, str) else (v or {})  # span data is stored as JSON text
    read = next((io(s.outputs) for s in trace.data.spans if s.name == "guardrail.client_reply" and s.outputs), None)
    if not read:
        return None
    ok = (read.get("confidence") or 0) >= 0.8 and read.get("sender_verified", True) and read.get("decision") != "unclear"
    return Feedback(value=ok, rationale=f"read as {read.get('decision')} {read.get('options') or ''} at {read.get('confidence')} "
                    f"by {read.get('read_by') or 'the model'}" + (f"; {'; '.join(read['notes'])}" if read.get("notes") else ""))


@scorer
def seller_confirmed_reading(trace):
    """The seller's confirmation matches how the reply was read: accept -> book, decline -> refund, counter -> search again."""
    import json

    from mlflow.entities import Feedback

    io = lambda v: json.loads(v) if isinstance(v, str) else (v or {})  # span data is stored as JSON text
    span = next((s for s in trace.data.spans if s.name == "seller_confirm" and s.outputs), None)
    decision = io(span.outputs).get("confirm_decision") if span else None
    if not decision or not decision.get("client_read") or decision.get("action") == "withdraw":
        return None
    expected = {"accept": "book", "decline": "refund", "counter": "counter"}.get(decision["client_read"])
    if expected is None:
        return None  # a question or an unclear reply has no single right action
    return Feedback(value=decision.get("action") == expected,
                    rationale=f"read as {decision['client_read']}, seller chose {decision.get('action')}")


# COMMAND ----------

# MAGIC %md
# MAGIC ## Every step

# COMMAND ----------


@scorer
def guardrails_passed(trace):
    """Every guardrail in the trace passed: avail ids, dollar figures, offer facts."""
    import json

    from mlflow.entities import Feedback

    io = lambda v: json.loads(v) if isinstance(v, str) else (v or {})  # span data is stored as JSON text
    outs = {s.name.replace("guardrail.", ""): io(s.outputs) for s in trace.data.spans if s.name.startswith("guardrail.") and s.outputs}
    checks = {k: bool(v.get("passed")) for k, v in outs.items() if isinstance(v, dict) and "passed" in v}
    if not checks:
        return None
    failed = [k for k, v in checks.items() if not v]
    return Feedback(value=not failed, rationale=f"failed: {', '.join(failed)}" if failed else f"passed: {', '.join(checks)}")


@scorer
def response_time(trace):
    """Within budget: 60 s for a new request (search, scoring, explanation or analytics), 30 s for a lifecycle step."""
    import json

    from mlflow.entities import Feedback

    io = lambda v: json.loads(v) if isinstance(v, str) else (v or {})  # span data is stored as JSON text
    root = next((s for s in trace.data.spans if s.name == "makegoods_agent"), None)
    if root is None or not trace.info.execution_duration:
        return None
    seconds = trace.info.execution_duration / 1000
    budget = 30 if io(root.inputs).get("resumed_from") else 60
    return Feedback(value=seconds <= budget, rationale=f"{seconds:.1f}s (budget {budget}s)")


# COMMAND ----------

CODE = [grounded_ids, dollar_math, option_a_meets_all_rules, option_a_dollars_match, token_budget, seller_took_recommendation,
        offer_grounded, manager_approved, reply_read_confident, seller_confirmed_reading, guardrails_passed, response_time]
JUDGES = [business_rationale, offer_email_quality]

registered = {s.name for s in list_scorers(experiment_id=EXPERIMENT_ID)}
started = []
for sc, rate in [(s, SAMPLE) for s in CODE] + [(s, JUDGE_SAMPLE) for s in JUDGES]:
    if sc.name in registered:
        delete_scorer(name=sc.name, experiment_id=EXPERIMENT_ID)  # re-running replaces the scorer's code and sampling
    sc.register(name=sc.name, experiment_id=EXPERIMENT_ID).start(
        name=sc.name, experiment_id=EXPERIMENT_ID, sampling_config=ScorerSamplingConfig(sample_rate=rate))
    started.append(f"{sc.name} ({rate:.0%})")
print("scoring production traces with:\n  " + "\n  ".join(started))
