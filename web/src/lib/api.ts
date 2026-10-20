export type Row = Record<string, unknown>;

export class Api {
  constructor(public key: string, public base = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/$/, "")) {}

  async response(path: string, init: RequestInit = {}) {
    const headers = new Headers(init.headers);
    headers.set("Authorization", ["Bearer", this.key].join(" "));
    if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
    const response = await fetch(`${this.base}${path}`, { ...init, headers });
    if (!response.ok) {
      let detail = response.statusText;
      const body = await response.text();
      try { const value = JSON.parse(body); detail = typeof value.detail === "string" ? value.detail : JSON.stringify(value.detail || value.error || value); }
      catch { detail = body || detail; }
      throw new Error(`API ${response.status}: ${detail}`);
    }
    return response;
  }

  async request(path: string, init?: RequestInit): Promise<unknown> {
    const response = await this.response(path, init);
    return response.status === 204 ? null : response.json();
  }
}

export function list(value: unknown): Row[] {
  if (Array.isArray(value)) return value;
  if (value && typeof value === "object") {
    const object = value as Row;
    for (const key of ["data", "items", "agents", "collections", "traces", "evals", "results"]) {
      if (Array.isArray(object[key])) return object[key] as Row[];
    }
  }
  return [];
}

export async function streamChat(api: Api, body: unknown, signal: AbortSignal, onText: (text: string) => void) {
  const response = await api.response("/v1/chat/completions", { method: "POST", body: JSON.stringify(body), signal });
  if (!response.body) throw new Error("The API returned no response stream.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let data: string[] = [];
  let done = false;
  function dispatch() {
    if (!data.length) return;
    const payload = data.join("\n"); data = [];
    if (payload.trim() === "[DONE]") { done = true; return; }
    const chunk = JSON.parse(payload);
    if (chunk.error) throw new Error(typeof chunk.error === "string" ? chunk.error : chunk.error.message || "Streaming request failed.");
    const content = chunk.choices?.[0]?.delta?.content;
    if (typeof content === "string") onText(content);
  }
  function consume(final = false) {
    // Keep incomplete UTF-8 and SSE lines across network chunks.
    const lines: string[] = [];
    const breaks = /\r\n|\r|\n/g;
    let start = 0;
    let match: RegExpExecArray | null;
    while ((match = breaks.exec(buffer))) {
      if (!final && match[0] === "\r" && breaks.lastIndex === buffer.length) break;
      lines.push(buffer.slice(start, match.index));
      start = breaks.lastIndex;
    }
    if (final && start < buffer.length) lines.push(buffer.slice(start));
    buffer = final ? "" : buffer.slice(start);
    for (const line of lines) {
      if (!line) dispatch();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
      if (done) break;
    }
    if (final && !done) dispatch();
  }
  try {
    while (!done) {
      const part = await reader.read();
      buffer += decoder.decode(part.value, { stream: !part.done });
      consume(part.done);
      if (part.done) break;
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}
