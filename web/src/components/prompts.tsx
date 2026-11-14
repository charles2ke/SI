"use client";

import { useState } from "react";
import type { Api, Row } from "@/lib/api";
import { ErrorBox, Notice, ResourceStatus, placeholders, plural, segment, useAction, useResource } from "@/components/common";

function library(rows: Row[]) {
  const groups = new Map<string, Row[]>();
  for (const row of rows) groups.set(String(row.name), [...(groups.get(String(row.name)) ?? []), row]);
  return [...groups].map(([name, versions]) => ({ name, versions: [...versions].sort((a, b) => Number(b.version) - Number(a.version)) }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

export function Prompts({ api }: { api: Api }) {
  const prompts = useResource(api, "/v1/prompts");
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [template, setTemplate] = useState("");
  const [version, setVersion] = useState(0);
  const [variables, setVariables] = useState<Record<string, string>>({});
  const [rendered, setRendered] = useState<string | null>(null);
  const action = useAction();
  const entries = library(prompts.rows);
  const entry = creating ? undefined : entries.find(item => item.name === name) ?? entries[0];
  const current = entry?.versions.find(item => Number(item.version) === version) ?? entry?.versions[0];
  const fields = current ? placeholders(String(current.template)) : [];
  const latest = Number(entry?.versions[0]?.version ?? 0);

  function choose(next: string | null) {
    setCreating(next === null); setName(next ?? ""); setVersion(0); setTemplate(""); setRendered(null); setVariables({});
    action.setError(""); action.setNotice("");
  }
  function save(promptName: string) {
    void action.perform(async () => {
      const saved = await api.request("/v1/prompts", { method: "POST", body: JSON.stringify({ name: promptName, template }) }) as Row;
      await prompts.refresh();
      choose(String(saved.name)); setNewName("");
      return `Saved ${String(saved.name)} v${String(saved.version)}.`;
    });
  }
  function render(promptName: string, promptVersion: number) {
    void action.perform(async () => {
      const body = { variables: Object.fromEntries(fields.map(field => [field, variables[field] ?? ""])) };
      const value = await api.request(`/v1/prompts/${segment(promptName)}/${promptVersion}/render`, { method: "POST", body: JSON.stringify(body) }) as Row;
      setRendered(String(value.rendered ?? ""));
    });
  }

  return <div className="split">
    <div className="panel">
      <div className="panel-heading"><h2>Prompt library</h2><button onClick={() => choose(null)}>+ New</button></div>
      <ResourceStatus {...prompts} />
      {!prompts.loading && !entries.length && <p className="padded muted">No prompts yet. Create your first template.</p>}
      {entries.map(item => <button key={item.name} className={`list-item ${entry?.name === item.name ? "selected" : ""}`} onClick={() => choose(item.name)}><strong>{item.name}</strong><small>v{String(item.versions[0].version)} latest · {plural(item.versions.length, "version")}</small></button>)}
    </div>
    <div className="panel padded">
      <ErrorBox error={action.error} /><Notice notice={action.notice} />
      {entry && current ? <>
        <div className="section-heading">
          <h2>{entry.name}</h2>
          <label className="inline">Version<select value={Number(current.version)} onChange={e => { setVersion(Number(e.target.value)); setRendered(null); }}>{entry.versions.map((item, i) => <option key={String(item.version)} value={Number(item.version)}>v{String(item.version)}{i === 0 ? " (latest)" : ""}</option>)}</select></label>
        </div>
        <h3>Template</h3>
        <pre>{String(current.template)}</pre>
        <form className="run-section" onSubmit={e => { e.preventDefault(); render(entry.name, Number(current.version)); }}>
          <h3>Render</h3>
          {!fields.length && <p className="muted">This template has no variables.</p>}
          <div className="two-columns">{fields.map(field => <label key={field}>{field}<input value={variables[field] ?? ""} onChange={e => setVariables({ ...variables, [field]: e.target.value })} /></label>)}</div>
          <button disabled={action.busy}>Render prompt</button>
          {rendered !== null && <><h4>Rendered</h4><pre>{rendered}</pre></>}
        </form>
        <form className="run-section" onSubmit={e => { e.preventDefault(); save(entry.name); }}>
          <h3>New version</h3>
          <label>Template<textarea className="code-input" required rows={6} maxLength={100000} value={template} onChange={e => setTemplate(e.target.value)} placeholder="Use {name} placeholders for variables." /></label>
          <div className="actions">
            <button type="button" onClick={() => setTemplate(String(current.template))}>Copy v{String(current.version)}</button>
            <button className="primary" disabled={action.busy || !template.trim()}>Save as v{latest + 1}</button>
          </div>
        </form>
      </> : <form onSubmit={e => { e.preventDefault(); save(newName.trim()); }}>
        <h2>Create a prompt</h2>
        <p className="muted">Templates use {"{name}"} placeholders. Saving an existing name again creates a new version.</p>
        <label>Prompt name <small>Letters, numbers, dots, dashes, and underscores.</small><input required maxLength={200} pattern="(?=.*[A-Za-z0-9_\-])[A-Za-z0-9_.\-]+" value={newName} onChange={e => setNewName(e.target.value)} placeholder="support-reply" /></label>
        <label>Template<textarea className="code-input" required rows={8} maxLength={100000} value={template} onChange={e => setTemplate(e.target.value)} placeholder="Answer {question} for {customer}." /></label>
        <button className="primary" disabled={action.busy}>Create prompt</button>
      </form>}
    </div>
  </div>;
}
