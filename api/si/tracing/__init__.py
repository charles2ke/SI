from contextlib import contextmanager
from time import perf_counter
from typing import Any

from sqlalchemy.orm import Session

from si.db import Span


def record_span(session: Session, project_id: str, kind: str, name: str,
                inputs: dict[str, Any] | None = None, outputs: dict[str, Any] | None = None,
                run_id: str | None = None, latency_ms: float = 0,
                tokens: int = 0, cost: float = 0) -> Span:
    span = Span(project_id=project_id, run_id=run_id, kind=kind, name=name,
                inputs=inputs or {}, outputs=outputs or {}, latency_ms=latency_ms,
                tokens=tokens, cost=cost)
    session.add(span)
    return span


@contextmanager
def trace(session: Session, project_id: str, kind: str, name: str, **kwargs: Any):
    started = perf_counter()
    result: dict[str, Any] = {}
    try:
        yield result
    except Exception:
        result["error"] = "operation_failed"
        raise
    finally:
        record_span(session, project_id, kind, name, outputs=result,
                    latency_ms=(perf_counter() - started) * 1000, **kwargs)
