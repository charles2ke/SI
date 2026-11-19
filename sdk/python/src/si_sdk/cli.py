"""Small CLI suitable for interactive use and threshold-based CI evaluation."""

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from .client import Client, SIError

SCORERS = ["exact_match", "contains", "regex", "json_schema", "llm_judge"]
RUN_STATUSES = [
    "queued", "running", "waiting_approval", "cancelling",
    "completed", "failed", "interrupted", "cancelled",
]


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="si")
    root.add_argument("--api-url", default=None)
    root.add_argument("--api-key", default=None, help="Prefer SI_API_KEY to avoid shell history")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("models", help="List available models")
    commands.add_parser("tools", help="List built-in tools and configured MCP servers")
    traces = commands.add_parser("traces", help="List recent trace spans")
    traces.add_argument("--run", dest="run_id")
    traces.add_argument("--kind", help="Span kind, e.g. model, tool, gateway, embedding, request")
    chat = commands.add_parser("chat")
    chat.add_argument("input")
    chat.add_argument("--model", default="mock")
    chat.add_argument("--stream", action="store_true")
    ingest = commands.add_parser("ingest", help="Upload a document to a collection")
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--collection", required=True)

    collections = commands.add_parser("collection").add_subparsers(dest="action", required=True)
    collections.add_parser("list")
    collections.add_parser("get").add_argument("collection_id")
    collections.add_parser("create").add_argument("name")
    collections.add_parser("delete", help="Delete a collection no agent uses").add_argument(
        "collection_id")
    ingest_url = collections.add_parser(
        "ingest-url", help="Fetch a URL on the server's HTTP_ALLOWED_HOSTS into a collection")
    ingest_url.add_argument("collection_id")
    ingest_url.add_argument("url")
    search = collections.add_parser("search")
    search.add_argument("collection_id")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)

    agents = commands.add_parser("agent").add_subparsers(dest="action", required=True)
    agents.add_parser("list")
    agents.add_parser("get").add_argument("agent_id")
    agents.add_parser("delete", help="Delete an agent and its run history").add_argument(
        "agent_id")
    create = agents.add_parser("create")
    create.add_argument("name")
    create.add_argument("--model", default="mock")
    create.add_argument("--system-prompt")
    create.add_argument("--tool", action="append", default=[], dest="tools")
    create.add_argument("--collection", action="append", default=[], dest="collection_ids")
    create.add_argument("--max-steps", type=int)
    run = agents.add_parser("run")
    run.add_argument("agent_id")
    run.add_argument("input")
    run.add_argument("--session")
    run.add_argument("--no-wait", action="store_true")
    run.add_argument("--timeout", type=float, default=120)

    runs = commands.add_parser("run").add_subparsers(dest="action", required=True)
    listing = runs.add_parser("list", help="Recent runs, newest first")
    listing.add_argument("--agent")
    listing.add_argument("--status", choices=RUN_STATUSES)
    listing.add_argument("--session")
    listing.add_argument("--limit", type=int, default=50)
    for action in ("get", "approve", "reject", "cancel"):
        runs.add_parser(action).add_argument("run_id")
    events = runs.add_parser("events", help="Stream run events as JSON lines")
    events.add_argument("run_id")
    events.add_argument("--after", type=int, help="Only events after this event index")
    resume = runs.add_parser("resume", help="Requeue an interrupted run")
    resume.add_argument("run_id")
    resume.add_argument("--no-wait", action="store_true")
    resume.add_argument("--timeout", type=float, default=120)

    prompts = commands.add_parser("prompt").add_subparsers(dest="action", required=True)
    prompts.add_parser("list")
    prompts.add_parser("versions").add_argument("name")
    create_prompt = prompts.add_parser("create", help="Create the next version of a prompt")
    create_prompt.add_argument("name")
    source = create_prompt.add_mutually_exclusive_group(required=True)
    source.add_argument("--template")
    source.add_argument("--file", type=Path)
    render = prompts.add_parser("render")
    render.add_argument("name")
    render.add_argument("version", type=int)
    render.add_argument("--var", action="append", default=[], metavar="KEY=VALUE")

    evaluations = commands.add_parser("eval").add_subparsers(dest="action", required=True)
    evaluations.add_parser("list")
    evaluations.add_parser("get").add_argument("eval_id")
    evaluate = evaluations.add_parser("run")
    evaluate.add_argument("dataset", type=Path)
    target = evaluate.add_mutually_exclusive_group(required=True)
    target.add_argument("--agent")
    target.add_argument("--prompt")
    evaluate.add_argument("--version", type=int, help="Prompt version; defaults to the latest")
    evaluate.add_argument("--model", default="mock", help="Prompt evaluation and LLM-judge model")
    evaluate.add_argument("--scorer", default="exact_match", choices=SCORERS)
    evaluate.add_argument("--threshold", type=float, default=1)
    return root


def show(value: Any) -> None:
    print(json.dumps(value, indent=2))


