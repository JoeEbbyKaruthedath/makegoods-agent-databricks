# How it was built

This is the account of the build itself: the workflow, the AI tools that did the heavy lifting, and the judgment calls
made along the way, often after something didn't work the first time. The design is described in the
[README](../README.md); the proof that it runs is in [`evidence/`](../evidence/README.md).

## Workflow

I started from the use-case write-up and had **Genie** turn it into a phased implementation plan: a data foundation
first, then a seller-side matching agent, then the full make-good lifecycle. The plan set the order of the work, the
success measures (match quality, rules compliance, a token budget per make-good) and the risks to check early.

**Claude Code** was the coding agent for the whole build, working with the Databricks plugin skills for the CLI, Asset
Bundles, Apps, Lakebase and MLflow evaluation. It wrote most of the code, ran the CLI and SQL against the workspace, ran
the evaluations and dug into failures. My part was scope, design decisions, reviewing the business wording, and approving
anything that changed the workspace: schemas, grants, deployments.

Each layer followed the same loop: build it, test it on its own, then deploy it.

1. **Data.** A generator (`gen/`) writes synthetic source extracts, including a seeded "hero" scenario for demos and
   history with known expert decisions for evaluation. The Lakeflow pipeline (`src/makegoods_sdp/`) lands them bronze →
   silver → gold. It was checked with SQL story checks (`tests/phase0_story_checks.sql`).
2. **Rules.** Unity Catalog SQL functions (`uc_functions/`) for audience mapping, inventory search, scoring, packages and
   dollar reconciliation. Each was checked with SQL tests against the hero spot (`tests/tools_hero_check.sql`,
   `tests/v2_dollars_check.sql`).
3. **Agent.** A LangGraph graph that calls those functions as tools. It was tested locally with fakes for every
   workspace dependency (`agent_app/tests/lifecycle_local.py`), then evaluated with MLflow GenAI evaluation on a dataset
   built from historical expert decisions (`agent_app/eval/`).
4. **App.** A FastAPI app with a static UI. Before each deploy I checked the UI against a mock backend with headless-browser
   screenshots, then deployed it with the bundle.
5. **End to end.** Four make-goods run through the live app's API (`tools/e2e_app.py`), with the production scorers run on
   the resulting traces.

The scope grew after a working session with the ad-ops and engineering team. Their asks drove the second phase:
- dollars that match the missed spot
- several options per make-good
- manager sign-off
- client rules stated in conversation
- each seller seeing only their own markets
- options prepared overnight
- the client email loop

I built that phase on its own branch, in its own schema, app and Lakebase branch, so the first version stayed live as a
fallback.

## AI tools and where they carried the weight

| Tool | What it did |
|---|---|
| Genie | Turned the use-case write-up into the phased implementation plan. In the product, it answers analytics questions over the metric views. |
| Claude Code with the Databricks plugin skills | Wrote and debugged most of the code, SQL and bundle configuration; ran the CLI, SQL checks, evaluations and end-to-end tests |
| Foundation Model APIs | The agent's model (Claude Sonnet) for routing, explanations and email prose. A different model family (GPT-5.5) acts as the judge for business reasoning. |
| AI Functions | `ai_classify` and `ai_extract` read client replies in SQL |
| MLflow GenAI evaluation | Code scorers and LLM judges as the release gate, and as production monitors |

## Judgment calls made during the build

**LangGraph instead of the SDK in the plan.** The plan named the OpenAI Agents SDK. I switched to LangGraph for two
reasons:
- Every decision shows up as a named step in the MLflow trace, which is what reviewers and evaluators look at.
- Its interrupts let a make-good pause for days (for the manager, then the client) and resume from a checkpoint in
  Lakebase.

**Business rules in SQL, and the cost of getting the SQL right.** Every rule runs as a Unity Catalog function, and the
model only orchestrates and writes. Two things didn't work at first:
- **Speed.** The first package builder (Options A/B/C) was too slow, because each reference to the scoring function in
  the query ran it again. I rewrote it to score candidates once into an array and choose packages with window functions.
