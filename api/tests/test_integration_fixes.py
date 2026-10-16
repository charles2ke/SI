import asyncio

import pytest

from si.db import Agent, Run, Span
from si.models import ChatResponse


async def test_eval_model_reaches_gateway(client, monkeypatch):
    calls = []

    async def chat(_self, request):
        calls.append(request.model)
        return ChatResponse(content="answer", model=request.model, input_tokens=1, output_tokens=1)

    monkeypatch.setattr("si.models.Gateway.chat", chat)
    client.post("/v1/prompts", json={"name": "model-test", "template": "{input}"})
    response = client.post("/v1/evals", json={
        "prompt_name": "model-test", "prompt_version": 1,
        "model": "openai:example-model", "scorer": "exact_match",
        "dataset": [{"input": "question", "expected": "answer"}],
    })
    assert response.status_code == 201, response.text
    assert response.json()["score"] == 1
    assert calls == ["openai:example-model"]


def test_chat_quota_estimate_includes_tool_schema(client, monkeypatch):

    estimates = []

    class Reservation:
        async def reconcile(self, tokens, cost):
            pass

    async def capture_reservation(redis, project_id, tokens, cost):
        estimates.append(tokens)
        return Reservation()

    async def chat(_self, request):
        return ChatResponse(content="ok", model=request.model, input_tokens=1, output_tokens=1)

    monkeypatch.setattr("si.main.reserve", capture_reservation)
    monkeypatch.setattr("si.models.Gateway.chat", chat)
    payload = {
        "model": "mock",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 20,
        "tools": [{
            "type": "function",
            "function": {
                "name": "lookup",
                "parameters": {"type": "object", "description": "x" * 5000},
            },
        }],
    }

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200, response.text
    assert estimates[0] > 20 + len("hi")


async def test_worker_cancellation_persists_interruption(database, monkeypatch):
    from worker.main import execute_agent

    with database() as session:
        agent = Agent(project_id="test-one", name="Cancellation test")
        session.add(agent)
        session.flush()
        run = Run(project_id="test-one", agent_id=agent.id, input="hello", status="queued")
        session.add(run)
        session.commit()
        run_id = run.id

    async def cancel(session, identity):
        run = session.get(Run, identity)
        run.status = "running"
        session.commit()
        raise asyncio.CancelledError

    monkeypatch.setattr("worker.main.SessionLocal", database)
    monkeypatch.setattr("si.agents.execute_run", cancel)
    with pytest.raises(asyncio.CancelledError):
        await execute_agent({}, run_id)
    with database() as session:
        persisted = session.get(Run, run_id)
        assert persisted.status == "interrupted"
        assert persisted.events[-1]["type"] == "interrupted"
        assert "timed out" in persisted.error


def test_http_reads_are_rate_limited(client, redis):
    from si.config import settings
    redis.http_rate = settings.rate_limit
    response = client.get("/v1/agents")
    assert response.status_code == 429
    assert redis.usage == [0, 0]


def test_request_span_records_status_and_latency(client, database):
    from sqlalchemy import select
    assert client.get("/v1/agents").status_code == 200
    with database() as session:
        span = session.scalar(select(Span).where(Span.kind == "request"))
        assert span.outputs == {"status": 200}
        assert span.latency_ms > 0
        assert span.inputs == {"method": "GET", "path": "/v1/agents"}


def test_long_term_memory_is_project_scoped(client):
    response = client.post("/v1/memory", json={
        "session_id": "session-a", "content": "Prefer concise answers.", "long_term": True,
    })
    assert response.status_code == 201
    assert response.json()["long_term"] is True
    assert len(client.get("/v1/memory", params={"session_id": "session-b"}).json()) == 1
    assert client.get("/v1/memory", params={"session_id": "session-a"},
                      headers={"x-api-key": "other"}).json() == []
    assert client.post("/v1/memory", json={"session_id": "a", "content": "Cannot write"},
                       headers={"x-api-key": "viewer"}).status_code == 403


def test_memory_redacts_pii_and_rejects_invalid_roles(client):
    response = client.post("/v1/memory", json={
        "session_id": "a", "content": "Contact test@example.com",
    })
    assert response.status_code == 201
    assert "test@example.com" not in response.json()["content"]
    assert client.post("/v1/memory", json={
        "session_id": "a", "content": "hello", "role": "system",
    }).status_code == 422
