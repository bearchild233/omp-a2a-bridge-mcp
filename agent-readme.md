# agent-readme.md — for AI agents working on this repo

MCP bridge that exposes the **omp (oh-my-pi) coding agent** as callable tools.
MCP client → stdio JSON-RPC → `src/mcp_server.py` → `src/omp_bridge_core.py` → `omp --mode rpc` subprocess.

## Layout

- `src/mcp_server.py` — MCP stdio server (protocol handling, `run_task` tool, tool schema). **Edit here for MCP-level behavior.**
- `src/omp_bridge_core.py` — core: `OmpWorker` (long-lived `omp --mode rpc` subprocess, prompt queue), worker pool, idle reaper, plus the original A2A HTTP server (unused in MCP mode). **Edit here for omp invocation / concurrency.**
- `mcp.json` — omp plugin MCP-server declaration (stdio, `python3 src/mcp_server.py`).
- `package.json` — npm metadata only; no build step, no dependencies.

## Constraints (do not break)

- **Pure Python stdlib only** — no third-party imports, no requirements.txt. Adding a dependency is a breaking change.
- stdout carries MCP JSON-RPC only; **logs go to stderr** (a stray debug line on stdout corrupts the protocol).
- `OmpWorker` reuses one subprocess per `context_id` for multi-turn memory — do not spawn per-call.
- omp runs with `--approval-mode yolo`; tool calls execute with the launching user's permissions. Never disable that guarantee silently.

## Test

```bash
# MCP handshake + tools/list
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n' | python3 src/mcp_server.py

# Real tool call through omp (slow; needs `omp` on PATH)
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"run_task","arguments":{"prompt":"say ok","context_id":"t1"}}}\n' | python3 src/mcp_server.py
```

Use `OMP_A2A_LOG=DEBUG` for verbose stderr. `OMP_BIN` / `OMP_A2A_CWD` / timeouts are env-configurable (see README).

## Gotchas

- `omp_bridge_core.py` has an unused/broken `WorkerPool._reaper` method — the working reaper is module-level `_start_reaper()`. Do not "fix" the dead method into use.
- `OMP_BIN` default is `/usr/local/bin/omp`; `mcp_server.py` probes PATH for `omp`/`omp.exe` at startup (Windows support). Keep that probe.
- bump both `serverInfo.version` in `mcp_server.py` and `package.json` on release.
