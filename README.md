# Make-Goods Agent — Phase 0 + Phase 1 reference build

A seller-side **make-good agent** for broadcast ad operations.
- **Input:** a preempted or missed spot.
- **Output:** ranked replacement inventory from the station's own avails, each scored out of 10. Below 4/10 the agent shows the options and recommends a refund.
- **Review:** an ad-ops reviewer accepts, modifies or rejects the recommendation.
- **Learning:** the agent learns advertiser preferences from those decisions.

It is built to show *how to build agents on Databricks*:

| Concern | Databricks building block | Where |
|---|---|---|
| Data foundation | Lakeflow Spark Declarative Pipeline (Auto Loader → silver expectations → gold) | `src/makegoods_sdp/` |
| Business rules | Unity Catalog SQL functions (deterministic, governed); thresholds and weights in the `mg_rules_config` table | `uc_functions/` |
| Agent | LangGraph state machine (13 nodes, 2 call an LLM) packaged as an MLflow `ResponsesAgent` | `agent_app/agent_server/graph.py`, `makegoods_model.py` |
| Open questions | Genie Space over 4 UC metric views, called through the Genie Conversation API | `metric_views/`, `genie/`, `agent_app/agent_server/genie.py` |
| Memory + human review | Lakebase Postgres: LangGraph checkpointer (pause/resume), preference store, review queue, decision log | `agent_app/agent_server/memory.py` |
| Observability | MLflow Tracing stored in Unity Catalog tables: one span per decision step, UC tool calls, LLM tokens, reviewer feedback | `agent_app/agent_server/uc_tools.py`, `graph.py` |
| Quality | MLflow GenAI evaluation on a UC dataset: code scorers, LLM judges, cost from traces, release gate | `agent_app/eval/` |
| Versioning | Agent registered as a Unity Catalog model; the `@champion` alias is what the app serves | `agent_app/notebooks/01_register_agent.py` |
| Serving + UI | Databricks App (FastAPI + static UI) for ad-ops users: work queue, review queue, decision log, live decision trail. It uses business wording only; `app_shell/business_view.py` translates the agent's technical step messages, and the full detail stays in the MLflow traces | `app/` |

The LLM makes **1–2 calls per make-good**, about 1.4K tokens, roughly 5% of the inherited GPT-assist matcher's ~28K. Every rule runs in SQL.

### v2: the full make-good lifecycle, per-user access
The `v2` bundle target (branch `feature/v2-lifecycle`) adds, in its own schema, app and Lakebase branch:
- **Dollars that match:** each option's value against the missed spot, with any partial refund owed.
- **Options A/B/C:** multi-spot packages with an 11-rule checklist, and a gap-closing search.
- **The lifecycle in the app:** offer email → manager sign-off → send → the client's reply read by the agent → seller confirms → booked.
- **Client rules from conversation:** "Ironwood never wants Network Prime Drama".
- **Per-user access:** Unity Catalog row filters, the app acting as the signed-in person, and an admin "view as".
- **Operations:** a morning preparation job and seller digest, business results, and production monitoring.

Setup, checks and tests: [docs/v2_setup.md](docs/v2_setup.md).