def variables(pairs: list[str]) -> dict[str, str]:
    values = {}
    for pair in pairs:
        name, separator, value = pair.partition("=")
        if not separator or not name:
            raise ValueError(f"--var expects KEY=VALUE, got {pair!r}")
        values[name] = value
    return values


def agent_definition(args: argparse.Namespace) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "name": args.name, "model": args.model,
        "tools": args.tools, "collection_ids": args.collection_ids,
    }
    if args.system_prompt is not None:
        definition["system_prompt"] = args.system_prompt
    if args.max_steps is not None:
        definition["max_steps"] = args.max_steps
    return definition


def report_run(run: dict[str, Any]) -> int:
    """Print the run; exit 2 when it needs a human or did not finish successfully."""
    show(run)
    if run["status"] == "waiting_approval":
        print(f"Approval required: si run approve {run['id']} (or si run reject {run['id']})",
              file=sys.stderr)
        return 2
    if run["status"] in {"failed", "interrupted", "cancelled"}:
        print(f"si: run {run['id']} {run['status']}: {run.get('error')}", file=sys.stderr)
        return 2
    return 0


def evaluate(client: Client, args: argparse.Namespace) -> int:
    if not 0 <= args.threshold <= 1:
        raise ValueError("Threshold must be between 0 and 1")
    dataset = json.loads(args.dataset.read_text())
    target: dict[str, Any] = {"agent_id": args.agent} if args.agent else {
        "prompt_name": args.prompt,
    }
    if args.version is not None:
        target["prompt_version"] = args.version
    result = client.evaluate(dataset, args.scorer, model=args.model, threshold=args.threshold,
                             **target)
    show(result)
    return 0 if result["score"] >= args.threshold else 1


def execute(client: Client, args: argparse.Namespace) -> int:
    command = (args.command, getattr(args, "action", None))
    printed: dict[tuple[str, str | None], Callable[[], Any]] = {
        ("models", None): client.models,
        ("tools", None): client.tools,
        ("traces", None): lambda: client.traces(args.run_id, args.kind),
        ("ingest", None): lambda: client.ingest(args.collection, args.file),
        ("collection", "list"): client.collections,
        ("collection", "get"): lambda: client.get_collection(args.collection_id),
        ("collection", "create"): lambda: client.create_collection(args.name),
        ("collection", "ingest-url"): lambda: client.ingest_url(args.collection_id, args.url),
        ("collection", "search"): lambda: client.search(args.collection_id, args.query,
                                                         args.limit),
        ("agent", "list"): client.agents,
        ("agent", "get"): lambda: client.get_agent(args.agent_id),
        ("agent", "create"): lambda: client.create_agent(**agent_definition(args)),
        ("run", "list"): lambda: client.runs(agent_id=args.agent, status=args.status,
                                             session_id=args.session, limit=args.limit),
        ("run", "get"): lambda: client.get_run(args.run_id),
        ("run", "cancel"): lambda: client.cancel_run(args.run_id),
        ("prompt", "list"): client.prompts,
        ("prompt", "versions"): lambda: client.prompt_versions(args.name),
        ("prompt", "create"): lambda: client.create_prompt(
            args.name, args.template if args.template is not None else args.file.read_text()),
        ("prompt", "render"): lambda: client.render_prompt(args.name, args.version,
                                                           variables(args.var)),
        ("eval", "list"): client.evals,
        ("eval", "get"): lambda: client.get_eval(args.eval_id),
    }
    if command in printed:
        show(printed[command]())
    elif command == ("chat", None):
        if args.stream:
            for chunk in client.stream(args.input, args.model):
                print(chunk, end="", flush=True)
            print()
        else:
            print(client.chat(args.input, args.model)["choices"][0]["message"]["content"])
    elif command == ("collection", "delete"):
        client.delete_collection(args.collection_id)
        print(f"Deleted collection {args.collection_id}", file=sys.stderr)
    elif command == ("agent", "delete"):
        client.delete_agent(args.agent_id)
        print(f"Deleted agent {args.agent_id}", file=sys.stderr)
    elif command == ("agent", "run"):
        return report_run(client.run_agent(args.agent_id, args.input, session_id=args.session,
                                           wait=not args.no_wait, timeout=args.timeout))
    elif command in {("run", "approve"), ("run", "reject")}:
        return report_run(client.approve(args.run_id, approved=args.action == "approve"))
    elif command == ("run", "resume"):
        return report_run(client.resume_run(args.run_id, wait=not args.no_wait,
                                            timeout=args.timeout))
    elif command == ("run", "events"):
        for event in client.run_events(args.run_id, last_event_id=args.after):
            print(json.dumps(event), flush=True)
    elif command == ("eval", "run"):
        return evaluate(client, args)
    return 0


def main() -> int:
    root = parser()
    args = root.parse_args()
    if args.command == "eval" and args.action == "run" and args.agent and args.version is not None:
        root.error("--version applies only to --prompt evaluations")
    try:
        with Client(args.api_url, args.api_key) as client:
            return execute(client, args)
    except (SIError, ValueError, OSError, TimeoutError, httpx.HTTPError) as error:
        print(f"si: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
