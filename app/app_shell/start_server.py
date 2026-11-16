"""Databricks App entry point: serves the make-goods agent registered in Unity Catalog.

At startup the app loads MG_AGENT_MODEL_URI (e.g. models:/<catalog>.<schema>.makegoods_agent@champion)
from the MLflow registry, attaches Lakebase memory, and serves it at /invocations + /responses
(MLflow agent server) and to the review UI. Traces are linked to the loaded model version. Every request runs
as the signed-in person (app_shell.auth), and a background task picks up clients' email replies (agent_server.watcher).
Promote a new version by moving the alias and restarting the app. Without MG_AGENT_MODEL_URI
(local development) it runs the local agent code from ../agent_app instead.
"""
# ruff: noqa: E402
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / "agent_app" / ".env", override=False)

import mlflow
from mlflow import MlflowClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_agent():
    uri = os.getenv("MG_AGENT_MODEL_URI")
    if not uri:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent_app"))
        from agent_server.makegoods_model import MakeGoodsAgent

        logger.info("MG_AGENT_MODEL_URI not set: running the local agent code (development mode)")
        return MakeGoodsAgent(), {"source": "local code"}
    mlflow.set_registry_uri("databricks-uc")
    model = mlflow.pyfunc.load_model(uri)  # downloads the registered agent code and puts it on sys.path
    info = {"source": "registry", "uri": uri, "model_id": model.metadata.model_id}
    name, _, alias = uri.removeprefix("models:/").partition("@")
    if alias:
        mv = MlflowClient(registry_uri="databricks-uc").get_model_version_by_alias(name, alias)
        info.update(name=name, alias=alias, version=mv.version, model_id=getattr(mv, "model_id", None) or info["model_id"])
    if info.get("model_id"):
        mlflow.set_active_model(model_id=info["model_id"])  # every trace from this app links to the model version
    logger.info("loaded agent %s", info)
    return model.get_raw_model(), info  # the MakeGoodsAgent instance (ResponsesAgent models don't support unwrap_python_model)


AGENT, AGENT_INFO = load_agent()

from mlflow.genai.agent_server import AgentServer, invoke, stream
from mlflow.types.responses import ResponsesAgentRequest

from agent_server.memory import Memory  # from the loaded model's code
from agent_server import watcher  # noqa: E402
from app_shell import api
from app_shell.auth import UserMiddleware

api.AGENT, api.AGENT_INFO = AGENT, AGENT_INFO


@invoke()
async def invoke_handler(request: ResponsesAgentRequest):
    return await AGENT.ainvoke(request)


@stream()
async def stream_handler(request: ResponsesAgentRequest):
    async for event in AGENT.astream(request):
        yield event


agent_server = AgentServer("ResponsesAgent", enable_chat_proxy=False)
app = agent_server.app
app.include_router(api.router)
# every route, including /invocations and /responses, runs as the signed-in person
app.add_middleware(UserMiddleware)

_original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan(app):
    async with Memory() as memory:
        AGENT.set_memory(memory)
        replies = asyncio.create_task(watcher.run_forever(memory, AGENT.run_events))  # client email replies
        try:
            async with _original_lifespan(app):
                yield
        finally:
            replies.cancel()
    AGENT.set_memory(None)


app.router.lifespan_context = _lifespan


def main():
    agent_server.run(app_import_string="app_shell.start_server:app")
