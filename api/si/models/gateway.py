"""Direct REST adapters. Prices are estimates in USD, not billing guarantees.

The default mock needs no credentials. Use provider-prefixed model names, or
``auto`` / ``cheapest`` / ``fastest`` / ``best`` for configured-provider routing.
No prompt or credentials are included in provider error messages.
"""

import asyncio
import hashlib
import json
import math
import os
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jsonschema
from referencing import Registry
from referencing.exceptions import Unresolvable

from .schemas import ChatRequest, ChatResponse, ToolCall

MODELS: dict[str, dict[str, Any]] = {
    "mock": {"provider": "mock", "input_price": 0.0, "output_price": 0.0, "speed": 0, "quality": 0},
    "openai:gpt-4o-mini": {
        "provider": "openai", "input_price": 0.15, "output_price": 0.60, "speed": 1, "quality": 2,
    },
    "anthropic:claude-sonnet-4-20250514": {
        "provider": "anthropic", "input_price": 3.0, "output_price": 15.0, "speed": 3, "quality": 3,
    },
    "gemini:gemini-2.0-flash": {
        "provider": "gemini", "input_price": 0.10, "output_price": 0.40, "speed": 2, "quality": 1,
    },
    "compatible:default": {
        "provider": "compatible", "input_price": 0.0, "output_price": 0.0, "speed": 1, "quality": 1,
    },
}
KEYS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "compatible": "OPENAI_COMPATIBLE_API_KEY",
}


class GatewayError(RuntimeError):
    """A provider failed, or returned an invalid normalized response."""


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value or "{}")
    if not isinstance(value, dict):
        raise GatewayError("Provider tool arguments must be a JSON object")
    return value


def _sample(
    schema: dict[str, Any], root: dict[str, Any] | None = None,
    depth: int = 0, budget: list[int] | None = None,
) -> Any:
    budget = [2048] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0:
        raise GatewayError("Mock schema sample exceeds node limits")
    if depth > 16:
        raise GatewayError("Mock schema exceeds supported recursion depth")
    root = schema if root is None else root
    if "$ref" in schema:
        reference = schema["$ref"]
        if not reference.startswith("#/"):
            raise GatewayError("Only local schema references are supported")
        target: Any = root
        for part in reference[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        return _sample(target, root, depth + 1, budget)
    for field in ("const", "default"):
        if field in schema:
            return schema[field]
    if "enum" in schema:
        return schema["enum"][0]
    for union in ("anyOf", "oneOf"):
        if union in schema:
            return _sample(schema[union][0], root, depth + 1, budget)
    kind = schema.get("type", "object")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "null")
    if kind == "object":
        return {
            key: _sample(value, root, depth + 1, budget)
            for key, value in schema.get("properties", {}).items()
        }
    if kind == "array":
        count = schema.get("minItems", 0)
        if count > 128:
            raise GatewayError("Mock schema sample exceeds array limits")
        return [_sample(schema.get("items", {}), root, depth + 1, budget) for _ in range(count)]
    if kind == "string":
        if schema.get("minLength", 0) > 4096:
            raise GatewayError("Mock schema sample exceeds string limits")
        formats = {"email": "sample@example.com", "date": "2026-01-01", "date-time": "2026-01-01T00:00:00Z"}
        value = formats.get(schema.get("format", ""), "sample")
        return value[:schema.get("maxLength", len(value))].ljust(schema.get("minLength", 0), "x")
    if kind in ("number", "integer"):
        value = schema.get("minimum", 0)
        if "exclusiveMinimum" in schema:
            value = schema["exclusiveMinimum"] + 1
        value = min(value, schema.get("maximum", value))
        return int(value) if kind == "integer" else float(value)
    return False if kind == "boolean" else None


