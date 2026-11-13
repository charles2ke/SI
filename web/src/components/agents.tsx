"use client";

import { useEffect, useState } from "react";
import type { Api, Row } from "@/lib/api";
import { ConfirmButton, ErrorBox, Json, Notice, ResourceStatus, StatusBadge, clip, plural, rows, segment, time, useAction, useResource } from "@/components/common";

type Draft = { name: string; system_prompt: string; model: string; tools: string[]; extra_tools: string; collection_ids: string[]; max_steps: number };
const blank: Draft = { name: "", system_prompt: "", model: "", tools: [], extra_tools: "", collection_ids: [], max_steps: 8 };
// Statuses another actor (worker, approver, canceller) may still change.
const LIVE = ["queued", "running", "cancelling", "waiting_approval"];
const CANCELLABLE = ["queued", "running", "waiting_approval", "interrupted"];
const strings = (value: unknown) => Array.isArray(value) ? value.map(String) : [];
const csv = (value: string) => value.split(",").map(item => item.trim()).filter(Boolean);
const toggle = (values: string[], value: string, on: boolean) => on ? [...new Set([...values, value])] : values.filter(item => item !== value);
const isMcp = (tool: string) => tool.startsWith("mcp_");

function toDraft(agent: Row): Draft {
  const tools = strings(agent.tools);
  return {
    name: String(agent.name ?? ""), system_prompt: String(agent.system_prompt ?? ""), model: String(agent.model ?? ""),
    tools: tools.filter(tool => !isMcp(tool)), extra_tools: tools.filter(isMcp).join(", "),
    collection_ids: strings(agent.collection_ids), max_steps: Number(agent.max_steps || 8),
  };
}

function describe(event: Row) {
  const data = (event.data && typeof event.data === "object" ? event.data : {}) as Row;
  switch (event.type) {
    case "started": return Number(data.steps) ? `Continued after ${plural(Number(data.steps), "step")}` : "Started";
    case "model_response": {
      const calls = rows(data.tool_calls).map(call => String(call.name));
      return calls.length ? `Requested ${calls.join(", ")}` : clip(data.content || "Responded");
    }
    case "approval_required": return `Waiting for approval to call ${String(data.name)}`;
    case "approval": return data.approved ? "Tool call approved" : "Tool call rejected";
    case "tool_result": return `${String(data.name)} → ${clip((data.result as Row | undefined)?.untrusted_tool_result ?? data.result, 120)}`;
    case "completed": return clip(data.output);
    case "failed": return clip(data.error);
    case "cancelled": return `Cancelled while ${String(data.previous_status || "active").replace(/_/g, " ")}`;
    case "resumed": return data.pending_tool ? `Resumed with a pending ${String(data.pending_tool)} call` : "Resumed";
    default: return clip(event.data ?? "");
  }
}

