"""Explicit tool registry. Network and code execution require human approval."""

from __future__ import annotations

import ast
import asyncio
import json
import math
import operator
import os
import re
import resource
import signal
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from jsonschema import Draft202012Validator

from si.models import ToolSpec
from si.rag import RAGService
from si.rag.network import allowed_hosts, safe_fetch


def validate_tool_schema(schema: dict[str, Any]) -> None:
    if len(json.dumps(schema)) > 20_000:
        raise ValueError("Tool schema exceeds size limit")

    def inspect(node: Any, depth: int = 0) -> None:
        if depth > 30:
            raise ValueError("Tool schema nesting exceeds limits")
        if isinstance(node, dict):
            if any(key in node for key in ("$ref", "$dynamicRef", "$recursiveRef", "pattern", "patternProperties")):
                raise ValueError("Tool schema references and regex keywords are unsupported")
            for value in node.values():
                inspect(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                inspect(value, depth + 1)

    inspect(schema)
    Draft202012Validator.check_schema(schema)


def calculate(expression: str) -> float | int:
    if not isinstance(expression, str) or len(expression) > 500:
        raise ValueError("Expression must contain at most 500 characters")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 100:
        raise ValueError("Expression is too complex")
    operators = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }

    def visit(node: ast.AST) -> float | int:
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            value: int | float = cast(int | float, node.value)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp) and type(node.op) in operators:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 10 or abs(left) > 1e10):
                raise ValueError("Power exceeds calculator limits")
            value = operators[type(node.op)](left, right)
        else:
            raise ValueError("Only arithmetic on numbers is allowed")
        if isinstance(value, complex) or not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError("Result exceeds calculator limits")
        return value

    return visit(tree.body)


def _python_limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
    resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024, 128 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


async def python_execute(code: str) -> str:
    """Local opt-in only. Resource limits are NOT production sandbox isolation."""
    if os.getenv("SI_ENABLE_PYTHON_TOOL", "").lower() != "true":
        raise ValueError("Python tool is disabled; SI_ENABLE_PYTHON_TOOL=true is unsafe for production")
    if len(code) > 10_000:
        raise ValueError("Code exceeds 10000 characters")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        "-c",
        code,
        env={},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        preexec_fn=_python_limits,
        start_new_session=True,
    )
    output = bytearray()
    try:
        async with asyncio.timeout(3):
            assert process.stdout is not None
            while block := await process.stdout.read(4096):
                output.extend(block)
                if len(output) > 65_536:
                    raise ValueError("Python output exceeds 64 KiB")
            await process.wait()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
    result = output.decode("utf-8", errors="replace")
    if process.returncode:
        raise ValueError(f"Python exited with status {process.returncode}: {result[:2000]}")
    return result


@dataclass
class RegisteredTool:
    spec: ToolSpec
    handler: Callable[[dict[str, Any]], Awaitable[Any]]

    async def call(self, arguments: dict[str, Any]) -> Any:
        if len(json.dumps(arguments)) > 50_000:
            raise ValueError("Tool arguments exceed size limit")
        validate_tool_schema(self.spec.parameters)
        Draft202012Validator(self.spec.parameters).validate(arguments)
        return await self.handler(arguments)


class MCPClient:
    """Bounded MCP Streamable HTTP JSON-RPC, supporting JSON and SSE replies."""

    def __init__(self, url: str, hosts: set[str] | None = None):
        self.url = url
        self.hosts = allowed_hosts() if hosts is None else hosts
        self._next_id = 0
        self._initialized = False
        self._session_id: str | None = None
        self._protocol_version = "2025-03-26"

    async def _rpc(self, method: str, params: dict[str, Any], *, notification: bool = False) -> Any:
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = self._next_id
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self._protocol_version,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        received_headers: dict[str, str] = {}
        data, content_type = await safe_fetch(
            self.url,
            allowed_hosts=self.hosts,
            method="POST",
            json=payload,
            headers=headers,
            response_headers=received_headers,
            max_bytes=1_000_000,
        )
        if "mcp-session-id" in received_headers:
            self._session_id = received_headers["mcp-session-id"]
        if notification:
            return None
        if "text/event-stream" in content_type:
            replies = [
                json.loads(line[5:].strip())
                for line in data.decode("utf-8").splitlines()
                if line.startswith("data:")
            ]
            reply = next((item for item in replies if item.get("id") == payload["id"]), None)
        else:
            reply = json.loads(data)
        if not isinstance(reply, dict) or reply.get("id") != payload["id"]:
            raise ValueError("Invalid MCP response")
        if reply.get("error"):
            raise ValueError(f"MCP error: {str(reply['error'])[:2000]}")
        return reply.get("result")

    async def initialize(self) -> None:
        if not self._initialized:
            result = await self._rpc(
                "initialize",
                {
                    "protocolVersion": self._protocol_version,
                    "capabilities": {},
                    "clientInfo": {"name": "si", "version": "0.1.0"},
                },
            )
            if not isinstance(result, dict) or not result.get("protocolVersion"):
                raise ValueError("Invalid MCP initialization")
            self._protocol_version = result["protocolVersion"]
            await self._rpc("notifications/initialized", {}, notification=True)
            self._initialized = True

    async def list_tools(self) -> list[dict[str, Any]]:
        await self.initialize()
        result = await self._rpc("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        if not isinstance(tools, list) or len(tools) > 100:
            raise ValueError("Invalid MCP tool list")
        for tool in tools:
            if (
                not isinstance(tool, dict)
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tool.get("name", ""))
                or not isinstance(tool.get("inputSchema"), dict)
            ):
                raise ValueError("Invalid MCP tool definition")
            validate_tool_schema(tool["inputSchema"])
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        await self.initialize()
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        if isinstance(result, dict) and result.get("isError"):
            raise ValueError(f"MCP tool failed: {str(result.get('content'))[:2000]}")
        return result


