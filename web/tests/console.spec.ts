import { test, expect, type Page } from "@playwright/test";
import { Api, streamChat } from "../src/lib/api";

async function connect(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Use local dev key" }).click();
  expect(await page.evaluate(() => ({ local: Object.keys(localStorage), session: Object.keys(sessionStorage) }))).toEqual({ local: [], session: [] });
}
async function mock(page: Page, handler?: (path: string, method: string, body: Record<string, unknown> | null) => unknown) {
  await page.route("http://localhost:8000/**", async route => {
    const req = route.request();
    if (req.method() === "OPTIONS") { await route.fulfill({ status: 204 }); return; }
    expect(req.headers().authorization).toBe(["Bearer", "si-local-development"].join(" "));
    const path = new URL(req.url()).pathname;
    const data = req.headers()["content-type"]?.includes("application/json") ? req.postDataJSON() : null;
    const result = handler?.(path, req.method(), data);
    await route.fulfill({ json: result ?? (path === "/v1/models" ? { data: [{ id: "local-model" }] } : { data: [] }) });
  });
}
const go = async (page: Page, tab: string) => page.getByRole("navigation", { name: "Workspace" }).getByRole("button", { name: tab, exact: true }).click();

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

test("create, edit, run, poll, and approve an agent", async ({ page }) => {
  let agent: Record<string, unknown> | null = null;
  let approved = false;
  let patched = false;
  await mock(page, (path, method, body) => {
    if (path === "/v1/agents" && method === "POST") {
      expect(body).toMatchObject({ tools: ["search"], collection_ids: ["docs"], max_steps: 8 });
      agent = { id: "agent-1", ...body }; return agent;
    }
    if (path === "/v1/agents") return { data: agent ? [agent] : [] };
    if (path === "/v1/agents/agent-1" && method === "PATCH") { patched = true; agent = { ...agent, ...body }; return agent; }
    if (path === "/v1/agents/agent-1/runs") { expect(body).toEqual({ input: "Find the answer", background: true }); return { id: "run-1", status: "queued" }; }
    if (path === "/v1/runs/run-1/approve") { approved = true; return { id: "run-1", status: "completed", output: "Approved answer" }; }
    if (path === "/v1/runs/run-1") return { id: "run-1", status: approved ? "completed" : "waiting_approval" };
  });
  await connect(page); await go(page, "Agents");
  await expect(page.getByText("No agents yet. Create your first one.")).toBeVisible();
  await page.getByLabel("Name", { exact: true }).fill("Researcher");
  await page.getByLabel("System prompt").fill("Research carefully.");
  await page.getByLabel("Model", { exact: true }).fill("local-model");
  await page.getByLabel("Tools").fill("search");
  await page.getByLabel("Collection IDs").fill("docs");
  await page.getByRole("button", { name: "Save agent" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Agent saved." })).toBeVisible();
  await page.getByLabel("Name", { exact: true }).fill("Updated researcher");
  await page.getByRole("button", { name: "Save agent" }).click();
  await expect.poll(() => patched).toBe(true);
  await page.getByLabel("Task input").fill("Find the answer");
  await page.getByRole("button", { name: "Start run" }).click();
  await page.getByRole("button", { name: "Approve run" }).click();
  await expect(page.getByText("Approved answer", { exact: false })).toBeVisible();
  expect(approved).toBe(true);
});

test("create collection, upload a document and retrieve knowledge", async ({ page }) => {
  let created = false;
  let uploaded = false;
  await mock(page, (path, method, body) => {
    if (path === "/v1/collections" && method === "POST") { expect(body).toEqual({ name: "Research" }); created = true; return { id: "collection-1", name: "Research" }; }
    if (path === "/v1/collections") return { data: created ? [{ id: "collection-1", name: "Research" }] : [] };
    if (path.endsWith("/documents")) { uploaded = true; return { id: "doc-1", status: "indexed" }; }
    if (path.endsWith("/search")) { expect(body).toEqual({ query: "What is SI?" }); return { results: [{ content: "SI connects intelligence.", score: 0.9 }] }; }
  });
  await connect(page); await go(page, "Knowledge");
  await page.getByLabel("Collection name").fill("Research");
  await page.getByRole("button", { name: "Create collection" }).click();
  await page.getByLabel("Document", { exact: true }).setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("SI connects intelligence.") });
  await page.getByRole("button", { name: "Upload document" }).click();
  await expect(page.getByRole("status")).toHaveText("Document uploaded.");
  expect(uploaded).toBe(true);
  await page.getByLabel("Search query").fill("What is SI?");
  await page.getByRole("button", { name: "Search knowledge" }).click();
  await expect(page.locator("pre")).toContainText("SI connects intelligence.");
});

test("display traces and evaluate a dataset", async ({ page }) => {
  let evaluated = false;
  await mock(page, (path, method, body) => {
    if (path === "/v1/traces") return { data: [{ id: "trace-1", name: "Agent execution", status: "completed", duration_ms: 24 }] };
    if (path === "/v1/evals" && method === "POST") { expect(body).toEqual({ dataset: [{ input: "Hi", expected: "Hello" }], scorer: "contains", agent_id: "agent-1" }); evaluated = true; return { id: "eval-1", score: 1, results: [{ output: "Hello", score: 1 }] }; }
    if (path === "/v1/evals") return { data: evaluated ? [{ id: "eval-1", score: 1 }] : [] };
  });
  await connect(page); await go(page, "Traces");
  await page.getByText("Agent execution", { exact: true }).click();
  await expect(page.locator("pre")).toContainText("duration_ms");
  await go(page, "Evals");
  await page.getByLabel("Agent ID", { exact: true }).fill("agent-1");
  await page.getByLabel("Dataset (JSON)").fill("invalid");
  await page.getByRole("button", { name: "Run evaluation" }).click();
  await expect(page.getByRole("alert").filter({ hasText: /Unexpected token|JSON/ })).toBeVisible();
  await page.getByLabel("Dataset (JSON)").fill('[{"input":"Hi","expected":"Hello"}]');
  await page.getByRole("combobox", { name: "Scorer", exact: true }).selectOption("contains");
  await page.getByRole("button", { name: "Run evaluation" }).click();
  await expect(page.getByRole("heading", { name: "Latest result" })).toBeVisible();
  await expect(page.locator("pre").filter({ hasText: '"results"' })).toContainText('"score": 1');
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
