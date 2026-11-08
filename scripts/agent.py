"""Autonomous podcast-editing agent (LangGraph + Nebius + ffmpeg-skill MCP).

Run it:

    .venv/bin/python scripts/agent.py

Environment (see .env / .env.example):
    NEBIUS_API_KEY   - required
    NEBIUS_BASE_URL  - default https://api.tokenfactory.nebius.com/v1/
    NEBIUS_MODEL     - default zai-org/GLM-5.3-Flash
    LANGSMITH_*      - optional tracing (inherited from the environment)
"""

from __future__ import annotations

# Running from a subdirectory: make the project root importable
# (docs/refactoring_plan.md) so `agents`, `common`, `ffmpeg_skill` and
# `scripts` resolve no matter where the command is invoked from.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import os

from agents.orchestrator import (
    INPUT_VIDEO,
    OUTPUT_VIDEO,
    SERVER_SCRIPT,
    SUBTITLES,
    run_editing_agent,
)


async def run() -> None:
    result = await run_editing_agent(
        input_video=INPUT_VIDEO,
        srt_path=SUBTITLES,
        output_video=OUTPUT_VIDEO,
        server_script=SERVER_SCRIPT,
    )

    print("\n=== Agent transcript ===")
    for msg in result["messages"]:
        role = getattr(msg, "type", "?")
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            for tc in tool_calls:
                print(f"[{role}] -> tool call: {tc['name']}({tc['args']})")
        content = (msg.content or "").strip() if isinstance(msg.content, str) else msg.content
        if content:
            print(f"[{role}] {content}")

    print("\n=== Result ===")
    if os.path.exists(OUTPUT_VIDEO):
        print(f"OK - rendered video at: {OUTPUT_VIDEO}")
    else:
        print("WARNING - the agent finished but the output file was not created.")


if __name__ == "__main__":
    asyncio.run(run())
