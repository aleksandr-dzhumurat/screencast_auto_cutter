"""Autonomous podcast-editing agent (LangGraph + Nebius + ffmpeg-skill MCP)."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

load_dotenv()

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_VIDEO = os.path.join(PROJECT_DIR, "input", "video1772263817.mp4")
SUBTITLES = os.path.join(PROJECT_DIR, "input", "subtitles.srt")
OUTPUT_VIDEO = os.path.join(PROJECT_DIR, "output", "podcast_with_subtitles.mp4")
SERVER_SCRIPT = os.path.join(PROJECT_DIR, "scripts", "run_ffmpeg_skill.sh")

MODEL = os.getenv("NEBIUS_MODEL", "zai-org/GLM-5.3-Flash")
BASE_URL = os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/")

SYSTEM_PROMPT = (
    "You are an autonomous video-editing agent for podcasts. "
    "You have tools from a local ffmpeg-skill server. Work step by step:\n"
    "1. Call probe_video to learn the frame size and duration.\n"
    "2. Call inspect_subtitles to confirm the cues and timing.\n"
    "3. If the user wants silent/no-speech gaps removed, call "
    "autocut_by_subtitles first, then use its returned output_video and the "
    "re-timed output_srt for the overlay step.\n"
    "4. Call overlay_subtitles to render styled subtitles onto the video.\n"
    "Style presets for overlay_subtitles: 'modern_box' (default grey box, white "
    "text), 'youtube_pop' (bold yellow), 'adaptive' (box tinted to each shot's "
    "dominant colour by a deterministic rule), and 'designer' (a design "
    "sub-agent chooses each shot's palette with colour-pairing theory). If the "
    "user asks for subtitles that blend with or adapt to the scene colours, use "
    "'designer'; keep 'modern_box' otherwise.\n"
    "Always use the exact file paths given by the user. When you are done, "
    "report the output path, the number of cues rendered, and the video size."
)


def build_llm() -> ChatOpenAI:
    api_key = os.environ["NEBIUS_API_KEY"]
    return ChatOpenAI(
        model=MODEL,
        base_url=BASE_URL,
        api_key=api_key,
        temperature=0,
    )


async def run_editing_agent(
    input_video: str = INPUT_VIDEO,
    srt_path: str = SUBTITLES,
    output_video: str = OUTPUT_VIDEO,
    server_script: str = SERVER_SCRIPT,
    style_preset: str = "modern_box",
    trim_seconds: int = 18,
) -> dict:
    client = MultiServerMCPClient(
        {
            "ffmpeg-skill": {
                "command": server_script,
                "args": [],
                "transport": "stdio",
            }
        }
    )

    tools = await client.get_tools()
    print(f"Connected to ffmpeg-skill MCP server. Tools: {[t.name for t in tools]}")

    agent = create_react_agent(build_llm(), tools, prompt=SYSTEM_PROMPT)

    task = (
        "Overlay subtitles onto a podcast video.\n"
        f"- input_video: {input_video}\n"
        f"- srt_path: {srt_path}\n"
        f"- output_video: {output_video}\n"
        f"- style_preset: {style_preset}\n"
        f"- trim_seconds: {trim_seconds}  (render an {trim_seconds}-second preview so it is fast)\n"
        "First probe the video and inspect the subtitles, then render the overlay."
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": task}]},
        config={"recursion_limit": 25},
    )
    return result

