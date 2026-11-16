"""The make-goods agent as an MLflow ResponsesAgent (models-from-code).

Logged and registered to Unity Catalog by notebooks/01_register_agent; the Databricks App loads
`models:/<catalog>.<schema>.makegoods_agent@champion` and serves it. The same object runs anywhere:
  - in the app, with Lakebase memory attached (set_memory) and the async methods (run_events / ainvoke / astream);
  - in a notebook or a serving endpoint, statelessly through predict / predict_stream.
"""
import asyncio
import contextvars
import queue
import threading
from typing import AsyncGenerator, Generator

import mlflow
from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import ResponsesAgentRequest, ResponsesAgentResponse, ResponsesAgentStreamEvent

from agent_server import agent
from agent_server.config import LLM_ENDPOINT
from agent_server.graph import build_graph


class MakeGoodsAgent(ResponsesAgent):
    """Seller-side make-goods agent: LangGraph flow over Unity Catalog rule functions, Genie for open questions."""

    # ---------------------------------------------------------------- app (async, long-lived event loop)
    def set_memory(self, memory) -> None:
        """Attach Lakebase memory (checkpointer, preference store, review queue, decision log)."""
        agent.set_memory(memory)

    @property
    def memory(self):
        return agent._memory

    def run_events(self, request: ResponsesAgentRequest) -> AsyncGenerator:
        """("step", trail) / ("text", markdown) / ("final", custom_outputs) events for the review UI."""
        return agent.run_agent(request)

    async def ainvoke(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        return await agent.ainvoke(request)

    def astream(self, request: ResponsesAgentRequest) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
        return agent.astream(request)

    # ---------------------------------------------------------------- notebook / serving (sync)
    # Each call gets its own event loop on a worker thread (works even where a loop is already running,
    # e.g. a notebook) and its own graph, so async LLM clients never outlive their loop. The caller's
    # context is carried into the thread, so the agent's spans stay in the caller's trace (e.g. evaluation).
    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        result: dict = {}

        def work():
            result["r"] = asyncio.run(agent.ainvoke(request, graph=build_graph(agent._memory, _endpoint(request))))

        t = threading.Thread(target=contextvars.copy_context().run, args=(work,))
        t.start()
        t.join()
        return result["r"]

    def predict_stream(self, request: ResponsesAgentRequest) -> Generator[ResponsesAgentStreamEvent, None, None]:
        q: queue.Queue = queue.Queue()
        done = object()

        async def produce():
            async for event in agent.astream(request, graph=build_graph(agent._memory, _endpoint(request))):
                q.put(event)

        def work():
            try:
                asyncio.run(produce())
            except BaseException as e:  # surface failures to the caller
                q.put(e)
            finally:
                q.put(done)

        threading.Thread(target=contextvars.copy_context().run, args=(work,), daemon=True).start()
        while (item := q.get()) is not done:
            if isinstance(item, BaseException):
                raise item
            yield item


def _endpoint(request: ResponsesAgentRequest) -> str:
    return (request.custom_inputs or {}).get("llm_endpoint") or LLM_ENDPOINT


mlflow.models.set_model(MakeGoodsAgent())
