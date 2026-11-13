import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from si.agents import AgentExecutor, cancel_run, resume_run
from si.agents.tools import RegisteredTool
from si.db import Agent, Audit, Memory, Run, Span
from si.models import ChatResponse, ToolCall, ToolSpec


def response(content="", calls=None):
    return ChatResponse(content=content, model="mock", tool_calls=calls or [], input_tokens=1, output_tokens=1)


def make_agent(database, project_id="test-one", **kwargs):
    with database() as session:
        agent = Agent(project_id=project_id, name=kwargs.pop("name", "Agent"), **kwargs)
        session.add(agent)
        session.commit()
        return agent.id


def make_run(database, agent_id, project_id="test-one", **kwargs):
    with database() as session:
        run = Run(project_id=project_id, agent_id=agent_id, input=kwargs.pop("input", "hello"), **kwargs)
        session.add(run)
        session.commit()
        return run.id


class CancelDuringCall:
    """Simulates a user cancelling while the model request is in flight."""

    def __init__(self, database, run_id, result):
        self.database = database
        self.run_id = run_id
        self.result = result
        self.calls = 0

    async def chat(self, request):
        self.calls += 1
        with self.database() as other:
            assert cancel_run(other, self.run_id).status == "cancelling"
            other.commit()
        return self.result


class Blocked:
    """A model call that stays in flight until the test releases it."""

    def __init__(self, result):
        self.result = result
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def chat(self, request):
        self.entered.set()
        await self.release.wait()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Queue:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, run_id, **options):
        self.jobs.append((name, run_id, options))


@pytest.mark.parametrize("sensitive, calls", [
    (False, []),
    (False, [ToolCall(id="a", name="tool", arguments={})]),
    (True, [ToolCall(id="a", name="tool", arguments={})]),
])
async def test_running_executor_stops_at_next_checkpoint(database, sensitive, calls):
    agent_id = make_agent(database, tools=["tool"])
    run_id = make_run(database, agent_id, session_id="s")
    executed = []

    async def handler(arguments):
        executed.append(arguments)
        return "ran"

    tool = RegisteredTool(ToolSpec(name="tool", description="", parameters={}, sensitive=sensitive), handler)
    gateway = CancelDuringCall(database, run_id, response("final", calls))
    with database() as session:
        result = await AgentExecutor(session, gateway, tools={"tool": tool}).run(run_id)
        assert result.status == "cancelled"
        assert result.error == "Cancelled by request"
        assert result.output is None and result.pending_tool is None
        assert [event["type"] for event in result.events][-2:] == ["model_response", "cancelled"]
        assert result.events[-1]["data"] == {"previous_status": "running"}
        assert session.scalar(select(func.count()).select_from(Memory)) == 0
    assert executed == [] and gateway.calls == 1


async def test_cancelled_queued_run_is_never_claimed(database):
    agent_id = make_agent(database)
    run_id = make_run(database, agent_id)

    class NoCalls:
        async def chat(self, request):
            raise AssertionError("Cancelled run reached the model")

    with database() as session:
        assert cancel_run(session, run_id).status == "cancelled"
        session.commit()
        assert (await AgentExecutor(session, NoCalls()).run(run_id)).status == "cancelled"


async def test_executor_failure_after_cancel_request_is_cancelled(database):
    agent_id = make_agent(database)
    run_id = make_run(database, agent_id)

    class FailingAfterCancel:
        async def chat(self, request):
            with database() as other:
                cancel_run(other, run_id)
                other.commit()
            raise RuntimeError("provider unavailable")

    with database() as session:
        result = await AgentExecutor(session, FailingAfterCancel()).run(run_id)
        assert result.status == "cancelled"
        assert "failed" not in [event["type"] for event in result.events]


@pytest.mark.parametrize("stale_result", [
    response(calls=[ToolCall(id="a", name="tool", arguments={})]),
    RuntimeError("provider unavailable"),
])
async def test_stale_executor_leaves_reclaimed_run_alone(database, monkeypatch, stale_result):
    from worker.main import startup

    monkeypatch.setattr("worker.main.SessionLocal", database)
    run_id = make_run(database, make_agent(database, tools=["tool"]))
    executed = []

    async def handler(arguments):
        executed.append(arguments)
        return "ran"

    tools = {"tool": RegisteredTool(ToolSpec(name="tool", description="", parameters={}), handler)}
    stale, current = Blocked(stale_result), Blocked(response("fresh"))
    with database() as first, database() as second:
        old = asyncio.create_task(AgentExecutor(first, stale, tools=tools).run(run_id))
        await stale.entered.wait()
        # A worker restart interrupts the run the API is executing inline; a user resumes it.
        await startup({"redis": Queue()})
        with database() as session:
            resume_run(session, run_id)
            session.commit()
        new = asyncio.create_task(AgentExecutor(second, current, tools=tools).run(run_id))
        await current.entered.wait()
        stale.release.set()
        await old
        current.release.set()
        await new
    with database() as session:
        result = session.get(Run, run_id)
        assert executed == []
        assert (result.status, result.output, result.error, result.steps) == ("completed", "fresh", None, 1)
        assert [event["type"] for event in result.events] == [
            "started", "interrupted", "resumed", "started", "model_response", "completed"]
        assert [message["role"] for message in result.messages] == ["system", "user", "assistant"]


