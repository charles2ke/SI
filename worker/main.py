from arq.connections import RedisSettings
from sqlalchemy import select

from si.config import settings
from si.db import Run, SessionLocal


async def execute_agent(ctx, run_id: str):
    from si.agents import execute_run

    with SessionLocal() as session:
        run = session.get(Run, run_id)
        if run is None or run.status not in {"queued", "interrupted"}:
            return
        await execute_run(session, run_id)


async def startup(ctx):
    with SessionLocal() as session:
        runs = session.scalars(select(Run).where(Run.status.in_(["queued", "running"]))).all()
        for run in runs:
            if run.status == "running":
                # Never blindly replay a tool which might have executed before a worker crash.
                run.status = "interrupted"
                run.error = "Worker interrupted; inspect persisted events before retrying"
            else:
                await ctx["redis"].enqueue_job("execute_agent", run.id,
                                                _job_id=f"recover:{run.id}")
        session.commit()


class WorkerSettings:
    functions = [execute_agent]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = startup
    max_tries = 1
    job_timeout = 300
