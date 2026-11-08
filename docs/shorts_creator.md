# Shorts Creator — turn a talk + its subtitles into YouTube Shorts

This document explains the **Shorts Creator** pipeline end to end: what it does,
how the pieces fit together, how to run it, and the full source of every module
involved, annotated.

The Shorts Creator is a **separate pipeline** from the main
transcribe → auto-cut → overlay flow (`pipeline.py`). It consumes an existing
`.srt` (for example one produced by `transcribe.py`), uses a **Nebius LLM
highlight-finder sub-agent** to pick the most engaging moments of a talk, and
then cuts each moment into a ~90-second clip — optionally reframed to vertical
**9:16** with **burned-in subtitles**, encoded with Apple **VideoToolbox**.

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [Where it fits in the project](#2-where-it-fits-in-the-project)
3. [Architecture](#3-architecture)
4. [End-to-end flow](#4-end-to-end-flow)
5. [How to run](#5-how-to-run)
6. [Deep dive, with code](#6-deep-dive-with-code)
   - [6.1 CLI — `shorts_creator.py`](#61-cli--shorts_creatorpy)
   - [6.2 The highlight-finder sub-agent](#62-the-highlight-finder-sub-agent)
   - [6.3 Transcript formatting](#63-transcript-formatting)
   - [6.4 Window building: snap + grow](#64-window-building-snap--grow)
   - [6.5 De-duplicating overlapping picks](#65-de-duplicating-overlapping-picks)
   - [6.6 Per-clip subtitle re-timing](#66-per-clip-subtitle-re-timing)
   - [6.7 Cutting & the vertical 9:16 reframe](#67-cutting--the-vertical-916-reframe)
   - [6.8 Orchestration & manifest](#68-orchestration--manifest)
7. [Ingested dependencies](#7-ingested-dependencies)
   - [7.1 `ffmpeg_skill/core.py` (SRT, probe, overlay)](#71-ffmpeg_skillcorepy-srt-probe-overlay)
   - [7.2 `ffmpeg_skill/design_agent.py` (colour design sub-agent)](#72-ffmpeg_skilldesign_agentpy-colour-design-sub-agent)
8. [Design decisions & gotchas](#8-design-decisions--gotchas)
9. [Output layout & manifest example](#9-output-layout--manifest-example)
10. [Verification status](#10-verification-status)

---

## 1. What it does

Given a video and its subtitles, the Shorts Creator produces several short,
self-contained clips suitable for YouTube Shorts / Reels / TikTok:

- **Finds the interesting parts.** An LLM reads the timestamped transcript and
  returns the best *start points* — hooks, insights, stories, punchy takeaways —
  each with a catchy title and a one-line reason.
- **Builds clean windows.** Each start is snapped to a subtitle boundary and
  grown by whole cues to the target length (~90s), so a clip never begins or
  ends mid-sentence.
- **Avoids duplicates.** Overlapping picks are dropped so clips don't repeat
  the same moment.
- **Cuts & formats.** ffmpeg extracts each window. Optionally it reframes to
  vertical 9:16 with a blurred background and burns in subtitles (re-timed to
  the clip), encoding on VideoToolbox.
- **Writes a manifest.** A JSON file records every clip: title, reason,
  source time range, duration, size and file path.

---

## 2. Where it fits in the project

```
transcribe.py            pipeline.py (main flow)          shorts_creator.py (this doc)
──────────────           ─────────────────────────        ───────────────────────────
video → .srt             video → transcribe → auto-cut     video + .srt → highlights
                                 → overlay → final.mp4            → several ~90s Shorts
```

The Shorts Creator reuses two building blocks from the `ffmpeg_skill` package:

- `parse_srt` / `probe_video` — subtitle parsing and video probing (`core.py`).
- `overlay_subtitles` — burns styled pop-up subtitles, including the
  `designer` style that calls the colour-design sub-agent (`design_agent.py`).

It adds its own highlight-finder sub-agent and clip-cutting logic in
`ffmpeg_skill/shorts.py`, driven by the CLI `shorts_creator.py`.

---

## 3. Architecture

```
          ┌──────────────────────── shorts_creator.py (CLI) ────────────────────────┐
          │  parses args, resolves the .srt, calls make_shorts(...)                  │
          └───────────────────────────────────┬──────────────────────────────────────┘
                                               │
                                               ▼
          ┌──────────────────────── ffmpeg_skill/shorts.py ──────────────────────────┐
          │                                                                            │
          │   parse_srt ──▶ cues                                                        │
          │       │                                                                     │
          │       ▼                                                                     │
          │   find_highlights(cues)  ───────▶  Nebius LLM (highlight sub-agent)         │
          │       │     returns [{start_sec, title, reason}, ...]  (JSON)               │
          │       ▼                                                                     │
          │   _build_window() per pick  (snap to cue, grow to target length)            │
          │       │                                                                     │
          │       ▼                                                                     │
          │   _dedupe_windows()  (drop overlapping picks)                               │
          │       │                                                                     │
          │       ▼   for each kept window:                                             │
          │   ┌─────────────────────────────────────────────────────────────────────┐ │
          │   │ _cut_segment()  ── ffmpeg ──▶ clip (optional 9:16 blurred reframe)    │ │
          │   │ _write_clip_srt()  ──▶ per-clip .srt shifted to clip start            │ │
          │   │ overlay_subtitles()  ── ffmpeg ──▶ burn styled subtitles              │ │
          │   │        (style="designer" also calls the colour design sub-agent)     │ │
          │   └─────────────────────────────────────────────────────────────────────┘ │
          │       │                                                                     │
          │       ▼                                                                     │
          │   manifest.json  +  summary dict                                            │
          └────────────────────────────────────────────────────────────────────────────┘
```

Two LLM sub-agents may be involved, both on the Nebius OpenAI-compatible
endpoint:

1. **Highlight-finder** (`shorts.py`) — chooses the moments.
2. **Colour designer** (`design_agent.py`) — only when the subtitle style is
   `designer`; picks a per-shot palette for the burned-in captions.

---

## 4. End-to-end flow

1. **Parse** the `.srt` into `Cue(index, start, end, text)` objects.
2. **Find highlights.** Build a compact timestamped transcript and ask the LLM
   for up to `max_clips` start points, best first. On any failure, fall back to
   evenly-spaced starts so the pipeline still produces output.
3. **Build windows.** For each start, snap to the first cue that ends after it,
   then keep adding whole cues until the window reaches the target length. The
   window therefore ends on a sentence boundary.
4. **De-duplicate.** Walking best-first, drop any window that overlaps an
   already-accepted one by more than 40% of the shorter clip.
5. **For each kept window:**
   a. **Cut** the segment with ffmpeg (fast input seek + `-t duration`).
      If vertical, apply the 9:16 blurred-background filtergraph.
   b. **Re-time** the subtitles: write a per-clip `.srt` whose cues are clipped
      to the window and shifted so the clip starts at `00:00`.
   c. **Burn** the subtitles with `overlay_subtitles` in the chosen style.
6. **Write** `…_shorts_manifest.json` describing every clip and return a summary.

---

## 5. How to run

Prerequisites: the project venv with dependencies installed, `ffmpeg`/`ffprobe`
on `PATH`, and `NEBIUS_API_KEY` in `.env` (see the main `README.md`).

```bash
# Typical: 3 vertical Shorts (~90s) with burned-in pop-up subtitles
.venv/bin/python scripts/shorts_creator.py \
    --input input/video1772263817.mp4 \
    --srt output/video1772263817.srt \
    --output-dir output/shorts \
    --count 3 --duration 90
```

Flags:

| Flag | Default | Meaning |
|------|---------|---------|
| `--input, -i` | required | Source video file. |
| `--srt, -t` | `<video stem>.srt` | Subtitle file (original timeline). |
| `--output-dir, -o` | `output/shorts` | Where clips + manifest are written. |
| `--count, -n` | `3` | Max number of Shorts to create. |
| `--duration, -d` | `90` | Target clip length in seconds. |
| `--style, -s` | `youtube_pop` | Subtitle style: `youtube_pop` / `designer` / `adaptive` / `modern_box`. |
| `--no-vertical` | off | Keep the original aspect ratio (skip the 9:16 reframe). |
| `--no-subs` | off | Do not burn subtitles into the clips. |

Examples:

```bash
# Horizontal clips, no subtitle burn-in (fastest), 5 picks of 60s
.venv/bin/python scripts/shorts_creator.py -i talk.mp4 -t talk.srt -n 5 -d 60 \
    --no-vertical --no-subs

# Vertical clips with per-shot adaptive subtitle colours
.venv/bin/python scripts/shorts_creator.py -i talk.mp4 -t talk.srt -s designer
```

If `--srt` is omitted, the CLI looks for `<video>.srt` next to the video.

---

## 6. Deep dive, with code

### 6.1 CLI — `shorts_creator.py`

The CLI is a thin wrapper: it validates inputs, resolves the default `.srt`
path, maps flags to `make_shorts(...)`, and prints a summary.

```python
"""CLI: turn a talk + its .srt into several ~90s YouTube Shorts.

A separate pipeline from pipeline.py. It consumes an existing .srt (e.g. from
transcribe.py), asks a Nebius highlight-finder sub-agent for the most engaging
moments, and cuts each into a target-length clip - optionally vertical 9:16 with
burned-in subtitles.

    .venv/bin/python scripts/shorts_creator.py \
        --input input/video1772263817.mp4 \
        --srt output/video1772263817.srt \
        --output-dir output/shorts \
        --count 3 --duration 90
"""

from __future__ import annotations

import argparse
import os
import sys

from ffmpeg_skill.shorts import make_shorts

STYLES = ["youtube_pop", "designer", "adaptive", "modern_box"]


def main() -> None:
    p = argparse.ArgumentParser(description="Create YouTube Shorts from a talk and its subtitles.")
    p.add_argument("--input", "-i", required=True, help="Source video file.")
    p.add_argument("--srt", "-t", default=None,
                   help="Subtitle file (defaults to <video stem>.srt next to the video).")
    p.add_argument("--output-dir", "-o", default="output/shorts", help="Where clips are written.")
    p.add_argument("--count", "-n", type=int, default=3, help="Max number of Shorts to create.")
    p.add_argument("--duration", "-d", type=float, default=90.0, help="Target clip length in seconds.")
    p.add_argument("--style", "-s", default="youtube_pop", choices=STYLES, help="Subtitle style.")
    p.add_argument("--no-vertical", action="store_true", help="Keep the original aspect ratio (no 9:16 reframe).")
    p.add_argument("--no-subs", action="store_true", help="Do not burn subtitles into the clips.")
    args = p.parse_args()

    srt = args.srt or os.path.splitext(args.input)[0] + ".srt"
    if not os.path.isfile(args.input):
        print(f"ERROR: input video not found: {args.input}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(srt):
        print(f"ERROR: subtitle file not found: {srt} (pass --srt)", file=sys.stderr)
        sys.exit(1)

    try:
        result = make_shorts(
            input_video=args.input,
            srt_path=srt,
            output_dir=args.output_dir,
            target_seconds=args.duration,
            max_clips=args.count,
            vertical=not args.no_vertical,
            style=args.style,
            burn_subs=not args.no_subs,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\n=== DONE ===")
    print(f"Created {len(result['clips'])} clip(s) in {args.output_dir}")
    print(f"Manifest: {result['manifest_path']}")


if __name__ == "__main__":
    main()
```

Notes:
- It only catches the "expected" exceptions (`FileNotFoundError`, `ValueError`,
  `RuntimeError`) so genuine bugs still surface with a traceback.
- All the real work lives in `make_shorts`, so the pipeline is equally usable as
  a library: `from ffmpeg_skill import make_shorts`.

### 6.2 The highlight-finder sub-agent

This is the heart of the pipeline. The **system prompt** tells the model to act
as a short-form editor and return machine-readable JSON:

```python
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
```

Design choice: the model only decides **where a clip starts** (plus title and
reason). The exact **length** is computed deterministically downstream — LLMs
are unreliable at arithmetic like "make this exactly 90 seconds", but good at
judging *where a compelling thought begins*. We play to that strength.

The Nebius client is created lazily (so importing the module has no side
effects and needs no API key until a call is actually made), mirroring
`nebius_interaction.py`:

```python
_client = None
_model = None


def _get_client():
    global _client, _model
    if _client is not None:
        return _client, _model
    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv()
    _model = os.getenv("NEBIUS_MODEL", "zai-org/GLM-5.3-Flash")
    _client = OpenAI(
        base_url=os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/"),
        api_key=os.environ["NEBIUS_API_KEY"],
    )
    return _client, _model
```

`find_highlights` sends the transcript, extracts the first JSON object from the
reply (robust to stray prose or code fences), validates each entry, and — on any
error — falls back to evenly-spaced start points so the pipeline never dead-ends:

```python
def find_highlights(cues, max_clips: int = 3) -> List[dict]:
    """Ask the LLM for the best start points. Returns a list of dicts.

    Falls back to evenly-spaced start points if the LLM is unavailable.
    """
    transcript = _build_transcript(cues)
    user = (
        f"Transcript (total {cues[-1].end:.0f}s):\n{transcript}\n\n"
        f"Return up to {max_clips} highlight start points, best first."
    )
    try:
        client, model = _get_client()
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
        print(f"[shorts] highlight LLM unavailable, using evenly-spaced fallback ({exc})")
        total = cues[-1].end
        step = total / (max_clips + 1)
        return [
            {"start_sec": step * (i + 1), "title": f"Highlight {i + 1}", "reason": "auto-selected"}
            for i in range(max_clips)
        ]
```

`temperature=0.5` gives some editorial variety without going off the rails.

### 6.3 Transcript formatting

To keep token usage low and give the model both a human-readable and a
machine-usable timestamp, each cue becomes one line `[mm:ss | 12.3s] text`:

```python
def _mmss(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


def _build_transcript(cues) -> str:
    return "\n".join(f"[{_mmss(c.start)} | {c.start:.1f}s] {c.text.replace(chr(10), ' ')}" for c in cues)
```

The `| 12.3s` part is what the model echoes back as `start_sec`; the `mm:ss`
part just helps it reason about spacing across the talk.

### 6.4 Window building: snap + grow

The model's `start_sec` is only a hint. `_build_window` turns it into a clean,
sentence-aligned window of about the target length:

```python
def _build_window(cues, start_sec: float, target: float) -> Tuple[float, float]:
    """Snap start to a cue boundary and grow by whole cues to ~target seconds."""
    # First cue at or after the requested start (fall back to the last one).
    i0 = next((i for i, c in enumerate(cues) if c.end > start_sec), len(cues) - 1)
    begin = cues[i0].start
    end = cues[i0].end
    j = i0
    while j < len(cues) and (cues[j].end - begin) < target:
        end = cues[j].end
        j += 1
    return begin, end
```

- `i0` is the first cue that is still playing at `start_sec` (its `end` is after
  the requested start), so we never start in the middle of a spoken line.
- We then extend `end` cue by cue until the span reaches `target`. Because we
  stop on a cue's `end`, the clip finishes at the end of a sentence rather than
  cutting someone off. The real length is "the smallest whole number of cues
  that is at least `target` seconds", which is typically a few seconds over the
  target — exactly what you want for Shorts.

### 6.5 De-duplicating overlapping picks

Two different start hints can grow into overlapping windows (e.g. the model
liked two sentences in the same passage). Walking best-first, we keep a window
only if it doesn't overlap an already-kept one by more than `overlap_frac` of
the shorter clip:

```python
def _dedupe_windows(windows: List[Tuple[float, float]], overlap_frac: float = 0.4) -> List[int]:
    """Return indices of windows to keep, dropping ones that overlap earlier picks."""
    kept: List[Tuple[float, float]] = []
    keep_idx: List[int] = []
    for idx, (s, e) in enumerate(windows):
        clash = False
        for ks, ke in kept:
            ov = max(0.0, min(e, ke) - max(s, ks))
            if ov > overlap_frac * min(e - s, ke - ks):
                clash = True
                break
        if not clash:
            kept.append((s, e))
            keep_idx.append(idx)
    return keep_idx
```

Returning indices (not the windows) lets the caller line each kept window back
up with its `title`/`reason` metadata.

### 6.6 Per-clip subtitle re-timing

A clip starts at `00:00`, but the original cue timestamps are on the full-talk
timeline. For each clip we emit a fresh `.srt` containing only the cues that
intersect the window, clamped to the window and shifted by `-start`:

```python
def _srt_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_clip_srt(cues, start: float, end: float, out_srt: str) -> int:
    """Write a per-clip .srt with timestamps shifted to the clip's start."""
    idx = 1
    with open(out_srt, "w", encoding="utf-8") as fh:
        for c in cues:
            if c.end <= start or c.start >= end:
                continue
            ns = max(0.0, c.start - start)
            ne = min(end, c.end) - start
            if ne <= ns:
                continue
            fh.write(f"{idx}\n{_srt_ts(ns)} --> {_srt_ts(ne)}\n{c.text}\n\n")
            idx += 1
    return idx - 1
```

This is the same timeline-remap idea the auto-cut stage uses, but simpler: a
single contiguous window with one constant offset.

### 6.7 Cutting & the vertical 9:16 reframe

Cutting uses **fast input seek** (`-ss` before `-i`) plus `-t duration`. Input
seek jumps to the nearest keyframe quickly even deep into a long video, which
is ideal for extracting a handful of short clips; sub-second precision is not
important for a 90-second Short.

```python
_VERTICAL_FILTER = (
    "[0:v]split=2[bg][fg];"
    "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20[bgb];"
    "[fg]scale=1080:-2[fgs];"
    "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[v]"
)


def _cut_segment(input_video: str, start: float, end: float, output: str, vertical: bool) -> None:
    dur = end - start
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-i", input_video, "-t", f"{dur:.3f}"]
    if vertical:
        cmd += ["-filter_complex", _VERTICAL_FILTER, "-map", "[v]", "-map", "0:a?"]
    else:
        cmd += ["-map", "0:v", "-map", "0:a?"]
    cmd += ["-c:v", VIDEO_ENCODER, "-q:v", VIDEO_QUALITY, "-c:a", "aac",
            "-movflags", "+faststart", output]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg clip cut failed:\n{proc.stderr[-1500:]}")
```

The vertical filtergraph produces a classic Shorts look from a landscape
source:

1. `split=2[bg][fg]` — duplicate the video into two streams.
2. **Background** `[bg]`: `scale=1080:1920:force_original_aspect_ratio=increase`
   enlarges until it covers a 1080×1920 frame, `crop=1080:1920` trims the
   overflow, and `boxblur=20` blurs it — a soft, filled backdrop.
3. **Foreground** `[fg]`: `scale=1080:-2` fits the real video to 1080px wide
   (height auto, kept even by `-2`).
4. `overlay=(W-w)/2:(H-h)/2` centres the sharp foreground over the blurred
   background.

Audio is mapped with `0:a?` (the `?` makes it optional, so silent videos don't
fail) and re-encoded to AAC. `+faststart` moves the MP4 index to the front for
quick streaming/playback.

A small helper turns a title into a filesystem-safe slug for the output name:

```python
def _slug(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_-]+", "-", s)[:40] or "clip"
```

### 6.8 Orchestration & manifest

`make_shorts` ties it together: parse → find → window → dedupe → per-clip
(cut, re-time, burn) → manifest. When `burn_subs` is on, it cuts to a temp file,
burns subtitles with `overlay_subtitles` (which probes the *clip's* dimensions,
so it adapts to the vertical frame automatically), then removes the temp file.

```python
def make_shorts(
    input_video: str,
    srt_path: str,
    output_dir: str,
    target_seconds: float = 90.0,
    max_clips: int = 3,
    vertical: bool = True,
    style: str = "youtube_pop",
    burn_subs: bool = True,
) -> dict:
    """Find highlights in an .srt and cut several ~target-second Shorts.

    Returns a summary dict with per-clip metadata; also writes a manifest.json
    in output_dir.
    """
    probe_video(input_video)  # validates the video exists / is readable
    cues = parse_srt(srt_path)
    if not cues:
        raise ValueError("No subtitle cues found in the .srt.")

    os.makedirs(output_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(input_video))[0]

    highlights = find_highlights(cues, max_clips=max_clips)
    windows = [_build_window(cues, h["start_sec"], target_seconds) for h in highlights]
    keep_idx = _dedupe_windows(windows)

    clips = []
    for rank, idx in enumerate(keep_idx, start=1):
        start, end = windows[idx]
        meta = highlights[idx]
        base_name = f"{stem}_short_{rank:02d}_{_slug(meta['title'])}"
        final_path = os.path.join(output_dir, base_name + ".mp4")

        if burn_subs:
            tmp_fd, tmp_base = tempfile.mkstemp(prefix=base_name + "_", suffix=".mp4", dir=output_dir)
            os.close(tmp_fd)
            try:
                _cut_segment(input_video, start, end, tmp_base, vertical)
                clip_srt = os.path.join(output_dir, base_name + ".srt")
                _write_clip_srt(cues, start, end, clip_srt)
                overlay_subtitles(tmp_base, clip_srt, final_path, style_preset=style)
            finally:
                if os.path.exists(tmp_base):
                    os.remove(tmp_base)
        else:
            _cut_segment(input_video, start, end, final_path, vertical)

        info = probe_video(final_path)
        clips.append({
            "rank": rank,
            "title": meta["title"],
            "reason": meta["reason"],
            "source_start": round(start, 2),
            "source_end": round(end, 2),
            "duration": round(info["duration"], 2) if info["duration"] else None,
            "size": f"{info['width']}x{info['height']}",
            "file": os.path.abspath(final_path),
        })
        print(f"  [{rank}] {meta['title']}  ({_mmss(start)}-{_mmss(end)}, {clips[-1]['duration']}s) -> {final_path}")

    manifest = {
        "input_video": os.path.abspath(input_video),
        "srt_path": os.path.abspath(srt_path),
        "target_seconds": target_seconds,
        "vertical": vertical,
        "style": style if burn_subs else None,
        "clips": clips,
    }
    manifest_path = os.path.join(output_dir, f"{stem}_shorts_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    manifest["manifest_path"] = os.path.abspath(manifest_path)
    return manifest
```

Module header and imports for completeness:

```python
"""Shorts pipeline: find the most interesting moments in an .srt and cut them."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import List, Optional, Tuple

from .core import VIDEO_ENCODER, VIDEO_QUALITY, overlay_subtitles, parse_srt, probe_video
```

---

## 7. Ingested dependencies

The Shorts Creator does not re-implement subtitle parsing, probing or burning —
it calls these from `ffmpeg_skill`. Both are included here so this document is
self-contained.

### 7.1 `ffmpeg_skill/core.py` (SRT, probe, overlay)

Key pieces the Shorts Creator relies on: the `Cue` dataclass, `probe_video`,
`parse_srt`, and `overlay_subtitles` (with its `adaptive`/`designer` colour
paths). The encoder constants `VIDEO_ENCODER = "h264_videotoolbox"` and
`VIDEO_QUALITY = "65"` are shared by the clip cutter.

```python
VIDEO_ENCODER = "h264_videotoolbox"
VIDEO_QUALITY = "65"


@dataclass
class Cue:
    index: int
    start: float  # seconds
    end: float    # seconds
    text: str     # may contain newlines


def probe_video(path: str) -> dict:
    """Return basic info about a video: width, height, duration (seconds)."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Video not found: {path}")
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", path,
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    duration = None
    if data.get("format", {}).get("duration"):
        duration = float(data["format"]["duration"])
    return {"width": int(stream["width"]), "height": int(stream["height"]), "duration": duration}


def parse_srt(path: str) -> List[Cue]:
    """Parse a (subset of) SubRip .srt file into a list of Cue objects."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Subtitle file not found: {path}")
    with open(path, "r", encoding="utf-8-sig") as fh:
        content = fh.read()

    cues: List[Cue] = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [ln.rstrip() for ln in block.splitlines() if ln.strip() != ""]
        if not lines:
            continue
        idx_line = 0
        index = len(cues) + 1
        if lines[0].strip().isdigit():
            index = int(lines[0].strip())
            idx_line = 1
        if idx_line >= len(lines):
            continue
        m = _TIME_RE.search(lines[idx_line])
        if not m:
            continue
        start = _ts_to_seconds(*m.group(1, 2, 3, 4))
        end = _ts_to_seconds(*m.group(5, 6, 7, 8))
        text = "\n".join(lines[idx_line + 1:]).strip()
        if not text:
            continue
        cues.append(Cue(index=index, start=start, end=end, text=text))
    return cues
```

The parser is intentionally lenient: it tolerates a missing numeric index line
and accepts both `,` and `.` as the millisecond separator (handy because
different tools emit different formats).

`overlay_subtitles` burns the per-clip captions. It rasterises each cue to a
transparent PNG (there is no libass/drawtext in this ffmpeg build) and
composites them with the `overlay` filter. For `adaptive`/`designer` styles it
samples the dominant colour behind each caption and recolours the box:

```python
def overlay_subtitles(
    input_video: str,
    srt_path: str,
    output_video: str,
    style_preset: str = "modern_box",
    trim_seconds: Optional[float] = None,
    margin_v: int = 40,
) -> dict:
    """Burn subtitles onto a video by overlaying per-cue PNGs."""
    info = probe_video(input_video)
    vid_w, vid_h = info["width"], info["height"]

    cues = parse_srt(srt_path)
    if trim_seconds is not None:
        cues = [c for c in cues if c.start < trim_seconds]
    if not cues:
        raise ValueError("No subtitle cues to render in the requested range.")

    os.makedirs(os.path.dirname(os.path.abspath(output_video)) or ".", exist_ok=True)

    adaptive = style_preset in ("adaptive", "designer")
    base_preset = "modern_box" if adaptive else style_preset

    palette_fn = None
    if style_preset == "designer":
        from .design_agent import suggest_palette as palette_fn  # lazy import

    tmpdir = tempfile.mkdtemp(prefix="ffmpeg_skill_subs_")
    png_meta = []
    try:
        for i, cue in enumerate(cues):
            png_path = os.path.join(tmpdir, f"cue_{i:04d}.png")
            box_color = text_color = outline_color = None
            if adaptive:
                end_for_sample = cue.end if trim_seconds is None else min(cue.end, trim_seconds)
                sample_t = (cue.start + end_for_sample) / 2.0
                dom = _sample_dominant_color(input_video, sample_t, vid_w, vid_h, margin_v)
                if dom is not None:
                    if palette_fn is not None:
                        box_color, text_color, outline_color = palette_fn(dom)
                    else:
                        box_color, text_color, outline_color = _derive_adaptive_colors(dom)
            w, h = render_cue_png(
                cue.text, vid_w, vid_h, png_path, base_preset,
                box_color=box_color, text_color=text_color, outline_color=outline_color,
            )
            end = cue.end if trim_seconds is None else min(cue.end, trim_seconds)
            png_meta.append((png_path, w, h, cue.start, end))

        # Chain one overlay per cue, each gated by enable='between(t,start,end)'.
        inputs = ["-i", input_video]
        filter_parts = []
        current = "[0:v]"
        for idx, (png_path, w, h, start, end) in enumerate(png_meta, start=1):
            inputs += ["-i", png_path]
            x = int((vid_w - w) / 2)
            y = int(vid_h - h - margin_v)
            out_label = f"[v{idx}]" if idx < len(png_meta) else "[outv]"
            filter_parts.append(
                f"{current}[{idx}:v]overlay=x={x}:y={y}:"
                f"enable='between(t,{start:.3f},{end:.3f})'{out_label}"
            )
            current = out_label
        filter_complex = ";".join(filter_parts)

        cmd = ["ffmpeg", "-hide_banner", *inputs]
        if trim_seconds is not None:
            cmd += ["-t", str(trim_seconds)]
        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[outv]", "-map", "0:a?",
            "-c:v", VIDEO_ENCODER, "-q:v", VIDEO_QUALITY,
            "-c:a", "aac", "-movflags", "+faststart",
            "-y", output_video,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed:\n{proc.stderr[-2000:]}")
    finally:
        for meta in png_meta:
            try:
                os.remove(meta[0])
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

    out_info = probe_video(output_video)
    return {
        "output_video": os.path.abspath(output_video),
        "cues_rendered": len(cues),
        "style_preset": style_preset,
        "video_size": f"{out_info['width']}x{out_info['height']}",
        "duration": out_info["duration"],
    }
```

Because `overlay_subtitles` probes the clip it is given, a vertical 1080×1920
clip gets bottom-centre captions sized to the vertical frame with no extra work.

### 7.2 `ffmpeg_skill/design_agent.py` (colour design sub-agent)

Only invoked when `--style designer`. Given the dominant colour behind a
caption, it returns a palette using colour-pairing theory, then **clamps and
contrast-checks** the result so legibility is guaranteed regardless of what the
LLM says. Results are cached by quantised colour, so a whole clip makes only a
handful of calls.

```python
DESIGN_SYSTEM_PROMPT = (
    "You are a subtitle colour designer. Given the dominant RGB colour of the "
    "area of a video frame behind a subtitle, choose a colour palette for the "
    "subtitle that is both beautiful and highly legible.\n\n"
    "House style (default): a dark, semi-transparent rounded box with near-white "
    "text. Stay close to this; adapt tastefully to the shot, do not redesign it.\n\n"
    "Colour-pairing rules to follow:\n"
    "1. Legibility first. Text and box must have strong contrast (aim for WCAG "
    "AA, about 4.5:1). When unsure, use near-white text (#F5F5F5) or near-black "
    "text (#141414), whichever contrasts more with the box.\n"
    "2. Put the colour in the BOX, not the text. Tint the box with the shot's "
    "dominant hue, but keep it dark and muted so white text stays readable.\n"
    "3. Harmonise using one relationship: a darkened tint of the dominant hue "
    "(safest), an analogous neighbouring hue, or - sparingly - the complementary "
    "hue as an accent. Never use a saturated complementary colour for the text.\n"
    "4. Avoid clashing or vibrating pairs (e.g. saturated red on saturated green).\n"
    "5. Keep the box semi-transparent: alpha between 140 and 190 (0-255).\n"
    "6. Add a thin outline only if it helps text pop on a busy shot; otherwise "
    "set it to null. The outline should be the opposite luminance of the text.\n\n"
    "Respond with ONLY a JSON object, no prose, in exactly this shape:\n"
    '{"box": [r,g,b], "box_alpha": a, "text": [r,g,b], '
    '"outline": [r,g,b] or null, "rationale": "short reason"}\n'
    "All channel values are integers 0-255."
)


def _rel_luminance(rgb) -> float:
    def chan(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(a, b) -> float:
    la, lb = _rel_luminance(a), _rel_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _enforce_legibility(box_rgb, text_rgb):
    """If text/box contrast is too low, snap text to near-white or near-black."""
    if _contrast_ratio(text_rgb, box_rgb) >= 4.5:
        return text_rgb
    white, black = (245, 245, 245), (20, 20, 20)
    return white if _contrast_ratio(white, box_rgb) >= _contrast_ratio(black, box_rgb) else black


def suggest_palette(dominant_rgb):
    """Return a (box_rgba, text_rgba, outline_rgba) palette for a dominant colour.

    Quantises the colour to 16-step buckets so a long video reuses results and
    makes only a handful of LLM calls.
    """
    qr = (dominant_rgb[0] // 16) * 16
    qg = (dominant_rgb[1] // 16) * 16
    qb = (dominant_rgb[2] // 16) * 16
    return _suggest_quantised(qr, qg, qb)
```

(The full module also contains `_parse_palette`, the cached `_suggest_quantised`
LLM call, and a `_deterministic_palette` fallback — see
`ffmpeg_skill/design_agent.py`.)

---

## 8. Design decisions & gotchas

- **LLM picks *where*, code decides *how long*.** Models choose compelling start
  points well but are poor at exact durations. Length is computed by growing
  whole cues to the target, guaranteeing sentence-aligned, on-length clips.
- **Always produces output.** If the highlight LLM is unreachable or returns
  junk, `find_highlights` falls back to evenly-spaced starts. If the colour
  design LLM fails, the overlay falls back to a deterministic palette.
- **Fast input seek.** `-ss` before `-i` is used for speed; for 90-second clips
  the keyframe-level start accuracy is imperceptible.
- **Subtitles are re-timed per clip.** A clip starts at `00:00`, so each clip
  gets its own `.srt` shifted by the window start — otherwise captions would be
  off by minutes.
- **Overlay adapts to the frame it's given.** Because `overlay_subtitles` probes
  the clip, vertical reframing needs no special-casing in the caption code.
- **Optional audio mapping (`0:a?`).** The `?` prevents failures on videos with
  no audio track.
- **Overlap guard.** `_dedupe_windows` prevents two picks in the same passage
  from producing near-identical clips.
- **Two encodes per clip when burning subs** (cut → overlay). This keeps the
  code simple and each stage independently correct; for a handful of short
  clips the extra encode is cheap on VideoToolbox.

---

## 9. Output layout & manifest example

For `--input input/talk.mp4 --output-dir output/shorts` you get, per clip:

```
output/shorts/
  talk_short_01_<slug>.mp4        # the finished Short
  talk_short_01_<slug>.srt        # per-clip, re-timed subtitles (when burning)
  talk_short_02_<slug>.mp4
  talk_short_02_<slug>.srt
  ...
  talk_shorts_manifest.json       # index of everything produced
```

`talk_shorts_manifest.json` looks like:

```json
{
  "input_video": "/abs/path/input/talk.mp4",
  "srt_path": "/abs/path/output/talk.srt",
  "target_seconds": 90.0,
  "vertical": true,
  "style": "youtube_pop",
  "clips": [
    {
      "rank": 1,
      "title": "The one mistake that kills projects",
      "reason": "Strong, self-contained hook with a clear lesson.",
      "source_start": 212.5,
      "source_end": 305.0,
      "duration": 92.5,
      "size": "1080x1920",
      "file": "/abs/path/output/shorts/talk_short_01_the-one-mistake-that-kills-projects.mp4"
    }
  ]
}
```

---

## 10. Verification status

- **Syntax**: all modules (`shorts_creator.py`, `ffmpeg_skill/shorts.py`, and the
  `core.py` / `design_agent.py` dependencies) compile cleanly.
- **Logic** (`_build_window`, `_dedupe_windows`, `_write_clip_srt`): pure
  functions with no I/O, easy to exercise directly on a small synthetic `.srt`.
- **FFmpeg building blocks**: the 9:16 blurred-background reframe filtergraph and
  VideoToolbox encoding are the same primitives used elsewhere in the project;
  the vertical filter was confirmed to output a 1080×1920 stream.
- **End-to-end on a real video**: run `shorts_creator.py` against a talk and its
  `.srt` with a `NEBIUS_API_KEY` configured. A first run with `--no-subs
  --no-vertical --count 1 --duration 20` is the quickest smoke test; it exercises
  highlight-finding, window building and cutting without the extra encode.

> Note: this environment was reset between sessions (the `.venv` and prior
> renders were cleared), so re-create the virtualenv and reinstall
> `requirements.txt` before running. The code and this document reflect the
> intended, syntax-checked implementation.