async def test_interrupted_executor_keeps_late_result_out_of_run(database, monkeypatch):
    from worker.main import startup

    monkeypatch.setattr("worker.main.SessionLocal", database)
    run_id = make_run(database, make_agent(database))
    gateway = Blocked(response("late"))
    with database() as session:
        task = asyncio.create_task(AgentExecutor(session, gateway).run(run_id))
        await gateway.entered.wait()
        await startup({"redis": Queue()})
        gateway.release.set()
        await task
    with database() as session:
        result = session.get(Run, run_id)
        assert (result.status, result.output, result.steps) == ("interrupted", None, 0)
        assert [event["type"] for event in result.events] == ["started", "interrupted"]
        assert [message["role"] for message in result.messages] == ["system", "user"]
        # The call still happened, so it is traced even though the run ignores it.
        assert session.scalar(select(func.count()).select_from(Span).where(Span.run_id == run_id)) == 1


@pytest.mark.parametrize("status", ["queued", "waiting_approval", "interrupted"])
def test_cancel_paused_runs_immediately_and_idempotently(client, database, status):
    agent_id = make_agent(database)
    pending = {"id": "a", "name": "python", "arguments": {}, "approved": None, "remaining": []}
    run_id = make_run(database, agent_id, status=status,
                      pending_tool=pending if status != "queued" else None)
    cancelled = client.post(f"/v1/runs/{run_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert body["status"] == "cancelled"
    assert body["error"] == "Cancelled by request"
    assert body["pending_tool"] is None
    assert body["events"][-1]["type"] == "cancelled"
    assert body["events"][-1]["data"] == {"previous_status": status}
    again = client.post(f"/v1/runs/{run_id}/cancel").json()
    assert again["status"] == "cancelled" and len(again["events"]) == len(body["events"])
    with database() as session:
        assert session.scalar(select(func.count()).select_from(Audit).where(
            Audit.action == "run.cancel", Audit.resource_id == run_id)) == 2
    stream = client.get(f"/v1/runs/{run_id}/events")
    assert '"cancelled"' in stream.text and stream.text.endswith("data: [DONE]\n\n")


def test_cancel_running_run_requests_cooperative_stop(client, database):
    agent_id = make_agent(database)
    run_id = make_run(database, agent_id, status="running")
    for _ in range(2):
        response = client.post(f"/v1/runs/{run_id}/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == "cancelling"
        assert response.json()["events"] == []


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_cancel_finished_run_conflicts(client, database, status):
    run_id = make_run(database, make_agent(database), status=status)
    response = client.post(f"/v1/runs/{run_id}/cancel")
    assert response.status_code == 409
    assert status in response.json()["detail"]


def test_run_lifecycle_actions_are_scoped_and_role_checked(client, database):
    run_id = make_run(database, make_agent(database), status="interrupted")
    for action in ("cancel", "resume"):
        assert client.post(f"/v1/runs/{run_id}/{action}", headers={"x-api-key": "viewer"}).status_code == 403
        assert client.post(f"/v1/runs/{run_id}/{action}", headers={"x-api-key": "other"}).status_code == 404
    with database() as session:
        assert session.get(Run, run_id).status == "interrupted"


def test_resume_requires_fresh_approval_for_in_flight_sensitive_tool(client, database):
    agent_id = make_agent(database, tools=["memory_store"])
    call = {"id": "call-1", "name": "memory_store", "arguments": {"content": "remember me"}}
    run_id = make_run(
        database, agent_id, status="interrupted", session_id="s1", steps=1,
        error="Worker interrupted; inspect persisted events before retrying",
        messages=[
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "", "tool_calls": [call]},
        ],
        pending_tool={**call, "approved": True, "remaining": []},
    )
    resumed = client.post(f"/v1/runs/{run_id}/resume", json={"background": False})
    assert resumed.status_code == 200, resumed.text
    body = resumed.json()
    assert body["status"] == "waiting_approval"
    assert body["error"] is None
    assert "claim_id" not in body
    assert body["pending_tool"]["approved"] is None
    assert [event["type"] for event in body["events"]][-3:] == ["resumed", "started", "approval_required"]
    assert body["events"][-3]["data"] == {"pending_tool": "memory_store"}
    with database() as session:
        assert session.scalar(select(func.count()).select_from(Memory)) == 0

    approved = client.post(f"/v1/runs/{run_id}/approve", json={"approved": True})
    assert approved.json()["status"] == "completed", approved.text
    with database() as session:
        stored = session.scalars(select(Memory.content).where(Memory.content == "remember me")).all()
        assert stored == ["remember me"]


def test_resume_enqueues_by_default_and_rejects_other_states(client, database, monkeypatch):
    enqueued = []

    async def enqueue(run_id):
        enqueued.append(run_id)

    monkeypatch.setattr("si.main.enqueue_run", enqueue)
    agent_id = make_agent(database)
    run_id = make_run(database, agent_id, status="interrupted")
    resumed = client.post(f"/v1/runs/{run_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "queued"
    assert enqueued == [run_id]
    conflict = client.post(f"/v1/runs/{run_id}/resume")
    assert conflict.status_code == 409
    assert "queued" in conflict.json()["detail"]


def test_approve_conflict_is_reported_not_server_error(client, database):
    run_id = make_run(database, make_agent(database), status="waiting_approval", pending_tool=None)
    response = client.post(f"/v1/runs/{run_id}/approve", json={"approved": True})
    assert response.status_code == 409


def test_list_runs_filters_orders_and_omits_transcripts(client, database):
    first_agent = make_agent(database, name="First")
    second_agent = make_agent(database, name="Second")
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    oldest = make_run(database, first_agent, status="completed", created_at=start,
                      messages=[{"role": "user", "content": "secret transcript"}],
                      events=[{"type": "completed", "data": {}}])
    middle = make_run(database, first_agent, status="failed", created_at=start + timedelta(minutes=1))
    newest = make_run(database, second_agent, status="completed", session_id="chat",
                      created_at=start + timedelta(minutes=2))
    make_run(database, make_agent(database, project_id="test-two"), project_id="test-two")

    def ids(**params):
        response = client.get("/v1/runs", params=params)
        assert response.status_code == 200, response.text
        return [run["id"] for run in response.json()]

    listed = client.get("/v1/runs").json()
    assert [run["id"] for run in listed] == [newest, middle, oldest]
    assert "messages" not in listed[0] and "events" not in listed[0] and "claim_id" not in listed[0]
    assert "secret transcript" not in client.get("/v1/runs").text
    assert ids(agent_id=first_agent) == [middle, oldest]
    assert ids(status="completed") == [newest, oldest]
    assert ids(session_id="chat") == [newest]
    assert ids(limit=1) == [newest]
    assert client.get("/v1/runs", params={"status": "unknown"}).status_code == 422
    assert client.get("/v1/runs", params={"limit": 0}).status_code == 422


async def test_worker_timeout_finishes_requested_cancellation(database, monkeypatch):
    from worker.main import execute_agent

    run_id = make_run(database, make_agent(database))

    async def cancelled_mid_run(session, identity):
        run = session.get(Run, identity)
        run.status = "cancelling"
        session.commit()
        raise asyncio.CancelledError

    monkeypatch.setattr("worker.main.SessionLocal", database)
    monkeypatch.setattr("si.agents.execute_run", cancelled_mid_run)
    with pytest.raises(asyncio.CancelledError):
        await execute_agent({}, run_id)
    with database() as session:
        run = session.get(Run, run_id)
        assert run.status == "cancelled"
        assert run.events[-1]["type"] == "cancelled"


async def test_worker_ignores_runs_that_are_not_queued(database, monkeypatch):
    from worker.main import execute_agent

    run_id = make_run(database, make_agent(database), status="interrupted")

    async def never(session, identity):
        raise AssertionError("Interrupted runs must be resumed explicitly")

    monkeypatch.setattr("worker.main.SessionLocal", database)
    monkeypatch.setattr("si.agents.execute_run", never)
    await execute_agent({}, run_id)


async def test_worker_startup_recovers_each_status(database, monkeypatch):
    from worker.main import startup

    agent_id = make_agent(database)
    running = make_run(database, agent_id, status="running")
    cancelling = make_run(database, agent_id, status="cancelling")
    queued = make_run(database, agent_id, status="queued")
    waiting = make_run(database, agent_id, status="waiting_approval")

    queue = Queue()
    monkeypatch.setattr("worker.main.SessionLocal", database)
    await startup({"redis": queue})
    assert queue.jobs == [("execute_agent", queued, {"_job_id": f"recover:{queued}"})]
    with database() as session:
        assert session.get(Run, running).status == "interrupted"
        assert session.get(Run, running).events[-1]["type"] == "interrupted"
        assert session.get(Run, cancelling).status == "cancelled"
        assert session.get(Run, queued).status == "queued"
        assert session.get(Run, waiting).status == "waiting_approval"
