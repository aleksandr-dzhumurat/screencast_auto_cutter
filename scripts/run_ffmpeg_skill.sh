#!/usr/bin/env bash
#
# Startup script for the ffmpeg-skill MCP server.
#
# Launches the server over stdio using the project virtualenv. The LangGraph
# agent spawns this script automatically, but you can also run it by hand to
# sanity-check that the server boots:
#
#     ./scripts/run_ffmpeg_skill.sh
#
# (it will sit waiting for MCP stdio input; Ctrl-C to exit).

set -euo pipefail

# The script lives in scripts/ — everything it needs (the venv, the
# ffmpeg_skill package) is one level up, at the project root.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

exec "$PYTHON" -m ffmpeg_skill.server "$@"
