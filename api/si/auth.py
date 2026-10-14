import hashlib
import logging
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from si.config import settings
from si.db import APIKey, Audit, Project, SessionLocal, get_session
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


def require_key(request: Request, session: Annotated[Session, Depends(get_session)]) -> APIKey:
    header = request.headers.get("authorization", "")
    raw = header[7:] if header.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
    key = session.scalar(select(APIKey).where(
        APIKey.key_hash == hash_key(raw), APIKey.revoked.is_(False)))
    if key is None:
        raise HTTPException(401, "Invalid API key", headers={"WWW-Authenticate": "Bearer"})
    if request.method not in {"GET", "HEAD", "OPTIONS"} and key.role == "viewer":
        raise HTTPException(403, "Viewer keys are read-only")
    record_span(session, key.project_id, "request", request.method + " " + request.url.path)
    session.commit()
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
