import { test, expect, type Locator, type Page } from "@playwright/test";
import { Api, streamChat } from "../src/lib/api";

type Body = Record<string, unknown> | null;
type Handler = (path: string, method: string, body: Body, query: URLSearchParams) => unknown;
/** Return from a mock handler to send a non-200 response. */
class Reply { constructor(readonly status: number, readonly body?: unknown) {} }

async function connect(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Use local dev key" }).click();
  expect(await page.evaluate(() => ({ local: Object.keys(localStorage), session: Object.keys(sessionStorage) }))).toEqual({ local: [], session: [] });
}
async function mock(page: Page, handler?: Handler) {
  await page.route("http://localhost:8000/**", async route => {
    const req = route.request();
    if (req.method() === "OPTIONS") { await route.fulfill({ status: 204 }); return; }
    expect(req.headers().authorization).toBe(["Bearer", "si-local-development"].join(" "));
    const url = new URL(req.url());
    const data = req.headers()["content-type"]?.includes("application/json") ? req.postDataJSON() : null;
    const result = handler?.(url.pathname, req.method(), data, url.searchParams);
    if (result instanceof Reply) { await route.fulfill(result.body === undefined ? { status: result.status } : { status: result.status, json: result.body }); return; }
    await route.fulfill({ json: result ?? (url.pathname === "/v1/models" ? { data: [{ id: "local-model" }] } : { data: [] }) });
  });
}
const go = async (page: Page, tab: string) => page.getByRole("navigation", { name: "Workspace" }).getByRole("button", { name: tab, exact: true }).click();
async function fitsMobile(page: Page) {
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}
/** Number of rendered lines in an element's text; more than one means it wrapped. */
const lineCount = (locator: Locator) => locator.evaluate(el => { const range = document.createRange(); range.selectNodeContents(el); return range.getClientRects().length; });

test("connect, stream chat, and render responsive workspace", async ({ page }, testInfo) => {
  await mock(page);
  await page.route("**/v1/chat/completions", async route => {
    expect(route.request().postDataJSON()).toMatchObject({ model: "local-model", stream: true, messages: [{ role: "user", content: "Hello" }] });
    await route.fulfill({ contentType: "text/event-stream", body: 'data: {"choices":[{"delta":{"content":"Hello 🌍"}}]}\n\ndata: {"choices":[{"delta":{"content":" — ready."}}]}\n\ndata: [DONE]\n\n' });
  });
  await page.goto("/");
  await expect(page.getByText("Your workspace is ready")).toBeVisible();
  await page.getByRole("button", { name: "Use local dev key" }).click();
  await expect(page.getByLabel("Model", { exact: true })).toHaveValue("local-model");
  await page.getByLabel("Message", { exact: true }).fill("Hello");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByRole("log")).toContainText("Hello 🌍 — ready.");
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath("console-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("button", { name: "Send message" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("console-mobile.png"), fullPage: true });
  await page.reload();
  await expect(page.getByText("Your workspace is ready")).toBeVisible();
  await expect(page.getByLabel("API key", { exact: true })).toHaveValue("");
});

test("API and streaming errors surface accessibly", async ({ page }) => {
  await page.route("http://localhost:8000/**", route => route.fulfill({ status: 401, json: { detail: "Invalid API key" } }));
  await connect(page);
  await expect(page.getByRole("alert").filter({ hasText: "API 401" })).toContainText("API 401: Invalid API key");
  await mock(page);
  await go(page, "Agents");
  await go(page, "Playground");
  await page.route("**/v1/chat/completions", route => route.fulfill({ contentType: "text/event-stream", body: 'data: {"error":{"message":"Provider unavailable"}}\n\n' }));
  await expect(page.getByLabel("Model", { exact: true })).toHaveValue("local-model");
  await page.getByLabel("Message", { exact: true }).fill("Hello");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Provider unavailable" })).toBeVisible();
});

