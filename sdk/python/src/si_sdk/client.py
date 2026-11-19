"""Synchronous, project-scoped SI client. API errors are never hidden."""

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

PENDING_RUN_STATUSES = frozenset({"queued", "running", "cancelling"})


class SIError(RuntimeError):
    pass


def _segment(value: str) -> str:
    """Quote an identifier so it always stays one path segment."""
    encoded = quote(str(value), safe="")
    # HTTP clients collapse literal dot segments, which would change the endpoint.
    return encoded.replace(".", "%2E") if encoded in {".", ".."} else encoded


class Client:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 60,
        transport: httpx.BaseTransport | None = None,
    ):
        key = api_key or os.getenv("SI_API_KEY")
        if not key:
            raise ValueError("Set SI_API_KEY or pass api_key")
        self.http = httpx.Client(
            base_url=(base_url or os.getenv("SI_API_URL") or "http://localhost:8000").rstrip("/"),
            headers={"Authorization": "Bearer " + key},
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self.http.close()

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.is_error:
            raise SIError(f"SI returned HTTP {response.status_code}: {response.text[:1000]}")

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.http.request(method, path, **kwargs)
        self._check(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _events(self, method: str, path: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
        """Yield server-sent JSON events until the [DONE] sentinel."""
        with self.http.stream(method, path, **kwargs) as response:
            if response.is_error:
                response.read()
                self._check(response)
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                yield json.loads(data)

    def models(self) -> Any:
        return self.request("GET", "/v1/models")

    def chat(self, content: str, model: str = "mock", **options: Any) -> dict[str, Any]:
        return self.request(
            "POST", "/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": content}], **options},
        )

    def stream(self, content: str, model: str = "mock") -> Iterator[str]:
        for event in self._events(
            "POST", "/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": content}], "stream": True},
        ):
            if "error" in event:
                raise SIError(str(event["error"]))
            for choice in event.get("choices", []):
                text = choice.get("delta", {}).get("content")
                if text:
                    yield text

    def collections(self) -> Any:
        return self.request("GET", "/v1/collections")

    def get_collection(self, collection_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/collections/{_segment(collection_id)}")

    def create_collection(self, name: str) -> dict[str, Any]:
        return self.request("POST", "/v1/collections", json={"name": name})

    def delete_collection(self, collection_id: str) -> None:
        self.request("DELETE", f"/v1/collections/{_segment(collection_id)}")

    def ingest(self, collection_id: str, file: str | Path) -> Any:
        path = Path(file)
        with path.open("rb") as document:
            return self.request(
                "POST", f"/v1/collections/{_segment(collection_id)}/documents",
                files={"file": (path.name, document)},
            )

    def ingest_url(self, collection_id: str, url: str) -> dict[str, Any]:
        """Fetch a page server-side; its host must be on the server's HTTP_ALLOWED_HOSTS."""
        return self.request(
            "POST", f"/v1/collections/{_segment(collection_id)}/urls", json={"url": url},
        )

    def search(self, collection_id: str, query: str, limit: int = 5) -> Any:
        return self.request(
            "POST", f"/v1/collections/{_segment(collection_id)}/search",
            json={"query": query, "limit": limit},
        )

    def tools(self) -> dict[str, Any]:
        return self.request("GET", "/v1/tools")

    def agents(self) -> Any:
        return self.request("GET", "/v1/agents")

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/agents/{_segment(agent_id)}")

    def create_agent(self, **definition: Any) -> dict[str, Any]:
        return self.request("POST", "/v1/agents", json=definition)

    def update_agent(self, agent_id: str, **changes: Any) -> dict[str, Any]:
        return self.request("PATCH", f"/v1/agents/{_segment(agent_id)}", json=changes)

    def delete_agent(self, agent_id: str) -> None:
        """Delete an agent with its run history; fails while it has active runs."""
        self.request("DELETE", f"/v1/agents/{_segment(agent_id)}")

    def run_agent(
        self, agent_id: str, content: str, *, session_id: str | None = None,
        wait: bool = True, timeout: float = 120,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"input": content, "background": True}
        if session_id:
            payload["session_id"] = session_id
        run = self.request("POST", f"/v1/agents/{_segment(agent_id)}/runs", json=payload)
        return self._settled(self.wait_for_run(run, timeout=timeout)) if wait else run

    def runs(
        self, *, agent_id: str | None = None, status: str | None = None,
        session_id: str | None = None, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Newest first, without transcripts or events; use get_run for details."""
        filters = {"agent_id": agent_id, "status": status, "session_id": session_id}
        params = {name: value for name, value in filters.items() if value is not None}
        return self.request("GET", "/v1/runs", params={**params, "limit": limit})

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/runs/{_segment(run_id)}")

    def wait_for_run(
        self, run: str | dict[str, Any], *, timeout: float = 120, interval: float = 0.25,
    ) -> dict[str, Any]:
        """Poll until the run stops progressing on its own; the outcome is not judged."""
        current = self.get_run(run) if isinstance(run, str) else run
        deadline = time.monotonic() + timeout
        while current["status"] in PENDING_RUN_STATUSES:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Run {current['id']} is still {current['status']}")
            time.sleep(interval)
            current = self.get_run(current["id"])
        return current

    @staticmethod
    def _settled(run: dict[str, Any]) -> dict[str, Any]:
        if run["status"] not in {"completed", "waiting_approval"}:
            raise SIError(f"Run {run['id']} {run['status']}: {run.get('error')}")
        return run

    def run_events(
        self, run_id: str, *, last_event_id: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream persisted run events until the run completes, pauses, or stops."""
        headers = {} if last_event_id is None else {"Last-Event-ID": str(last_event_id)}
        yield from self._events("GET", f"/v1/runs/{_segment(run_id)}/events", headers=headers)

    def approve(self, run_id: str, approved: bool = True) -> Any:
        return self.request(
            "POST", f"/v1/runs/{_segment(run_id)}/approve", json={"approved": approved},
        )

    def reject(self, run_id: str) -> Any:
        return self.approve(run_id, approved=False)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Paused runs cancel at once; running ones report "cancelling" until they stop."""
        return self.request("POST", f"/v1/runs/{_segment(run_id)}/cancel")

    def resume_run(
        self, run_id: str, *, wait: bool = True, timeout: float = 120,
    ) -> dict[str, Any]:
        """Requeue an interrupted run. Sensitive tools in flight need approval again."""
        run = self.request(
            "POST", f"/v1/runs/{_segment(run_id)}/resume", json={"background": True},
        )
        return self._settled(self.wait_for_run(run, timeout=timeout)) if wait else run

    def memory(self, session_id: str) -> Any:
        return self.request("GET", "/v1/memory", params={"session_id": session_id})

    def remember(
        self, session_id: str, content: str, *, role: str = "user", long_term: bool = False,
    ) -> Any:
        return self.request("POST", "/v1/memory", json={
            "session_id": session_id, "content": content, "role": role, "long_term": long_term,
        })

    def traces(self, run_id: str | None = None, kind: str | None = None) -> Any:
        params = {key: value for key, value in (("run_id", run_id), ("kind", kind)) if value}
        return self.request("GET", "/v1/traces", params=params or None)

    def prompts(self) -> Any:
        return self.request("GET", "/v1/prompts")

    def prompt_versions(self, name: str) -> Any:
        return self.request("GET", f"/v1/prompts/{_segment(name)}/versions")

    def create_prompt(self, name: str, template: str) -> Any:
        return self.request("POST", "/v1/prompts", json={"name": name, "template": template})

    def render_prompt(self, name: str, version: int, variables: dict[str, Any]) -> Any:
        return self.request(
            "POST", f"/v1/prompts/{_segment(name)}/{int(version)}/render",
            json={"variables": variables},
        )

    def evaluate(
        self, dataset: list[dict[str, Any]], scorer: str = "exact_match", **target: Any,
    ) -> dict[str, Any]:
        return self.request("POST", "/v1/evals", json={
            "dataset": dataset, "scorer": scorer, **target,
        })

    def evals(self) -> Any:
        return self.request("GET", "/v1/evals")

    def get_eval(self, eval_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/evals/{_segment(eval_id)}")

    def api_keys(self) -> Any:
        return self.request("GET", "/v1/api-keys")

    def create_api_key(self, name: str, role: str = "developer") -> dict[str, Any]:
        """The returned key is shown once; store it securely."""
        return self.request("POST", "/v1/api-keys", json={"name": name, "role": role})

    def revoke_api_key(self, key_id: str) -> None:
        self.request("DELETE", f"/v1/api-keys/{_segment(key_id)}")
