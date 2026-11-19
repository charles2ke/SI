import json
from pathlib import Path

import httpx
from sqlalchemy import create_engine, func, inspect, select, text

from si.db import Agent, Audit, Chunk, EvalRun, Run, Span

VIEWER = {"x-api-key": "viewer"}
OTHER = {"x-api-key": "other"}


def actions(database):
    with database() as session:
        return set(session.scalars(select(Audit.action)))


def test_agent_get_and_delete_removes_runs_and_their_traces(client, database):
    agent = client.post("/v1/agents", json={"name": "Helper"}).json()
    assert client.get(f"/v1/agents/{agent['id']}").json()["name"] == "Helper"
    assert client.get(f"/v1/agents/{agent['id']}", headers=OTHER).status_code == 404
    run = client.post(f"/v1/agents/{agent['id']}/runs", json={"input": "hi", "background": False}).json()
    assert run["status"] == "completed"
    with database() as session:
        assert session.scalar(select(func.count()).select_from(Span).where(Span.run_id == run["id"]))
        # Request/gateway spans carry no transcript and are not tied to the run.
        unlinked = session.scalar(select(func.count()).select_from(Span).where(Span.run_id.is_(None)))

    assert client.delete(f"/v1/agents/{agent['id']}", headers=VIEWER).status_code == 403
    assert client.delete(f"/v1/agents/{agent['id']}", headers=OTHER).status_code == 404
    deleted = client.delete(f"/v1/agents/{agent['id']}")
    assert deleted.status_code == 204 and deleted.content == b""
    assert client.get(f"/v1/agents/{agent['id']}").status_code == 404
    assert client.get(f"/v1/runs/{run['id']}").status_code == 404
    with database() as session:
        assert session.scalar(select(func.count()).select_from(Span).where(Span.run_id == run["id"])) == 0
        assert session.scalar(select(func.count()).select_from(Span).where(Span.run_id.is_(None))) > unlinked
    assert "agent.delete" in actions(database)


def test_agent_delete_refuses_active_runs(client, database):
    agent = client.post("/v1/agents", json={"name": "Busy"}).json()
    with database() as session:
        session.add(Run(project_id="test-one", agent_id=agent["id"], input="x", status="waiting_approval"))
        session.commit()
    response = client.delete(f"/v1/agents/{agent['id']}")
    assert response.status_code == 409
    assert "1 active run" in response.json()["detail"]
    with database() as session:
        assert session.get(Agent, agent["id"]) is not None


def test_agents_reject_tools_that_can_never_resolve(client):
    rejected = client.post("/v1/agents", json={"name": "A", "tools": ["calculator", "shell", "sh"]})
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "Unknown tools: sh, shell"
    created = client.post("/v1/agents", json={"name": "A", "tools": ["calculator", "mcp_docs_search"]})
    assert created.status_code == 201, created.text
    patched = client.patch(f"/v1/agents/{created.json()['id']}", json={"tools": ["nope"]})
    assert patched.status_code == 422
    assert client.get(f"/v1/agents/{created.json()['id']}").json()["tools"] == ["calculator", "mcp_docs_search"]


def test_traces_filter_by_kind_and_run(client):
    agent = client.post("/v1/agents", json={"name": "Tracer"}).json()
    run = client.post(f"/v1/agents/{agent['id']}/runs", json={"input": "hi", "background": False}).json()
    assert {"request", "model"} <= {span["kind"] for span in client.get("/v1/traces").json()}
    model = client.get("/v1/traces", params={"kind": "model"}).json()
    assert model and {span["kind"] for span in model} == {"model"}
    scoped = client.get("/v1/traces", params={"kind": "model", "run_id": run["id"]}).json()
    assert scoped and {span["run_id"] for span in scoped} == {run["id"]}
    assert client.get("/v1/traces", params={"kind": "request", "run_id": run["id"]}).json() == []
    assert client.get("/v1/traces", params={"kind": "x" * 51}).status_code == 422


