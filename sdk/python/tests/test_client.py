import json

import httpx
import pytest

from si_sdk import Client, SIError
from si_sdk.cli import parser


def client(handler):
    return Client(api_key="test-key", transport=httpx.MockTransport(handler))


def test_chat_auth_and_payload():
    def handler(request):
        assert request.headers["authorization"] == "Bearer " + "test-key"
        assert request.url.path == "/v1/chat/completions"
        assert json.loads(request.content)["messages"][0]["content"] == "hello"
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})
    with client(handler) as sdk:
        assert sdk.chat("hello")["choices"][0]["message"]["content"] == "hello"


def test_stream():
    stream = (
        'data: {"choices":[{"delta":{"content":"héllo"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    with client(lambda _: httpx.Response(200, text=stream)) as sdk:
        assert "".join(sdk.stream("hello")) == "héllo world"


def test_errors_are_not_success():
    with client(lambda _: httpx.Response(403, json={"detail": "Forbidden"})) as sdk:
        with pytest.raises(SIError, match="403"):
            sdk.chat("hello")
        with pytest.raises(SIError, match="403"):
            list(sdk.stream("hello"))


def test_stream_error():
    with client(lambda _: httpx.Response(200, text='data: {"error":"failed"}\n\n')) as sdk:
        with pytest.raises(SIError, match="failed"):
            list(sdk.stream("hello"))


def test_run_waits_and_reports_failure(monkeypatch):
    monkeypatch.setattr("si_sdk.client.time.sleep", lambda _: None)
    responses = iter([{"id": "r1", "status": "queued"},
                      {"id": "r1", "status": "failed", "error": "Tool unavailable"}])
    with client(lambda _: httpx.Response(200, json=next(responses))) as sdk:
        with pytest.raises(SIError, match="Tool unavailable"):
            sdk.run_agent("a1", "hello")


def test_interrupted_run_is_not_success():
    with client(lambda _: httpx.Response(
        200, json={"id": "r1", "status": "interrupted", "error": "Worker stopped"},
    )) as sdk:
        with pytest.raises(SIError, match="interrupted"):
            sdk.run_agent("a1", "hello")


def test_ingest_closes_file(tmp_path):
    document = tmp_path / "hello.txt"
    document.write_text("hello")
    def handler(request):
        assert request.url.path == "/v1/collections/c1/documents"
        assert b"hello" in request.read()
        return httpx.Response(200, json={"chunks": 1})
    with client(handler) as sdk:
        assert sdk.ingest("c1", document)["chunks"] == 1


def test_cli_target_is_required():
    with pytest.raises(SystemExit):
        parser().parse_args(["eval", "run", "dataset.json"])
    args = parser().parse_args(["eval", "run", "dataset.json", "--agent", "a1"])
    assert args.threshold == 1


def test_approval_payload():
    def handler(request):
        assert json.loads(request.content) == {"approved": True}
        return httpx.Response(200, json={"status": "queued"})
    with client(handler) as sdk:
        assert sdk.approve("r1")["status"] == "queued"


def test_cli_threshold_exit(monkeypatch, tmp_path):
    from si_sdk.cli import main
    dataset = tmp_path / "eval.json"
    dataset.write_text('[{"input":"hi","expected":"hello"}]')
    monkeypatch.setenv("SI_API_KEY", "test-key")
    monkeypatch.setattr(Client, "evaluate", lambda *_args, **_kwargs: {"score": 0.5})
    monkeypatch.setattr("sys.argv", ["si", "eval", "run", str(dataset),
                                    "--agent", "a1", "--threshold", "0.8"])
    assert main() == 1
    monkeypatch.setattr("sys.argv", ["si", "eval", "run", str(dataset),
                                    "--agent", "a1", "--threshold", "0.4"])
    assert main() == 0
