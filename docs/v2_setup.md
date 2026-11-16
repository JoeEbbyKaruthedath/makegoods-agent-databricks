# v2: full make-good lifecycle, per-user access

v2 runs next to the dev build, in its own schema, app, Lakebase branch, experiment and model, and deploys only from the
`feature/v2-lifecycle` branch (the `v2` target pins it). In a fresh clone, create that branch
(`git checkout -b feature/v2-lifecycle`) or remove `bundle.git.branch` from the `v2` target. What it adds:

- **Dollars that match:** every option shows its value against the missed spot (`value_reconciliation`), and the gap a
  partial refund would close.
- **Options A/B/C:** `package_builder` combines up to three avails per option, each with an 11-rule checklist. When no
  option is 10/10, the agent widens the search (`close_gap`).
- **The whole lifecycle in the app:**
  1. The seller picks options; the agent drafts the email, with facts from the rules engine and every number checked.
  2. A sales manager signs off, then the seller sends it.
  3. The agent waits for the client's reply, reads it, and the seller confirms before anything is booked.
- **Client rules from conversation:** "Ironwood never wants Network Prime Drama". Rules are saved per client and
  applied by the rules engine on every search.
- **Per-user access:**
  - The app acts as the signed-in person, and Unity Catalog row filters scope everything they see:
    - sellers see their clients
    - managers see their markets
    - admins see everything
  - Admins can view the app as someone else for up to two hours.
- **Morning preparation and digest** (job), **business results**, and **production monitoring** (10% of traces).

## Setup

Same tools as the main README (`databricks` CLI, `uv`), run from a checkout of `feature/v2-lifecycle`.

### 0. Workspace values for the v2 target
Put your values in `.databricks/bundle/v2/variable-overrides.json`. Git ignores this file; the pre-commit check
`tools/check_no_workspace_ids.py` blocks commits that contain these values.
```json
{"catalog": "<your_catalog>", "warehouse_id": "<warehouse_id>", "experiment_id": "<from step 6>", "genie_space_id": "<from step 7>",
 "mail_mode": "sandbox", "mail_redirect_to": "<demo inbox, e.g. you+mg@example.org>"}
```
```bash
export DATABRICKS_CONFIG_PROFILE=<your-profile> DATABRICKS_WAREHOUSE_ID=<warehouse_id>
CAT=<your_catalog>; SCH=makegoods_v2
alias mgsql='uv run --project agent_app python tools/run_sql.py --catalog $CAT --schema $SCH'
```

### 1. Schema, volumes and data
```sql
CREATE SCHEMA IF NOT EXISTS <your_catalog>.makegoods_v2;
CREATE VOLUME IF NOT EXISTS <your_catalog>.makegoods_v2.raw_landing;
CREATE VOLUME IF NOT EXISTS <your_catalog>.makegoods_v2.staging;
```
```bash
uv run --with numpy --with pandas python gen/gen_raw.py        # skip if data/raw already exists
uv run --with pandas python gen/gen_sellers.py                 # sales org, seller book, client contacts
databricks fs cp -r data/raw          dbfs:/Volumes/$CAT/$SCH/raw_landing --overwrite
databricks fs cp -r data/eval_labels  dbfs:/Volumes/$CAT/$SCH/raw_landing/eval_labels --overwrite
```

### 2. Access control and rule settings (before the pipeline)
```bash
mgsql uc_functions/00_config_and_core.sql
mgsql uc_functions/02_access_control.sql      # creates makegoods_v2_security: access map, row filter
mgsql uc_functions/05_near_miss_config.sql
mgsql uc_functions/06_value_package_config.sql
```

### 3. Pipeline
```bash
databricks bundle deploy -t v2                # pipeline, jobs; deploy fails on any other branch
databricks bundle run makegoods_sdp -t v2
```

### 4. Rule functions, lifecycle tables, metric views
```bash
mgsql uc_functions/10_agent_tools.sql
mgsql uc_functions/15_packages.sql
mgsql uc_functions/20_queue_tools.sql
mgsql uc_functions/30_oldway_tools.sql
mgsql uc_functions/50_lifecycle_tables.sql
mgsql uc_functions/52_client_emails.sql        # client replies + read_client_email (AI Functions: ai_classify, ai_extract)
mgsql uc_functions/53_client_email_samples.sql # 16 sample replies for the client inbox, read with AI Functions
for f in metric_views/makegoods_*.sql; do mgsql "$f"; done
```

