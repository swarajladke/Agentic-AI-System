"""Retriever agent implementation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from agents.base_agent import BaseAgent
from config import Settings
from pipeline.queue import RedisStreamClient
from utils.retry import async_retry


class RetrieverAgent(BaseAgent):
    """Fetches or simulates external information for downstream analysis."""

    def __init__(self, *, queue_client: RedisStreamClient, settings: Settings) -> None:
        super().__init__(
            queue_client=queue_client,
            settings=settings,
            handled_agent="retriever",
            consumer_group="group:retriever",
            name="retriever",
        )

    async def process(self, task: dict[str, Any]) -> dict[str, Any]:
        """Retrieve documents for the given instruction."""
        query = task.get("instruction", "").strip()
        if self.settings.retriever_use_real_search and self.settings.brave_api_key:
            docs = await self._real_search(query)
        else:
            docs = self._simulate_search(query)

        return {
            "query": query,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "documents": docs,
            "source_count": len(docs),
        }

    @async_retry(max_retries=3, base_delay_seconds=1.0)
    async def _real_search(self, query: str) -> list[dict[str, str]]:
        """Call Brave Search API and normalize results."""
        headers = {"X-Subscription-Token": self.settings.brave_api_key or ""}
        params = {"q": query, "count": 5}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
            payload = response.json()
        results = payload.get("web", {}).get("results", [])
        normalized: list[dict[str, str]] = []
        for item in results[:5]:
            normalized.append(
                {
                    "title": str(item.get("title", "")),
                    "url": str(item.get("url", "")),
                    "snippet": str(item.get("description", "")),
                }
            )
        if not normalized:
            raise RuntimeError("Search API returned no results")
        return normalized

    @staticmethod
    def _simulate_search(query: str) -> list[dict[str, str]]:
        """Return deterministic simulation data suitable for local development."""
        lower_query = query.lower()
        if "quantum computing" in lower_query:
            return [
                {
                    "title": "Enterprise Quantum Roadmaps Expand in 2026",
                    "url": "https://example.org/quantum-roadmaps-2026",
                    "snippet": "Large enterprises are piloting hybrid quantum-classical workflows in optimization and chemistry.",
                },
                {
                    "title": "Error Mitigation Beats Raw Qubit Count in Practical Benchmarks",
                    "url": "https://example.org/error-mitigation-benchmarks",
                    "snippet": "Research focus shifts from headline qubit numbers to stability, calibration, and repeatability.",
                },
                {
                    "title": "Quantum Talent Gap Widens Across Regulated Industries",
                    "url": "https://example.org/quantum-talent-gap",
                    "snippet": "Financial services and pharma report increased hiring for quantum algorithm engineering roles.",
                },
            ]
        return [
            {
                "title": "Simulated Search Result 1",
                "url": "https://example.org/simulated-1",
                "snippet": f"Synthesized evidence related to '{query}'.",
            },
            {
                "title": "Simulated Search Result 2",
                "url": "https://example.org/simulated-2",
                "snippet": f"Secondary perspective and trade-offs for '{query}'.",
            },
            {
                "title": "Simulated Search Result 3",
                "url": "https://example.org/simulated-3",
                "snippet": f"Recent implementation signals and adoption metrics for '{query}'.",
            },
        ]