test("create an agent from the tool catalog, run it, and approve a sensitive call", async ({ page }, testInfo) => {
  let agent: Body = null;
  let patched = false;
  let run: Body = null;
  const call = { id: "call-1", name: "http_fetch", arguments: { url: "https://docs.example.com" } };
  await mock(page, (path, method, body, query) => {
    if (path === "/v1/models") return { data: [{ id: "local-model" }, { id: "other-model" }] };
    if (path === "/v1/tools") return {
      data: [
        { name: "calculator", description: "Evaluate arithmetic.", sensitive: false, available: true, unavailable_reason: null },
        { name: "http_fetch", description: "Fetch an allowlisted URL.", sensitive: true, available: true, unavailable_reason: null },
        { name: "python", description: "Run Python code.", sensitive: true, available: false, unavailable_reason: "Disabled; SI_ENABLE_PYTHON_TOOL=true is unsafe for production" },
      ],
      mcp_servers: [{ name: "docs", tool_prefix: "mcp_docs_" }],
    };
    if (path === "/v1/collections") return { data: [{ id: "docs", name: "Docs", chunks: 3 }] };
    if (path === "/v1/agents" && method === "POST") {
      expect(body).toEqual({ name: "Researcher", system_prompt: "Research carefully.", model: "other-model", tools: ["calculator", "http_fetch", "mcp_docs_search"], collection_ids: ["docs"], max_steps: 8 });
      agent = { id: "agent-1", ...body }; return agent;
    }
    if (path === "/v1/agents") return { data: agent ? [agent] : [] };
    if (path === "/v1/agents/agent-1" && method === "PATCH") { expect(body).toMatchObject({ name: "Updated researcher", model: "other-model" }); patched = true; agent = { ...agent, ...body }; return agent; }
    if (path === "/v1/runs") { expect(Object.fromEntries(query)).toEqual({ agent_id: "agent-1", limit: "20" }); return { data: run ? [run] : [] }; }
    if (path === "/v1/agents/agent-1/runs") {
      expect(body).toEqual({ input: "Find the answer", background: true, session_id: "session-7" });
      run = { id: "run-1", status: "queued", input: "Find the answer", session_id: "session-7", created_at: "2025-01-02T10:00:00", events: [] };
      return run;
    }
    if (path === "/v1/runs/run-1/approve") {
      expect(body).toEqual({ approved: true });
      run = { ...run, status: "completed", output: "Approved answer", pending_tool: null, events: [...(run?.events as unknown[]), { type: "approval", data: { tool_call_id: "call-1", approved: true } }, { type: "completed", data: { output: "Approved answer" } }] };
      return run;
    }
    if (path === "/v1/runs/run-1") {
      if (run?.status === "queued") run = { ...run, status: "waiting_approval", pending_tool: { ...call, approved: null, remaining: [] }, events: [{ type: "started", data: { steps: 0 }, at: "2025-01-02T10:00:01" }, { type: "model_response", data: { content: "", tool_calls: [call], step: 1 } }, { type: "approval_required", data: call }] };
      return run;
    }
  });
  await connect(page); await go(page, "Agents");
  await expect(page.getByText("No agents yet. Create your first one.")).toBeVisible();
  const tools = page.getByRole("group", { name: "Tools", exact: true });
  await expect(tools.getByRole("checkbox", { name: /^python/ })).toBeDisabled();
  await expect(tools.getByText("Requires approval")).toHaveCount(2);
  await expect(page.getByText("Configured servers: mcp_docs_…")).toBeVisible();
  await page.getByLabel("Name", { exact: true }).fill("Researcher");
  await page.getByLabel("System prompt").fill("Research carefully.");
  const model = page.getByRole("combobox", { name: "Model", exact: true });
  await expect(model).toHaveValue("local-model");
  await model.selectOption("other-model");
  await tools.getByRole("checkbox", { name: /^calculator/ }).check();
  await tools.getByRole("checkbox", { name: /^http_fetch/ }).check();
  await page.getByLabel("Additional tools").fill("mcp_docs_search");
  await page.getByRole("group", { name: "Knowledge collections" }).getByRole("checkbox", { name: /^Docs/ }).check();
  await page.getByRole("button", { name: "Save agent" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Agent saved." })).toBeVisible();
  await page.getByLabel("Name", { exact: true }).fill("Updated researcher");
  await page.getByRole("button", { name: "Save agent" }).click();
  await expect.poll(() => patched).toBe(true);
  await page.getByLabel("Task input").fill("Find the answer");
  await page.getByLabel("Session ID").fill("session-7");
  await page.getByRole("button", { name: "Start run" }).click();
  const detail = page.getByRole("region", { name: "Selected run" });
  await expect(detail.getByText("Approval required: http_fetch")).toBeVisible();
  await expect(detail.locator(".approval pre")).toContainText('"url": "https://docs.example.com"');
  await expect(detail.locator(".timeline")).toContainText("Requested http_fetch");
  await detail.getByRole("button", { name: "Approve tool call" }).click();
  await expect(detail.locator(".output")).toHaveText("Approved answer");
  await expect(detail.getByRole("heading", { name: "Run completed" })).toBeVisible();
  await expect(detail.locator(".timeline")).toContainText("Tool call approved");
  await expect(page.getByRole("region", { name: "Recent runs" }).getByRole("button", { name: /Find the answer/ })).toContainText("session session-7");
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath("agents-desktop.png"), fullPage: true });
  await fitsMobile(page);
  await page.screenshot({ path: testInfo.outputPath("agents-mobile.png"), fullPage: true });
});