### 5. Lakebase branches
```bash
for b in app-v2 dev-local-v2; do
  databricks postgres create-branch projects/makegoods $b \
    --json '{"spec": {"source_branch": "projects/makegoods/branches/production", "no_expiry": true}}'
done
```

### 6. Experiment (traces in Unity Catalog)
```bash
(cd agent_app && uv run python scripts/setup_experiment.py --catalog $CAT --schema $SCH --name /Users/<you>/makegoods-v2)
```

### 7. Genie space
```bash
python3 genie/build_space.py --catalog $CAT --schema $SCH
databricks genie create-space $DATABRICKS_WAREHOUSE_ID "$(cat genie/space.json)" --title "Make-Goods Analytics (v2)" -o json
```
Put the experiment and space ids in the overrides file.

### 8. Calibrated weights, evaluation set and the agent
The ranking weights are fitted to the experts' past decisions. On the synthetic data this lifts the expert's choice into
the top 3 from 68% to 95% of training decisions, so run it before evaluating:
```bash
(cd agent_app && MG_CATALOG=$CAT MG_SCHEMA=$SCH uv run python eval/calibrate_weights.py --apply)
(cd agent_app && MG_CATALOG=$CAT MG_SCHEMA=$SCH uv run python eval/build_eval_dataset.py)
databricks bundle deploy -t v2 && databricks bundle run register_agent -t v2
```

### 9. App, grants, first admin
```bash
databricks bundle deploy -t v2
SP=$(databricks apps get makegoods-v2 -o json | python3 -c "import json,sys; print(json.load(sys.stdin)['service_principal_client_id'])")
mgsql uc_functions/91_grants_v2.sql --param admin_user=<you@company.com> --param admin_name="<Your Name>" \
  --param app_sp=$SP --param users=<group or user>
databricks bundle run makegoods_agent -t v2
```
Workspace permissions for the people using the app:
- CAN USE on the app
- CAN USE on the SQL warehouse
- CAN RUN on the Genie space

### 10. Mail (optional; the default `sandbox` keeps mail inside the app)
Create the secret scope `makegoods-mail` and give the app's service principal READ on it. Then add these keys:
- `gmail_client_id`, `gmail_client_secret`, `gmail_refresh_token`: an OAuth credential with Gmail send and read scopes, used for nothing else.
- `gmail_sender`
- `ref_secret`: any random string.

Set `mail_mode` to `gmail` and `mail_redirect_to` to the demo inbox. Every synthetic client address is delivered there,
plus-addressed per advertiser, and anything not allowed is refused. Revoke the credential after the demo.

## Day-one checks
| | Check | How |
|---|---|---|
| S1 | The app acts as the signed-in person | Open the app as yourself: the header shows "Admin" and an identity picker. Pick *Jordan Price (seller)*: *My make-goods* shows only Ironwood Motors and the other clients Jordan owns. Pick *me (admin)* to see everything again |
| S2 | Mail reaches the client inbox | `mail_mode=gmail`: offer Option A on SPT-0058222, approve, send; it arrives in the demo inbox; reply "Option A works", then *Check for a reply now* in *Client inbox* and book it from the thread |
| S3 | Rule functions work for a seller with only the serving grants | Run `tests/access_matrix.py` with a seller identity. If `spot_context` fails with a permission error on a silver table, grant that person SELECT on `silver_ad_schedule_clean`, `silver_contract_obligations`, `silver_audience_mapped`, `ref_advertiser_contacts` and `ref_seller_book` |
| S4 | A thread waits for days and resumes | Send an offer, restart the app (`databricks bundle run makegoods_agent -t v2`), then reply: the reply still resumes the same thread |

## Tests
```bash
(cd agent_app && uv run python tests/lifecycle_local.py)           # the whole lifecycle with fakes, no workspace needed
(cd agent_app && uv run python eval/run_component_eval.py)         # client replies, client rules, offer emails (live model)
python tools/run_sql.py tests/v2_dollars_check.sql --catalog $CAT --schema $SCH --show
python tools/e2e_app.py --profile <profile>   # demo mail: four spots end to end in the deployed app (accept, decline, counter, question)
databricks bundle run production_monitoring -t v2   # start the production scorers (code checks on every trace, AI judges on 10%)
(cd agent_app && uv run python eval/production_report.py --days 7 --by-day)   # how the agent is doing on app traffic
uv run --project agent_app python tests/access_matrix.py --profiles <admin>,<seller>,<manager> --catalog $CAT --warehouse $DATABRICKS_WAREHOUSE_ID
```
