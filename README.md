# Super Intelligence (SI)

A local-first MVP for building AI applications: a model gateway, project-scoped
knowledge bases, tool-using agents, prompt versions, evaluations, and traces.
Python 3.12 / FastAPI / PostgreSQL + pgvector / Redis / Next.js.
Licensed under the existing **AGPL-3.0** [LICENSE](LICENSE).

## Quickstart — no model keys required

Requires Docker with Compose v2. Ports 8000 and 3000 must be free.

```sh
cp .env.example .env
docker compose up --build
```

Open **http://localhost:3000** for the console and
**http://localhost:8000/docs** for interactive OpenAPI documentation.
Use the local demo API key `si-local-development` (or your `SI_BOOTSTRAP_KEY`).
The deterministic `mock` provider needs no network access or provider account.
It demonstrates tool calls and echoes/combines observations; it is **not an LLM**.

In the console:

1. Create a knowledge collection; upload `examples/knowledge.txt`.
2. Create an agent using `mock`, tools `rag_search` and `calculator`, and select
   that collection.
3. Run `What is SI? Also calculate 2 + 3.`; inspect the persisted run and traces.
4. Run `examples/eval.json` against the agent with the `contains` scorer.
5. Try streaming chat in the playground.

Data survives restarts. `docker compose down -v` **deletes** the databases.
Only the API and console ports are published, bound to localhost by default.

### Python SDK and CLI