test("reject, cancel, resume, trace, and delete agent runs", async ({ page }, testInfo) => {
  let agents = [{ id: "agent-1", name: "Researcher", system_prompt: "Research.", model: "local-model", tools: ["http_fetch"], collection_ids: [], max_steps: 8 }];
  let deletes = 0;
  let traceQuery: string | null = null;
  const pending = { id: "call-1", name: "http_fetch", arguments: { url: "https://example.com/report" } };
  const interrupted = "Worker interrupted; inspect persisted events before retrying";
  const runs: Record<string, Record<string, unknown>> = {
    "run-a": { id: "run-a", input: "Fetch the report", status: "waiting_approval", pending_tool: { ...pending, approved: null, remaining: [] }, created_at: "2025-01-02T10:00:00", events: [{ type: "approval_required", data: pending }] },
    "run-b": { id: "run-b", input: "Summarize findings", status: "interrupted", error: interrupted, session_id: "s-1", created_at: "2025-01-02T09:00:00", events: [{ type: "interrupted", data: interrupted }] },
  };
  const update = (id: string, changes: Record<string, unknown>, event: Record<string, unknown>) => {
    runs[id] = { ...runs[id], ...changes, events: [...(runs[id].events as unknown[]), event] };
    return runs[id];
  };
  await mock(page, (path, method, body, query) => {
    if (path === "/v1/agents") return { data: agents };
    if (path === "/v1/agents/agent-1" && method === "DELETE") {
      deletes += 1;
      if (deletes === 1) return new Reply(409, { detail: "Agent has 1 active run(s); cancel them before deleting" });
      agents = []; return new Reply(204);
    }
    if (path === "/v1/runs") return { data: Object.values(runs).map(row => ({ ...row, events: undefined })) };
    if (path === "/v1/runs/run-a/approve") { expect(body).toEqual({ approved: false }); return update("run-a", { status: "running", pending_tool: null }, { type: "approval", data: { tool_call_id: "call-1", approved: false } }); }
    if (path === "/v1/runs/run-a/cancel") { expect(body).toBeNull(); runs["run-a"] = { ...runs["run-a"], status: "cancelling" }; return runs["run-a"]; }
    if (path === "/v1/runs/run-a") {
      if (runs["run-a"].status === "cancelling") update("run-a", { status: "cancelled", error: "Cancelled by request" }, { type: "cancelled", data: { previous_status: "running" } });
      return runs["run-a"];
    }
    if (path === "/v1/runs/run-b/resume") { expect(body).toEqual({ background: true }); return update("run-b", { status: "queued", error: null }, { type: "resumed", data: { pending_tool: null } }); }
    if (path === "/v1/runs/run-b") {
      if (runs["run-b"].status === "queued") update("run-b", { status: "completed", output: "Recovered answer" }, { type: "completed", data: { output: "Recovered answer" } });
      return runs["run-b"];
    }
    if (path === "/v1/traces") {
      traceQuery = query.toString();
      return [{ id: "span-1", kind: "model", name: "local-model", run_id: "run-b", latency_ms: 31, tokens: 42, cost: 0, created_at: "2025-01-02T10:05:00", inputs: {}, outputs: { content: "Recovered answer" } }];
    }
  });
  await connect(page); await go(page, "Agents");
  await page.getByRole("button", { name: /^Researcher/ }).click();
  const history = page.getByRole("region", { name: "Recent runs" });
  const detail = page.getByRole("region", { name: "Selected run" });
  await history.getByRole("button", { name: /Fetch the report/ }).click();
  await expect(detail.getByText("Approval required: http_fetch")).toBeVisible();
  await detail.getByRole("button", { name: "Reject tool call" }).click();
  await expect(detail.locator(".timeline")).toContainText("Tool call rejected");
  await detail.getByRole("button", { name: "Cancel run", exact: true }).click();
  await detail.getByRole("group", { name: "Cancel run" }).getByRole("button", { name: "Yes, cancel run" }).click();
  await expect(detail.getByText("Cancellation requested.", { exact: false })).toBeVisible();
  await expect(detail.getByRole("heading", { name: "Run cancelled" })).toBeVisible();
  await expect(detail.locator(".timeline")).toContainText("Cancelled while running");
  await expect(detail.getByRole("button", { name: "Cancel run", exact: true })).toHaveCount(0);
  await expect(history.getByRole("button", { name: /Fetch the report/ })).toContainText("cancelled");

  await history.getByRole("button", { name: /Summarize findings/ }).click();
  await expect(detail.getByText("The worker stopped before this run finished.", { exact: false })).toBeVisible();
  await expect(detail.locator(".run-error")).toHaveText(interrupted);
  await detail.getByRole("button", { name: "Resume run" }).click();
  await expect(detail.locator(".output")).toHaveText("Recovered answer");
  await expect(detail.locator(".timeline")).toContainText("Resumed");
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath("run-lifecycle.png"), fullPage: true });

  await detail.getByRole("button", { name: "View traces" }).click();
  await expect(page.getByRole("heading", { name: "Traces", level: 1 })).toBeVisible();
  await expect(page.getByLabel("Run ID")).toHaveValue("run-b");
  await expect.poll(() => traceQuery).toBe("run_id=run-b");
  await expect(page.getByText("local-model", { exact: true })).toBeVisible();

  await go(page, "Agents");
  await page.getByRole("button", { name: /^Researcher/ }).click();
  await page.getByRole("button", { name: "Delete agent", exact: true }).click();
  await page.getByRole("button", { name: "Delete agent and its runs" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "409" })).toContainText("Agent has 1 active run(s)");
  await page.getByRole("button", { name: "Delete agent", exact: true }).click();
  await page.getByRole("button", { name: "Delete agent and its runs" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Agent deleted." })).toBeVisible();
  await expect(page.getByText("No agents yet. Create your first one.")).toBeVisible();
  expect(deletes).toBe(2);
});

