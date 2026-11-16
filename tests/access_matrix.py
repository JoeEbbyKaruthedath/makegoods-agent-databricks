"""Who sees what: run the same queries as several identities and compare (row filters + rule functions).

Each identity is a Databricks CLI profile (a user, or a test service principal with its own OAuth secret).

  uv run --project agent_app python tests/access_matrix.py --profiles admin,seller,manager [--catalog <c>] [--schema makegoods_v2]

Expect: the admin sees every market; a seller sees only their own clients (Jordan Price: Ironwood Motors in
Nashville, ...); a manager sees their region's markets; the app's own service principal and anyone without an access
row see nothing; spot_context of another seller's spot returns no row.
"""
import argparse

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

HERO = "SPT-0058222"


def run(w, warehouse, sql):
    r = w.statement_execution.execute_statement(statement=sql, warehouse_id=warehouse, wait_timeout="50s")
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        return f"error: {(r.status.error.message if r.status.error else r.status.state)[:80]}"
    return (r.result.data_array or [[None]])[0][0] if r.result else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--schema", default="makegoods_v2")
    ap.add_argument("--warehouse", required=True)
    a = ap.parse_args()
    fq = lambda n: f"{a.catalog}.{a.schema}.{n}"  # noqa: E731
    checks = {
        "role": f"SELECT concat_ws(' ', role, seller_id, array_join(markets, ',')) FROM {a.catalog}.{a.schema}_security.mg_my_access",
        "queue spots": f"SELECT count(*) FROM {fq('gold_make_good_candidates')}",
        "markets": f"SELECT count(DISTINCT market) FROM {fq('gold_make_good_candidates')}",
        "clients": f"SELECT count(DISTINCT advertiser_id) FROM {fq('gold_make_good_candidates')}",
        f"spot_context({HERO})": f"SELECT count(*) FROM {fq('spot_context')}('{HERO}')",
        "options for hero": f"SELECT count(*) FROM {fq('inventory_search')}('{HERO}')",
        "decisions": f"SELECT count(*) FROM {fq('mg_decision_events')}",
        "contracts (silver, not granted)": f"SELECT count(*) FROM {fq('silver_contract_obligations')}",
    }
    rows = []
    for p in a.profiles.split(","):
        w = WorkspaceClient(profile=p)
        who = w.current_user.me().user_name
        rows.append((p, who, {k: run(w, a.warehouse, q) for k, q in checks.items()}))
    width = max(len(k) for k in checks)
    print(f"{'':<{width}}  " + "  ".join(f"{p:<28}" for p, _, _ in rows))
    print(f"{'identity':<{width}}  " + "  ".join(f"{str(w)[:28]:<28}" for _, w, _ in rows))
    for k in checks:
        print(f"{k:<{width}}  " + "  ".join(f"{str(r[k])[:28]:<28}" for _, _, r in rows))


if __name__ == "__main__":
    main()
