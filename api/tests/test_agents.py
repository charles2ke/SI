import json
import os
import signal

import pytest
from sqlalchemy import select

from si.agents import AgentExecutor
from si.agents.memory import MemoryStore
from si.agents.tools import (
    MCPClient,
    RegisteredTool,
    build_tools,
    calculate,
    python_execute,
    validate_tool_schema,
)
from si.db import Agent, Memory, Run, Span
from si.models import ChatResponse, ToolCall, ToolSpec


class ScriptedGateway:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        return next(self.responses)


def response(content="", calls=None):
    return ChatResponse(content=content, model="mock", tool_calls=calls or [], input_tokens=3, output_tokens=2)


@pytest.fixture
def agent_session(database):
    with database() as session:
        session.add(Agent(id="agent", project_id="test-one", name="Agent", tools=["calculator"], max_steps=4))
        session.commit()
        yield session


def create_run(session, **kwargs):
    run = Run(project_id="test-one", agent_id="agent", input="calculate", **kwargs)
    session.add(run)
    session.commit()
    return run


@pytest.mark.parametrize("expression, expected", [("2+3*4", 14), ("-(4/2)", -2), ("2**10", 1024)])
def test_calculator(expression, expected):
    assert calculate(expression) == expected


@pytest.mark.parametrize("expression", ["__import__('os')", "(1).__class__", "2**10000", "1e309", "True+1", "x", "[" * 501])
def test_calculator_rejects_unsafe(expression):
    with pytest.raises((ValueError, SyntaxError)):
        calculate(expression)


async def test_python_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SI_ENABLE_PYTHON_TOOL", raising=False)
    with pytest.raises(ValueError, match="disabled"):
        await python_execute("print('hello')")


async def test_python_opt_in_does_not_inherit_secrets(monkeypatch):
    monkeypatch.setenv("SI_ENABLE_PYTHON_TOOL", "true")
    monkeypatch.setenv("SI_TEST_PRIVATE_VALUE", "not-inherited")
    assert (await python_execute("import os; print(os.environ.get('SI_TEST_PRIVATE_VALUE'))")).strip() == "None"
    with pytest.raises(ValueError, match="output"):
        await python_execute("print('x' * 100000)")


async def test_python_always_cleans_process_group(monkeypatch):
    monkeypatch.setenv("SI_ENABLE_PYTHON_TOOL", "true")
    groups = []
    killpg = os.killpg

    def record(group, sig):
        groups.append((group, sig))
        return killpg(group, sig)

    monkeypatch.setattr(os, "killpg", record)
    assert (await python_execute("print('done')")).strip() == "done"
    with pytest.raises(ValueError, match="output"):
        await python_execute("print('x' * 100000)")
    assert len(groups) == 2
    assert all(group > 0 and sig == signal.SIGKILL for group, sig in groups)


async def test_memory_store_long_term_project_scope(agent_session):
    store = MemoryStore(agent_session)
    store.remember("test-one", "a", "session only")
    store.remember("test-one", "a", "long term", long_term=True)
    store.remember("test-two", "a", "other project", long_term=True)
    agent_session.commit()
    assert [item.content for item in store.load("test-one", "b")] == ["long term"]
    assert store.load("test-two", "b")[0].content == "other project"


async def test_memory_store_requires_approval(agent_session):
    tools = await build_tools(
        agent_session, None, "test-one", [], ["memory_store"], session_id="session"
    )
    assert tools["memory_store"].spec.sensitive


