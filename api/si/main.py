import asyncio
import hashlib
import json
import logging
import os
import string
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from alembic.config import Config
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from redis.exceptions import RedisError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from si.auth import audit, bootstrap, hash_key, issue_key, require_admin, require_key
from si.config import settings
from si.db import (
    Agent,
    APIKey,
    Collection,
    EvalRun,
    PromptVersion,
    Run,
    Span,
    engine,
    get_session,
    new_id,
)
from si.quotas import get_redis, reserve
from si.schemas import (
    AgentCreate,
    AgentPatch,
    Approval,
    CollectionCreate,
    EvalCreate,
    KeyCreate,
    MemoryCreate,
    PromptCreate,
    Render,
    RunCreate,
    Search,
)
from si.tracing import record_span

logging.basicConfig(level=logging.INFO, format="%(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(command.upgrade, Config(str(Path(__file__).parents[1] / "alembic.ini")), "head")
    await asyncio.to_thread(bootstrap)
    try:
        yield
    finally:
        await get_redis().aclose()


app = FastAPI(title="SI", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in
                   os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if origin.strip()],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "Last-Event-ID"],
)
DB = Annotated[Session, Depends(get_session)]
Key = Annotated[APIKey, Depends(require_key)]
Admin = Annotated[APIKey, Depends(require_admin)]
RedisDep = Annotated[Any, Depends(get_redis)]


@app.middleware("http")
async def request_log(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        span = getattr(request.state, "trace_span", None)
        if span is not None:
            span.latency_ms = (time.perf_counter() - started) * 1000
            span.outputs = {"status": getattr(locals().get("response"), "status_code", 500)}
            request.state.trace_session.commit()
    logging.getLogger("si").info(json.dumps({
        "method": request.method, "path": request.url.path, "status": response.status_code,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2)}))
    return response


def owned(session: Session, model: Any, identity: str, project_id: str):
    obj = session.get(model, identity)
    if obj is None or obj.project_id != project_id:
        raise HTTPException(404, "Resource not found")
    return obj


def serialize(obj: Any) -> dict[str, Any]:
    return {column.name: jsonable_encoder(getattr(obj, column.name))
            for column in obj.__table__.columns}


def list_owned(session: Session, model: Any, project_id: str):
    return [serialize(obj) for obj in session.scalars(
        select(model).where(model.project_id == project_id)).all()]


@app.get("/health/live")
@app.get("/health")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
async def ready(redis: RedisDep):
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        await redis.ping()
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ready"}


@app.get("/v1/api-keys")
def keys(session: DB, key: Admin):
    return [{k: v for k, v in item.items() if k != "key_hash"}
            for item in list_owned(session, APIKey, key.project_id)]


@app.post("/v1/api-keys", status_code=201)
def create_key(body: KeyCreate, session: DB, key: Admin):
    raw = issue_key()
    item = APIKey(project_id=key.project_id, key_hash=hash_key(raw), **body.model_dump())
    session.add(item)
    session.flush()
    audit(session, key, "api_key.create", item.id)
    session.commit()
    return {"id": item.id, "name": item.name, "role": item.role, "key": raw}


@app.delete("/v1/api-keys/{key_id}", status_code=204)
def revoke_key(key_id: str, session: DB, key: Admin):
    item = owned(session, APIKey, key_id, key.project_id)
    item.revoked = True
    audit(session, key, "api_key.revoke", item.id)
    session.commit()


@app.get("/v1/models")
def models(key: Key):
    from si.models import Gateway

    return {"object": "list", "data": [
        {"id": model["id"], "object": "model", "owned_by": "si"}
        for model in Gateway().list_models() if model["available"]]}


