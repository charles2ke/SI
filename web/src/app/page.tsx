"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Api, list, streamChat, type Row } from "@/lib/api";

const tabs = ["Playground", "Agents", "Knowledge", "Traces", "Evals"] as const;
type Tab = (typeof tabs)[number];
type Message = { role: "user" | "assistant"; content: string };
const text = (value: unknown) => typeof value === "string" ? value : JSON.stringify(value, null, 2);

export default function Home() {
  const [key, setKey] = useState("");
  const [activeKey, setActiveKey] = useState("");
  const [tab, setTab] = useState<Tab>("Playground");
  const api = new Api(activeKey);

  return <div className="shell">
    <aside className="sidebar">
      <Link className="brand" href="/"><span className="brand-icon">si</span><span>SI<span className="muted"> / console</span></span></Link>
      <div className="workspace"><span className="workspace-icon">W</span><div>My workspace<small>Intelligence, connected.</small></div></div>
      <p className="eyebrow">WORKSPACE</p>
      <nav aria-label="Workspace">{tabs.map((item, i) => <button key={item} aria-current={tab === item ? "page" : undefined} onClick={() => setTab(item)}><span aria-hidden="true">{["◈", "⌘", "▤", "≋", "◎"][i]}</span>{item}</button>)}</nav>
      <div className="sidebar-bottom"><span className="status-dot" /> Your infrastructure. Your intelligence.<small>SI · Developer console</small></div>
    </aside>
    <div className="content">
      <header><span>Workspace <span className="muted">/ {tab}</span></span><span className="environment">LOCAL WORKSPACE</span></header>
      <main>
        <div className="page-heading"><div><p className="eyebrow">BUILD WITH INTELLIGENCE</p><h1>{tab}</h1><p className="muted">{({ Playground: "A space to explore your models. Start a conversation.", Agents: "Give your models a purpose, tools, and room to work.", Knowledge: "Ground your intelligence in your own knowledge.", Traces: "Follow every step. Understand every outcome.", Evals: "Measure what matters before you ship." })[tab]}</p></div><span className="section-number">0{tabs.indexOf(tab) + 1} / 05</span></div>
        <form className="connection panel" onSubmit={e => { e.preventDefault(); setActiveKey(key.trim()); }}>
          <div><strong>API connection</strong><small>{api.base} · Key stays in memory for this page only.</small></div>
          <label className="sr-only" htmlFor="api-key">API key</label><input id="api-key" type="password" autoComplete="off" placeholder="Enter your API key" value={key} onChange={e => setKey(e.target.value)} required />
          <button className="primary" type="submit">{activeKey ? "Reconnect" : "Connect"}</button>
          <button type="button" className="subtle" onClick={() => { setKey("si-local-development"); setActiveKey("si-local-development"); }}>Use local dev key</button>
        </form>
        {!activeKey ? <Empty title="Your workspace is ready" detail="Connect with an API key to load live models, agents, and knowledge. For the local bootstrap server, choose “Use local dev key”." /> :
          <section key={`${activeKey}-${tab}`} aria-label={`${tab} workspace`}>
            {tab === "Playground" && <Playground api={api} />}
            {tab === "Agents" && <Agents api={api} />}
            {tab === "Knowledge" && <Knowledge api={api} />}
            {tab === "Traces" && <Traces api={api} />}
            {tab === "Evals" && <Evals api={api} />}
          </section>}
        <footer>Built for the way you think.<span>SI / OPEN INTELLIGENCE</span></footer>
      </main>
    </div>
  </div>;
}

