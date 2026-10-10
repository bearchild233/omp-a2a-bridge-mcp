#!/usr/bin/env python3
"""
omp-a2a-bridge-mcp: stdio MCP server exposing `omp` (oh-my-pi coding agent)
as callable tools.

This is the MCP facade over the same core used by the A2A bridge
(src/omp_bridge_core.py). Each tool call maps to one prompt sent to a
long-lived `omp --mode rpc` subprocess; multi-turn conversation is preserved
by reusing the same subprocess per `context_id`.

Transport: MCP stdio (newline-delimited JSON-RPC on stdin/stdout).
Logs go to stderr — stdout carries MCP messages only.

Install as an omp plugin (package ships `mcp.json`), or run standalone:
    python3 src/mcp_server.py
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys

# Make the core importable regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from omp_bridge_core import (  # noqa: E402
    OMP_BIN,
    POOL,
    PROMPT_TIMEOUT_SEC,
    _run_task,
    Task,
)

logging.basicConfig(
    level=os.environ.get("OMP_A2A_LOG", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("omp-mcp")


def _resolve_omp_bin() -> str:
    """Return an omp binary that actually exists on this machine.

    The core defaults to /usr/local/bin/omp (Linux install path). On Windows
    the binary is `omp.exe` found on PATH, and on macOS it may live elsewhere.
    Users can still override with the OMP_BIN env var.
    """
    candidate = os.environ.get("OMP_BIN", "")
    if candidate and (os.path.exists(candidate) or shutil.which(candidate)):
        return candidate
    found = shutil.which("omp") or shutil.which("omp.exe")
    if found:
        return found
    return OMP_BIN  # last resort; will fail at runtime with a clear error


def _tool_schema() -> list[dict]:
    return [
        {
            "name": "run_task",
            "description": (
                "Send a prompt to the oh-my-pi (omp) coding agent and get its "
                "reply. Supports code reading/writing/refactoring, builds, tests, "
                "debugging, and general engineering tasks in the configured "
                "workspace. Pass the same context_id to continue a conversation "
                "(multi-turn memory preserved)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The instruction/prompt to run in omp.",
                    },
                    "context_id": {
                        "type": "string",
                        "description": (
                            "Optional conversation id. Reuse one to keep omp's "
                            "history across calls; omit for a fresh single-shot."
                        ),
                    },
                },
                "required": ["prompt"],
            },
        }
    ]


def _handle_call(name: str, arguments: dict) -> dict:
    if name != "run_task":
        return {
            "content": [{"type": "text", "text": f"unknown tool: {name}"}],
            "isError": True,
        }
    prompt = str(arguments.get("prompt", "")).strip()
    if not prompt:
        return {
            "content": [{"type": "text", "text": "missing required argument: prompt"}],
            "isError": True,
        }
    context_id = str(arguments.get("context_id") or "").strip() or None
    # Match the A2A bridge's single-turn contract: one task, synchronous reply.
    task = Task(
        task_id=f"mcp-{context_id or 'anon'}",
        context_id=context_id or "",
        text=prompt,
    )
    task.state = "TASK_STATE_WORKING"
    try:
        worker = POOL.get(task.context_id)
        reply = worker.prompt(prompt, timeout=PROMPT_TIMEOUT_SEC)
        task.reply = reply
        task.state = "TASK_STATE_COMPLETED"
        return {
            "content": [{"type": "text", "text": reply}],
            "isError": False,
        }
    except Exception as e:
        log.exception("run_task failed")
        return {
            "content": [{"type": "text", "text": f"omp error: {e}"}],
            "isError": True,
        }
    finally:
        task.finished_at = __import__("time").time()


def main() -> None:
    resolved_bin = _resolve_omp_bin()
    if resolved_bin != OMP_BIN:
        os.environ["OMP_BIN"] = resolved_bin
        # Re-import would re-read the constant; patch the module in place.
        import omp_bridge_core

        omp_bridge_core.OMP_BIN = resolved_bin
    log.info("omp-mcp ready: omp=%s cwd=%s", resolved_bin, os.environ.get("OMP_A2A_CWD", "/root"))

    protocol_version = "2025-06-18"
    server_info = {"name": "omp-a2a-bridge-mcp", "version": "0.1.0"}
    capabilities = {"tools": {}}

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log.warning("bad JSON line: %r", line[:200])
            continue

        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            protocol_version = params.get("protocolVersion", protocol_version)
            out = {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": protocol_version,
                    "capabilities": capabilities,
                    "serverInfo": server_info,
                },
            }
        elif method == "notifications/initialized":
            continue  # no response
        elif method == "ping":
            out = {"jsonrpc": "2.0", "id": mid, "result": {}}
        elif method == "tools/list":
            out = {"jsonrpc": "2.0", "id": mid, "result": {"tools": _tool_schema()}}
        elif method == "tools/call":
            name = (params.get("name") or "")
            arguments = params.get("arguments") or {}
            result = _handle_call(name, arguments)
            out = {"jsonrpc": "2.0", "id": mid, "result": result}
        else:
            out = {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }

        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