async def test_tool_call_respond_and_scoped_memory(agent_session):
    session = agent_session
    session.add_all([
        Memory(project_id="test-one", session_id="same", role="user", content="my history"),
        Memory(project_id="test-one", session_id="other", role="user", content="private session"),
        Memory(project_id="test-two", session_id="same", role="user", content="private project"),
        Memory(project_id="test-one", session_id="old", role="user", content="long term", long_term=True),
    ])
    session.commit()
    gateway = ScriptedGateway([
        response(calls=[ToolCall(id="call1", name="calculator", arguments={"expression": "2+3"})]),
        response("5"),
    ])
    run = create_run(session, session_id="same")
    result = await AgentExecutor(session, gateway).run(run.id)
    assert result.status == "completed", result.error
    assert result.output == "5"
    assert result.steps == 2
    initial = json.dumps(gateway.requests[0].model_dump())
    assert "my history" in initial and "long term" in initial
    assert "private session" not in initial and "private project" not in initial
    assert gateway.requests[1].messages[-1].tool_call_id == "call1"
    assert "untrusted_tool_result" in gateway.requests[1].messages[-1].content
    assert len(session.scalars(select(Span).where(Span.run_id == run.id)).all()) == 3
    assert [event["type"] for event in run.events][-1] == "completed"
    # Terminal runs are idempotent.
    await AgentExecutor(session, gateway).run(run.id)
    assert len(gateway.requests) == 2


async def test_approval_resume_does_not_replay_calls(agent_session):
    session = agent_session
    agent = session.get(Agent, "agent")
    agent.tools = ["sensitive"]
    session.commit()
    invocations = []

    async def handler(arguments):
        invocations.append(arguments)
        return "approved execution"

    tool = RegisteredTool(
        ToolSpec(name="sensitive", description="test", parameters={"type": "object"}, sensitive=True),
        handler,
    )
    gateway = ScriptedGateway([
        response(calls=[
            ToolCall(id="a", name="sensitive", arguments={"value": 1}),
            ToolCall(id="b", name="sensitive", arguments={"value": 2}),
        ]),
        response("done"),
    ])
    run = create_run(session)
    executor = AgentExecutor(session, gateway, tools={"sensitive": tool})
    assert (await executor.run(run.id)).status == "waiting_approval"
    assert invocations == []
    assert len(gateway.requests) == 1
    assert (await executor.approve(run.id, True)).status == "waiting_approval"
    assert invocations == [{"value": 1}]
    assert run.pending_tool["id"] == "b"
    assert (await executor.approve(run.id, True)).status == "completed"
    assert invocations == [{"value": 1}, {"value": 2}]
    assert len(gateway.requests) == 2


async def test_denied_tool_is_observed_not_executed(agent_session):
    session = agent_session
    agent = session.get(Agent, "agent")
    agent.tools = ["sensitive"]
    session.commit()

    async def never(arguments):
        raise AssertionError("Denied tool was executed")

    tool = RegisteredTool(ToolSpec(name="sensitive", description="", parameters={}, sensitive=True), never)
    gateway = ScriptedGateway([
        response(calls=[ToolCall(id="a", name="sensitive")]), response("denied"),
    ])
    executor = AgentExecutor(session, gateway, tools={"sensitive": tool})
    run = create_run(session)
    await executor.run(run.id)
    assert (await executor.approve(run.id, False)).status == "completed"
    assert "denied" in gateway.requests[-1].messages[-1].content


async def test_step_limit_is_durable_across_approval(agent_session):
    session = agent_session
    session.get(Agent, "agent").max_steps = 1
    session.commit()
    gateway = ScriptedGateway([
        response(calls=[ToolCall(id="a", name="calculator", arguments={"expression": "1+1"})]),
    ])
    run = create_run(session)
    result = await AgentExecutor(session, gateway).run(run.id)
    assert result.status == "failed"
    assert "maximum steps" in result.error
    assert len(gateway.requests) == 1


async def test_tool_arguments_validated(agent_session):
    tools = await build_tools(agent_session, None, "test-one", [], ["calculator"])
    with pytest.raises(Exception):
        await tools["calculator"].call({"expression": "1", "unexpected": True})


async def test_running_worker_cannot_be_claimed_twice(agent_session):
    run = create_run(agent_session)
    run.status = "running"
    agent_session.commit()
    gateway = ScriptedGateway([])
    assert (await AgentExecutor(agent_session, gateway).run(run.id)).status == "running"
    assert gateway.requests == []


