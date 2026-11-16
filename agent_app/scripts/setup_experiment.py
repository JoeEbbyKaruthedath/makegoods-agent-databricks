"""Create the agent's MLflow experiment with traces stored in Unity Catalog Delta tables.

Traces land in <catalog>.<schema>.mg_agent_otel_* so they can be queried with SQL next to the business data.
A UC trace location is permanent per experiment.

  python scripts/setup_experiment.py [--catalog <catalog>] [--schema makegoods_v2] [--name /Users/<you>/makegoods-v2]
"""
import argparse
import os

import mlflow
from mlflow.entities.trace_location import UnityCatalog

ap = argparse.ArgumentParser()
ap.add_argument("--catalog", default=os.environ.get("MG_CATALOG", "YOUR_CATALOG"))
ap.add_argument("--schema", default=os.environ.get("MG_SCHEMA", "makegoods"))
ap.add_argument("--name", default="/Users/YOUR_USER/makegoods-agent")
a = ap.parse_args()

PROFILE = os.environ.get("DATABRICKS_CONFIG_PROFILE", "DEFAULT")
LOCATION = UnityCatalog(catalog_name=a.catalog, schema_name=a.schema, table_prefix="mg_agent")

mlflow.set_tracking_uri(f"databricks://{PROFILE}")
os.environ.setdefault("MLFLOW_TRACING_SQL_WAREHOUSE_ID", os.environ.get("MG_WAREHOUSE_ID", "YOUR_WAREHOUSE_ID"))

exp = mlflow.get_experiment_by_name(a.name)
if exp is None:
    exp_id = mlflow.create_experiment(name=a.name, trace_location=LOCATION)
    exp = mlflow.get_experiment(exp_id)
print("experiment_id:", exp.experiment_id)
print("trace_location:", exp.trace_location)
