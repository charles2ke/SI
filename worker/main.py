import asyncio

from arq.connections import RedisSettings
from sqlalchemy import select

from si.agents import append_event, mark_cancelled
from si.config import settings
from si.db import Run, SessionLocal
from si.quotas import get_redis


async def execute_agent(ctx, run_id: str):
    from si.agents import execute_run

    with SessionLocal() as session:
        run = session.get(Run, run_id)
        if run is None or run.status != "queued":
            return
        try:
            await execute_run(session, run_id)
        except asyncio.CancelledError:
            session.rollback()
            raise


async def startup(ctx):
    with SessionLocal() as session:
        runs = session.scalars(
            select(Run).where(Run.status.in_(["queued", "running", "cancelling"]))
        ).all()
        for run in runs:
            if run.status == "running":
                # Never blindly replay a tool which might have executed before a worker crash.
                run.status = "interrupted"
                run.error = "Worker interrupted; inspect persisted events before retrying"
                append_event(run, "interrupted", run.error)
            elif run.status == "cancelling":
                mark_cancelled(run, "running")
            else:
                await ctx["redis"].enqueue_job("execute_agent", run.id,
                                                _job_id=f"recover:{run.id}")
        session.commit()


async def shutdown(ctx):
    await get_redis().aclose()


class WorkerSettings:
    functions = [execute_agent]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = startup
    on_shutdown = shutdown
    max_tries = 1
    job_timeout = 300