function Empty({ title, detail }: { title: string; detail: string }) {
  return <div className="empty panel"><span className="empty-symbol" aria-hidden="true">✳</span><h2>{title}</h2><p className="muted">{detail}</p></div>;
}
function ErrorBox({ error }: { error: string }) { return error ? <div role="alert" className="error">{error}</div> : null; }
function Json({ value }: { value: unknown }) { return <pre>{text(value)}</pre>; }
function useResource(api: Api, path: string) {
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const base = api.base, key = api.key;
  const refresh = useCallback(async () => {
    setLoading(true); setError("");
    try { setRows(list(await new Api(key, base).request(path))); }
    catch (e) { setError((e as Error).message); }
    finally { setLoading(false); }
  }, [base, key, path]);
  useEffect(() => {
    const controller = new AbortController();
    void new Api(key, base).request(path, { signal: controller.signal }).then(value => setRows(list(value))).catch(e => {
      if (e.name !== "AbortError") setError(e.message);
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [base, key, path]);
  return { rows, loading, error, refresh };
}
function ResourceStatus({ loading, error }: { loading: boolean; error: string }) {
  return <><ErrorBox error={error} />{loading && <p role="status" className="muted">Loading live data…</p>}</>;
}

function Playground({ api }: { api: Api }) {
  const models = useResource(api, "/v1/models");
  const [model, setModel] = useState("");
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const abort = useRef<AbortController | null>(null);
  const selected = model || String(models.rows[0]?.id || "");
  useEffect(() => () => abort.current?.abort(), []);
  async function send() {
    if (!input.trim() || !selected || busy) return;
    const history: Message[] = [...messages, { role: "user", content: input.trim() }];
    setMessages([...history, { role: "assistant", content: "" }]); setInput(""); setBusy(true); setError("");
    const controller = new AbortController(); abort.current = controller;
    try {
      await streamChat(api, { model: selected, messages: history, stream: true }, controller.signal, chunk => {
        setMessages(current => current.map((m, i) => i === current.length - 1 ? { ...m, content: m.content + chunk } : m));
      });
    } catch (e) { if ((e as Error).name !== "AbortError") setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <div className="panel chat">
    <div className="panel-heading"><div><span className="status-dot" /><strong>Chat playground</strong></div><label>Model <select aria-label="Model" value={selected} onChange={e => setModel(e.target.value)} disabled={busy || models.loading}>{!models.rows.length && <option value="">No models available</option>}{models.rows.map(m => <option key={String(m.id)} value={String(m.id)}>{String(m.id)}</option>)}</select></label></div>
    <ResourceStatus {...models} /><ErrorBox error={error} />
    <div className="messages" role="log" aria-label="Conversation" aria-live="polite">
      {!messages.length && <div className="chat-empty"><span className="orb" aria-hidden="true">✳</span><h2>What will you build today?</h2><p className="muted">Talk to a model. Test an idea. Find a new direction.</p><div className="chips">{["Explain a complex idea", "Help me plan a project", "Explore a new perspective"].map(s => <button key={s} onClick={() => setInput(s)}>{s} ↗</button>)}</div></div>}
      {messages.map((message, i) => <article className={`message ${message.role}`} key={i}><span className="message-avatar">{message.role === "user" ? "You" : "SI"}</span><div><strong>{message.role === "user" ? "You" : selected}</strong><p>{message.content || (busy ? "Thinking…" : "No text returned.")}</p></div></article>)}
    </div>
    <form className="composer" onSubmit={e => { e.preventDefault(); void send(); }}><label className="sr-only" htmlFor="chat-input">Message</label><textarea id="chat-input" placeholder="Send a message to your model…" value={input} onChange={e => setInput(e.target.value)} disabled={busy} rows={3} /><div><small>Live streaming · Your conversation stays on this page</small>{busy ? <button type="button" onClick={() => abort.current?.abort()}>Stop generation</button> : <button className="primary" disabled={!selected || !input.trim()} type="submit">Send message ↑</button>}</div></form>
  </div>;
}

const blankAgent = { name: "", system_prompt: "", model: "", tools: "", collection_ids: "", max_steps: 8 };
function Agents({ api }: { api: Api }) {
  const agents = useResource(api, "/v1/agents");
  const [id, setId] = useState("");
  const [draft, setDraft] = useState(blankAgent);
  const [input, setInput] = useState("");
  const [run, setRun] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const runId = run?.id;
  const runStatus = run?.status;
  useEffect(() => {
    if (!runId || ["completed", "failed", "cancelled", "succeeded", "interrupted"].includes(String(runStatus))) return;
    const controller = new AbortController();
    const timer = setInterval(() => {
      void api.request(`/v1/runs/${encodeURIComponent(String(runId))}`, { signal: controller.signal }).then(value => { setRun(value as Row); setError(""); }).catch(e => { if (e.name !== "AbortError") setError(e.message); });
    }, 1500);
    return () => { clearInterval(timer); controller.abort(); };
  }, [api.base, api.key, runId, runStatus]); // eslint-disable-line react-hooks/exhaustive-deps
  async function action(fn: () => Promise<void>) { setBusy(true); setError(""); setNotice(""); try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } }
  const csv = (v: string) => v.split(",").map(s => s.trim()).filter(Boolean);
  return <div className="split"><div className="panel"><div className="panel-heading"><h2>Your agents</h2><button onClick={() => { setId(""); setDraft(blankAgent); setRun(null); }}>+ New</button></div><ResourceStatus {...agents} />{!agents.loading && !agents.rows.length && <p className="padded muted">No agents yet. Create your first one.</p>}{agents.rows.map(a => <button key={String(a.id)} className={`list-item ${id === a.id ? "selected" : ""}`} onClick={() => { setId(String(a.id)); setRun(null); setDraft({ name: String(a.name || ""), system_prompt: String(a.system_prompt || ""), model: String(a.model || ""), tools: Array.isArray(a.tools) ? a.tools.join(", ") : "", collection_ids: Array.isArray(a.collection_ids) ? a.collection_ids.join(", ") : "", max_steps: Number(a.max_steps || 8) }); }}><strong>{String(a.name)}</strong><small>{String(a.model || "No model")} <span>↗</span></small></button>)}</div>
    <div className="panel padded"><h2>{id ? "Edit agent" : "Create an agent"}</h2><ErrorBox error={error} />{notice && <p role="status" className="success">{notice}</p>}
      <form onSubmit={e => { e.preventDefault(); void action(async () => { const body = { ...draft, tools: csv(draft.tools), collection_ids: csv(draft.collection_ids) }; const saved = await api.request(id ? `/v1/agents/${encodeURIComponent(id)}` : "/v1/agents", { method: id ? "PATCH" : "POST", body: JSON.stringify(body) }) as Row; setId(String(saved.id)); setNotice("Agent saved."); await agents.refresh(); }); }}>
        <label>Name<input required value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} placeholder="Research assistant" /></label>
        <label>System prompt<textarea required rows={4} value={draft.system_prompt} onChange={e => setDraft({ ...draft, system_prompt: e.target.value })} placeholder="Describe how your agent should work…" /></label>
        <label>Model<input required value={draft.model} onChange={e => setDraft({ ...draft, model: e.target.value })} placeholder="Model ID from /v1/models" /></label>
        <div className="two-columns"><label>Tools <small>Comma-separated names</small><input value={draft.tools} onChange={e => setDraft({ ...draft, tools: e.target.value })} /></label><label>Collection IDs <small>Comma-separated IDs</small><input value={draft.collection_ids} onChange={e => setDraft({ ...draft, collection_ids: e.target.value })} /></label></div>
        <label>Maximum steps<input type="number" min={1} max={32} required value={draft.max_steps} onChange={e => setDraft({ ...draft, max_steps: Number(e.target.value) })} /></label>
        <button className="primary" disabled={busy}>{busy ? "Working…" : "Save agent"}</button>
      </form>
      {id && <div className="run-section"><h3>Run agent</h3><form onSubmit={e => { e.preventDefault(); void action(async () => { setRun(await api.request(`/v1/agents/${encodeURIComponent(id)}/runs`, { method: "POST", body: JSON.stringify({ input, background: true }) }) as Row); }); }}><label>Task input<textarea required value={input} onChange={e => setInput(e.target.value)} /></label><button disabled={busy || !input.trim()}>Start run →</button></form></div>}
      {run && <div className="run-section"><h3>Run <span className="badge">{String(run.status || "queued")}</span></h3><Json value={run} />{["awaiting_approval", "pending_approval", "requires_approval", "waiting_approval"].includes(String(run.status)) && <button className="primary" disabled={busy} onClick={() => void action(async () => { const result = await api.request(`/v1/runs/${encodeURIComponent(String(run.id))}/approve`, { method: "POST", body: JSON.stringify({ approved: true }) }); setRun(result as Row); })}>Approve run</button>}</div>}
    </div></div>;
}

function Knowledge({ api }: { api: Api }) {
  const collections = useResource(api, "/v1/collections");
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  async function action(fn: () => Promise<void>) { setBusy(true); setError(""); setNotice(""); try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } }
  const selected = id || String(collections.rows[0]?.id || "");
  return <div className="split"><div className="panel padded"><h2>Knowledge bases</h2><ResourceStatus {...collections} /><form onSubmit={e => { e.preventDefault(); void action(async () => { const created = await api.request("/v1/collections", { method: "POST", body: JSON.stringify({ name }) }) as Row; setId(String(created.id)); setName(""); await collections.refresh(); }); }}><label>Collection name<input required value={name} onChange={e => setName(e.target.value)} placeholder="Product documentation" /></label><button disabled={busy || !name.trim()}>+ Create collection</button></form><div className="run-section">{!collections.loading && !collections.rows.length && <p className="muted">No collections yet.</p>}{collections.rows.map(c => <button key={String(c.id)} className={`list-item ${selected === c.id ? "selected" : ""}`} onClick={() => { setId(String(c.id)); setResult(null); }}><strong>{String(c.name)}</strong><small>{String(c.id)}</small></button>)}</div></div>
    <div className="panel padded"><h2>Explore your knowledge</h2><ErrorBox error={error} />{notice && <p role="status" className="success">{notice}</p>}{selected ? <><p className="muted">Add documents, then search for relevant passages.</p><form onSubmit={e => { e.preventDefault(); const form = e.currentTarget; const data = new FormData(form); void action(async () => { const uploaded = await api.request(`/v1/collections/${encodeURIComponent(selected)}/documents`, { method: "POST", body: data }); setNotice("Document uploaded."); setResult(uploaded); form.reset(); }); }}><label>Document<input type="file" name="file" required /></label><button disabled={busy}>Upload document ↑</button></form><form className="run-section" onSubmit={e => { e.preventDefault(); void action(async () => { setResult(await api.request(`/v1/collections/${encodeURIComponent(selected)}/search`, { method: "POST", body: JSON.stringify({ query }) })); }); }}><label>Search query<input value={query} required onChange={e => setQuery(e.target.value)} placeholder="What would you like to find?" /></label><button className="primary" disabled={busy || !query.trim()}>{busy ? "Working…" : "Search knowledge →"}</button></form>{result !== null && <div className="run-section"><h3>Results</h3><Json value={result} /></div>}</> : <p className="muted">Create a collection to upload documents and search.</p>}</div></div>;
}

function Traces({ api }: { api: Api }) {
  const traces = useResource(api, "/v1/traces");
  return <div className="panel padded"><div className="panel-heading"><h2>Execution traces</h2><button disabled={traces.loading} onClick={() => void traces.refresh()}>Refresh ↻</button></div><ResourceStatus {...traces} />{!traces.loading && !traces.error && !traces.rows.length && <Empty title="Nothing to trace. Yet." detail="Run a model or agent to see its execution history here." />}{traces.rows.map((trace, i) => <details className="trace" key={String(trace.id || i)}><summary><strong>{String(trace.name || trace.id || `Trace ${i + 1}`)}</strong><span className="badge">{String(trace.status || "recorded")}</span></summary><Json value={trace} /></details>)}</div>;
}

function Evals({ api }: { api: Api }) {
  const evals = useResource(api, "/v1/evals");
  const [dataset, setDataset] = useState('[\n  { "input": "", "expected": "" }\n]');
  const [scorer, setScorer] = useState("exact_match");
  const [target, setTarget] = useState("agent_id");
  const [targetId, setTargetId] = useState("");
  const [version, setVersion] = useState("");
  const [result, setResult] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return <div className="split"><div className="panel padded"><h2>Run an evaluation</h2><p className="muted">Real inputs. Expected outputs. Measurable quality.</p><ErrorBox error={error} /><form onSubmit={e => { e.preventDefault(); void (async () => { setBusy(true); setError(""); try { const parsed: unknown = JSON.parse(dataset); if (!Array.isArray(parsed) || !parsed.length || !parsed.every(r => r && typeof r.input === "string" && typeof r.expected === "string")) throw new Error("Dataset must be a non-empty JSON array of input and expected strings."); const body = { dataset: parsed, scorer, [target]: targetId, ...(target === "prompt_name" && version ? { prompt_version: Number(version) } : {}) }; setResult(await api.request("/v1/evals", { method: "POST", body: JSON.stringify(body) }) as Row); await evals.refresh(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } })(); }}>
    <label>Target type<select value={target} onChange={e => setTarget(e.target.value)}><option value="agent_id">Agent</option><option value="prompt_name">Prompt</option></select></label><label>{target === "agent_id" ? "Agent ID" : "Prompt name"}<input required value={targetId} onChange={e => setTargetId(e.target.value)} /></label>{target === "prompt_name" && <label>Prompt version (optional)<input type="number" min={1} value={version} onChange={e => setVersion(e.target.value)} /></label>}
    <label>Scorer<select value={scorer} onChange={e => setScorer(e.target.value)}><option value="exact_match">Exact match</option><option value="contains">Contains</option></select></label><label>Dataset (JSON)<textarea className="code-input" rows={8} required value={dataset} onChange={e => setDataset(e.target.value)} spellCheck={false} /></label><button className="primary" disabled={busy}>{busy ? "Evaluating…" : "Run evaluation →"}</button></form></div>
    <div className="panel padded"><div className="panel-heading"><h2>Evaluation results</h2><button disabled={evals.loading} onClick={() => void evals.refresh()}>Refresh ↻</button></div><ResourceStatus {...evals} />{result && <div className="run-section"><h3>Latest result</h3><p className="score">Score <strong>{text(result.score ?? result.aggregate_score ?? "See results")}</strong></p><Json value={result} /></div>}{!evals.loading && !evals.rows.length && !result && <p className="muted">No evaluations yet. Run a dataset to establish your baseline.</p>}{evals.rows.map((item, i) => <details className="trace" key={String(item.id || i)}><summary>{String(item.id || `Evaluation ${i + 1}`)}<span className="badge">Score {text(item.score ?? item.aggregate_score ?? "—")}</span></summary><Json value={item} /></details>)}</div></div>;
}
