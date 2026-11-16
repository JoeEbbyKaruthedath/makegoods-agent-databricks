"""Evaluate the steps that read or write text outside the rules engine, one at a time (no graph, no workspace data):

  reply   parse_client_reply: client emails read with AI Functions in SQL (read_client_email), kept to the options offered
  rule    parse_request: client rules stated in conversation ("they only take Tuesdays and Thursdays")
  offer   draft_offer: the email prose, with every number checked against the options offered

  python eval/run_component_eval.py [--only reply,rule,offer] [--endpoint databricks-claude-sonnet-5-5]

Cases are in eval/component_cases.py. Each kind is a named MLflow evaluation run; exit code 1 when COMPONENT_GATE fails.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
os.environ.setdefault("MLFLOW_GENAI_EVAL_MAX_WORKERS", "8")

import mlflow  # noqa: E402
from databricks_langchain import ChatDatabricks  # noqa: E402
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from agent_server import offer_email, prompts, reply_parser  # noqa: E402
from agent_server import rules as R  # noqa: E402
from agent_server.config import LLM_ENDPOINT, PROMPT_OFFER, PROMPT_PARSE, fq, llm_params  # noqa: E402
from agent_server.mail.matching import own_text  # noqa: E402
from agent_server.uc_tools import _execute  # noqa: E402
from agent_server.graph import _parse_json  # noqa: E402
from agent_server.steps import DAYPARTS, TAGS, _text  # noqa: E402
from eval.component_cases import package, records  # noqa: E402
from eval.scorers import COMPONENT_GATE, COMPONENT_SCORERS  # noqa: E402

GENRES = ["Entertainment", "News", "Sports", "Syndicated", "Talk/Lifestyle"]


def reply_predict(llm=None):
    """The app's path: the reply read in SQL with AI Functions (read_client_email), then checked by from_ai_read."""
    def predict_fn(email: str, offered: list, case_type: str = "reply"):
        text = own_text(email)
        read = _execute(f"SELECT {fq('read_client_email')}(:body) AS r", {"body": text})[0]["r"]
        return reply_parser.from_ai_read(read, text, offered, dayparts=DAYPARTS)
    return predict_fn


def rule_predict(llm):
    def predict_fn(text: str, case_type: str = "rule"):
        template, _ = prompts.load(PROMPT_PARSE)
        system = prompts.render(template, tags=", ".join(TAGS), dayparts=", ".join(DAYPARTS),
                                current_spot="SPT-0058222 (Ironwood Motors)")
        p = _parse_json(_text(llm.invoke([SystemMessage(system), HumanMessage(text)]).content), text)
        draft = p.rule.model_dump() if p.rule else {}
        # programs are matched case-insensitively here; in the app program_lookup resolves them against inventory
        rule, why = R.normalize(draft, tags=TAGS, genres=GENRES, programs=[v.title() for v in draft.get("values") or []])
        return {"intent": p.intent, "rule": rule or draft, "why": why}
    return predict_fn


def offer_predict(llm):
    def predict_fn(spot: dict, labels: list, case_type: str = "offer"):
        packages = [package(l, spot["unit_rate_usd"], spot["owed_impressions"]) for l in labels]
        f = offer_email.facts(spot, packages)
        template, _ = prompts.load(PROMPT_OFFER)
        raw = llm.invoke([SystemMessage(template), HumanMessage(json.dumps(offer_email.prose_request(spot, f)))])
        prose, problems = offer_email.clean_prose(_text(raw.content), f)
        subject, text, _ = offer_email.render(f, prose, "TESTREF2")
        return {"email": f"Subject: {subject}\n\n{text}", "ungrounded": offer_email.check_grounding(text, f, "TESTREF2"),
                "prose_problems": problems}
    return predict_fn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="reply,rule,offer")
    ap.add_argument("--endpoint", default=LLM_ENDPOINT)
    a = ap.parse_args()
    mlflow.set_experiment(experiment_id=os.environ["MLFLOW_EXPERIMENT_ID"])
    llm = ChatDatabricks(endpoint=a.endpoint, extra_params=llm_params(a.endpoint))
    predictors = {"reply": reply_predict, "rule": rule_predict, "offer": offer_predict}
    cases, metrics = records(), {}
    for kind in a.only.split(","):
        with mlflow.start_run(run_name=f"component-{kind}"):
            mlflow.set_tags({"eval.component": kind, "eval.cases": len(cases[kind])})
            mlflow.log_params({"llm_endpoint": a.endpoint, "n_cases": len(cases[kind])})
            res = mlflow.genai.evaluate(data=cases[kind], predict_fn=predictors[kind](llm), scorers=COMPONENT_SCORERS[kind])
        metrics.update(res.metrics)
        for k in sorted(res.metrics):
            if k.endswith("/mean"):
                print(f"  {kind:<6} {k:<36} {res.metrics[k]:.3f}")
    failed = {k: (metrics.get(k), t) for k, t in COMPONENT_GATE.items() if k.split("/")[0] in
              {s.name for kind in a.only.split(",") for s in COMPONENT_SCORERS[kind]} and (metrics.get(k) is None or metrics[k] < t)}
    print("\nCOMPONENT GATE:", "PASSED" if not failed else f"FAILED {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
