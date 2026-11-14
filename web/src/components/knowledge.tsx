"use client";

import { useEffect, useRef, useState } from "react";
import { list, type Api, type Row } from "@/lib/api";
import { ConfirmButton, ErrorBox, Notice, ResourceStatus, plural, segment, useAction, useResource } from "@/components/common";

const chunks = (collection: Row | undefined) => typeof collection?.chunks === "number" ? plural(collection.chunks, "chunk") : "";

export function Knowledge({ api }: { api: Api }) {
  const collections = useResource(api, "/v1/collections");
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState("5");
  const [results, setResults] = useState<Row[] | null>(null);
  const searchRequest = useRef(0);
  const action = useAction();
  const selected = id || String(collections.rows[0]?.id || "");
  const selectedRef = useRef(selected);
  useEffect(() => { selectedRef.current = selected; }, [selected]);
  const collection = collections.rows.find(row => String(row.id) === selected);
  const base = `/v1/collections/${segment(selected)}`;

  function choose(next: string) {
    searchRequest.current += 1;
    selectedRef.current = next || String(collections.rows[0]?.id || "");
    setId(next); setResults(null); action.setError(""); action.setNotice("");
  }
  function create() {
    void action.perform(async () => {
      const created = await api.request("/v1/collections", { method: "POST", body: JSON.stringify({ name }) }) as Row;
      setId(String(created.id)); setName(""); setResults(null);
      await collections.refresh();
      return `Created ${String(created.name)}.`;
    });
  }
  function upload(form: HTMLFormElement) {
    const data = new FormData(form);
    void action.perform(async () => {
      const uploaded = await api.request(`${base}/documents`, { method: "POST", body: data }) as Row;
      form.reset();
      await collections.refresh();
      return `Document uploaded. ${plural(Number(uploaded?.chunks ?? 0), "chunk")} indexed.`;
    });
  }
  function importUrl() {
    void action.perform(async () => {
      const imported = await api.request(`${base}/urls`, { method: "POST", body: JSON.stringify({ url }) }) as Row;
      setUrl("");
      await collections.refresh();
      return `Imported ${String(imported.url ?? url)}. ${plural(Number(imported.chunks ?? 0), "chunk")} indexed.`;
    });
  }
  function search() {
    const target = selected;
    const request = ++searchRequest.current;
    void action.perform(async () => {
      const result = await api.request(`${base}/search`, { method: "POST", body: JSON.stringify({ query, limit: Number(limit) }) });
      if (request === searchRequest.current && target === selectedRef.current) setResults(list(result));
    });
  }
  function remove() {
    void action.perform(async () => {
      await api.request(base, { method: "DELETE" });
      choose("");
      await collections.refresh();
      return "Collection deleted.";
    });
  }

  return <div className="split">
    <div className="panel padded">
      <h2>Knowledge bases</h2>
      <ResourceStatus {...collections} />
      <form onSubmit={e => { e.preventDefault(); create(); }}>
        <label>Collection name<input required maxLength={200} value={name} onChange={e => setName(e.target.value)} placeholder="Product documentation" /></label>
        <button disabled={action.busy || !name.trim()}>+ Create collection</button>
      </form>
      <div className="run-section">
        {!collections.loading && !collections.rows.length && <p className="muted">No collections yet.</p>}
        {collections.rows.map(row => <button key={String(row.id)} className={`list-item ${selected === row.id ? "selected" : ""}`} onClick={() => choose(String(row.id))}><strong>{String(row.name)}</strong><small>{[chunks(row), String(row.id)].filter(Boolean).join(" · ")}</small></button>)}
      </div>
    </div>
    <div className="panel padded">
      <div className="section-heading">
        <h2>{collection ? String(collection.name) : "Explore your knowledge"}</h2>
        {collection && <ConfirmButton key={selected} label="Delete collection" confirm="Delete collection and chunks" disabled={action.busy} onConfirm={remove} />}
      </div>
      <ErrorBox error={action.error} /><Notice notice={action.notice} />
      {selected ? <>
        <p className="muted">{chunks(collection) ? `${chunks(collection)} indexed. ` : ""}Add documents or web pages, then search for relevant passages. Collections used by agents can’t be deleted.</p>
        <form onSubmit={e => { e.preventDefault(); upload(e.currentTarget); }}>
          <label>Document<input type="file" name="file" required /></label>
          <button disabled={action.busy}>Upload document ↑</button>
        </form>
        <form className="run-section" onSubmit={e => { e.preventDefault(); importUrl(); }}>
          <label>Web page URL <small>Only hosts on the server’s HTTP_ALLOWED_HOSTS allowlist can be fetched.</small><input type="url" required maxLength={8192} value={url} onChange={e => setUrl(e.target.value)} placeholder="https://docs.example.com/guide" /></label>
          <button disabled={action.busy || !url.trim()}>Import URL ↓</button>
        </form>
        <form className="run-section" onSubmit={e => { e.preventDefault(); search(); }}>
          <div className="two-columns wide-first">
            <label>Search query<input value={query} required maxLength={10000} onChange={e => setQuery(e.target.value)} placeholder="What would you like to find?" /></label>
            <label>Results<input type="number" min={1} max={50} required value={limit} onChange={e => setLimit(e.target.value)} /></label>
          </div>
          <button className="primary" disabled={action.busy || !query.trim()}>{action.busy ? "Working…" : "Search knowledge →"}</button>
        </form>
        {results && <section className="run-section" aria-label="Search results">
          <h3>Results <small>{plural(results.length, "passage")}</small></h3>
          {!results.length && <p className="muted">No matching passages.</p>}
          <ol className="results">{results.map((result, i) => <li key={String(result.id ?? i)}>
            <span className="badge">Score {typeof result.score === "number" ? result.score.toFixed(3) : "—"}</span><small>{String(result.id ?? "")}</small>
            <p>{String(result.text ?? "")}</p>
          </li>)}</ol>
        </section>}
      </> : <p className="muted">Create a collection to upload documents and search.</p>}
    </div>
  </div>;
}
