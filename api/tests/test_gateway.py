import json
from typing import Any

import httpx
import pytest

from si.models import ChatRequest, Gateway, GatewayError, Message, ToolCall, ToolSpec


def request(**kwargs):
    return ChatRequest(messages=[Message(role="user", content="Find notes and compute 2 + 3")], **kwargs)


@pytest.fixture(autouse=True)
def clear_credentials(monkeypatch):
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                "OPENAI_COMPATIBLE_BASE_URL", "OPENAI_COMPATIBLE_API_KEY", "EMBEDDING_MODEL"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.asyncio
async def test_mock_tool_plan_and_final():
    gateway = Gateway()
    tools = [ToolSpec(name=name, description=name, parameters={"type": "object"})
             for name in ("calculator", "rag_search")]
    req = request(tools=tools)
    first = await gateway.chat(req)
    assert first.tool_calls[0].name == "rag_search"
    assert first.tool_calls[0].arguments == {"query": req.messages[0].content}
    req.messages.extend([
        Message(role="assistant", content="", tool_calls=first.tool_calls),
        Message(role="tool", content="Relevant notes", tool_call_id=first.tool_calls[0].id),
    ])
    second = await gateway.chat(req)
    assert second.tool_calls[0].arguments == {"expression": "2 + 3"}
    req.messages.extend([
        Message(role="assistant", content="", tool_calls=second.tool_calls),
        Message(role="tool", content="5", tool_call_id=second.tool_calls[0].id),
    ])
    final = await gateway.chat(req)
    assert not final.tool_calls
    assert "Relevant notes" in final.content and "5" in final.content
    assert final.cost == 0 and final.input_tokens > 0


@pytest.mark.asyncio
async def test_mock_structured_stream_and_embeddings():
    gateway = Gateway()
    schema = {"type": "object", "required": ["answer"], "properties": {
        "answer": {"type": "integer", "minimum": 3},
    }}
    response = await gateway.chat(request(response_schema=schema))
    assert json.loads(response.content) == {"answer": 3}
    assert "".join([chunk async for chunk in gateway.stream(request())]) == (await gateway.chat(request())).content
    vectors = await gateway.embed(["hello", "hello", "world"])
    assert vectors[0] == vectors[1] != vectors[2]
    assert len(vectors[0]) == 64
    assert sum(value * value for value in vectors[0]) == pytest.approx(1)
    assert await gateway.embed([]) == []


def test_routing(monkeypatch):
    gateway = Gateway()
    assert gateway.route() == "mock"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert gateway.route("cheapest").startswith("gemini:")
    assert gateway.route("fastest").startswith("openai:")
    assert gateway.route("best").startswith("anthropic:")
    assert gateway.model_info("openai:custom")["available"]
    assert len(gateway.list_models()) == 5


@pytest.mark.asyncio
async def test_openai_payload_usage_and_function_result(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    def handle(req):
        data = json.loads(req.content)
        assert req.headers["Authorization"] == "Bearer " + "test-key"
        assert data["messages"][-1] == {"role": "tool", "content": "5", "tool_call_id": "calc-1"}
        assert data["messages"][-2]["tool_calls"][0]["function"]["arguments"] == '{"expression": "2+3"}'
        assert data["tools"][0]["function"]["parameters"]["type"] == "object"
        assert data["response_format"]["json_schema"]["schema"] == {"type": "object"}
        return httpx.Response(200, json={"choices": [{"message": {
            "content": None, "tool_calls": [{
                "id": "calc-2", "function": {"name": "calculator", "arguments": '{"expression":"3+4"}'},
            }],
        }}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    req = request(
        model="openai:gpt-4o-mini", response_schema={"type": "object"},
        tools=[ToolSpec(name="calculator", description="math", parameters={"type": "object"})],
    )
    req.messages += [
        Message(role="assistant", content="", tool_calls=[ToolCall(id="calc-1", name="calculator", arguments={"expression": "2+3"})]),
        Message(role="tool", content="5", tool_call_id="calc-1"),
    ]
    response = await Gateway(transport=httpx.MockTransport(handle)).chat(req)
    assert response.tool_calls[0].arguments == {"expression": "3+4"}
    assert response.content == ""
    assert response.input_tokens == 10 and response.output_tokens == 5
    assert response.cost == pytest.approx(4.5 / 1_000_000)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
async def test_provider_function_normalization(monkeypatch, provider):
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "test-key")
    def handle(req):
        data = json.loads(req.content)
        if provider == "anthropic":
            assert data["messages"][-1]["content"][0]["type"] == "tool_result"
            assert data["tools"][0]["input_schema"] == {"type": "object"}
            return httpx.Response(200, json={
                "content": [{"type": "text", "text": "done"}, {
                    "type": "tool_use", "id": "a1", "name": "calculator", "input": {"expression": "1+1"},
                }], "usage": {"input_tokens": 2, "output_tokens": 3},
            })
        assert data["contents"][-1]["parts"][0]["functionResponse"]["name"] == "calculator"
        assert data["generationConfig"]["responseMimeType"] == "application/json"
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "done"}, {
                "functionCall": {"name": "calculator", "args": {"expression": "1+1"}},
            }]}}], "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3},
        })
    req = request(model=f"{provider}:custom", response_schema={"type": "object"},
                  tools=[ToolSpec(name="calculator", description="math", parameters={"type": "object"})])
    req.messages += [
        Message(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="calculator", arguments={})]),
        Message(role="tool", content="2", tool_call_id="c1"),
    ]
    response = await Gateway(transport=httpx.MockTransport(handle)).chat(req)
    assert response.content == "done"
    assert response.tool_calls[0].name == "calculator"
    assert response.input_tokens == 2 and response.output_tokens == 3


