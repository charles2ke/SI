import hashlib
import logging
import secrets
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from si.config import settings
from si.db import APIKey, Audit, Project, SessionLocal, get_session
from si.quotas import check_request_rate, get_redis
from si.tracing import record_span


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def bootstrap() -> None:
    with SessionLocal() as session:
        if session.scalar(select(APIKey).where(APIKey.key_hash == hash_key(settings.bootstrap_key))):
            return
        project = session.get(Project, "default")
        if project is None:
            project = Project(id="default", name="Default")
            session.add(project)
            session.flush()
        session.add(APIKey(project_id=project.id, name="Bootstrap", role="admin",
                           key_hash=hash_key(settings.bootstrap_key)))
        session.commit()
    if settings.bootstrap_key == "si-local-development":
        logging.getLogger("si").warning(
            '{"event":"demo_bootstrap_credential","message":"Set SI_BOOTSTRAP_KEY before deployment"}')


async def require_key(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    redis: Annotated[Any, Depends(get_redis)],
) -> APIKey:
    header = request.headers.get("authorization", "")
    raw = header[7:] if header.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
    key = session.scalar(select(APIKey).where(
        APIKey.key_hash == hash_key(raw), APIKey.revoked.is_(False)))
    if key is None:
        raise HTTPException(401, "Invalid API key", headers={"WWW-Authenticate": "Bearer"})
    await check_request_rate(redis, key.project_id)
    if request.method not in {"GET", "HEAD", "OPTIONS"} and key.role == "viewer":
        raise HTTPException(403, "Viewer keys are read-only")
    span = record_span(session, key.project_id, "request", request.method + " " + request.url.path,
                       inputs={"method": request.method, "path": request.url.path})
    session.commit()
    request.state.trace_span = span
    request.state.trace_session = session
    return key


def require_admin(key: Annotated[APIKey, Depends(require_key)]) -> APIKey:
    if key.role != "admin":
        raise HTTPException(403, "Admin role required")
    return key


def issue_key() -> str:
    return "si_" + secrets.token_urlsafe(32)


def audit(session: Session, key: APIKey, action: str, resource_id: str | None = None) -> None:
    session.add(Audit(project_id=key.project_id, key_id=key.id, action=action,
                      resource_id=resource_id))
