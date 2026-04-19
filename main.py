"""FastAPI entrypoint for the agentic orchestration system."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from agents.analyzer import AnalyzerAgent
from agents.orchestrator import OrchestratorAgent
from agents.retriever import RetrieverAgent
from agents.writer import WriterAgent
from api.routes import router
from config import Settings, get_settings
from pipeline.batch_dispatcher import BatchDispatcher
from pipeline.dag_resolver import DagResolver
from pipeline.queue import RedisStreamClient
from pipeline.result_aggregator import ResultAggregator


@dataclass(slots=True)
class AppServices:
    """Container for initialized application services."""

    settings: Settings
    queue_client: RedisStreamClient
    resolver: DagResolver
    orchestrator: OrchestratorAgent
    retriever: RetrieverAgent
    analyzer: AnalyzerAgent
    writer: WriterAgent
    aggregator: ResultAggregator
    batch_dispatcher: BatchDispatcher
    background_tasks: list[asyncio.Task[Any]] = field(default_factory=list)


def create_app() -> FastAPI:
    """App factory."""
    settings = get_settings()
    app = FastAPI(title=settings.app_name)
    app.include_router(router)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Agentic AI SSE Client</title>
  </head>
  <body>
    <h2>Agentic AI System</h2>
    <textarea id="task" rows="4" cols="80">Research the latest trends in quantum computing and write a 3-paragraph executive summary with key takeaways</textarea>
    <br />
    <button onclick="submitTask()">Submit Task</button>
    <pre id="out"></pre>
    <script>
      async function submitTask() {
        const task = document.getElementById("task").value;
        const response = await fetch("/task", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({task})
        });
        const body = await response.json();
        const stream = new EventSource(body.stream_url);
        const out = document.getElementById("out");
        out.textContent = "";
        stream.onmessage = (event) => {
          out.textContent += event.data + "\\n";
          try {
            const parsed = JSON.parse(event.data);
            if (parsed.done || parsed.type === "task_completed" || parsed.type === "task_failed") {
              stream.close();
            }
          } catch (_) {}
        };
      }
    </script>
  </body>
</html>
"""

    @app.on_event("startup")
    async def on_startup() -> None:
        queue_client = RedisStreamClient(settings.redis_url)
        resolver = DagResolver()
        orchestrator = OrchestratorAgent(queue_client=queue_client, settings=settings, resolver=resolver)
        retriever = RetrieverAgent(queue_client=queue_client, settings=settings)
        analyzer = AnalyzerAgent(queue_client=queue_client, settings=settings)
        writer = WriterAgent(queue_client=queue_client, settings=settings)
        aggregator = ResultAggregator(
            queue_client=queue_client,
            resolver=resolver,
            settings=settings,
            dispatch_step_callback=orchestrator.dispatch_step,
        )
        batch_dispatcher = BatchDispatcher(
            batch_size=settings.batch_size,
            batch_window_seconds=settings.batch_window_seconds,
            dispatch_callback=orchestrator.handle_batch,
        )
        await batch_dispatcher.start()

        background_tasks = [
            asyncio.create_task(retriever.start(), name="retriever-consumer"),
            asyncio.create_task(analyzer.start(), name="analyzer-consumer"),
            asyncio.create_task(writer.start(), name="writer-consumer"),
            asyncio.create_task(aggregator.start(), name="result-aggregator"),
        ]

        app.state.services = AppServices(
            settings=settings,
            queue_client=queue_client,
            resolver=resolver,
            orchestrator=orchestrator,
            retriever=retriever,
            analyzer=analyzer,
            writer=writer,
            aggregator=aggregator,
            batch_dispatcher=batch_dispatcher,
            background_tasks=background_tasks,
        )

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        services: AppServices = app.state.services
        await services.batch_dispatcher.stop()
        await services.retriever.stop()
        await services.analyzer.stop()
        await services.writer.stop()
        await services.aggregator.stop()
        for task in services.background_tasks:
            task.cancel()
        if services.background_tasks:
            await asyncio.gather(*services.background_tasks, return_exceptions=True)
        await services.queue_client.close()

    return app


app = create_app()