test("manage collections, import knowledge, and search with scores", async ({ page }, testInfo) => {
  let collections: Record<string, unknown>[] = [{ id: "collection-0", name: "Archive", chunks: 4 }];
  let deletes = 0;
  const grow = (chunks: number) => {
    collections = collections.map(row => row.id === "collection-1" ? { ...row, chunks: Number(row.chunks) + chunks } : row);
    return chunks;
  };
  await mock(page, (path, method, body) => {
    if (path === "/v1/collections" && method === "POST") { expect(body).toEqual({ name: "Research" }); collections = [...collections, { id: "collection-1", name: "Research", chunks: 0 }]; return collections[1]; }
    if (path === "/v1/collections") return { data: collections };
    if (path === "/v1/collections/collection-1/documents") return { chunks: grow(2) };
    if (path === "/v1/collections/collection-1/urls") { expect(body).toEqual({ url: "https://docs.example.com/guide" }); return { url: "https://docs.example.com/guide", chunks: grow(3) }; }
    if (path === "/v1/collections/collection-1/search") {
      expect(body).toEqual({ query: "What is SI?", limit: 3 });
      return [{ id: "chunk-1", text: "SI connects intelligence.", score: 0.9 }, { id: "chunk-2", text: "Agents use tools.", score: 0.41 }];
    }
    if (path === "/v1/collections/collection-1" && method === "DELETE") {
      deletes += 1;
      if (deletes === 1) return new Reply(409, { detail: "Collection is used by agents: Researcher" });
      collections = collections.filter(row => row.id !== "collection-1"); return new Reply(204);
    }
  });
  await connect(page); await go(page, "Knowledge");
  await expect(page.getByRole("heading", { name: "Archive", exact: true })).toBeVisible();
  await page.getByLabel("Collection name").fill("Research");
  await page.getByRole("button", { name: "Create collection" }).click();
  await expect(page.getByRole("heading", { name: "Research", exact: true })).toBeVisible();
  await page.getByLabel("Document", { exact: true }).setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("SI connects intelligence.") });
  await page.getByRole("button", { name: "Upload document" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Document uploaded" })).toHaveText("Document uploaded. 2 chunks indexed.");
  await expect(page.getByRole("button", { name: /^Research/ })).toContainText("2 chunks · collection-1");
  await page.getByLabel("Web page URL").fill("https://docs.example.com/guide");
  await page.getByRole("button", { name: "Import URL" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Imported" })).toHaveText("Imported https://docs.example.com/guide. 3 chunks indexed.");
  await expect(page.getByRole("button", { name: /^Research/ })).toContainText("5 chunks");
  await page.getByLabel("Search query").fill("What is SI?");
  await page.getByLabel("Results", { exact: true }).fill("3");
  await page.getByRole("button", { name: "Search knowledge" }).click();
  const results = page.getByRole("region", { name: "Search results" }).getByRole("listitem");
  await expect(results).toHaveCount(2);
  await expect(results.first()).toContainText("Score 0.900");
  await expect(results.first()).toContainText("SI connects intelligence.");
  await expect(results.nth(1)).toContainText("Score 0.410");
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath("knowledge-desktop.png"), fullPage: true });
  await page.getByRole("button", { name: "Delete collection", exact: true }).click();
  await page.getByRole("button", { name: "Delete collection and chunks" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "409" })).toContainText("Collection is used by agents: Researcher");
  await page.getByRole("button", { name: "Delete collection", exact: true }).click();
  await page.getByRole("button", { name: "Delete collection and chunks" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Collection deleted." })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Research/ })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Archive", exact: true })).toBeVisible();
});