## Layout
```
databricks.yml                 bundle: variables (catalog, schema, warehouse, app name, Lakebase branch, mail) + dev and v2 targets
resources/                     bundle resources: SDP pipeline, agent registration, daily preparation, monitoring, Databricks App
gen/gen_raw.py                 synthetic source extracts (simulation clock: 2026-10-30)
gen/gen_sellers.py             sales org, seller book (who owns each client) and client contacts
src/makegoods_sdp/             SDP pipeline: bronze_* / silver_* / gold_*
uc_functions/                  00_ rules config + core rules · 02_ access control (row filter) · 05_ tiered-score penalties
                               06_ dollar and package settings · 10_ agent tools · 15_ packages, gap fillers, lookups
                               20_ queue tools · 30_ "old way" baseline tools · 50_ lifecycle tables
                               52/53_ client replies read with AI Functions (+ samples) · 90/91_ grants
metric_views/                  4 governed metric views (the Genie Space's sources) + sample queries
genie/build_space.py           Genie Space definition (writes genie/space.json)
agent_app/                     the agent (its own uv project)
  agent_server/                graph, offer lifecycle and client-rule nodes, UC tool caller, prompts, memory, mail
                               adapters + reply watcher, Genie client, ResponsesAgent wrapper
  eval/                        eval dataset builder, scorers + release gate, predict adapters, local and component eval runners
  notebooks/01_register_agent.py   log → register → smoke test → evaluate → tag scores → move @champion
  notebooks/02_, 03_, 04_      daily preparation, seller digest, production monitoring
  scripts/setup_experiment.py  creates the MLflow experiment with Unity Catalog trace storage
  tests/                       smoke_graph, hitl_flow, genie_check, inspect_trace, lifecycle_local (no workspace needed)
app/                           the Databricks App: loads makegoods_agent@champion, acts as the signed-in person, serves the UI
docs/                          discovery questionnaire, commensurate rules, pacing feasibility (go/no-go)
notebooks/00_pacing_feasibility.sql
tests/                         data story checks, hero-spot tool and dollar checks, access matrix
tools/run_sql.py               runs a multi-statement .sql file on a SQL warehouse (--catalog/--schema/--param)
tools/check_no_workspace_ids.py   pre-commit check: no workspace-specific values in commits
tools/replace_value.py         swaps a workspace-specific value (catalog, IDs) across the repo
```

---

## Build it in your workspace

About an hour end to end. Most of that is waiting on the pipeline, the registration job and the app deploy.

### Prerequisites

**Workspace features**
- Unity Catalog, plus a catalog where you can `CREATE SCHEMA` (or an existing schema you own).
- Serverless compute for jobs, notebooks and Lakeflow pipelines.
- A Pro or Serverless SQL warehouse.
- Databricks Apps.
- Lakebase Postgres (Autoscaling).
- AI/BI Genie and UC metric views.
- MLflow trace storage in Unity Catalog. If `setup_experiment.py` (step 6) fails, check that this is enabled under the workspace's Previews.
- Foundation Model API endpoint `databricks-claude-sonnet-5-5`. To use a different model, see *Change the LLM* below. `databricks-claude-haiku-5-5` is needed only for the optional Haiku eval variant.
- MLflow Prompt Registry is **optional**. Without it the agent uses the prompts in `agent_server/prompts.py`.