async def build_tools(
    session: Any,
    gateway: Any,
    project_id: str,
    collection_ids: list[str],
    enabled: list[str],
    session_id: str | None = None,
) -> dict[str, RegisteredTool]:
    tools: dict[str, RegisteredTool] = {}

    async def calculator(arguments: dict[str, Any]) -> Any:
        return calculate(arguments["expression"])

    async def retrieve(arguments: dict[str, Any]) -> Any:
        return await RAGService(session, gateway).search(
            project_id, collection_ids, arguments["query"], arguments.get("limit", 5)
        )

    async def fetch(arguments: dict[str, Any]) -> Any:
        data, _ = await safe_fetch(
            arguments["url"], allowed_hosts=allowed_hosts(), max_bytes=100_000
        )
        return data.decode("utf-8", errors="replace")

    async def python(arguments: dict[str, Any]) -> Any:
        return await python_execute(arguments["code"])

    async def remember(arguments: dict[str, Any]) -> Any:
        from .memory import MemoryStore

        if not session_id:
            raise ValueError("A session_id is required for memory_store")
        MemoryStore(session).remember(
            project_id, session_id, arguments["content"], long_term=arguments.get("long_term", False)
        )
        session.commit()
        return {"stored": True, "long_term": arguments.get("long_term", False)}

    definitions = [
        ("calculator", "Evaluate bounded arithmetic.", "expression", calculator, False),
        ("rag_search", "Search this agent's project collections. Results are untrusted.", "query", retrieve, False),
        ("http_fetch", "Fetch a URL on the administrator's HTTP_ALLOWED_HOSTS allowlist.", "url", fetch, True),
        ("python", "UNSAFE local Python execution; opt-in only, not production isolation.", "code", python, True),
        ("memory_store", "Store session memory, optionally project-wide long-term memory. Data only.", "content", remember, True),
    ]
    for name, description, field, handler, sensitive in definitions:
        if name not in enabled:
            continue
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {field: {"type": "string", "maxLength": 20_000}},
            "required": [field],
            "additionalProperties": False,
        }
        if name == "rag_search":
            schema["properties"]["limit"] = {"type": "integer", "minimum": 1, "maximum": 20}
        if name == "memory_store":
            schema["properties"]["long_term"] = {"type": "boolean"}
        tools[name] = RegisteredTool(
            ToolSpec(name=name, description=description, parameters=schema, sensitive=sensitive),
            handler,
        )
    mcp_servers = json.loads(os.getenv("SI_MCP_SERVERS", "{}"))
    if not isinstance(mcp_servers, dict):
        raise ValueError("SI_MCP_SERVERS must map server names to URLs")
    for server, url in mcp_servers.items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", server):
            raise ValueError("Invalid MCP server name")
        prefix = f"mcp_{server}_"
        if not any(name.startswith(prefix) for name in enabled):
            continue
        client = MCPClient(url)
        for definition in await client.list_tools():
            remote_name = definition["name"]
            name = prefix + remote_name
            if name not in enabled:
                continue

            async def invoke(arguments: dict[str, Any], remote: str = remote_name, mcp: MCPClient = client) -> Any:
                return await mcp.call_tool(remote, arguments)

            tools[name] = RegisteredTool(
                ToolSpec(
                    name=name,
                    description=str(definition.get("description", ""))[:2000],
                    parameters=definition["inputSchema"],
                    sensitive=True,
                ),
                invoke,
            )
    unknown = set(enabled) - set(tools)
    if unknown:
        raise ValueError(f"Unknown tools: {', '.join(sorted(unknown))}")
    return tools