@pytest.mark.asyncio
async def test_retries_fallback_and_safe_errors(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    attempts = []
    def handle(req):
        attempts.append(req)
        return httpx.Response(503, text="private provider response")
    gateway = Gateway(transport=httpx.MockTransport(handle), retries=1, backoff=0, fallback_models=["mock"])
    assert (await gateway.chat(request(model="openai:test"))).model == "mock"
    assert len(attempts) == 2
    with pytest.raises(GatewayError, match="All configured"):
        await Gateway(transport=httpx.MockTransport(handle), retries=0).chat(request(model="openai:test"))


@pytest.mark.asyncio
async def test_no_retry_on_auth_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    attempts = []
    def handle(req):
        attempts.append(req)
        return httpx.Response(401)
    with pytest.raises(GatewayError):
        await Gateway(transport=httpx.MockTransport(handle), retries=3).chat(request(model="openai:test"))
    assert len(attempts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
async def test_real_stream_payload_and_deltas(monkeypatch, provider):
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "test-key")
    def handle(req):
        data = json.loads(req.content)
        events: list[dict[str, Any]]
        if provider == "anthropic":
            assert data["stream"] is True
            events = [{"type": "content_block_delta", "delta": {"text": text}} for text in ("one", "two")]
        elif provider == "gemini":
            assert "alt=sse" in str(req.url)
            events = [{"candidates": [{"content": {"parts": [{"text": text}]}}]} for text in ("one", "two")]
        else:
            assert data["stream"] is True
            events = [{"choices": [{"delta": {"content": text}}]} for text in ("one", "two")]
        return httpx.Response(200, text="".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n")
    chunks = [chunk async for chunk in Gateway(transport=httpx.MockTransport(handle)).stream(request(model=f"{provider}:test"))]
    assert chunks == ["one", "two"]


@pytest.mark.asyncio
async def test_compatible_embedding_dimensions_and_order(monkeypatch):
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://local.example/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "compatible:embed")
    def handle(req):
        assert str(req.url) == "https://local.example/v1/embeddings"
        assert json.loads(req.content)["dimensions"] == 64
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [1.0] * 64}, {"index": 0, "embedding": [0.0] * 64},
        ]})
    assert await Gateway(transport=httpx.MockTransport(handle)).embed(["a", "b"]) == [[0.0] * 64, [1.0] * 64]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={
        "data": [{"index": 0, "embedding": [1.0] * 128}],
    }))
    with pytest.raises(GatewayError, match="64 finite"):
        await Gateway(transport=transport).embed(["a"])


@pytest.mark.asyncio
async def test_remote_json_schema_is_rejected_without_network():
    with pytest.raises(GatewayError, match="local schema"):
        await Gateway().chat(request(response_schema={"$ref": "https://example.com/schema"}))


@pytest.mark.asyncio
async def test_automatic_routing_falls_back_to_other_configured_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    def handle(req):
        if "googleapis" in req.url.host:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": "fallback"}}]})
    result = await Gateway(transport=httpx.MockTransport(handle), retries=0).chat(request(model="cheapest"))
    assert result.model.startswith("openai:")
    assert result.content == "fallback"


@pytest.mark.asyncio
async def test_stream_does_not_silently_hide_provider_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    body = 'data: {"error": {"message": "private"}}\n\n'
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=body))
    with pytest.raises(GatewayError, match="streams failed"):
        _ = [chunk async for chunk in Gateway(transport=transport).stream(request(model="openai:test"))]
