"""Timecode agent: backward-compatible wrapper around agents.timecodes."""

from agents.timecodes import (
    TIMECODE_SYSTEM_PROMPT,
    generate_timecodes,
)

__all__ = ["TIMECODE_SYSTEM_PROMPT", "generate_timecodes"]
