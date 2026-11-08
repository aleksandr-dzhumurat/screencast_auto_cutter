"""ffmpeg-skill MCP server.

Exposes a small set of FFmpeg video-editing tools over the Model Context
Protocol (stdio transport). Launch it with ``run_ffmpeg_skill.sh`` or:

    python -m ffmpeg_skill.server

The LangGraph agent in ``agent.py`` connects to this server, lets a Nebius
model pick the tools, and renders the result.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from .autocut import autocut_by_subtitles as _autocut_by_subtitles
from .core import overlay_subtitles as _overlay_subtitles
from .core import parse_srt as _parse_srt
from .core import probe_video as _probe_video

mcp = FastMCP("ffmpeg-skill")


@mcp.tool()
def probe_video(path: str) -> dict:
    """Return width, height and duration (seconds) of a video file.

    Use this first to learn the frame size before planning an overlay.
    """
    return _probe_video(path)


@mcp.tool()
def inspect_subtitles(srt_path: str) -> dict:
    """Summarise an .srt subtitle file: cue count and the first few cues.

    Helpful to confirm timing/text before rendering.
    """
    cues = _parse_srt(srt_path)
    preview = [
        {"index": c.index, "start": round(c.start, 2), "end": round(c.end, 2), "text": c.text}
        for c in cues[:5]
    ]
    return {"srt_path": os.path.abspath(srt_path), "cue_count": len(cues), "preview": preview}


@mcp.tool()
def autocut_by_subtitles(
    input_video: str,
    srt_path: str,
    output_video: str,
    output_srt: str | None = None,
    pad: float = 0.25,
    min_gap: float = 0.7,
) -> dict:
    """Remove no-speech gaps from a video using its subtitles as a speech map.

    The .srt cues are speech; the gaps between them are silence. This keeps the
    speech (padded so words are not clipped), drops gaps >= `min_gap` seconds,
    re-encodes a tighter video, AND writes a re-timed .srt matching the cut
    timeline. Run this BEFORE overlay_subtitles and feed it the returned
    `output_srt`.

    Args:
        input_video:  source video.
        srt_path:     subtitles on the original timeline (e.g. from Whisper).
        output_video: destination for the tightened video.
        output_srt:   destination for the re-timed .srt (defaults to the
                      output_video stem + ".srt").
        pad:          seconds kept around each cue (avoids clipping words).
        min_gap:      only gaps this long or longer are cut.

    Returns a summary with segments_kept, removed_seconds, cut_duration and the
    output video / srt paths.
    """
    return _autocut_by_subtitles(
        input_video=input_video,
        srt_path=srt_path,
        output_video=output_video,
        output_srt=output_srt,
        pad=pad,
        min_gap=min_gap,
    )


@mcp.tool()
def overlay_subtitles(
    input_video: str,
    srt_path: str,
    output_video: str,
    style_preset: str = "modern_box",
    trim_seconds: float | None = None,
) -> dict:
    """Overlay styled subtitles from an .srt onto a video and encode it.

    Each subtitle cue is rendered as a transparent, rounded, semi-transparent
    box with centred text and composited onto the video for its time window,
    then encoded with Apple VideoToolbox hardware acceleration.

    Args:
        input_video:  path to the source video.
        srt_path:     path to the SubRip (.srt) subtitle file.
        output_video: destination path for the rendered video.
        style_preset: one of
                      - "modern_box"  : white text on a dark grey box (default);
                      - "youtube_pop" : bold yellow text with a black outline;
                      - "adaptive"    : box tinted per-shot to the scene's
                        dominant colour via a deterministic rule (offline);
                      - "designer"    : a Nebius design sub-agent chooses the
                        per-shot palette using colour-pairing theory, keeping
                        the dark-box/light-text look as the default.
                      Use "designer" (or "adaptive") when the user wants the
                      subtitles to blend with each shot's colours.
        trim_seconds: optional - render only the first N seconds for a fast
                      preview instead of the whole video.

    Returns a summary dict including the absolute output path and cue count.
    """
    return _overlay_subtitles(
        input_video=input_video,
        srt_path=srt_path,
        output_video=output_video,
        style_preset=style_preset,
        trim_seconds=trim_seconds,
    )


if __name__ == "__main__":
    mcp.run()
