"""Highlight-finder sub-agent: picks engaging moments for YouTube Shorts."""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from common.client import get_nebius_client
from common.text import build_transcript

HIGHLIGHT_SYSTEM_PROMPT = (
    "You are a short-form video editor. You are given the timestamped "
    "transcript of a talk. Find the most engaging moments to turn into "
    "standalone YouTube Shorts.\n\n"
    "Pick moments that:\n"
    "- make a strong, self-contained point (a hook, insight, story, or punchy "
    "takeaway) that makes sense without the rest of the talk;\n"
    "- start at a natural sentence boundary;\n"
    "- are spread across the talk (do not cluster them together).\n\n"
    "For each moment return the START time in seconds (where the clip should "
    "begin) and a short, catchy title (max 60 characters) plus a one-line "
    "reason. The clip length is handled downstream, so only the start matters.\n\n"
    "Respond with ONLY a JSON object of this shape:\n"
    '{"highlights": [{"start_sec": number, "title": "string", '
    '"reason": "string"}]}\n'
    "Order them best-first."
)

# Extra highlight candidates requested from the LLM so the overlap guard in
# the Shorts pipeline can drop clustered picks without the clip quota going
# unfilled.
CANDIDATE_SLACK = 3


def find_highlights(cues, max_clips: int = 3, log: Callable[[str], None] = print) -> list[dict]:
    """Ask the LLM for the best start points. Returns a list of dicts.

    Falls back to evenly-spaced start points if the LLM is unavailable.
    """
    transcript = build_transcript(cues)
    user = (
        f"Transcript (total {cues[-1].end:.0f}s):\n{transcript}\n\n"
        f"Return up to {max_clips} highlight start points, best first."
    )
    try:
        client, model = get_nebius_client()
        resp = client.chat.completions.create(
            model=model,
            temperature=0.5,
            messages=[
                {"role": "system", "content": HIGHLIGHT_SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        )
        raw = resp.choices[0].message.content or ""
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError("no JSON in response")
        data = json.loads(match.group(0))
        highlights = data.get("highlights", [])
        cleaned = []
        for h in highlights:
            cleaned.append({
                "start_sec": float(h["start_sec"]),
                "title": str(h.get("title", "")).strip()[:60] or "Highlight",
                "reason": str(h.get("reason", "")).strip(),
            })
        if cleaned:
            return cleaned[:max_clips]
        raise ValueError("empty highlights")
    except Exception as exc:  # noqa: BLE001
        log(f"[shorts] highlight LLM unavailable, using evenly-spaced fallback ({exc})")
        total = cues[-1].end
        step = total / (max_clips + 1)
        return [
            {"start_sec": step * (i + 1), "title": f"Highlight {i + 1}", "reason": "auto-selected"}
            for i in range(max_clips)
        ]