@app.post("/v1/chat/completions")
async def chat(request: Request, session: DB, key: Key, redis: RedisDep):
    from si.guardrails import GuardrailError, Guardrails
    from si.models import ChatRequest, Gateway

    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("Expected an object")
        streaming = payload.pop("stream", False)
        if not isinstance(streaming, bool):
            raise ValueError("stream must be a boolean")
        if "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        response_format = payload.pop("response_format", None)
        if response_format:
            if response_format.get("type") == "json_schema":
                payload["response_schema"] = response_format["json_schema"]["schema"]
            elif response_format.get("type") == "json_object":
                payload["response_schema"] = {"type": "object"}
        for tool in payload.get("tools", []):
            if tool.get("type") == "function":
                function = tool["function"]
                tool.clear()
                tool.update(name=function["name"], description=function.get("description", ""),
                            parameters=function.get("parameters", {"type": "object"}))
        for message_input in payload.get("messages", []):
            for call in message_input.get("tool_calls") or []:
                if call.get("type") == "function":
                    function = call.pop("function")
                    call.pop("type")
                    call["name"] = function["name"]
                    call["arguments"] = json.loads(function["arguments"])
            if message_input.get("content") is None:
                message_input["content"] = ""
        body = ChatRequest.model_validate(payload)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise HTTPException(422, "Invalid chat request") from exc
    guard = Guardrails()
    try:
        body = guard.check_request(body)
    except GuardrailError as exc:
        raise HTTPException(400, str(exc)) from exc
    maximum = body.max_tokens or 1024
    estimate = len(json.dumps(body.model_dump(mode="json")).encode()) + maximum
    reservation = await reserve(redis, key.project_id, estimate, estimate * 0.0001)
    cache_key = "si:cache:v1:" + key.project_id + ":" + hashlib.sha256(
        json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    cached = None
    try:
        if body.temperature == 0 and not streaming:
            cached = await redis.get(cache_key)
        if cached:
            result_data = json.loads(cached)
            result_data["choices"][0]["message"]["content"] = guard.post(
                result_data["choices"][0]["message"]["content"])
            await reservation.reconcile(0, 0)
            record_span(session, key.project_id, "gateway", body.model,
                        outputs={"cache_hit": True})
            audit(session, key, "chat.completions")
            session.commit()
            return result_data
        started = time.perf_counter()
        result = await Gateway().chat(body)
        result.content = guard.post(result.content or "")
    except GuardrailError as exc:
        await reservation.reconcile(
            getattr(locals().get("result"), "input_tokens", 0) +
            getattr(locals().get("result"), "output_tokens", 0),
            getattr(locals().get("result"), "cost", 0))
        raise HTTPException(400, str(exc)) from exc
    except RedisError as exc:
        raise HTTPException(503, "Cache service unavailable") from exc
    except Exception as exc:
        # Retain the reservation on unknown upstream failures: usage might have accrued.
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(502, "Model request failed") from exc
    await reservation.reconcile(result.input_tokens + result.output_tokens, result.cost)
    record_span(session, key.project_id, "gateway", body.model,
                inputs={"messages": [message.model_dump() for message in body.messages]},
                outputs={"model": result.model, "content": result.content,
                         "tool_calls": [call.model_dump() for call in result.tool_calls]},
                latency_ms=(time.perf_counter() - started) * 1000,
                tokens=result.input_tokens + result.output_tokens, cost=result.cost)
    audit(session, key, "chat.completions")
    session.commit()
    message: dict[str, Any] = {"role": "assistant", "content": result.content}
    openai_calls = [{"id": call.id, "type": "function",
                     "function": {"name": call.name, "arguments": json.dumps(call.arguments)}}
                    for call in result.tool_calls]
    if openai_calls:
        message["tool_calls"] = openai_calls
    response = {"id": "chatcmpl-" + new_id(), "object": "chat.completion",
                "created": int(time.time()), "model": result.model,
                "choices": [{"index": 0, "message": message,
                             "finish_reason": "tool_calls" if result.tool_calls else "stop"}],
                "usage": {"prompt_tokens": result.input_tokens,
                          "completion_tokens": result.output_tokens,
                          "total_tokens": result.input_tokens + result.output_tokens}}
    if body.temperature == 0 and not streaming:
        try:
            await redis.set(cache_key, json.dumps(response), ex=settings.cache_ttl)
        except RedisError as exc:
            raise HTTPException(503, "Cache service unavailable") from exc
    if not streaming:
        return response

    async def stream():
        # Buffer before emission so post-generation policy can never leak rejected content.
        base = {"id": response["id"], "object": "chat.completion.chunk",
                "created": response["created"], "model": result.model}
        yield "data: " + json.dumps({**base, "choices": [
            {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}) + "\n\n"
        content = result.content or ""
        for offset in range(0, len(content), 32):
            yield "data: " + json.dumps({**base, "choices": [
                {"index": 0, "delta": {"content": content[offset:offset + 32]},
                 "finish_reason": None}]}) + "\n\n"
        if result.tool_calls:
            yield "data: " + json.dumps({**base, "choices": [
                {"index": 0, "delta": {"tool_calls": [
                    {"index": index, **call} for index, call in enumerate(openai_calls)]},
                 "finish_reason": None}]}) + "\n\n"
        yield "data: " + json.dumps({**base, "choices": [
            {"index": 0, "delta": {}, "finish_reason": "tool_calls" if result.tool_calls else "stop"}],
            "usage": response["usage"]}) + "\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/v1/collections")
def collections(session: DB, key: Key):
    return list_owned(session, Collection, key.project_id)


@app.post("/v1/collections", status_code=201)
def create_collection(body: CollectionCreate, session: DB, key: Key):
    item = Collection(project_id=key.project_id, **body.model_dump())
    session.add(item)
    session.flush()
    audit(session, key, "collection.create", item.id)
    session.commit()
    return serialize(item)


@app.post("/v1/collections/{collection_id}/documents")
async def ingest_document(collection_id: str, file: UploadFile, session: DB, key: Key, redis: RedisDep):
    from si.agents.runtime import ProjectGateway
    from si.rag import ingest
    owned(session, Collection, collection_id, key.project_id)
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "Document exceeds 10 MiB")
    try:
        result = await ingest(session, key.project_id, collection_id,
                              file.filename or "document.txt", content,
                              gateway=ProjectGateway(session, key.project_id, redis))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(session, key, "document.ingest", collection_id)
    session.commit()
    return result


@app.post("/v1/collections/{collection_id}/search")
async def search_collection(collection_id: str, body: Search, session: DB, key: Key, redis: RedisDep):
    from si.agents.runtime import ProjectGateway
    from si.rag import search
    owned(session, Collection, collection_id, key.project_id)
    result = await search(session, key.project_id, collection_id, body.query, body.limit,
                          gateway=ProjectGateway(session, key.project_id, redis))
    audit(session, key, "collection.search", collection_id)
    session.commit()
    return result


@app.get("/v1/agents")
def agents(session: DB, key: Key):
    return list_owned(session, Agent, key.project_id)


@app.get("/v1/memory")
def memory(session_id: str, session: DB, key: Key):
    from si.agents.memory import MemoryStore
    try:
        return [serialize(item) for item in MemoryStore(session).load(key.project_id, session_id)]
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/v1/memory", status_code=201)
def remember(body: MemoryCreate, session: DB, key: Key):
    from si.agents.memory import MemoryStore
    from si.guardrails import GuardrailError, Guardrails
    try:
        guard = Guardrails()
        item = MemoryStore(session).remember(
            key.project_id, body.session_id, guard.post(guard.pre(body.content)),
            body.role, body.long_term,
        )
    except (ValueError, GuardrailError) as exc:
        raise HTTPException(422, str(exc)) from exc
    session.flush()
    audit(session, key, "memory.create", item.id)
    session.commit()
    return serialize(item)


def check_collections(session: Session, project_id: str, ids: list[str]):
    for collection_id in ids:
        owned(session, Collection, collection_id, project_id)


@app.post("/v1/agents", status_code=201)
def create_agent(body: AgentCreate, session: DB, key: Key):
    check_collections(session, key.project_id, body.collection_ids)
    item = Agent(project_id=key.project_id, **body.model_dump())
    session.add(item)
    session.flush()
    audit(session, key, "agent.create", item.id)
    session.commit()
    return serialize(item)


@app.patch("/v1/agents/{agent_id}")
def update_agent(agent_id: str, body: AgentPatch, session: DB, key: Key):
    item = owned(session, Agent, agent_id, key.project_id)
    values = body.model_dump(exclude_unset=True)
    if any(value is None for value in values.values()):
        raise HTTPException(422, "Fields cannot be null")
    check_collections(session, key.project_id, values.get("collection_ids", []))
    for name, value in values.items():
        setattr(item, name, value)
    audit(session, key, "agent.update", item.id)
    session.commit()
    return serialize(item)


async def enqueue_run(run_id: str):
    pool = None
    try:
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await pool.enqueue_job("execute_agent", run_id, _job_id=f"run:{run_id}:{new_id()}")
    except (RedisError, OSError) as exc:
        raise HTTPException(503, "Worker queue unavailable; run remains queued for recovery") from exc
    finally:
        if pool:
            await pool.aclose()


@app.post("/v1/agents/{agent_id}/runs", status_code=201)
async def create_run(agent_id: str, body: RunCreate, session: DB, key: Key, redis: RedisDep):
    from si.agents import execute_run
    owned(session, Agent, agent_id, key.project_id)
    run = Run(project_id=key.project_id, agent_id=agent_id, input=body.input,
              session_id=body.session_id)
    session.add(run)
    session.flush()
    audit(session, key, "run.create", run.id)
    session.commit()
    if body.background:
        await enqueue_run(run.id)
    else:
        await execute_run(session, run.id, redis=redis)
    session.refresh(run)
    return serialize(run)


@app.get("/v1/runs/{run_id}")
def get_run(run_id: str, session: DB, key: Key):
    return serialize(owned(session, Run, run_id, key.project_id))


@app.get("/v1/runs/{run_id}/events")
async def run_events(run_id: str, session: DB, key: Key, request: Request):
    owned(session, Run, run_id, key.project_id)
    session_factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    session.close()
    last = request.headers.get("last-event-id", "-1")
    try:
        offset = max(0, int(last) + 1)
    except ValueError as exc:
        raise HTTPException(400, "Invalid Last-Event-ID") from exc

    async def events():
        nonlocal offset
        while not await request.is_disconnected():
            with session_factory() as polling_session:
                run = owned(polling_session, Run, run_id, key.project_id)
                pending_events = list(run.events[offset:])
                status = run.status
            for index, event in enumerate(pending_events, start=offset):
                yield f"id: {index}\ndata: {json.dumps(event)}\n\n"
                offset = index + 1
            if status in {"completed", "failed", "waiting_approval", "interrupted", "cancelled"}:
                yield "data: [DONE]\n\n"
                break
            yield ": heartbeat\n\n"
            await asyncio.sleep(0.25)
    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/v1/runs/{run_id}/approve")
async def approve(run_id: str, body: Approval, session: DB, key: Key, redis: RedisDep):
    from si.agents import approve_run
    run = owned(session, Run, run_id, key.project_id)
    if run.status != "waiting_approval":
        raise HTTPException(409, "Run is not waiting for approval")
    await approve_run(session, run_id, body.approved, redis=redis)
    audit(session, key, "run.approve" if body.approved else "run.reject", run_id)
    session.commit()
    session.refresh(run)
    return serialize(run)


@app.get("/v1/traces")
def traces(session: DB, key: Key, run_id: str | None = None):
    query = select(Span).where(Span.project_id == key.project_id)
    if run_id:
        query = query.where(Span.run_id == run_id)
    return [serialize(item) for item in session.scalars(query.order_by(Span.created_at.desc()).limit(500))]


@app.get("/v1/prompts")
def prompts(session: DB, key: Key):
    return list_owned(session, PromptVersion, key.project_id)


@app.post("/v1/prompts", status_code=201)
def create_prompt(body: PromptCreate, session: DB, key: Key):
    # Lock the project to serialize version assignment across concurrent requests.
    from si.db import Project
    session.execute(select(Project).where(Project.id == key.project_id).with_for_update())
    version = session.scalar(select(func.max(PromptVersion.version)).where(
        PromptVersion.project_id == key.project_id, PromptVersion.name == body.name)) or 0
    item = PromptVersion(project_id=key.project_id, version=version + 1, **body.model_dump())
    session.add(item)
    session.flush()
    audit(session, key, "prompt.create", item.id)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "Concurrent prompt version conflict; retry") from exc
    return serialize(item)


