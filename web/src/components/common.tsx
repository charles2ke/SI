"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { list, type Api, type Row } from "@/lib/api";

export const text = (value: unknown) => typeof value === "string" ? value : JSON.stringify(value, null, 2);
export const segment = (value: unknown) => encodeURIComponent(String(value));
export const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;
export const score = (value: unknown) => typeof value === "number" ? value.toFixed(2) : "—";
export const rows = (value: unknown) => Array.isArray(value) ? value as Row[] : [];

/** Compact single-line preview of any value. */
export function clip(value: unknown, length = 160) {
  const content = typeof value === "string" ? value : JSON.stringify(value ?? "");
  return content.length > length ? `${content.slice(0, length - 1)}…` : content;
}

/** Format API timestamps; naive values (SQLite) are stored as UTC. */
export function time(value: unknown) {
  if (typeof value !== "string" || !value) return "";
  const date = new Date(/(z|[+-]\d\d:?\d\d)$/i.test(value) ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

/** Named fields of a Python str.format template; {{ and }} are literal braces. */
export function placeholders(template: string) {
  const names = [...template.matchAll(/\{\{|\}\}|\{([^{}]*)\}/g)]
    .map(match => match[1]).filter((name): name is string => name !== undefined)
    .map(name => name.split(/[!:]/)[0].trim()).filter(Boolean);
  return [...new Set(names)];
}

export function Empty({ title, detail }: { title: string; detail: string }) {
  return <div className="empty panel"><span className="empty-symbol" aria-hidden="true">✳</span><h2>{title}</h2><p className="muted">{detail}</p></div>;
}
export function ErrorBox({ error }: { error: string }) { return error ? <div role="alert" className="error">{error}</div> : null; }
export function Notice({ notice }: { notice: string }) { return notice ? <p role="status" className="success">{notice}</p> : null; }
export function Json({ value }: { value: unknown }) { return <pre>{text(value)}</pre>; }
export function StatusBadge({ status }: { status: unknown }) {
  const value = String(status || "unknown");
  return <span className={`badge status-${value.replace(/[^a-z_]/g, "")}`}>{value.replace(/_/g, " ")}</span>;
}

type Loaded = { path: string | null; value: unknown; error: string; loading: boolean };

/** Load a GET endpoint; a null path loads nothing. Only the latest request may update state. */
export function useResource(api: Api, path: string | null) {
  const [state, setState] = useState<Loaded>({ path: null, value: null, error: "", loading: false });
  const latest = useRef(0);
  const load = useCallback(async (signal?: AbortSignal) => {
    if (!path) return;
    const request = ++latest.current;
    try {
      const value = await api.request(path, { signal });
      if (request === latest.current) setState({ path, value, error: "", loading: false });
    } catch (e) {
      if ((e as Error).name === "AbortError" || request !== latest.current) return;
      setState(current => ({ path, value: current.path === path ? current.value : null, error: (e as Error).message, loading: false }));
    }
  }, [api, path]);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);
  const refresh = useCallback(async () => {
    setState(current => ({ ...current, loading: true }));
    await load();
  }, [load]);
  const current = state.path === path;
  const value = current ? state.value : null;
  return { value, rows: list(value), loading: Boolean(path) && (!current || state.loading), error: current ? state.error : "", refresh };
}

export function ResourceStatus({ loading, error, value }: { loading: boolean; error: string; value: unknown }) {
  return <><ErrorBox error={error} />{loading && value === null && <p role="status" className="muted">Loading live data…</p>}</>;
}

/** Shared busy/error/notice state for user-initiated actions; a returned string becomes the notice. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const perform = useCallback(async (fn: () => Promise<string | void>) => {
    setBusy(true); setError(""); setNotice("");
    try { const message = await fn(); if (message) setNotice(message); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }, []);
  return { busy, error, notice, perform, setError, setNotice };
}

/** Two-step button for destructive actions. */
export function ConfirmButton({ label, confirm, onConfirm, disabled }: { label: string; confirm: string; onConfirm: () => void; disabled?: boolean }) {
  const [asking, setAsking] = useState(false);
  if (!asking) return <button type="button" className="danger" disabled={disabled} onClick={() => setAsking(true)}>{label}</button>;
  return <span className="confirm" role="group" aria-label={label}>
    <button type="button" className="danger solid" disabled={disabled} onClick={() => { setAsking(false); onConfirm(); }}>{confirm}</button>
    <button type="button" autoFocus onClick={() => setAsking(false)}>Go back</button>
  </span>;
}
