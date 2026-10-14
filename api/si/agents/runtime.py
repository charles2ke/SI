"""Shared production gateway wrapper for quotas, guardrails, and traces."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from si.guardrails import Guardrails
from si.models import Gateway
from si.quotas import get_redis, reserve
from si.tracing import record_span


class ProjectGateway:
    def __init__(self, session: Any, project_id: str, redis: Any = None, gateway: Any = None):
        self.session = session
        self.project_id = project_id
        self.redis = redis
        self.gateway = gateway or Gateway()
        self.guard = Guardrails()

    async def _reserve(self, tokens: int) -> Any:
        if self.redis is None:
            self.redis = get_redis()
        return await reserve(self.redis, self.project_id, tokens, tokens * 0.0001)

    async def chat(self, request: Any) -> Any:
        request = self.guard.check_request(request)
        estimate = self.guard.tokens(json.dumps(request.model_dump(mode="json"))) + request.max_tokens
        reservation = await self._reserve(estimate)
        started = time.monotonic()
        # Unknown upstream failures retain reservations: usage may have accrued.
        response = await self.gateway.chat(request)
        await reservation.reconcile(response.input_tokens + response.output_tokens, response.cost)
        response.content = self.guard.post(response.content)
        record_span(
            self.session, self.project_id, "gateway", response.model,
            outputs={"model": response.model},
            latency_ms=(time.monotonic() - started) * 1000,
            tokens=response.input_tokens + response.output_tokens, cost=response.cost,
        )
        self.session.commit()
        return response

    async def embed(self, texts: list[str]) -> list[list[float]]:
        tokens = sum(self.guard.tokens(value) for value in texts)
        reservation = await self._reserve(tokens)
        started = time.monotonic()
        vectors = await self.gateway.embed(texts)
        # The normalized embedding interface exposes no provider usage. Charge
        # conservative estimates for remote embeddings, zero cost for local ones.
        cost = tokens * 0.0001 if os.getenv("EMBEDDING_MODEL") else 0.0
        await reservation.reconcile(tokens, cost)
        record_span(
            self.session, self.project_id, "embedding", os.getenv("EMBEDDING_MODEL", "local-hash"),
            inputs={"count": len(texts)}, outputs={"dimensions": 64},
            latency_ms=(time.monotonic() - started) * 1000, tokens=tokens, cost=cost,
        )
        self.session.commit()
        return vectors
