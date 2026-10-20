"""Synchronous, project-scoped SI client. API errors are never hidden."""

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx


class SIError(RuntimeError):
    pass


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
        return response.json()

    def models(self) -> Any:
        return self.request("GET", "/v1/models")

    def chat(self, content: str, model: str = "mock", **options: Any) -> dict[str, Any]:
        return self.request(
            "POST", "/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": content}], **options},
        )

    def stream(self, content: str, model: str = "mock") -> Iterator[str]:
        with self.http.stream(
            "POST", "/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": content}], "stream": True},
        ) as response:
            if response.is_error:
                response.read()
                self._check(response)
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                event = json.loads(data)
                if "error" in event:
                    raise SIError(str(event["error"]))
                for choice in event.get("choices", []):
                    text = choice.get("delta", {}).get("content")
                    if text:
                        yield text

    def collections(self) -> Any:
        return self.request("GET", "/v1/collections")

    def create_collection(self, name: str) -> dict[str, Any]:
        return self.request("POST", "/v1/collections", json={"name": name})

    def ingest(self, collection_id: str, file: str | Path) -> Any:
        path = Path(file)
        with path.open("rb") as document:
            return self.request(
                "POST", f"/v1/collections/{collection_id}/documents",
                files={"file": (path.name, document)},
            )

    def search(self, collection_id: str, query: str) -> Any:
        return self.request("POST", f"/v1/collections/{collection_id}/search", json={"query": query})

    def agents(self) -> Any:
        return self.request("GET", "/v1/agents")

    def create_agent(self, **definition: Any) -> dict[str, Any]:
        return self.request("POST", "/v1/agents", json=definition)

    def run_agent(
        self, agent_id: str, content: str, *, session_id: str | None = None,
        wait: bool = True, timeout: float = 120,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"input": content, "background": True}
        if session_id:
            payload["session_id"] = session_id
        run = self.request("POST", f"/v1/agents/{agent_id}/runs", json=payload)
        if not wait:
            return run
        deadline = time.monotonic() + timeout
        while run["status"] in {"queued", "running"}:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Run {run['id']} is still pending")
            time.sleep(0.25)
            run = self.request("GET", f"/v1/runs/{run['id']}")
        if run["status"] not in {"completed", "waiting_approval"}:
            raise SIError(f"Run {run['id']} {run['status']}: {run.get('error')}")
        return run

    def approve(self, run_id: str, approved: bool = True) -> Any:
        return self.request("POST", f"/v1/runs/{run_id}/approve", json={"approved": approved})

    def traces(self) -> Any:
        return self.request("GET", "/v1/traces")

    def create_prompt(self, name: str, template: str) -> Any:
        return self.request("POST", "/v1/prompts", json={"name": name, "template": template})

    def render_prompt(self, name: str, version: int, variables: dict[str, str]) -> Any:
        return self.request(
            "POST", f"/v1/prompts/{name}/{version}/render", json={"variables": variables},
        )

    def evaluate(
        self, dataset: list[dict[str, Any]], scorer: str = "exact_match", **target: Any,
    ) -> dict[str, Any]:
        return self.request("POST", "/v1/evals", json={
            "dataset": dataset, "scorer": scorer, **target,
        })