def test_tool_catalog_reports_availability(client, monkeypatch):
    monkeypatch.delenv("HTTP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("SI_ENABLE_PYTHON_TOOL", raising=False)
    monkeypatch.setenv("SI_MCP_SERVERS", json.dumps({"docs": "http://mcp.test", "bad name": "http://x"}))
    catalog = client.get("/v1/tools", headers=VIEWER).json()
    tools = {tool["name"]: tool for tool in catalog["data"]}
    assert list(tools) == ["calculator", "rag_search", "http_fetch", "python", "memory_store"]
    assert tools["calculator"]["available"] and not tools["calculator"]["sensitive"]
    assert tools["memory_store"]["sensitive"]
    assert tools["memory_store"]["parameters"]["properties"]["long_term"] == {"type": "boolean"}
    assert not tools["http_fetch"]["available"] and "HTTP_ALLOWED_HOSTS" in tools["http_fetch"]["unavailable_reason"]
    assert not tools["python"]["available"]
    assert catalog["mcp_servers"] == [{"name": "docs", "tool_prefix": "mcp_docs_"}]

    monkeypatch.setenv("HTTP_ALLOWED_HOSTS", "example.com")
    monkeypatch.setenv("SI_ENABLE_PYTHON_TOOL", "true")
    monkeypatch.setenv("SI_MCP_SERVERS", "[]")
    catalog = client.get("/v1/tools").json()
    assert all(tool["available"] and tool["unavailable_reason"] is None for tool in catalog["data"])
    assert catalog["mcp_servers"] == []


def ingest_text(client, collection_id, text_content):
    response = client.post(f"/v1/collections/{collection_id}/documents",
                           files={"file": ("facts.txt", text_content.encode(), "text/plain")})
    assert response.status_code == 200, response.text


def test_collection_get_counts_and_delete(client, database):
    used = client.post("/v1/collections", json={"name": "Used"}).json()
    empty = client.post("/v1/collections", json={"name": "Empty"}).json()
    ingest_text(client, used["id"], "SI keeps knowledge local.")
    assert client.get(f"/v1/collections/{used['id']}").json()["chunks"] == 1
    assert client.get(f"/v1/collections/{used['id']}", headers=OTHER).status_code == 404
    listed = {item["id"]: item["chunks"] for item in client.get("/v1/collections").json()}
    assert listed == {used["id"]: 1, empty["id"]: 0}

    agent = client.post("/v1/agents", json={"name": "Reader", "collection_ids": [used["id"]]}).json()
    conflict = client.delete(f"/v1/collections/{used['id']}")
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "Collection is used by agents: Reader"
    assert client.delete(f"/v1/collections/{used['id']}", headers=VIEWER).status_code == 403

    client.patch(f"/v1/agents/{agent['id']}", json={"collection_ids": []})
    assert client.delete(f"/v1/collections/{used['id']}").status_code == 204
    assert client.get(f"/v1/collections/{used['id']}").status_code == 404
    with database() as session:
        assert session.scalar(select(func.count()).select_from(Chunk)) == 0
    assert "collection.delete" in actions(database)


def test_ingest_url_indexes_fetched_content(client, database, monkeypatch):
    fetched = []

    async def fake_fetch(url, *, allowed_hosts, max_bytes):
        fetched.append((url, allowed_hosts))
        return b"Remote facts about SI.", "text/plain; charset=utf-8"

    monkeypatch.setenv("HTTP_ALLOWED_HOSTS", "docs.example.com")
    monkeypatch.setattr("si.rag.safe_fetch", fake_fetch)
    collection = client.post("/v1/collections", json={"name": "Web"}).json()
    url = "https://docs.example.com/page"
    assert client.post(f"/v1/collections/{collection['id']}/urls", json={"url": url},
                       headers=VIEWER).status_code == 403
    assert client.post(f"/v1/collections/{collection['id']}/urls", json={"url": url},
                       headers=OTHER).status_code == 404
    response = client.post(f"/v1/collections/{collection['id']}/urls", json={"url": url})
    assert response.status_code == 200, response.text
    assert response.json() == {"chunks": 1, "url": url}
    assert fetched == [(url, {"docs.example.com"})]
    hits = client.post(f"/v1/collections/{collection['id']}/search", json={"query": "Remote"}).json()
    assert hits[0]["text"] == "Remote facts about SI."
    assert "document.ingest_url" in actions(database)


def test_ingest_url_maps_fetch_failures(client, monkeypatch):
    collection = client.post("/v1/collections", json={"name": "Web"}).json()
    path = f"/v1/collections/{collection['id']}/urls"
    monkeypatch.delenv("HTTP_ALLOWED_HOSTS", raising=False)
    blocked = client.post(path, json={"url": "https://example.com/"})
    assert blocked.status_code == 422 and blocked.json()["detail"] == "Host is not allowlisted"
    assert client.post(path, json={"url": "file:///etc/passwd"}).status_code == 422

    request = httpx.Request("GET", "https://example.com/")
    for error, status, detail in [
        (httpx.HTTPStatusError("missing", request=request, response=httpx.Response(404, request=request)),
         502, "URL returned HTTP 404"),
        (httpx.ConnectError("refused", request=request), 502, "URL could not be fetched"),
        (TimeoutError(), 502, "URL could not be fetched"),
    ]:
        async def failing_fetch(url, *, allowed_hosts, max_bytes, error=error):
            raise error

        monkeypatch.setattr("si.rag.safe_fetch", failing_fetch)
        response = client.post(path, json={"url": "https://example.com/"})
        assert (response.status_code, response.json()["detail"]) == (status, detail)


def test_evals_record_config_and_list_newest_first(client, database):
    client.post("/v1/prompts", json={"name": "echo", "template": "{input}"})
    client.post("/v1/prompts", json={"name": "echo", "template": "Echo: {input}"})
    with database() as session:
        session.add(EvalRun(id="legacy", project_id="test-one", status="completed", results=[], score=1))
        session.commit()
    first = client.post("/v1/evals", json={
        "prompt_name": "echo", "scorer": "contains", "threshold": 0.5,
        "dataset": [{"input": "hi", "expected": "hi"}, {"input": "yo", "expected": "yo"}]}).json()
    assert first["config"] == {
        "scorer": "contains", "threshold": 0.5, "model": "mock", "agent_id": None,
        "prompt_name": "echo", "prompt_version": 2, "cases": 2,
    }
    assert first["created_at"]
    second = client.post("/v1/evals", json={
        "prompt_name": "echo", "prompt_version": 1, "scorer": "exact_match",
        "dataset": [{"input": "hi", "expected": "nope"}]}).json()
    assert second["config"]["prompt_version"] == 1
    assert [item["id"] for item in client.get("/v1/evals").json()] == [second["id"], first["id"], "legacy"]
    fetched = client.get(f"/v1/evals/{first['id']}").json()
    assert (fetched["config"], fetched["score"]) == (first["config"], first["score"])
    assert client.get(f"/v1/evals/{first['id']}", headers=OTHER).status_code == 404


def test_eval_metadata_migration_round_trip(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config

    engine = create_engine(f"sqlite:///{tmp_path / 'migrate.db'}")
    monkeypatch.setattr("si.db.engine", engine)
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.upgrade(config, "0001")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO projects (id, name) VALUES ('p', 'P')"))
        connection.execute(text(
            "INSERT INTO eval_runs (id, project_id, results, score, status) "
            "VALUES ('old', 'p', '[]', 1, 'completed')"))
    command.upgrade(config, "head")
    assert "claim_id" in {column["name"] for column in inspect(engine).get_columns("runs")}
    with engine.connect() as connection:
        row = connection.execute(text("SELECT config, created_at FROM eval_runs")).one()
    assert (json.loads(row.config), row.created_at) == ({}, None)
    command.downgrade(config, "0001")
    columns = {column["name"] for column in inspect(engine).get_columns("eval_runs")}
    assert columns == {"id", "project_id", "results", "score", "status"}
    assert "claim_id" not in {column["name"] for column in inspect(engine).get_columns("runs")}
    engine.dispose()