async def test_budget_callbacks_and_guardrail_output(agent_session):
    from si.guardrails import Guardrails

    reservations = []
    settled = []

    async def before(project_id, request):
        reservations.append(project_id)
        return "reservation"

    async def after(reservation, result):
        settled.append((reservation, result.cost))

    gateway = ScriptedGateway([response("contact person@example.com")])
    run = create_run(agent_session)
    executor = AgentExecutor(
        agent_session, gateway, guardrails=Guardrails(), before_call=before, after_call=after
    )
    assert (await executor.run(run.id)).output == "contact [REDACTED_EMAIL]"
    assert "person@example.com" not in json.dumps(run.events)
    assert reservations == ["test-one"]
    assert settled == [("reservation", 0)]


async def test_collection_ownership_checked_before_model(agent_session):
    session = agent_session
    session.get(Agent, "agent").collection_ids = ["foreign"]
    session.commit()
    gateway = ScriptedGateway([])
    run = create_run(session)
    assert (await AgentExecutor(session, gateway).run(run.id)).status == "failed"
    assert gateway.requests == []


async def test_mcp_initialize_list_call_and_session_header(monkeypatch):
    requests = []

    async def transport(url, **kwargs):
        payload = kwargs["json"]
        requests.append(payload)
        method = payload["method"]
        if method == "initialize":
            kwargs["response_headers"]["mcp-session-id"] = "session123"
            result = {"protocolVersion": "2025-03-26", "capabilities": {}}
        elif method == "notifications/initialized":
            return b"", ""
        elif method == "tools/list":
            assert kwargs["headers"]["Mcp-Session-Id"] == "session123"
            result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
        else:
            result = {"content": [{"type": "text", "text": payload["params"]["arguments"]["message"]}]}
        return json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result}).encode(), "application/json"

    monkeypatch.setattr("si.agents.tools.safe_fetch", transport)
    client = MCPClient("https://mcp.example", {"mcp.example"})
    assert (await client.list_tools())[0]["name"] == "echo"
    result = await client.call_tool("echo", {"message": "hello"})
    assert result["content"][0]["text"] == "hello"
    assert [request["method"] for request in requests] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call",
    ]


async def test_mcp_registered_tools_are_sensitive(agent_session, monkeypatch):
    monkeypatch.setenv("SI_MCP_SERVERS", '{"example":"https://mcp.example"}')

    async def definitions(self):
        return [{"name": "echo", "inputSchema": {"type": "object"}}]

    monkeypatch.setattr(MCPClient, "list_tools", definitions)
    tools = await build_tools(agent_session, None, "test-one", [], ["mcp_example_echo"])
    assert tools["mcp_example_echo"].spec.sensitive


async def test_tool_schema_cannot_resolve_network_references():
    async def handler(arguments):
        return arguments

    tool = RegisteredTool(
        ToolSpec(name="remote", description="", parameters={"$ref": "http://127.0.0.1/schema"}),
        handler,
    )
    with pytest.raises(ValueError, match="references"):
        await tool.call({})


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_mcp_schema_nested_definitions_cannot_fetch_references(keyword):
    schema = {
        "type": "object",
        "$defs": {"nested": {"allOf": [{keyword: "http://127.0.0.1/internal"}]}},
    }
    with pytest.raises(ValueError, match="references"):
        validate_tool_schema(schema)


def test_agent_http_api_mock_tool_loop_and_sse(client, redis):
    agent = client.post(
        "/v1/agents", json={"name": "Math", "tools": ["calculator"], "max_steps": 3}
    ).json()
    created = client.post(
        f"/v1/agents/{agent['id']}/runs", json={"input": "What is 2+3?", "background": False}
    )
    assert created.status_code == 201, created.text
    run = created.json()
    assert run["status"] == "completed", run.get("error")
    assert "5" in run["output"]
    events = client.get(f"/v1/runs/{run['id']}/events")
    assert events.status_code == 200
    assert "tool_result" in events.text and "[DONE]" in events.text
    assert redis.usage[0] > 0
