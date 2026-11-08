"""Common text formatting and parsing utilities."""

from __future__ import annotations

import re


def format_mmss(seconds: float) -> str:
    """Format seconds as mm:ss (e.g. 65.2 -> '01:05')."""
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


def slugify(text: str, max_length: int = 40) -> str:
    """Turn arbitrary text into a URL/filename-safe lowercase slug."""
    s = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_-]+", "-", s)[:max_length] or "clip"


def build_transcript(cues) -> str:
    """Format subtitle cues into a timestamped transcript string."""
    return "\n".join(
        f"[{format_mmss(c.start)} | {c.start:.1f}s] {c.text.replace(chr(10), ' ')}"
        for c in cues
    )