@app.get("/v1/prompts/{name}/versions")
def prompt_versions(name: str, session: DB, key: Key):
    return [serialize(item) for item in session.scalars(select(PromptVersion).where(
        PromptVersion.project_id == key.project_id, PromptVersion.name == name
    ).order_by(PromptVersion.version))]


@app.post("/v1/prompts/{name}/{version}/render")
def render_prompt(name: str, version: int, body: Render, session: DB, key: Key):
    item = session.scalar(select(PromptVersion).where(
        PromptVersion.project_id == key.project_id, PromptVersion.name == name,
        PromptVersion.version == version))
    if item is None:
        raise HTTPException(404, "Prompt version not found")
    try:
        for _, field, _, _ in string.Formatter().parse(item.template):
            if field is not None and not field.isidentifier():
                raise ValueError("Only simple named placeholders are supported")
        rendered = item.template.format_map(body.variables)
    except (KeyError, ValueError, AttributeError, IndexError) as exc:
        raise HTTPException(422, "Invalid template or missing variable") from exc
    audit(session, key, "prompt.render", item.id)
    session.commit()
    return {"name": name, "version": version, "rendered": rendered}


@app.get("/v1/evals")
def evals(session: DB, key: Key):
    return list_owned(session, EvalRun, key.project_id)


@app.post("/v1/evals", status_code=201)
async def create_eval(body: EvalCreate, session: DB, key: Key, redis: RedisDep):
    from si.evals import evaluate
    if bool(body.agent_id) == bool(body.prompt_name):
        raise HTTPException(422, "Specify exactly one agent_id or prompt_name")
    if body.agent_id:
        owned(session, Agent, body.agent_id, key.project_id)
    try:
        result = await evaluate(session, key.project_id, body.model_dump(), redis=redis)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(session, key, "eval.create")
    session.commit()
    return serialize(result) if isinstance(result, EvalRun) else result