export function Agents({ api, onTraces }: { api: Api; onTraces: (runId: string) => void }) {
  const agents = useResource(api, "/v1/agents");
  const models = useResource(api, "/v1/models");
  const tools = useResource(api, "/v1/tools");
  const collections = useResource(api, "/v1/collections");
  const [id, setId] = useState("");
  const history = useResource(api, id ? `/v1/runs?agent_id=${segment(id)}&limit=20` : null);
  const [draft, setDraft] = useState(blank);
  const [input, setInput] = useState("");
  const [session, setSession] = useState("");
  const [run, setRun] = useState<Row | null>(null);
  const [pollError, setPollError] = useState("");
  const action = useAction();
  const runAction = useAction();

  const runId = run ? String(run.id) : "";
  const runStatus = run ? String(run.status) : "";
  const refreshHistory = history.refresh;
  const modelIds = models.rows.map(row => String(row.id));
  const model = draft.model || modelIds[0] || "";
  useEffect(() => {
    if (!runId || !LIVE.includes(runStatus)) return;
    const controller = new AbortController();
    const timer = setInterval(() => {
      void api.request(`/v1/runs/${segment(runId)}`, { signal: controller.signal }).then(value => {
        const next = value as Row;
        setRun(current => current?.id === next.id ? next : current);
        setPollError("");
        if (next.status !== runStatus) void refreshHistory();
      }).catch(e => { if ((e as Error).name !== "AbortError") setPollError((e as Error).message); });
    }, 1500);
    return () => { clearInterval(timer); controller.abort(); };
  }, [api, runId, runStatus, refreshHistory]);

  function select(agent: Row | null) {
    setId(agent ? String(agent.id) : ""); setDraft(agent ? toDraft(agent) : blank);
    setRun(null); setPollError("");
    action.setError(""); action.setNotice(""); runAction.setError(""); runAction.setNotice("");
  }
  function save() {
    void action.perform(async () => {
      const body = { name: draft.name, system_prompt: draft.system_prompt, model, tools: [...new Set([...draft.tools, ...csv(draft.extra_tools)])], collection_ids: draft.collection_ids, max_steps: draft.max_steps };
      const saved = await api.request(id ? `/v1/agents/${segment(id)}` : "/v1/agents", { method: id ? "PATCH" : "POST", body: JSON.stringify(body) }) as Row;
      setId(String(saved.id));
      await agents.refresh();
      return "Agent saved.";
    });
  }
  function remove() {
    const target = id;
    void action.perform(async () => {
      await api.request(`/v1/agents/${segment(target)}`, { method: "DELETE" });
      select(null);
      await agents.refresh();
      return "Agent deleted.";
    });
  }
  function start() {
    void runAction.perform(async () => {
      const body = { input, background: true, ...(session.trim() ? { session_id: session.trim() } : {}) };
      setRun(await api.request(`/v1/agents/${segment(id)}/runs`, { method: "POST", body: JSON.stringify(body) }) as Row);
      setPollError("");
      void history.refresh();
    });
  }
  function openRun(target: string) {
    void runAction.perform(async () => { setRun(await api.request(`/v1/runs/${segment(target)}`) as Row); setPollError(""); });
  }
  function act(path: string, body?: unknown) {
    const target = runId;
    void runAction.perform(async () => {
      setRun(await api.request(`/v1/runs/${segment(target)}/${path}`, { method: "POST", ...(body === undefined ? {} : { body: JSON.stringify(body) }) }) as Row);
      setPollError("");
      void history.refresh();
    });
  }

  const knownTools = new Set(tools.rows.map(tool => String(tool.name)));
  const toolOptions: Row[] = [...tools.rows, ...draft.tools.filter(name => !knownTools.has(name)).map(name => ({ name, description: "Not in this server's tool catalog.", available: false, unavailable_reason: "Uncheck to remove it" }))];
  const servers = rows((tools.value as Row | null)?.mcp_servers);
  const knownCollections = new Set(collections.rows.map(row => String(row.id)));
  const collectionOptions: Row[] = [...collections.rows, ...draft.collection_ids.filter(item => !knownCollections.has(item)).map(item => ({ id: item, name: item, missing: true }))];

  return <div className="split">
    <div className="panel">
      <div className="panel-heading"><h2>Your agents</h2><button onClick={() => select(null)}>+ New</button></div>
      <ResourceStatus {...agents} />
      {!agents.loading && !agents.rows.length && <p className="padded muted">No agents yet. Create your first one.</p>}
      {agents.rows.map(agent => <button key={String(agent.id)} className={`list-item ${id === agent.id ? "selected" : ""}`} onClick={() => select(agent)}><strong>{String(agent.name)}</strong><small>{String(agent.model || "No model")} · {plural(strings(agent.tools).length, "tool")} <span>↗</span></small></button>)}
    </div>
    <div className="stack">
      <div className="panel padded">
        <h2>{id ? "Edit agent" : "Create an agent"}</h2>
        <ErrorBox error={action.error} /><Notice notice={action.notice} />
        <form onSubmit={e => { e.preventDefault(); save(); }}>
          <label>Name<input required maxLength={200} value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} placeholder="Research assistant" /></label>
          <label>System prompt<textarea required rows={4} value={draft.system_prompt} onChange={e => setDraft({ ...draft, system_prompt: e.target.value })} placeholder="Describe how your agent should work…" /></label>
          <label>Model{modelIds.length || models.loading
            ? <select required value={model} disabled={models.loading} onChange={e => setDraft({ ...draft, model: e.target.value })}>
              {model && !modelIds.includes(model) && <option value={model}>{model} (not listed)</option>}
              {modelIds.map(item => <option key={item} value={item}>{item}</option>)}
            </select>
            : <input required value={draft.model} onChange={e => setDraft({ ...draft, model: e.target.value })} placeholder="Model ID" />}</label>
          <fieldset className="choices">
            <legend>Tools</legend>
            <ResourceStatus {...tools} />
            {toolOptions.map(tool => {
              const name = String(tool.name);
              const checked = draft.tools.includes(name);
              const reason = tool.available === false ? String(tool.unavailable_reason || "Unavailable on this server") : "";
              return <label className="check" key={name}>
                <input type="checkbox" checked={checked} disabled={Boolean(reason) && !checked} onChange={e => setDraft({ ...draft, tools: toggle(draft.tools, name, e.target.checked) })} />
                <span><strong>{name}</strong>{tool.sensitive === true && <span className="badge warn">Requires approval</span>}<small>{String(tool.description ?? "")}{reason && ` ${reason}.`}</small></span>
              </label>;
            })}
          </fieldset>
          <label>Additional tools <small>Comma-separated MCP tool names. {servers.length ? `Configured servers: ${servers.map(server => `${String(server.tool_prefix)}…`).join(", ")}` : "No MCP servers are configured."}</small><input value={draft.extra_tools} onChange={e => setDraft({ ...draft, extra_tools: e.target.value })} placeholder="mcp_docs_search" /></label>
          <fieldset className="choices">
            <legend>Knowledge collections</legend>
            <ResourceStatus {...collections} />
            {!collections.loading && !collectionOptions.length && <p className="muted">No collections yet. Create one in Knowledge.</p>}
            {collectionOptions.map(collection => {
              const item = String(collection.id);
              return <label className="check" key={item}>
                <input type="checkbox" checked={draft.collection_ids.includes(item)} onChange={e => setDraft({ ...draft, collection_ids: toggle(draft.collection_ids, item, e.target.checked) })} />
                <span><strong>{String(collection.name)}</strong><small>{collection.missing ? "Collection not found." : typeof collection.chunks === "number" ? plural(collection.chunks, "chunk") : item}</small></span>
              </label>;
            })}
          </fieldset>
          <label>Maximum steps<input type="number" min={1} max={32} required value={draft.max_steps} onChange={e => setDraft({ ...draft, max_steps: Number(e.target.value) })} /></label>
          <div className="actions">
            <button className="primary" disabled={action.busy}>{action.busy ? "Working…" : "Save agent"}</button>
            {id && <ConfirmButton label="Delete agent" confirm="Delete agent and its runs" disabled={action.busy} onConfirm={remove} />}
          </div>
        </form>
      </div>
      {id && <div className="panel padded">
        <h2>Runs</h2>
        <ErrorBox error={runAction.error} /><ErrorBox error={pollError} /><Notice notice={runAction.notice} />
        <form onSubmit={e => { e.preventDefault(); start(); }}>
          <label>Task input<textarea required rows={3} value={input} onChange={e => setInput(e.target.value)} /></label>
          <label>Session ID <small>Optional. Runs with the same session ID share conversation memory.</small><input maxLength={200} value={session} onChange={e => setSession(e.target.value)} /></label>
          <button disabled={runAction.busy || !input.trim()}>Start run →</button>
        </form>
        {run && <RunDetail run={run} busy={runAction.busy} onAct={act} onTraces={onTraces} />}
        <section className="run-section" aria-label="Recent runs">
          <div className="section-heading"><h3>Recent runs</h3><button type="button" disabled={history.loading} onClick={() => void history.refresh()}>Refresh ↻</button></div>
          <ResourceStatus {...history} />
          {!history.loading && !history.error && !history.rows.length && <p className="muted">No runs yet.</p>}
          {history.rows.map(item => <button key={String(item.id)} className={`list-item ${item.id === runId ? "selected" : ""}`} onClick={() => openRun(String(item.id))}><strong>{clip(item.input, 90)}</strong><small>{[time(item.created_at), item.session_id ? `session ${String(item.session_id)}` : ""].filter(Boolean).join(" · ")}<StatusBadge status={item.status} /></small></button>)}
        </section>
      </div>}
    </div>
  </div>;
}