test("filter traces and evaluate agents and prompts", async ({ page }, testInfo) => {
  const traceQueries: string[] = [];
  const spans = [
    { id: "span-1", kind: "model", name: "local-model", run_id: "run-1", latency_ms: 24, tokens: 12, cost: 0.0012, created_at: "2025-01-02T10:00:00", inputs: { messages: 2 }, outputs: { content: "Hello" } },
    { id: "span-2", kind: "request", name: "GET /v1/models", run_id: null, latency_ms: 3, tokens: 0, cost: 0, created_at: "2025-01-02T10:00:01", inputs: {}, outputs: {} },
  ];
  const evalBodies: Body[] = [];
  const evals: Record<string, unknown>[] = [];
  await mock(page, (path, method, body, query) => {
    if (path === "/v1/traces") {
      traceQueries.push(query.toString());
      return spans.filter(span => (!query.get("kind") || span.kind === query.get("kind")) && (!query.get("run_id") || span.run_id === query.get("run_id")));
    }
    if (path === "/v1/agents") return { data: [{ id: "agent-1", name: "Researcher" }] };
    if (path === "/v1/prompts") return { data: [{ id: "p1", name: "greeting", version: 1, template: "Say hello to {input}" }, { id: "p2", name: "greeting", version: 2, template: "Greet {input} in {language}" }] };
    if (path === "/v1/evals" && method === "POST") {
      evalBodies.push(body);
      const dataset = body?.dataset as Record<string, unknown>[];
      const results = dataset.map((item, index) => ({ index, input: item.input, expected: item.expected, output: index ? `[${'"unbroken-token",'.repeat(12)}]` : '{"greeting": "Hello"}', score: index ? 0 : 1, passed: !index }));
      const config = { scorer: body?.scorer, threshold: body?.threshold, model: body?.model ?? "mock", agent_id: body?.agent_id ?? null, prompt_name: body?.prompt_name ?? null, prompt_version: body?.prompt_version ?? null, cases: dataset.length };
      const result = { id: `eval-${evals.length + 1}`, status: "completed", score: 1 / dataset.length, results, config, created_at: "2025-01-02T10:00:00" };
      evals.unshift(result);
      return result;
    }
    if (path === "/v1/evals") return evals;
  });
  await connect(page); await go(page, "Traces");
  await expect(page.getByText("GET /v1/models", { exact: true })).toBeVisible();
  await page.getByRole("combobox", { name: "Kind" }).selectOption("model");
  await expect(page.getByText("GET /v1/models", { exact: true })).toHaveCount(0);
  await page.getByLabel("Run ID").fill("run-1");
  await page.getByRole("button", { name: "Apply filter" }).click();
  await expect.poll(() => traceQueries.at(-1)).toBe("run_id=run-1&kind=model");
  expect(traceQueries).toContain("kind=model");
  await expect(page.getByText("Showing the latest 500 spans (Agent model calls) for run run-1.")).toBeVisible();
  await page.getByText("local-model", { exact: true }).click();
  await expect(page.locator(".trace pre")).toContainText('"latency_ms": 24');
  await expect(page.locator(".trace summary small")).toContainText("24 ms · 12 tokens · $0.0012");
  await page.screenshot({ path: testInfo.outputPath("traces-desktop.png"), fullPage: true });
  await page.getByRole("button", { name: "Clear" }).click();
  await expect.poll(() => traceQueries.at(-1)).toBe("");
  await expect(page.getByLabel("Run ID")).toHaveValue("");

  await go(page, "Evals");
  const scorer = page.getByRole("combobox", { name: "Scorer", exact: true });
  const modelSelect = page.getByRole("combobox", { name: /^Model/ });
  const dataset = page.getByLabel("Dataset (JSON)");
  const run = page.getByRole("button", { name: "Run evaluation" });
  await expect(scorer.locator("option")).toHaveText(["Exact match", "Contains", "Regular expression", "JSON Schema", "LLM judge"]);
  await expect(page.getByRole("combobox", { name: "Agent", exact: true })).toHaveValue("agent-1");
  await expect(modelSelect).toHaveCount(0);
  await dataset.fill("invalid");
  await run.click();
  await expect(page.getByRole("alert").filter({ hasText: /JSON/ })).toBeVisible();
  await dataset.fill('[{"input":"Hi"}]');
  await run.click();
  await expect(page.getByRole("alert").filter({ hasText: "Dataset must be" })).toContainText("each with a string input and an expected value");
  await dataset.fill('[{"input":"Hi","expected":"Hello"}]');
  await scorer.selectOption("contains");
  await run.click();
  const latest = page.getByRole("region", { name: "Latest result" });
  await expect(latest).toContainText("Contains · Agent Researcher · 1 case");
  await expect(latest).toContainText("1 / 1 passed");
  await expect(latest.getByRole("cell", { name: "Pass" })).toBeVisible();

  await page.getByRole("combobox", { name: "Target type" }).selectOption("prompt");
  await expect(page.getByRole("combobox", { name: "Prompt", exact: true })).toHaveValue("greeting");
  await expect(page.getByText("This version also uses {language}", { exact: false })).toBeVisible();
  await page.getByRole("combobox", { name: "Version", exact: true }).selectOption("1");
  await expect(page.getByText("This version also uses", { exact: false })).toHaveCount(0);
  await scorer.selectOption("json_schema");
  await page.getByLabel("Pass threshold").fill("0.5");
  await expect(modelSelect).toHaveValue("local-model");
  await dataset.fill(JSON.stringify([{ input: "Ada", expected: { type: "object", required: ["greeting"] } }, { input: "Lin", expected: { type: "object" } }]));
  await run.click();
  await expect(latest).toContainText("JSON Schema · Prompt greeting v1 · 2 cases");
  await expect(latest).toContainText("1 / 2 passed");
  await expect(latest.getByRole("row")).toHaveCount(3);
  await expect(latest.getByRole("cell", { name: "Fail" })).toBeVisible();
  for (const name of ["Input", "Expected", "Score", "Result"]) expect(await lineCount(latest.getByRole("columnheader", { name, exact: true }))).toBe(1);
  expect(await lineCount(latest.getByRole("cell", { name: "Fail" }).locator(".badge"))).toBe(1);
  expect(evalBodies).toEqual([
    { dataset: [{ input: "Hi", expected: "Hello" }], scorer: "contains", threshold: 1, agent_id: "agent-1" },
    { dataset: [{ input: "Ada", expected: { type: "object", required: ["greeting"] } }, { input: "Lin", expected: { type: "object" } }], scorer: "json_schema", threshold: 0.5, prompt_name: "greeting", prompt_version: 1, model: "local-model" },
  ]);
  const history = page.locator("details.trace");
  await expect(history).toHaveCount(2);
  await expect(history.first().locator("summary")).toContainText("JSON Schema · Prompt greeting v1 · 2 cases");
  await expect(history.nth(1).locator("summary")).toContainText("Contains · Agent Researcher · 1 case");
  await page.getByRole("combobox", { name: "Target type" }).selectOption("agent");
  await expect(modelSelect).toHaveCount(0);
  await scorer.selectOption("llm_judge");
  await expect(modelSelect).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath("evals-desktop.png"), fullPage: true });
  await fitsMobile(page);
  await page.screenshot({ path: testInfo.outputPath("evals-mobile.png"), fullPage: true });
});

