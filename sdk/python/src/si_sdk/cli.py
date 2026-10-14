"""Small CLI suitable for interactive use and threshold-based CI evaluation."""

import argparse
import json
import sys
from pathlib import Path

import httpx

from .client import Client, SIError


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="si")
    root.add_argument("--api-url", default=None)
    root.add_argument("--api-key", default=None, help="Prefer SI_API_KEY to avoid shell history")
    commands = root.add_subparsers(dest="command", required=True)
    chat = commands.add_parser("chat")
    chat.add_argument("input")
    chat.add_argument("--model", default="mock")
    chat.add_argument("--stream", action="store_true")
    ingest = commands.add_parser("ingest")
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--collection", required=True)
    agent = commands.add_parser("agent")
    runs = agent.add_subparsers(dest="action", required=True)
    run = runs.add_parser("run")
    run.add_argument("agent_id")
    run.add_argument("input")
    run.add_argument("--session")
    run.add_argument("--no-wait", action="store_true")
    evaluation = commands.add_parser("eval")
    evaluations = evaluation.add_subparsers(dest="action", required=True)
    evaluate = evaluations.add_parser("run")
    evaluate.add_argument("dataset", type=Path)
    target = evaluate.add_mutually_exclusive_group(required=True)
    target.add_argument("--agent")
    target.add_argument("--prompt")
    evaluate.add_argument("--version", type=int, default=1)
    evaluate.add_argument("--model", default="mock", help="Prompt evaluation and LLM-judge model")
    evaluate.add_argument("--scorer", default="exact_match",
                          choices=["exact_match", "contains", "regex", "json_schema", "llm_judge"])
    evaluate.add_argument("--threshold", type=float, default=1)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        with Client(args.api_url, args.api_key) as client:
            if args.command == "chat":
                if args.stream:
                    for chunk in client.stream(args.input, args.model):
                        print(chunk, end="", flush=True)
                    print()
                else:
                    print(client.chat(args.input, args.model)["choices"][0]["message"]["content"])
            elif args.command == "ingest":
                print(json.dumps(client.ingest(args.collection, args.file), indent=2))
            elif args.command == "agent":
                result = client.run_agent(args.agent_id, args.input, session_id=args.session,
                                          wait=not args.no_wait)
                print(json.dumps(result, indent=2))
                if result["status"] == "waiting_approval":
                    print("Approval required; approve this run through the API/console.",
                          file=sys.stderr)
                    return 2
            elif args.command == "eval":
                if not 0 <= args.threshold <= 1:
                    raise ValueError("Threshold must be between 0 and 1")
                dataset = json.loads(args.dataset.read_text())
                target = {"agent_id": args.agent} if args.agent else {
                    "prompt_name": args.prompt, "prompt_version": args.version,
                }
                result = client.evaluate(dataset, args.scorer, model=args.model, **target)
                print(json.dumps(result, indent=2))
                return 0 if result["score"] >= args.threshold else 1
    except (SIError, ValueError, OSError, TimeoutError, httpx.HTTPError) as error:
        print(f"si: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
