"""ffmpeg-skill: a small local FFmpeg toolkit exposed over MCP.

This package provides:
  * core.py    - pure-Python FFmpeg/FFprobe helpers (probe, subtitle overlay).
  * server.py  - a FastMCP server that exposes those helpers as MCP tools.
  * shorts.py  - highlight-finding + clip cutting for the Shorts pipeline.

It is tuned for macOS / Apple Silicon (VideoToolbox hardware encoding) and does
NOT depend on an ffmpeg built with libass or drawtext: subtitles are rasterised
to transparent PNGs with Pillow and composited with the always-available
`overlay` filter.
"""

from .autocut import autocut_by_subtitles
from .core import overlay_subtitles, parse_srt, probe_video
from .shorts import make_shorts
from .timecodes import generate_timecodes

__all__ = [
    "autocut_by_subtitles",
    "generate_timecodes",
    "make_shorts",
    "overlay_subtitles",
    "parse_srt",
    "probe_video",
]
