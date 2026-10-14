# Getting started

## Local deployment

1. Install Docker/Compose v2, copy `.env.example` to `.env`.
2. Run `docker compose up --build`. The API migrates Postgres and initializes the
   local project's hashed admin key before becoming ready.
3. Open `http://localhost:3000`; enter the bootstrap key.
4. Create a collection and upload `examples/knowledge.txt`.
5. Create an agent using mock + `rag_search` + `calculator`, with your collection.
6. Run a question plus arithmetic and inspect Traces.
7. Run an eval with `contains` and the pairs in `examples/eval.json`.

`docker compose logs -f api worker` shows request logs and worker status.
If a run remains queued, check worker connectivity to Redis and readiness of the API.
Use `docker compose down` to stop without deleting persisted data.

## REST example

```sh
export SI_API_KEY=si-local-development
curl -s http://localhost:8000/v1/chat/completions \
  -H "$(printf 'Authorization: %s %s' Bearer "$SI_API_KEY")" -H "Content-Type: application/json" \
  -d '{"model":"mock","messages":[{"role":"user","content":"Hello"}]}'
curl -N http://localhost:8000/v1/chat/completions \
  -H "$(printf 'Authorization: %s %s' Bearer "$SI_API_KEY")" -H "Content-Type: application/json" \
  -d '{"model":"mock","stream":true,"messages":[{"role":"user","content":"Hello"}]}'
```

Use the interactive `/docs` for collection uploads, agents, API keys, memory,
prompt versions, approvals and evaluation payloads. Models are listed at
`/v1/models`; names/policy availability depend on configured providers.

## Hosted or local models

Set a provider's key in `.env`, restart API/worker, then select its model identifier.
Compatible endpoints use `OPENAI_COMPATIBLE_BASE_URL` (including `/v1` when required
by that server) and an optional key. For a host Ollama/vLLM service, use a
container-reachable hostname instead of `localhost`; containers' localhost refers
to themselves. Provider API adapters are covered by mocked transport tests;
live paid-provider behavior requires credentials and a separate smoke test.

Keep embeddings fixed for each collection. The default mock embedding is suitable
only for this deterministic demo. Re-ingest after changing the embedding backend.

## Prompt versions and evaluation

Create a prompt with `POST /v1/prompts` using `name` and `template` (for example,
`Answer this: {input}`). Creating the same name again adds a new version.
List `/v1/prompts/{name}/versions`; render via
`POST /v1/prompts/{name}/{version}/render` with `{"variables":{"input":"Hello"}}`.

```sh
uv run --project sdk/python si eval run examples/eval.json \
  --agent AGENT_ID --scorer contains --threshold 1
```

Use `--prompt NAME --version NUMBER` instead to evaluate a specific prompt version.
Exact match, contains, regex, JSON-schema validation and model-judge scorers have
different expected-value formats:

| Scorer | Dataset `expected` |
|---|---|
| `exact_match` | The complete output string |
| `contains` | A substring that must occur in the output |
| `regex` | A regex pattern (bounded, isolated execution) |
| `json_schema` | A JSON-schema object; the output must parse as valid JSON |
| `llm_judge` | Desired answer/criteria, compared by a model returning score 0–1 |

Use `--model openai:gpt-4o-mini` (with a configured key) for prompt evaluation or a
hosted judge. The default mock judge is deterministic but **does not measure quality**.
Schema references and regex keywords may be restricted to prevent remote fetching
or unbounded validation; use simple inline schemas.
The CLI exits nonzero below threshold or on execution errors.

## Approvals and outbound tools

Only allow public hosts you trust with `HTTP_ALLOWED_HOSTS`. Sensitive HTTP, Python
and MCP tool calls stop in an approval state; inspect the pending tool arguments
before approving `/v1/runs/{id}/approve`. Python also needs
`SI_ENABLE_PYTHON_TOOL=true`; leave it disabled for untrusted users.

Do not treat approval as sandboxing. MCP tools can have external side effects.
Subprocess code shares the worker's filesystem/network boundary; use an isolated
execution service for production.

For MCP Streamable HTTP, set e.g.
`SI_MCP_SERVERS={"demo":"https://your-public-server.example/mcp"}` and allow that
hostname in `HTTP_ALLOWED_HOSTS`. Enable an advertised tool named `lookup` as
`mcp_demo_lookup` in the agent's `tools` list through the REST API.
The client initializes the server, lists schemas, then calls enabled tools with
approval. This MVP does not launch stdio servers or implement OAuth discovery.

## Operations

- `/health` is liveness; `/health/ready` checks Postgres and Redis.
- Use migrations (`cd api && uv run alembic upgrade head`) rather than editing
  deployment tables manually.
- Project API keys are returned once; keys can be revoked through the admin API.
- Redis quotas/cache/jobs and Postgres application records have separate persistent
  volumes. Back up both for durable run state; Redis data is not the audit source.
- Changing Postgres credentials after initialization also requires rotating them
  inside Postgres. Environment changes alone do not change the existing DB user.
- CORS permits the configured console origin; it is not an authentication boundary.
- `NEXT_PUBLIC_API_URL` is baked into the web build; rebuild when changing it.
- Configure budgets and outbound allowlists before exposing any public endpoint.