function RunDetail({ run, busy, onAct, onTraces }: { run: Row; busy: boolean; onAct: (path: string, body?: unknown) => void; onTraces: (runId: string) => void }) {
  const status = String(run.status ?? "");
  const pending = run.pending_tool && typeof run.pending_tool === "object" ? run.pending_tool as Row : null;
  const events = rows(run.events);
  return <section className="run-section" aria-label="Selected run">
    <div className="section-heading"><h3>Run <StatusBadge status={status} /></h3><small>{String(run.id)}</small></div>
    {status === "waiting_approval" && pending && <div className="approval">
      <strong>Approval required: {String(pending.name)}</strong>
      <p className="muted">The agent wants to call this sensitive tool with the arguments below. Rejecting tells the agent the call was denied, and the run continues without it.</p>
      <Json value={pending.arguments ?? {}} />
      <div className="actions">
        <button className="primary" disabled={busy} onClick={() => onAct("approve", { approved: true })}>Approve tool call</button>
        <button disabled={busy} onClick={() => onAct("approve", { approved: false })}>Reject tool call</button>
      </div>
    </div>}
    {status === "cancelling" && <p className="muted">Cancellation requested. The run stops at its next checkpoint.</p>}
    {status === "interrupted" && <p className="muted">The worker stopped before this run finished. Resuming queues it again; a pending sensitive tool call must be approved again.</p>}
    {typeof run.output === "string" && run.output && <><h4>Output</h4><p className="output">{run.output}</p></>}
    {typeof run.error === "string" && run.error && <p className="run-error">{run.error}</p>}
    <div className="actions">
      {CANCELLABLE.includes(status) && <ConfirmButton label="Cancel run" confirm="Yes, cancel run" disabled={busy} onConfirm={() => onAct("cancel")} />}
      {status === "interrupted" && <button disabled={busy} onClick={() => onAct("resume", { background: true })}>Resume run</button>}
      <button type="button" onClick={() => onTraces(String(run.id))}>View traces ↗</button>
    </div>
    {events.length > 0 && <><h4>Timeline</h4><ol className="timeline">{events.map((event, i) => <li key={i}><StatusBadge status={event.type} /><time dateTime={String(event.at ?? "")}>{time(event.at)}</time><p>{describe(event)}</p></li>)}</ol></>}
    <details className="raw"><summary>Raw run JSON</summary><Json value={run} /></details>
  </section>;
}