**Local tools**
- [Databricks CLI](https://docs.databricks.com/dev-tools/cli/install.html) v1.17 or later, authenticated with `databricks auth login --host <workspace-url> --profile <profile>`.
- [uv](https://docs.astral.sh/uv/) with Python 3.11+. No lock file is shipped; the first `uv sync` resolves the dependencies from PyPI.
- `git`.

### 0. Clone and point the code at your workspace

The code ships with placeholder tokens (`YOUR_...`) for everything tied to a specific workspace. Replace each with your value. Three of them only exist after you create the objects, so you'll replace those in the step that creates them.

| Value in the repo | What it is | Replace |
|---|---|---|
| `YOUR_CATALOG` | Unity Catalog catalog | now |
| `https://YOUR_WORKSPACE_HOST` | workspace URL (`databricks.yml`) | now |
| `YOUR_WAREHOUSE_ID` | SQL warehouse ID | now |
| `YOUR_USER` | your workspace user name, for the MLflow experiment path (`agent_app/scripts/setup_experiment.py`) | now |
| `YOUR_EXPERIMENT_ID` | MLflow experiment ID | step 6 |
| `YOUR_GENIE_SPACE_ID` | Genie Space ID | step 7 |
| `YOUR_APP_SP_CLIENT_ID` | the app's service principal (client ID) | step 11 |

The defaults are the schema `makegoods` and the Lakebase project `makegoods`. To use other names, set the `schema` and `lakebase_project` bundle variables (for example in `.databricks/bundle/<target>/variable-overrides.json`) and pass `--schema` to `tools/run_sql.py`.

```bash
git clone <this repository's URL> makegoods-agent && cd makegoods-agent

# replace a value in every file (plain text, no regex); prints what it changed
alias replace='python3 tools/replace_value.py'
replace YOUR_CATALOG                       <your_catalog>
replace https://YOUR_WORKSPACE_HOST        https://<your-workspace-url>
replace YOUR_WAREHOUSE_ID                  <your_warehouse_id>
replace YOUR_USER                          <you@your-company.com>

# used by every command below
export DATABRICKS_CONFIG_PROFILE=<your-profile>
export DATABRICKS_WAREHOUSE_ID=<your_warehouse_id>
alias mgsql='uv run --project agent_app python tools/run_sql.py'    # runs a .sql file on the warehouse
CAT=<your_catalog>; SCH=makegoods
```

### 1. Create the schema and landing volumes
Run these in a SQL editor, or with `databricks experimental aitools tools query "<statement>"`:
```sql
CREATE SCHEMA IF NOT EXISTS <your_catalog>.makegoods;
CREATE VOLUME IF NOT EXISTS <your_catalog>.makegoods.raw_landing;  -- Auto Loader source
CREATE VOLUME IF NOT EXISTS <your_catalog>.makegoods.staging;      -- day-2 files for the incremental-load demo
```

### 2. Generate and upload the synthetic data
`gen_raw.py` writes `data/raw` (the source extracts), `data/eval_labels` (ad-ops audience labels used by evaluation) and `data/staging/day2` (a held-back batch). The `data/` folder is gitignored, so you always generate it locally.
```bash
uv run --with numpy --with pandas python gen/gen_raw.py
databricks fs cp -r data/raw          dbfs:/Volumes/$CAT/$SCH/raw_landing --overwrite
databricks fs cp -r data/eval_labels  dbfs:/Volumes/$CAT/$SCH/raw_landing/eval_labels --overwrite
databricks fs cp -r data/staging/day2 dbfs:/Volumes/$CAT/$SCH/staging/day2 --overwrite
```

### 3. Create the rules config (the pipeline uses these functions)
```bash
mgsql uc_functions/00_config_and_core.sql
mgsql uc_functions/05_near_miss_config.sql
```

### 4. Deploy the bundle and run the pipeline
The app needs the experiment, Genie Space and Lakebase branch, which don't exist yet. So the first deploy leaves the app out. It creates the pipeline and the registration job.
```bash
mv resources/makegoods_agent.app.yml resources/makegoods_agent.app.yml.later
databricks bundle deploy -t dev
databricks bundle run makegoods_sdp -t dev        # bronze → silver → gold, a few minutes on serverless
```

### 5. Agent tools, metric views and data checks
These read the gold tables, so they run after the pipeline:
```bash
mgsql uc_functions/10_agent_tools.sql             # rules engine, scorer, shortfall, formatter (the agent's tools)
mgsql uc_functions/20_queue_tools.sql
mgsql uc_functions/30_oldway_tools.sql            # only for the "old way" evaluation baseline
for f in metric_views/makegoods_*.sql; do mgsql "$f"; done
mgsql tests/phase0_story_checks.sql --show        # optional: row counts and the data story
mgsql tests/tools_hero_check.sql --show           # optional: the hero spot SPT-0058222 through each tool
```

### 6. MLflow experiment with traces in Unity Catalog
This creates the experiment and the trace tables (`mg_agent_otel_*`) in your schema. A trace location is permanent for an experiment.
```bash
(cd agent_app && uv sync && uv run python scripts/setup_experiment.py)   # prints experiment_id
replace YOUR_EXPERIMENT_ID <new_experiment_id>
```

### 7. Genie Space
```bash
python3 genie/build_space.py                      # writes genie/space.json from the metric views + instructions
databricks genie create-space $DATABRICKS_WAREHOUSE_ID "$(cat genie/space.json)" \
  --title "Make-Goods Analytics" -o json  # note the space_id in the output
replace YOUR_GENIE_SPACE_ID <new_space_id>
```
To change the space later, edit `genie/build_space.py`, re-run it, then run `databricks genie update-space <space_id> --serialized-space "$(cat genie/space.json)"`.

### 8. Lakebase (memory, review queue, decision log)
There is one project with two branches, so that local runs never own the app's tables:
- `app-dev`: used by the deployed app. Its service principal creates and owns the schemas on first start.
- `dev-local`: used by your laptop.
```bash
databricks postgres create-project makegoods --json '{"spec": {"display_name": "Make-Goods"}}'
for b in app-dev dev-local; do
  databricks postgres create-branch projects/makegoods $b \
    --json '{"spec": {"source_branch": "projects/makegoods/branches/production", "no_expiry": true}}'
done
databricks postgres list-endpoints projects/makegoods/branches/app-dev     # expect .../endpoints/primary
databricks postgres list-endpoints projects/makegoods/branches/dev-local
```
If a branch has no endpoint, add a read-write endpoint named `primary` with `databricks postgres create-endpoint projects/makegoods/branches/<branch> primary`. Run it with `-h` to see the spec.

### 9. Local config, evaluation dataset and a smoke test
```bash
cd agent_app
cp .env.example .env                               # fill in profile, experiment ID, warehouse ID, Genie Space ID
uv run python eval/build_eval_dataset.py           # registers the UC evaluation dataset <catalog>.<schema>.mg_eval_set
uv run python tests/smoke_graph.py                 # hero spot end to end; prints the decision trail
uv run python tests/genie_check.py                 # optional: Genie Space answers through the Conversation API
uv run python tests/hitl_flow.py                   # optional: pause → reviewer modify → preference learned (dev-local branch)
cd ..
```

### 10. Register the agent (notebook job)
`agent_app/notebooks/01_register_agent.py` runs these steps:
1. Logs the agent as an MLflow `ResponsesAgent`, along with the resources it uses.
2. Registers a new version of `<catalog>.<schema>.makegoods_agent`.
3. Smoke-tests that version.
4. Evaluates it on `mg_eval_set` (40 cases by default).
5. Saves each score as a `score_<scorer>` tag on the version.
6. Points `@champion` at the version.
```bash
databricks bundle deploy -t dev                    # picks up the experiment and Genie IDs you replaced
databricks bundle run register_agent -t dev        # ~10 minutes; the run page shows each cell's output
```
By default the alias moves even if the release gate fails (`require_gate=false`), and the version is tagged `release_gate=failed`. To promote only when the gate passes, set the `require_gate` widget to `true`. You can do that by running the notebook interactively, or by adding the widget to the job's `parameters` in `resources/register_agent.job.yml`.

### 11. Deploy the app
```bash
mv resources/makegoods_agent.app.yml.later resources/makegoods_agent.app.yml
databricks bundle deploy -t dev                    # creates the app and its service principal
databricks apps get makegoods-dev -o json | python3 -c "import json,sys; print(json.load(sys.stdin)['service_principal_client_id'])"
replace YOUR_APP_SP_CLIENT_ID <service_principal_client_id>
mgsql uc_functions/90_grants_app.sql               # read data, execute the tools (and the model), write traces
databricks bundle run makegoods_agent -t dev       # deploys the code and starts the app; prints the URL
```
Apply the grants **before** the app starts. At startup it loads `makegoods_agent@champion` from Unity Catalog, which needs `EXECUTE` on the schema. Then open the URL:
- The *Overview* tab shows the open work queue's revenue at risk, deadlines and refund history.
- To confirm which agent version is being served, run `databricks apps logs makegoods-dev | grep "loaded agent"`. It should show version 1 at `@champion`. The app itself shows no model or platform details.
- *Find make-good* on SPT-0058222 streams the decision trail.

---

## Change the agent and promote a new version
1. Edit the code in `agent_app/agent_server/`. Optionally evaluate locally first: `cd agent_app && uv run python eval/run_eval.py --variant v1-sonnet --limit 40`. That creates a run in the experiment's *Evaluation* tab.
2. `databricks bundle deploy -t dev && databricks bundle run register_agent -t dev`: registers, evaluates and promotes the next version.
3. `databricks bundle run makegoods_agent -t dev`: restarts the app on the new `@champion`.

To roll back, point the alias at an earlier version (Catalog Explorer → model → *Aliases*), then restart the app.

**Tune the rules without code.** Thresholds, soft-match weights and near-miss penalties are rows in `<catalog>.<schema>.mg_rules_config`. The UC functions read them on every call. `docs/commensurate_rules.md` explains each key, and lists the question to settle with ad ops.

**Change the LLM.** Replace `databricks-claude-sonnet-5-5` in these places:
- `resources/makegoods_agent.app.yml`: the `MG_LLM_ENDPOINT` value and the serving-endpoint resource.
- `agent_app/agent_server/config.py`.
- `agent_app/.env`.

Then register and evaluate a new version, and compare its scores before promoting.

**Prompt Registry.** If it is enabled in your workspace:
1. Run `cd agent_app && uv run python scripts/register_prompts.py`.
2. Set `MG_USE_PROMPT_REGISTRY` to `true` in the app config and `.env`.

Prompt edits then go live within 5 minutes without a redeploy, and each trace records the prompt version it used.

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| App shows *500 Internal Server Error*, or fails to start with a permission error | The service principal is missing its grants: rerun step 11's `90_grants_app.sql` (also needed after recreating the app). |
| App fails to start: alias `champion` not found | Run the registration job (step 10) before deploying the app. |
| `permission denied for schema makegoods…` (Lakebase) | A local run used the app's branch. Point `agent_app/.env` at the `dev-local` branch. The app's service principal must own the schemas on `app-dev`. |
| `SQL warehouse ID is required for accessing traces in UC tables` | Set `MLFLOW_TRACING_SQL_WAREHOUSE_ID` (in `.env` locally; the notebook and the app already set it). |
| Evaluation stalls with no progress | Many parallel score writes, each re-authenticating. The notebook pins credentials once. Locally, `eval/run_eval.py` does the same; reuse its `pin_oauth_token()` in any new eval script. |
| `Prompt registry is not enabled` warning | Harmless: the agent falls back to the prompts in code. |
| App build fails resolving a package version | The bundle doesn't upload `uv.lock` files (`databricks.yml` → `sync.exclude`); the app resolves dependencies at deploy time. |

---

## Demo script
1. **The problem.**
   - Work queue: $234K at risk.
   - Prime refunds: from `gold_make_good_history`.
   - Old GPT tool: about 28K tokens per offer.
2. **Find a make-good.**
   - Spot: SPT-0058222, Ironwood Motors, KPBT Nashville, preempted by a college-football overrun.
   - Watch the decision trail stream in.
   - The rules engine evaluates 498 avails; 8 are commensurate.
   - Ranked options come back with match scores out of 10 and impression math.
3. **Open the trace.** It has named steps, each UC function call and its result, 1–2 LLM calls with tokens, and the guardrail check.
4. **Human review.**
   - The reviewer picks the sports option instead of #1; the graph resumes from its saved state in Lakebase.
   - The decision is logged, preferences are updated, and the feedback is attached to the trace.
5. **Memory.** Find a make-good for SPT-0058223, the same advertiser. Sports now ranks first.
6. **Ask a question.** "Which markets have the most make-good revenue at risk?" goes to the Genie Space; open the generated SQL.
7. **Evaluation and versioning.**
   - MLflow experiment → *Evaluation* runs: per-case judge rationales, and a link from each row to its trace.
   - Catalog Explorer → `makegoods_agent`: the versions, their `score_*` tags and `@champion`.
8. **Phase 2 teaser:** pacing feasibility plus the `ai_forecast` smoke test (`notebooks/00_pacing_feasibility.sql`, `docs/pacing_feasibility.md`).
9. **Incremental load (optional).** Copy `staging/day2` into `raw_landing` and re-run the pipeline. Auto Loader picks up only the new files.
