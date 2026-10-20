FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
WORKDIR /app
COPY api /app/api
COPY worker /app/worker
RUN uv sync --project api --frozen --no-dev
ENV PATH="/app/api/.venv/bin:$PATH" PYTHONPATH="/app/api:/app"
RUN useradd --create-home si && chown -R si:si /app
USER si
EXPOSE 8000
CMD ["sh", "-c", "alembic -c api/alembic.ini upgrade head && uvicorn si.main:app --host 0.0.0.0 --port 8000"]