test("browse, render, version, and create prompts", async ({ page }, testInfo) => {
  let prompts: Record<string, unknown>[] = [
    { id: "p1", name: "greeting", version: 1, template: "Hi {name}" },
    { id: "p2", name: "greeting", version: 2, template: "Hello {name}, welcome to {place}! {{literal}}" },
  ];
  const renders: unknown[] = [];
  await mock(page, (path, method, body) => {
    if (path === "/v1/prompts" && method === "POST") {
      const created = { id: `p${prompts.length + 1}`, name: body?.name, version: prompts.filter(row => row.name === body?.name).length + 1, template: body?.template };
      prompts = [...prompts, created];
      return created;
    }
    if (path === "/v1/prompts") return { data: prompts };
    const match = /^\/v1\/prompts\/([^/]+)\/(\d+)\/render$/.exec(path);
    if (match) {
      renders.push({ name: decodeURIComponent(match[1]), version: Number(match[2]), ...body });
      const variables = body?.variables as Record<string, string>;
      return { name: match[1], version: Number(match[2]), rendered: `Hello ${variables.name}, welcome to ${variables.place}! {literal}` };
    }
  });
  await connect(page); await go(page, "Prompts");
  await expect(page.getByRole("button", { name: /^greeting/ })).toContainText("v2 latest · 2 versions");
  const template = page.locator("pre").first();
  await expect(template).toHaveText("Hello {name}, welcome to {place}! {{literal}}");
  const render = page.locator("form").filter({ has: page.getByRole("button", { name: "Render prompt" }) });
  await expect(render.getByRole("textbox")).toHaveCount(2);
  await render.getByLabel("name", { exact: true }).fill("Ada");
  await render.getByLabel("place", { exact: true }).fill("SI");
  await render.getByRole("button", { name: "Render prompt" }).click();
  await expect(render.locator("pre")).toHaveText("Hello Ada, welcome to SI! {literal}");
  expect(renders).toEqual([{ name: "greeting", version: 2, variables: { name: "Ada", place: "SI" } }]);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath("prompts-desktop.png"), fullPage: true });

  await page.getByRole("combobox", { name: "Version" }).selectOption("1");
  await expect(template).toHaveText("Hi {name}");
  await expect(render.getByRole("textbox")).toHaveCount(1);
  await page.getByRole("button", { name: "Copy v1" }).click();
  // getByLabel reads label text, which includes the textarea's mirrored value, so match the prefix.
  const draft = page.getByLabel(/^Template/);
  await expect(draft).toHaveValue("Hi {name}");
  await draft.fill("Hi {name}!");
  await page.getByRole("button", { name: "Save as v3" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Saved" })).toHaveText("Saved greeting v3.");
  await expect(page.getByRole("button", { name: /^greeting/ })).toContainText("v3 latest · 3 versions");
  await expect(template).toHaveText("Hi {name}!");

  await page.getByRole("button", { name: "+ New" }).click();
  const name = page.getByLabel("Prompt name");
  await name.fill("..");
  await draft.fill("Reply to {input}");
  await page.getByRole("button", { name: "Create prompt" }).click();
  expect(await name.evaluate(input => (input as HTMLInputElement).validity.patternMismatch)).toBe(true);
  expect(prompts).toHaveLength(3);
  await name.fill("support.reply");
  await page.getByRole("button", { name: "Create prompt" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Saved" })).toHaveText("Saved support.reply v1.");
  await expect(page.getByRole("heading", { name: "support.reply" })).toBeVisible();
  await expect(template).toHaveText("Reply to {input}");
  await fitsMobile(page);
});

test("SSE parser preserves partial UTF-8, line boundaries, final events and DONE", async () => {
  const bytes = new TextEncoder().encode('data: {"choices":\r\ndata: [{"delta":{"content":"🌍"}}]}\r\n\r\ndata: {"choices":[{"delta":{"content":" ok"}}]}\n\ndata: [DONE]\n\ndata: invalid\n\n');
  const stream = new ReadableStream<Uint8Array>({ start(controller) { for (let i = 0; i < bytes.length; i++) controller.enqueue(bytes.slice(i, i + 1)); controller.close(); } });
  const api = new Api("test-only");
  api.response = async () => new Response(stream);
  let output = "";
  await streamChat(api, {}, new AbortController().signal, chunk => { output += chunk; });
  expect(output).toBe("🌍 ok");
  api.response = async () => new Response('data: {"choices":[{"delta":{"content":"final"}}]}');
  await streamChat(api, {}, new AbortController().signal, chunk => { output += chunk; });
  expect(output).toBe("🌍 okfinal");
});
