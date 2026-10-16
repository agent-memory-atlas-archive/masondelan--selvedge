"""Disposable save/retrieve check through two real local MCP server processes."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, TextIO

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

from . import __version__
from .config import SETTINGS

_TIMEOUT_SECONDS = 15
_ENTITY = "users.api_key"
_REASON = (
    "Rejected storing API keys in users.api_key: plaintext credentials would be "
    "exposed in a database leak. Store a hash instead."
)
_CONDITION = "The credential storage and threat model change."


class MCPDemoError(RuntimeError):
    """A local MCP self-check failed; no agent activation is inferred."""


def _server_parameters(directory: Path) -> StdioServerParameters:
    # Use this exact installation, even if PATH names an older server or the
    # caller runs from a source checkout. CWD must not select a real project.
    env = {key: value for key, value in os.environ.items() if not key.startswith("SELVEDGE_")}
    env.update({
        spec.env: ",".join(spec.default) if spec.kind == "str_list" else str(spec.default)
        for spec in SETTINGS.values()
    })
    env.update({
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        "SELVEDGE_DB": str(directory / "demo.db"),
        "SELVEDGE_QUIET": "1",
        "SELVEDGE_NO_UPDATE_CHECK": "1",
        "SELVEDGE_NO_TELEMETRY": "1",
        "SELVEDGE_TELEMETRY": "0",
        "SELVEDGE_LOG_LEVEL": "ERROR",
    })
    return StdioServerParameters(
        command=sys.executable, args=["-m", "selvedge.server"],
        cwd=str(directory), env=env,
    )


def _payload(result: CallToolResult, *, many: bool) -> Any:
    if result.isError:
        raise MCPDemoError("The server rejected a demo tool call.")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured.get("result", structured) if many else structured
    # Older MCP 1.x versions return only JSON text blocks.
    values = [json.loads(block.text) for block in result.content if block.type == "text"]
    if many:
        return values[0] if len(values) == 1 and isinstance(values[0], list) else values
    return values[0] if len(values) == 1 else None


async def _call(
    params: StdioServerParameters, errlog: TextIO, tool: str, arguments: dict,
) -> tuple[Any, dict[str, str]]:
    async with stdio_client(params, errlog=errlog) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            if init.serverInfo.name != "selvedge":
                raise MCPDemoError("The subprocess did not identify itself as Selvedge.")
            payload = _payload(
                await session.call_tool(tool, arguments=arguments), many=tool == "prior_attempts",
            )
            return payload, {
                "server_name": init.serverInfo.name,
                "server_version": init.serverInfo.version,
                "protocol_version": str(init.protocolVersion),
            }


async def _round_trip(params: StdioServerParameters, errlog: TextIO) -> dict:
    with anyio.fail_after(_TIMEOUT_SECONDS):
        saved, _ = await _call(params, errlog, "log_change", {
            "entity_path": _ENTITY, "entity_type": "column", "change_type": "reject",
            "reasoning": _REASON, "stale_when": _CONDITION,
            "agent": "selvedge-mcp-demo", "session_id": "demo-save",
        })
        if not isinstance(saved, dict) or saved.get("status") != "logged" or not saved.get("id"):
            raise MCPDemoError("The server did not confirm saving the demo decision.")
        # _call exits both SDK contexts and closes the first server before the
        # second starts: retrieval must come from persisted state, not memory.
        rows, server = await _call(params, errlog, "prior_attempts", {"entity_path": _ENTITY})
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise MCPDemoError("The restarted server did not return the saved decision.")
        row = rows[0]
        expected = {
            "id": saved["id"], "entity_path": _ENTITY, "reasoning": _REASON,
            "outcome": "rejected", "confidence": "exact", "stale_when": _CONDITION,
        }
        if any(row.get(key) != value for key, value in expected.items()):
            raise MCPDemoError("The retrieved decision did not match the saved ID and content.")
        return {
            "status": "ok", "transport": "stdio", "entity_path": _ENTITY,
            "event_id": saved["id"], "retrieved_event_id": row["id"],
            "reasoning": _REASON, "outcome": row["outcome"], "confidence": row["confidence"],
            "stale_when": _CONDITION, "isolated": True, "server_processes": 2,
            "command": [params.command, *params.args], "package_version": __version__,
            **server, "client_activation": "unverified",
        }


def _failure_detail(error: BaseException) -> str:
    # AnyIO task groups may wrap the useful error, including on Python 3.10.
    nested = getattr(error, "exceptions", ())
    if nested:
        return _failure_detail(nested[0])
    if isinstance(error, TimeoutError):
        return f"The local MCP check exceeded {_TIMEOUT_SECONDS} seconds."
    if isinstance(error, OSError):
        return "Could not launch the local MCP server or create its temporary files."
    if isinstance(error, MCPDemoError):
        return str(error)
    return "The local MCP server could not complete initialization or the save/retrieve exchange."


def run_mcp_demo() -> dict:
    """Save via stdio, restart, verify the same record and remove temporary state.

    Uses this Python installation's server, not the client's configured command.
    The SDK owns subprocess shutdown, including termination on cancellation.
    Client trust, hooks and automatic tool use remain unverified.
    """
    try:
        with TemporaryDirectory(prefix="selvedge-mcp-demo-") as directory:
            root = Path(directory)
            # Keep child logs out of JSON output and discard them with the demo.
            with (root / "server.log").open("w", encoding="utf-8") as errlog:
                return anyio.run(_round_trip, _server_parameters(root), errlog)
    except Exception as error:
        raise MCPDemoError(
            f"{_failure_detail(error)} Check this Python installation with "
            "`python -m pip check`, reinstall Selvedge if needed, then retry "
            "`selvedge demo --mcp`. No client activation was verified."
        ) from None