class Gateway:
    def __init__(
        self,
        *,
        policy: str = "cheapest",
        retries: int = 2,
        fallback_models: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
        backoff: float = 0.1,
    ) -> None:
        if policy not in ("cheapest", "fastest", "best"):
            raise ValueError("Unknown routing policy")
        self.policy = policy
        self.retries = max(0, retries)
        self.fallback_models = fallback_models or []
        self.transport = transport
        self.timeout = timeout
        self.backoff = backoff

    @staticmethod
    def _available(provider: str) -> bool:
        if provider == "mock":
            return True
        if provider == "compatible":
            return bool(os.getenv("OPENAI_COMPATIBLE_BASE_URL"))
        return bool(os.getenv(KEYS[provider]))

    def model_info(self, model: str) -> dict[str, Any]:
        provider, _ = self._resolve(model)
        info = MODELS.get(model, next(value for value in MODELS.values() if value["provider"] == provider))
        return {"id": model, **info, "available": self._available(provider)}

    def list_models(self) -> list[dict[str, Any]]:
        return [self.model_info(model) for model in MODELS]

    def route(self, policy: str | None = None) -> str:
        policy = policy or self.policy
        if policy not in ("cheapest", "fastest", "best"):
            raise ValueError("Unknown routing policy")
        candidates = [name for name, info in MODELS.items()
                      if name != "mock" and self._available(info["provider"])]
        if not candidates:
            return "mock"
        def score(name: str) -> float:
            info = MODELS[name]
            if policy == "best":
                return -float(info["quality"])
            return float(info["speed"] if policy == "fastest" else info["input_price"] + info["output_price"])
        return min(candidates, key=score)

    @staticmethod
    def _resolve(model: str) -> tuple[str, str]:
        if model == "mock":
            return "mock", "mock"
        if ":" in model:
            provider, name = model.split(":", 1)
        elif model.startswith("claude"):
            provider, name = "anthropic", model
        elif model.startswith("gemini"):
            provider, name = "gemini", model
        else:
            provider, name = "openai", model
        if provider not in KEYS or not name:
            raise GatewayError("Unknown provider or empty model name")
        return provider, name

    def _candidates(self, model: str) -> list[str]:
        automatic = model in ("auto", "cheapest", "fastest", "best")
        primary = self.route(None if model == "auto" else model) if automatic else model
        alternatives = [
            name for name, info in MODELS.items()
            if name != "mock" and self._available(info["provider"])
        ] if automatic else []
        return list(dict.fromkeys([primary, *self.fallback_models, *alternatives]))

    async def chat(self, request: ChatRequest) -> ChatResponse:
        self._check_request_schemas(request)
        for model in self._candidates(request.model):
            for attempt in range(self.retries + 1):
                try:
                    response = await self._chat(request.model_copy(update={"model": model}))
                    if request.response_schema is not None and not response.tool_calls:
                        jsonschema.Draft202012Validator(
                            request.response_schema, registry=Registry(),
                        ).validate(json.loads(response.content))
                    return response
                except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError,
                        jsonschema.ValidationError, jsonschema.SchemaError, Unresolvable,
                        RecursionError, GatewayError) as exc:
                    if not self._retryable(exc):
                        break
                    if attempt < self.retries:
                        await asyncio.sleep(self.backoff * 2**attempt)
        raise GatewayError("All configured model attempts failed") from None

    @staticmethod
    def _check_schema(schema: dict[str, Any]) -> None:
        """Bound untrusted schema work and forbid remote reference resolution."""
        pending: list[tuple[Any, int]] = [(schema, 0)]
        nodes = 0
        size = 0
        while pending:
            value, depth = pending.pop()
            nodes += 1
            if depth > 32 or nodes > 2048:
                raise GatewayError("JSON schema exceeds nesting or node limits")
            if isinstance(value, dict):
                if len(value) + len(pending) > 2048:
                    raise GatewayError("JSON schema exceeds node limits")
                for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
                    if keyword in value:
                        reference = value[keyword]
                        if not isinstance(reference, str) or not reference.startswith("#"):
                            raise GatewayError("Only local schema references are supported")
                for key, child in value.items():
                    if not isinstance(key, str):
                        raise GatewayError("JSON schema keys must be strings")
                    size += len(key)
                    pending.append((child, depth + 1))
            elif isinstance(value, list):
                if len(value) + len(pending) > 2048:
                    raise GatewayError("JSON schema exceeds node limits")
                for child in value:
                    pending.append((child, depth + 1))
            elif isinstance(value, str):
                size += len(value)
            if size > 65536 or len(pending) > 2048:
                raise GatewayError("JSON schema exceeds size limits")
        try:
            if len(json.dumps(schema, allow_nan=False).encode("utf-8")) > 65536:
                raise GatewayError("JSON schema exceeds size limits")
            jsonschema.Draft202012Validator.check_schema(schema)
        except (jsonschema.SchemaError, ValueError, TypeError, RecursionError):
            raise GatewayError("Invalid JSON schema") from None

    def _check_request_schemas(self, request: ChatRequest) -> None:
        if request.response_schema is not None:
            self._check_schema(request.response_schema)
        for tool in request.tools:
            self._check_schema(tool.parameters)

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in (408, 409, 429) or exc.response.status_code >= 500
        return isinstance(exc, httpx.TransportError)

    def _endpoint(self, provider: str, name: str, *, stream: bool = False) -> tuple[str, dict[str, str]]:
        key = os.getenv(KEYS[provider], "")
        if not self._available(provider):
            raise GatewayError("Provider is not configured")
        if provider == "anthropic":
            return "https://api.anthropic.com/v1/messages", {
                "x-api-key": key, "anthropic-version": "2023-06-01",
            }
        if provider == "gemini":
            action = "streamGenerateContent?alt=sse" if stream else "generateContent"
            return f"https://generativelanguage.googleapis.com/v1beta/models/{name}:{action}", {
                "x-goog-api-key": key,
            }
        base = os.getenv("OPENAI_COMPATIBLE_BASE_URL", "") if provider == "compatible" else "https://api.openai.com/v1"
        headers = {"Authorization": "Bearer " + key} if key else {}
        return f"{base.rstrip('/')}/chat/completions", headers

    def _payload(self, request: ChatRequest, provider: str, name: str) -> dict[str, Any]:
        common: dict[str, Any] = {
            "model": name, "temperature": request.temperature, "max_tokens": request.max_tokens,
        }
        if provider in ("openai", "compatible"):
            messages = []
            for message in request.messages:
                item: dict[str, Any] = {"role": message.role, "content": message.content}
                if message.tool_call_id:
                    item["tool_call_id"] = message.tool_call_id
                if message.tool_calls:
                    item["tool_calls"] = [{
                        "id": call.id, "type": "function", "function": {
                            "name": call.name, "arguments": json.dumps(call.arguments),
                        },
                    } for call in message.tool_calls]
                messages.append(item)
            common["messages"] = messages
            if request.tools:
                common["tools"] = [{"type": "function", "function": {
                    "name": tool.name, "description": tool.description, "parameters": tool.parameters,
                }} for tool in request.tools]
            if request.response_schema is not None:
                common["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "response", "strict": True, "schema": request.response_schema,
                }}
            return common
        call_names = {call.id: call.name for message in request.messages for call in message.tool_calls or []}
        if provider == "anthropic":
            common["system"] = "\n".join(m.content for m in request.messages if m.role == "system")
            converted = []
            for message in request.messages:
                if message.role == "system":
                    continue
                blocks: list[dict[str, Any]] = []
                if message.role == "tool":
                    blocks.append({"type": "tool_result", "tool_use_id": message.tool_call_id, "content": message.content})
                else:
                    if message.content:
                        blocks.append({"type": "text", "text": message.content})
                    blocks.extend({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                                  for c in message.tool_calls or [])
                converted.append({"role": "assistant" if message.role == "assistant" else "user", "content": blocks})
            common["messages"] = converted
            if request.tools:
                common["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                                   for t in request.tools]
            if request.response_schema is not None:
                common["system"] += "\nReturn only JSON matching this schema: " + json.dumps(request.response_schema)
            return common
        contents = []
        for message in request.messages:
            if message.role == "system":
                continue
            parts: list[dict[str, Any]] = []
            if message.role == "tool":
                try:
                    result = json.loads(message.content)
                except ValueError:
                    result = {"result": message.content}
                parts.append({"functionResponse": {
                    "name": call_names.get(message.tool_call_id or "", message.tool_call_id or "tool"),
                    "response": result if isinstance(result, dict) else {"result": result},
                }})
            else:
                if message.content:
                    parts.append({"text": message.content})
                parts.extend({"functionCall": {"name": c.name, "args": c.arguments}} for c in message.tool_calls or [])
            contents.append({"role": "model" if message.role == "assistant" else "user", "parts": parts})
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"temperature": request.temperature, "maxOutputTokens": request.max_tokens},
        }
        system = "\n".join(m.content for m in request.messages if m.role == "system")
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if request.tools:
            payload["tools"] = [{"functionDeclarations": [
                {"name": t.name, "description": t.description, "parameters": t.parameters} for t in request.tools
            ]}]
        if request.response_schema is not None:
            payload["generationConfig"].update({
                "responseMimeType": "application/json", "responseSchema": request.response_schema,
            })
        return payload

    async def _chat(self, request: ChatRequest) -> ChatResponse:
        provider, name = self._resolve(request.model)
        if provider == "mock":
            return self._mock(request)
        url, headers = self._endpoint(provider, name)
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
            response = await client.post(url, headers=headers, json=self._payload(request, provider, name))
            response.raise_for_status()
            data = response.json()
        calls: list[ToolCall] = []
        if provider == "anthropic":
            content = "".join(block.get("text", "") for block in data["content"] if block["type"] == "text")
            calls = [ToolCall(id=b["id"], name=b["name"], arguments=_arguments(b["input"]))
                     for b in data["content"] if b["type"] == "tool_use"]
            usage = data.get("usage", {})
            input_tokens, output_tokens = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        elif provider == "gemini":
            parts = data["candidates"][0]["content"].get("parts", [])
            content = "".join(p.get("text", "") for p in parts)
            calls = [ToolCall(id=f"gemini-{i}", name=p["functionCall"]["name"],
                              arguments=_arguments(p["functionCall"].get("args", {})))
                     for i, p in enumerate(parts) if "functionCall" in p]
            usage = data.get("usageMetadata", {})
            input_tokens, output_tokens = usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)
        else:
            message = data["choices"][0]["message"]
            content = message.get("content") or ""
            calls = [ToolCall(id=c["id"], name=c["function"]["name"],
                              arguments=_arguments(c["function"]["arguments"]))
                     for c in message.get("tool_calls", [])]
            usage = data.get("usage", {})
            input_tokens, output_tokens = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        info = self.model_info(request.model)
        cost = (input_tokens * info["input_price"] + output_tokens * info["output_price"]) / 1_000_000
        return ChatResponse(content=content, model=request.model, tool_calls=calls,
                            input_tokens=input_tokens, output_tokens=output_tokens, cost=cost)

    @staticmethod
    def _mock(request: ChatRequest) -> ChatResponse:
        last_user = next((i for i in range(len(request.messages) - 1, -1, -1)
                          if request.messages[i].role == "user"), -1)
        query = request.messages[last_user].content if last_user >= 0 else ""
        turn = request.messages[last_user + 1:]
        observations = [m.content for m in turn if m.role == "tool"]
        used = {c.name for m in turn for c in m.tool_calls or []}
        tools = {tool.name: tool for tool in request.tools}
        calls = []
        content = ""
        if "rag_search" in tools and "rag_search" not in used:
            calls = [ToolCall(id="mock-rag-search", name="rag_search", arguments={"query": query})]
        elif "calculator" in tools and "calculator" not in used and (
            match := re.search(r"(?<![\w.])-?\d+(?:\.\d+)?(?:\s*[+\-*/%]\s*-?\d+(?:\.\d+)?)+", query)
        ):
            calls = [ToolCall(id="mock-calculator", name="calculator", arguments={"expression": match.group().strip()})]
        elif request.response_schema is not None:
            content = json.dumps(_sample(request.response_schema), sort_keys=True)
        elif observations:
            content = "Based on the tool results:\n" + "\n".join(observations)
        else:
            content = f"Mock response: {query}" if query else "Mock response: ready."
        return ChatResponse(
            content=content, model="mock", tool_calls=calls,
            input_tokens=sum((len(m.content) + 3) // 4 for m in request.messages),
            output_tokens=(len(content) + len(json.dumps([c.model_dump() for c in calls])) + 3) // 4,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        """Yield content deltas. Retry/fallback only before emitting any content."""
        self._check_request_schemas(request)
        for model in self._candidates(request.model):
            for attempt in range(self.retries + 1):
                emitted = False
                try:
                    provider, name = self._resolve(model)
                    if provider == "mock":
                        content = (await self.chat(request.model_copy(update={"model": model}))).content
                        for offset in range(0, len(content), 24):
                            yield content[offset:offset + 24]
                        return
                    url, headers = self._endpoint(provider, name, stream=True)
                    payload = self._payload(request, provider, name)
                    if provider != "gemini":
                        payload["stream"] = True
                    async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
                        async with client.stream("POST", url, headers=headers, json=payload) as response:
                            response.raise_for_status()
                            async for event in self._events(response):
                                if "error" in event or event.get("type") == "error":
                                    raise GatewayError("Provider reported a stream error")
                                if provider == "anthropic":
                                    text = event.get("delta", {}).get("text", "")
                                elif provider == "gemini":
                                    text = "".join(p.get("text", "") for candidate in event.get("candidates", [])
                                                   for p in candidate.get("content", {}).get("parts", []))
                                else:
                                    text = "".join(c.get("delta", {}).get("content") or ""
                                                   for c in event.get("choices", []))
                                if text:
                                    emitted = True
                                    yield text
                    return
                except (httpx.HTTPError, ValueError, KeyError, TypeError, GatewayError) as exc:
                    if emitted:
                        raise GatewayError("Provider stream interrupted after partial output") from None
                    if not self._retryable(exc):
                        break
                    if attempt < self.retries:
                        await asyncio.sleep(self.backoff * 2**attempt)
        raise GatewayError("All configured model streams failed") from None

    @staticmethod
    async def _events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
        lines: list[str] = []
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                lines.append(line[5:].lstrip())
            elif not line and lines:
                raw = "\n".join(lines)
                lines = []
                if raw == "[DONE]":
                    return
                yield json.loads(raw)
        if lines and "\n".join(lines) != "[DONE]":
            yield json.loads("\n".join(lines))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = os.getenv("EMBEDDING_MODEL")
        if not model:
            vectors = []
            for text in texts:
                digest = hashlib.shake_256(text.encode()).digest(128)
                vector = [int.from_bytes(digest[i:i + 2], "big") / 32767.5 - 1 for i in range(0, 128, 2)]
                norm = math.sqrt(sum(value * value for value in vector))
                vectors.append([value / norm for value in vector])
            return vectors
        provider, name = self._resolve(
            f"compatible:{model}" if ":" not in model and os.getenv("OPENAI_COMPATIBLE_BASE_URL") else model
        )
        if provider not in ("openai", "compatible"):
            raise GatewayError("Embeddings require an OpenAI-compatible provider")
        url, headers = self._endpoint(provider, name)
        url = url.removesuffix("/chat/completions") + "/embeddings"
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
                response = await client.post(url, headers=headers, json={
                    "model": name, "input": texts, "dimensions": 64,
                })
                response.raise_for_status()
                data = sorted(response.json()["data"], key=lambda row: row["index"])
                vectors = [row["embedding"] for row in data]
                if len(vectors) != len(texts) or [row["index"] for row in data] != list(range(len(texts))):
                    raise GatewayError("Embedding response count or indexes are invalid")
                if any(len(v) != 64 or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in v)
                       for v in vectors):
                    raise GatewayError("Embedding vectors must contain 64 finite numbers")
                return vectors
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise GatewayError("Embedding provider request failed") from exc
