"""Timecode agent: pick 5-6 key chapter moments of a video from its .srt."""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from common.client import get_nebius_client
from common.text import build_transcript

TIMECODE_SYSTEM_PROMPT = (
    "You are a video editor preparing chapter markers for a talk or lecture. "
    "You are given the timestamped transcript of the FINAL cut of the video. "
    "Pick its most important moments - topic changes, key insights, stories, "
    "demos, conclusions - the points a viewer would want to jump to.\n\n"
    "Rules:\n"
    "- each time_sec must be a start time that appears in the transcript "
    "lines (the seconds value in brackets), where a NEW topic or idea begins,\n"
    "not mid-explanation;\n"
    "- spread the picks across the whole video, first and last sections "
    "included;\n"
    "- label: max 60 characters, no quotes; reason: one short line.\n\n"
    "Respond with ONLY a JSON object of this shape:\n"
    '{"timecodes": [{"time_sec": number, "label": "string", '
    '"reason": "string"}]}\n'
    "Order them chronologically."
)


def _fmt_tc(seconds: float) -> str:
    """Format as a chapter timestamp: M:SS under an hour, H:MM:SS above."""
    s = max(0, round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _snap_to_cue(cues, t: float) -> int:
    """Index of the cue whose start is nearest to t (chapters start on speech)."""
    return min(range(len(cues)), key=lambda i: abs(cues[i].start - t))


def _fallback_picks(cues, count: int) -> list[dict]:
    """Evenly-spaced cue starts with labels taken from the cue text."""
    if len(cues) <= count:
        indices = list(range(len(cues)))
    else:
        indices = [round(i * (len(cues) - 1) / (count - 1)) for i in range(count)]
    seen = set()
    picks = []
    for i in indices:
        if i in seen:
            continue
        seen.add(i)
        c = cues[i]
        picks.append({
            "time_sec": c.start,
            "label": c.text.replace("\n", " ")[:60],
            "reason": "auto-selected",
        })
    return picks


def generate_timecodes(
    srt_path: str,
    output_md: str,
    count: int = 6,
    log: Callable[[str], None] = print,
) -> dict:
    """Generate 5-6 chapter timecodes from an .srt on the FINAL video timeline."""
    from ffmpeg_skill.core import parse_srt
    cues = parse_srt(srt_path)
    if not cues:
        raise ValueError(f"No subtitle cues found in {srt_path}")

    user = (
        f"Transcript (total {cues[-1].end:.0f}s):\n{build_transcript(cues)}\n\n"
        f"Return {min(count, 6)} highlight timecodes, chronologically."
    )
    picks: list[dict] = []
    try:
        client, model = get_nebius_client()
        resp = client.chat.completions.create(
            model=model,
            temperature=0.3,
            messages=[
                {"role": "system", "content": TIMECODE_SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        )
        raw = resp.choices[0].message.content or ""
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError("no JSON in response")
        data = json.loads(match.group(0))
        total = cues[-1].end
        for t in data.get("timecodes", []):
            try:
                time_sec = float(t["time_sec"])
            except (KeyError, TypeError, ValueError):
                continue
            picks.append({
                "time_sec": max(0.0, min(total, time_sec)),
                "label": str(t.get("label", "")).strip()[:60] or "Key moment",
                "reason": str(t.get("reason", "")).strip(),
            })
        if not picks:
            raise ValueError("no valid timecodes")
    except Exception as exc:  # noqa: BLE001
        log(f"[timecodes] LLM unavailable, using evenly-spaced fallback ({exc})")
        picks = _fallback_picks(cues, count)

    snapped: list[dict] = []
    used_idx = set()
    for p in picks:
        i = _snap_to_cue(cues, p["time_sec"])
        if i in used_idx:
            continue
        used_idx.add(i)
        snapped.append({**p, "time_sec": cues[i].start})
    snapped.sort(key=lambda p: p["time_sec"])
    snapped = snapped[:count]
    if not snapped:
        raise ValueError("No timecodes could be derived.")

    # Write markdown file
    lines = [
        "# Chapter Timecodes",
        "",
        "Key moments identified by the Nebius timecode sub-agent:",
        "",
        "| Time | Chapter | Notes |",
        "|------|---------|-------|",
    ]
    for p in snapped:
        tc = _fmt_tc(p["time_sec"])
        lbl = p["label"].replace("|", "\\|")
        rsn = p["reason"].replace("|", "\\|")
        lines.append(f"| {tc} | {lbl} | {rsn} |")
    lines.append("")

    with open(output_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log(f"[timecodes] Wrote {len(snapped)} chapters -> {output_md}")
    return {
        "output_path": output_md,
        "count": len(snapped),
        "timecodes": snapped,
    }
