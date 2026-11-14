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
    assert args.model == "mock"
    assert parser().parse_args([
        "eval", "run", "dataset.json", "--prompt", "p1", "--model", "openai:gpt-4o-mini",
    ]).model == "openai:gpt-4o-mini"


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
    sent = []
    monkeypatch.setattr(Client, "evaluate",
                        lambda *_args, **kwargs: sent.append(kwargs["threshold"]) or {"score": 0.5})
    monkeypatch.setattr("sys.argv", ["si", "eval", "run", str(dataset),
                                    "--agent", "a1", "--threshold", "0.8"])
    assert main() == 1
    monkeypatch.setattr("sys.argv", ["si", "eval", "run", str(dataset),
                                    "--agent", "a1", "--threshold", "0.4"])
    assert main() == 0
    # The server scores each case against the same threshold the exit code uses.
    assert sent == [0.8, 0.4]


def run_cli(monkeypatch, capsys, handler, *argv):
    from si_sdk import cli
    monkeypatch.setattr(cli, "Client", lambda *_args: client(handler))
    monkeypatch.setattr("sys.argv", ["si", *argv])
    code = cli.main()
    return code, capsys.readouterr()


def test_identifiers_stay_one_path_segment_and_204_is_none():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.raw_path.decode()))
        return httpx.Response(204) if request.method == "DELETE" else httpx.Response(200, json=[])
    with client(handler) as sdk:
        assert sdk.delete_agent("a/b?x") is None
        sdk.prompt_versions("..")
        sdk.render_prompt("greet", 2, {"name": "Ada"})
    assert seen == [
        ("DELETE", "/v1/agents/a%2Fb%3Fx"),
        ("GET", "/v1/prompts/%2E%2E/versions"),
        ("POST", "/v1/prompts/greet/2/render"),
    ]


def test_run_listing_filters_and_waiting_through_cancellation(monkeypatch):
    monkeypatch.setattr("si_sdk.client.time.sleep", lambda _: None)
    statuses = iter(["cancelling", "cancelled"])

    def handler(request):
        if request.url.path == "/v1/runs":
            assert dict(request.url.params) == {"agent_id": "a1", "status": "failed", "limit": "5"}
            return httpx.Response(200, json=[])
        if request.url.path == "/v1/runs/r1/cancel":
            return httpx.Response(200, json={"id": "r1", "status": "cancelling"})
        return httpx.Response(200, json={"id": "r1", "status": next(statuses)})
    with client(handler) as sdk:
        assert sdk.runs(agent_id="a1", status="failed", limit=5) == []
        assert sdk.cancel_run("r1")["status"] == "cancelling"
        assert sdk.wait_for_run("r1")["status"] == "cancelled"


@pytest.mark.parametrize("final, error", [("completed", None), ("failed", "boom")])
def test_resume_waits_and_reports_outcome(monkeypatch, final, error):
    monkeypatch.setattr("si_sdk.client.time.sleep", lambda _: None)
    polls = iter(["running", final])

    def handler(request):
        if request.method == "POST":
            assert request.url.path == "/v1/runs/r1/resume"
            assert json.loads(request.content) == {"background": True}
            return httpx.Response(200, json={"id": "r1", "status": "queued"})
        return httpx.Response(200, json={"id": "r1", "status": next(polls), "error": error})
    with client(handler) as sdk:
        if error:
            with pytest.raises(SIError, match="failed: boom"):
                sdk.resume_run("r1")
        else:
            assert sdk.resume_run("r1")["status"] == "completed"


def test_run_events_stream_until_done():
    stream = (
        'id: 3\ndata: {"type":"started","data":{"steps":0}}\n\n'
        ": heartbeat\n\n"
        'id: 4\ndata: {"type":"cancelled","data":{"previous_status":"running"}}\n\n'
        "data: [DONE]\n\n"
        'data: {"type":"never"}\n\n'
    )

    def handler(request):
        assert request.url.path == "/v1/runs/r1/events"
        assert request.headers["last-event-id"] == "2"
        return httpx.Response(200, text=stream)
    with client(handler) as sdk:
        events = list(sdk.run_events("r1", last_event_id=2))
    assert [event["type"] for event in events] == ["started", "cancelled"]


def test_cli_eval_prompt_version_defaults_to_latest(monkeypatch, tmp_path):
    from si_sdk.cli import main
    dataset = tmp_path / "eval.json"
    dataset.write_text('[{"input":"hi","expected":"hi"}]')
    calls = []

    def evaluate(_self, _dataset, _scorer, **target):
        calls.append(target)
        return {"score": 1}
    monkeypatch.setenv("SI_API_KEY", "test-key")
    monkeypatch.setattr(Client, "evaluate", evaluate)
    for extra in ([], ["--version", "3"]):
        monkeypatch.setattr("sys.argv", ["si", "eval", "run", str(dataset), "--prompt", "p", *extra])
        assert main() == 0
    assert calls == [{"model": "mock", "threshold": 1, "prompt_name": "p"},
                     {"model": "mock", "threshold": 1, "prompt_name": "p", "prompt_version": 3}]
    monkeypatch.setattr("sys.argv", ["si", "eval", "run", str(dataset), "--agent", "a",
                                    "--version", "2"])
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 2


def test_cli_prompt_render_variables(monkeypatch, capsys):
    def handler(request):
        assert json.loads(request.content) == {"variables": {"name": "Ada", "eq": "a=b"}}
        return httpx.Response(200, json={"text": "Hello Ada"})
    code, output = run_cli(monkeypatch, capsys, handler, "prompt", "render", "greet", "1",
                           "--var", "name=Ada", "--var", "eq=a=b")
    assert code == 0 and json.loads(output.out) == {"text": "Hello Ada"}
    code, output = run_cli(monkeypatch, capsys, handler, "prompt", "render", "greet", "1",
                           "--var", "missing")
    assert code == 2 and "KEY=VALUE" in output.err


def test_cli_traces_filters(monkeypatch, capsys):
    def handler(request):
        assert request.url.path == "/v1/traces"
        assert dict(request.url.params) == {"run_id": "r1", "kind": "tool"}
        return httpx.Response(200, json=[{"kind": "tool"}])
    code, output = run_cli(monkeypatch, capsys, handler, "traces", "--run", "r1", "--kind", "tool")
    assert code == 0 and json.loads(output.out) == [{"kind": "tool"}]


@pytest.mark.parametrize("argv, run, code", [
    (["run", "approve", "r1"], {"id": "r1", "status": "completed"}, 0),
    (["run", "reject", "r1"], {"id": "r1", "status": "waiting_approval"}, 2),
    (["run", "approve", "r1"], {"id": "r1", "status": "failed", "error": "boom"}, 2),
    (["run", "cancel", "r1"], {"id": "r1", "status": "cancelling"}, 0),
    (["run", "resume", "r1", "--no-wait"], {"id": "r1", "status": "queued"}, 0),
])
def test_cli_run_actions_exit_codes(monkeypatch, capsys, argv, run, code):
    requests = []

    def handler(request):
        requests.append((request.url.path, request.content))
        return httpx.Response(200, json=run)
    result, output = run_cli(monkeypatch, capsys, handler, *argv)
    assert result == code and json.loads(output.out) == run
    path, body = requests[0]
    if argv[1] == "reject":
        assert (path, json.loads(body)) == ("/v1/runs/r1/approve", {"approved": False})
    else:
        assert path == f"/v1/runs/r1/{argv[1]}"
    if run["status"] == "waiting_approval":
        assert "si run approve r1" in output.err