Install [uv](https://docs.astral.sh/uv/), then:

```sh
uv sync --project sdk/python
export SI_API_KEY=si-local-development
uv run --project sdk/python si chat "Hello SI" --stream
```

An end-to-end example (run from the repository root):

```python
from si_sdk import Client

with Client() as si:  # SI_API_KEY; SI_API_URL defaults to http://localhost:8000
    collection = si.create_collection("Getting started")
    si.ingest(collection["id"], "examples/knowledge.txt")
    agent = si.create_agent(
        name="Knowledge assistant", model="mock",
        system_prompt="Search the knowledge base and use calculator for arithmetic.",
        tools=["rag_search", "calculator"], collection_ids=[collection["id"]],
        max_steps=6,
    )
    run = si.run_agent(agent["id"], "What is SI? Also calculate 2 + 3.")
    print(run["output"])
    print(si.traces())
    print(si.evaluate(
        [{"input": "What is 2 + 3?", "expected": "5"}],
        scorer="contains", agent_id=agent["id"],
    ))
```

Run that example with `uv run --project sdk/python python your_example.py`, or:

```sh
uv run --project sdk/python si ingest examples/knowledge.txt --collection COLLECTION_ID
uv run --project sdk/python si agent run AGENT_ID "What is 6 * 7?"
uv run --project sdk/python si eval run examples/eval.json \
  --agent AGENT_ID --scorer contains --threshold 1
```

`si eval run` exits **0** at/above threshold, **1** below it, and **2** on errors.
The agent run endpoint queues work in Redis; the separate worker executes it.
Run events are available as SSE at `/v1/runs/{id}/events`. Sensitive tools pause
for project-authorized approval via `/v1/runs/{id}/approve`.

## Layers

| Layer | Location | MVP implementation |
|---|---|---|
| Models | `api/si/models` | Mock, OpenAI, Anthropic, Gemini, OpenAI-compatible REST adapters; policies, retries/fallback, tool calls, schemas, usage |
| Context | `api/si/rag` | txt/md/pdf/html ingestion, overlapping chunks, 64-dimensional embeddings, vector + full-text RRF, URL connector, reranker seam |
| Agents | `api/si/agents` | Persisted definitions, bounded tool loop, session/long-term memory, approvals, MCP client, ARQ jobs |
| Developer tools | `sdk/python`, `web` | SDK/CLI, streaming playground, agent editor, uploads, traces, evals |
| Observability | `api/si/tracing`, `api/si/evals` | Persisted spans/results, exact/contains/regex/schema/LLM-judge scorers |
| Governance | `api/si/guardrails`, auth/quotas | Regex PII redaction, injection/block filters, hashed project keys/RBAC, audit, Redis limits/budgets/cache |

See [architecture](docs/architecture.md) and
[getting started](docs/getting-started.md) for interfaces, limitations, and operations.

## Configuration

Copy `.env.example`; never commit `.env`. Secrets are supplied only via environment.
To change an initialized bootstrap key, use the API key management endpoints;
changing the env value does not revoke previously issued keys.

| Variable | Purpose |
|---|---|
| `SI_BOOTSTRAP_KEY` | Initial local project's admin key; replace before deployment |
| `POSTGRES_PASSWORD` | Compose database password; local demo default is not a production credential |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` | Optional hosted adapters |
| `OPENAI_COMPATIBLE_BASE_URL`, `OPENAI_COMPATIBLE_API_KEY` | Ollama/vLLM or another compatible service |
| `EMBEDDING_MODEL` | Optional OpenAI/compatible embedding model; must support 64-dimensional vectors |
| `SI_RATE_LIMIT` | Requests/minute per project (default 120) |
| `SI_MONTHLY_TOKENS`, `SI_MONTHLY_COST` | Monthly project budgets (default 1,000,000 tokens / 100 USD) |
| `SI_CACHE_TTL` | Deterministic chat response cache lifetime (default 300 seconds) |
| `HTTP_ALLOWED_HOSTS` | Comma-separated explicit public-host allowlist for outbound tools/connectors |
| `SI_MCP_SERVERS` | JSON map of MCP server names to public Streamable HTTP endpoint URLs |
| `SI_ENABLE_PYTHON_TOOL` | Opt in to subprocess execution; default false |
| `NEXT_PUBLIC_API_URL`, `CORS_ORIGINS` | Browser API URL (build-time), permitted console origins |

Embeddings default to deterministic 64-dimensional mock vectors, even if chat
provider keys are configured. They support the demo, not semantic search quality.
Changing the embedding model/dimension requires re-ingestion and a schema migration.
The public chat API buffers a completion before sending SSE chunks so post-generation
guardrails and accounting finish before emission; the gateway also exposes upstream
streaming for integrations that can safely apply incremental policies.
Model policy metadata and USD prices are estimates, not billing statements.
Provider credentials stay on the backend; the console uses only the project API key.

## Extension points

- **Provider:** implement the gateway's `chat`, `stream`, and `embed` contract,
  register model metadata/routing, normalize tool calls and usage, and add mocked
  HTTP contract tests. Set credentials through env, not source.
- **Tool:** add a JSON-schema tool definition and handler to the agent tool registry.
  Keep project scope explicit; mark side-effecting/network tools sensitive.
  Validate arguments, bound output/runtime, and test approval/resume.
- **Scorer:** register a scorer in `api/si/evals`; return a bounded score from
  input/expected/actual. Add deterministic tests before using it in CI.
- **Connector/reranker/guardrail:** use the context and guardrail interfaces rather
  than bypassing ingestion, ownership, or safety checks.

## Development and CI

```sh
cd api
uv sync --frozen
uv run ruff check si tests
uv run mypy si
uv run pytest -q
# With DATABASE_URL pointing to pgvector/Postgres, tests cover the real DB path.

cd ../sdk/python
uv sync --frozen
uv run ruff check .
uv run mypy src
uv run pytest -q

cd ../../web
pnpm install --frozen-lockfile
pnpm lint
pnpm typecheck
pnpm build
pnpm exec playwright install --with-deps chromium
pnpm test
```

GitHub Actions runs these checks with Postgres/Redis services, builds Docker images,
and uploads browser reports/screenshots. Health: `/health`; readiness:
`/health/ready` checks database and Redis.

## Security boundaries

This is an **MVP, not a production multi-tenant security boundary**.
Use TLS, rotate demo keys, protect Redis/Postgres, restrict ingress/egress, and
review retention before deploying. Traces/memory can contain sensitive application
data; regex PII filtering is deliberately incomplete. Injection heuristics are
defense-in-depth, not a guarantee.

Python execution uses a timed/resource-limited subprocess with a scrubbed
environment, disabled by default. **It is not production-grade isolation**:
an approved program can still access worker files and network resources.
Use a separate hardened container/VM service with explicit network/filesystem policy
for untrusted code. MCP servers and HTTP tools are also trusted integrations, require
approval, and use the public-host allowlist. Never expose this demo key publicly.