- **A UC SQL restriction.** Unity Catalog SQL functions reject an aggregate that mixes a parameter and a column
  reference, so the "fixed spot" flag is computed per row before aggregating.

**Row filters only on leaf tables.** Per-user access uses Unity Catalog row filters keyed to an access map. The filters
sit only on gold tables that nothing downstream reads. A filter on an upstream table would also have filtered the
pipeline and the jobs, which run as different identities. Spot-level functions join a filtered access table, so a spot a
seller can't see simply returns nothing. One surprise: a row filter also blocks updates from an identity with no access
row, so the Unity Catalog copy of client rules is append-only.

**The app acts as the signed-in person.** I chose real per-user access over an app-level check:
- The app forwards the user's token, so Unity Catalog decides what each person sees.
- The token never goes into the agent's state, which is persisted to Postgres.
- Writes that must happen whoever is signed in, such as decision events and traces, go through the app's service
  principal.
- Switching identity in the demo first failed with a Delta concurrent-update conflict on the access map. Those writes are
  now serialized and retried.

**AI Functions instead of a free-form parser for client replies.** The first version read replies with an LLM prompt. I
moved replies into a Delta table read in SQL with `ai_classify` and `ai_extract`, so every reading is stored, auditable
and can be re-run in batch; the LLM remains the fallback. My first labels read "please just credit the spot" as a yes, so
I rewrote them as descriptive phrases ("declines the offer, or asks for a refund or a credit"). Reply reading now
scores 95–100% across runs on the 20-reply test set, and resists all four prompt-injection replies.

**A data-type bug that showed the wrong checkmark.** Nested values from the SQL Statement Execution API come back as
strings. "false" is a truthy string, so failed rules showed a ✓ in the checklist. I added type coercion from the
result's schema before anything reaches the UI.

**Keeping the model inside a token budget.** The release gate requires every recommendation to stay under a token
budget. Explanations were running 126 to 211 words against a 110-word instruction, pushing some cases over. I moved the
summaries of the other options and the search statistics into code, leaving the model only the business case for
Option A, and turned off extended reasoning on the short steps. Average tokens per make-good in the evaluation
runs fell from about 1,870 to about 1,480.

**Fixing the judge, not lowering the bar.** The release gate failed three registrations in a row on the business-reason
judge, at 0.897 against a 0.90 threshold. Reading the four failures:
- **A real defect.** The agent couldn't describe Option A when its spots weren't among the top-ranked single avails,
  because it never saw their dates and programs. I passed those details to it. That fix raised average tokens to about
  1,950, still within the budget.
- **A self-contradicting judge.** The other failures were the judge penalizing explanations for naming the rules an
  option misses, which its own guideline allows. I moved that one judge to a model from a different family than the
  agent's. Before trusting it, I checked that it still failed deliberately weak explanations.

The gate then passed (version 7) without changing the threshold.

**Testing the monitors on real traces before turning them on.** When I ran the production scorers locally on real app
traces, they returned nothing at all, because span data comes back from trace storage as JSON text, not objects. I fixed
the scorers to decode it before starting monitoring. Otherwise monitoring would have run on every trace and silently
scored none.

**Keeping workspace values out of the repo.** The repo carries only placeholders. Workspace values live in a git-ignored
overrides file that the bundle reads. A pre-commit check blocks any staged change that contains one of those values, a
workspace host, a token, or a listed name. The evidence logs pass through a redaction script before they're committed.

## What I would build next

- **Column masks.** Mask contract pricing fields for roles outside finance, reusing the access map behind the row
  filters.
- **Drift alerts.** Turn the production scorers into a feedback loop: when `business_rationale` or `dollar_math` drops
  below its gate on live traffic, trigger the registration job's evaluation and alert the owner.
- **An acceptance model.** Grow the learned advertiser preferences into a small model that predicts each option's chance
  of acceptance, giving the agent a quantitative signal next to the rule checklist.
