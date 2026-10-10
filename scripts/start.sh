#!/usr/bin/env bash
# Start the omp MCP server directly (no npm install needed).
# Usage: ./scripts/start.sh [OMP_BIN] [OMP_A2A_CWD]
set -euo pipefail
cd "$(dirname "$0")/.."

export OMP_BIN="${1:-${OMP_BIN:-omp}}"
export OMP_A2A_CWD="${2:-${OMP_A2A_CWD:-/root}}"

exec python3 src/mcp_server.py
