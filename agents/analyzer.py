"""Analyzer agent implementation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from agents.base_agent import BaseAgent
from config import Settings
from pipeline.queue import RedisStreamClient
from utils.retry import async_retry


class AnalyzerAgent(BaseAgent):
    """Analyzes retrieved material and produces structured insights."""

    def __init__(self, *, queue_client: RedisStreamClient, settings: Settings) -> None:
        super().__init__(
            queue_client=queue_client,
            settings=settings,
            handled_agent="analyzer",
            consumer_group="group:analyzer",
            name="analyzer",
        )

    async def process(self, task: dict[str, Any]) -> dict[str, Any]:
        """Analyze dependency context from retriever output."""
        context = self._parse_json(task.get("context", "{}"))
        user_task = task.get("user_task", "")

        if not self.settings.anthropic_api_key:
            return self._simulate_analysis(context=context, user_task=user_task)

        prompt = (
            "You are an analyst agent. Review the retrieval context and produce strict JSON.\n"
            "Schema:\n"
            '{"summary":"...","trends":[{"title":"...","evidence":"..."}],'
            '"risks":[{"title":"...","impact":"..."}],"key_takeaways":["..."]}\n\n'
            f"User task: {user_task}\n"
            f"Context: {json.dumps(context, ensure_ascii=True)}"
        )
        text = await self._call_claude(prompt)
        parsed = self._extract_json(text)
        if parsed:
            parsed["analyzed_at"] = datetime.now(UTC).isoformat()
            return parsed
        return self._simulate_analysis(context=context, user_task=user_task)

    @async_retry(max_retries=3, base_delay_seconds=1.0)
    async def _call_claude(self, prompt: str) -> str:
        """Call Claude for non-streaming structured analysis."""
        headers = {
            "x-api-key": self.settings.anthropic_api_key or "",
            "anthropic-version": self.settings.anthropic_version,
            "content-type": "application/json",
        }
        body = {
            "model": self.settings.anthropic_model,
            "max_tokens": 1400,
            "temperature": 0.2,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=self.settings.api_timeout_seconds) as client:
            response = await client.post(self.settings.anthropic_api_url, headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()
            text_blocks = [
                part.get("text", "")
                for part in payload.get("content", [])
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            return "\n".join(text_blocks).strip()

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _extract_json(raw_text: str) -> dict[str, Any]:
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}
        try:
            candidate = json.loads(raw_text[start : end + 1])
            if isinstance(candidate, dict):
                return candidate
        except json.JSONDecodeError:
            return {}
        return {}

    @staticmethod
    def _simulate_analysis(context: dict[str, Any], user_task: str) -> dict[str, Any]:
        docs = context.get("1", {}).get("documents", [])
        trends = []
        for doc in docs[:3]:
            trends.append(
                {
                    "title": doc.get("title", "Trend"),
                    "evidence": doc.get("snippet", ""),
                }
            )
        if not trends:
            trends = [
                {
                    "title": "Adoption Signals Increasing",
                    "evidence": "Simulated multi-source evidence suggests rising investment and pilots.",
                }
            ]

        return {
            "summary": f"Structured synthesis prepared for: {user_task}",
            "trends": trends,
            "risks": [
                {
                    "title": "Execution Risk",
                    "impact": "Limited in-house expertise can slow implementation and reduce ROI.",
                },
                {
                    "title": "Vendor Dependency",
                    "impact": "Rapidly changing tooling may introduce lock-in and migration cost.",
                },
            ],
            "key_takeaways": [
                "Prioritize use-cases with measurable value and realistic constraints.",
                "Invest in error mitigation, tooling maturity, and cross-functional talent.",
                "Run short pilot cycles before broad enterprise rollout.",
            ],
            "analyzed_at": datetime.now(UTC).isoformat(),
        }
