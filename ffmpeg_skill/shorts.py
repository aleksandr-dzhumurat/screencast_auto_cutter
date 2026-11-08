"""Shorts pipeline: find the most interesting moments in an .srt and cut them."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable

from agents.highlights import CANDIDATE_SLACK, find_highlights
from common.client import get_nebius_client
from common.text import build_transcript, format_mmss, slugify

from .core import (
    VIDEO_ENCODER,
    VIDEO_QUALITY,
    overlay_subtitles,
    parse_srt,
    probe_video,
    run_ffmpeg,
)

# Backward-compatible aliases
_get_client = get_nebius_client
_mmss = format_mmss
_slug = slugify
_build_transcript = build_transcript


# ---------------------------------------------------------------------------
# Speaker speed-up: slow speech is sped up so a Short keeps its pace.
# Code (not the LLM) decides the factor — it is pure arithmetic on the .srt.
# ---------------------------------------------------------------------------
def _speech_rate_wpm(cues) -> float | None:
    """Words per minute of actual speech, measured from the cue timings."""
    words = sum(len(c.text.split()) for c in cues)
    speech = sum(c.end - c.start for c in cues if c.end > c.start)
    if words == 0 or speech <= 0:
        return None
    return words / speech * 60.0


def _auto_speed(
    cues,
    target_wpm: float = 160.0,
    max_speed: float = 1.3,
    min_speed: float = 1.06,
) -> float:
    """Speed factor for a slow speaker: bring the rate up to ~target_wpm.

    Returns 1.0 (no change) when the speaker is already fast enough or the
    needed correction is imperceptible (< min_speed). Never exceeds max_speed
    so the voice stays natural.
    """
    wpm = _speech_rate_wpm(cues)
    if wpm is None or wpm >= target_wpm:
        return 1.0
    factor = target_wpm / wpm
    if factor < min_speed:
        return 1.0
    return round(min(max_speed, factor), 2)


def _resolve_speed(speed, cues) -> float:
    """'auto' -> measured factor; otherwise validate an explicit 1.0-2.0."""
    if isinstance(speed, str):
        if speed.strip().lower() in ("", "auto"):
            return _auto_speed(cues)
        speed = float(speed)
    speed = float(speed)
    if not 1.0 <= speed <= 2.0:
        raise ValueError(f"speed must be 'auto' or 1.0-2.0, got {speed!r}")
    return speed


def _has_audio(input_video: str) -> bool:
    """True if the video has an audio stream (atempo cannot run without one)."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index", "-of", "csv=p=0", input_video,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _build_window(cues, start_sec: float, target: float) -> tuple[float, float]:
    """Snap start to a cue boundary and grow by whole cues to ~target seconds."""
    # First cue at or after the requested start (fall back to the last one).
    i0 = next((i for i, c in enumerate(cues) if c.end > start_sec), len(cues) - 1)
    begin = cues[i0].start
    end = cues[i0].end
    j = i0
    # Grow by whole cues until the span reaches the target: the real length is
    # the smallest whole number of cues that is at least `target` seconds, so a
    # clip ends on a sentence boundary, typically a few seconds over target.
    while (end - begin) < target and j + 1 < len(cues):
        j += 1
        end = cues[j].end
    return begin, end


def _dedupe_windows(windows: list[tuple[float, float]], overlap_frac: float = 0.4) -> list[int]:
    """Return indices of windows to keep, dropping ones that overlap earlier picks."""
    kept: list[tuple[float, float]] = []
    keep_idx: list[int] = []
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


def _srt_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = round((seconds - int(seconds)) * 1000)
    if ms == 1000:
        s, ms = s + 1, 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_clip_srt(cues, start: float, end: float, out_srt: str, speed: float = 1.0) -> int:
    """Write a per-clip .srt with timestamps shifted to the clip's start.

    When the clip is sped up (speed > 1), cue times are also divided by the
    factor so the burned subtitles stay in sync with the faster audio.
    """
    idx = 1
    with open(out_srt, "w", encoding="utf-8") as fh:
        for c in cues:
            if c.end <= start or c.start >= end:
                continue
            ns = max(0.0, c.start - start) / speed
            ne = (min(end, c.end) - start) / speed
            if ne <= ns:
                continue
            fh.write(f"{idx}\n{_srt_ts(ns)} --> {_srt_ts(ne)}\n{c.text}\n\n")
            idx += 1
    return idx - 1


_VERTICAL_FILTER = (
    "[0:v]split=2[bg][fg];"
    "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20[bgb];"
    "[fg]scale=1080:-2[fgs];"
    "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[v]"
)


def _speed_filter(vertical: bool, speed: float, has_audio: bool) -> tuple[str, list]:
    """filter_complex + maps that speed video (and audio) up by `speed`."""
    vchain = f"setpts=PTS/{speed:g}"
    graphs = []
    if vertical:
        graphs.append(
            f"[0:v]{vchain},split=2[bg][fg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20[bgb];"
            "[fg]scale=1080:-2[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[v]"
        )
    else:
        graphs.append(f"[0:v]{vchain}[v]")
    maps = ["-map", "[v]"]
    if has_audio:
        graphs.append(f"[0:a]atempo={speed:g}[a]")
        maps += ["-map", "[a]"]
    return ";".join(graphs), maps


def _cut_segment(
    input_video: str,
    start: float,
    end: float,
    output: str,
    vertical: bool,
    speed: float = 1.0,
    has_audio: bool = True,
    progress_cb=None,
) -> None:
    dur = end - start
    # NB: -t must be an INPUT option (before -i). As an output option this
    # ffmpeg build applies it to the pre-filter timeline and silently defeats
    # setpts/atempo (the clip comes out unspeeded, full length).
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", input_video]
    if speed and speed != 1.0:
        filter_complex, maps = _speed_filter(vertical, speed, has_audio)
        cmd += ["-filter_complex", filter_complex] + maps
    elif vertical:
        cmd += ["-filter_complex", _VERTICAL_FILTER, "-map", "[v]", "-map", "0:a?"]
    else:
        cmd += ["-map", "0:v", "-map", "0:a?"]
    cmd += ["-c:v", VIDEO_ENCODER, "-q:v", VIDEO_QUALITY, "-c:a", "aac",
            "-movflags", "+faststart", output]
    # After the speed-up the OUTPUT lasts dur/speed; progress is output-based.
    run_ffmpeg(cmd, total_seconds=dur / (speed or 1.0), progress_cb=progress_cb,
               label="ffmpeg clip cut")


def make_shorts(
    input_video: str,
    srt_path: str,
    output_dir: str,
    target_seconds: float = 90.0,
    max_clips: int = 3,
    vertical: bool = True,
    style: str = "youtube_pop",
    burn_subs: bool = True,
    speed: str | float = "auto",
    keep_srt: bool = False,
    log: Callable[[str], None] = print,
    progress_cb=None,
) -> dict:
    """Find highlights in an .srt and cut several ~target-second Shorts.

    Returns a summary dict with per-clip metadata; also writes a manifest.json
    in output_dir. ``log`` receives progress lines (the CLI routes them
    through its spinner) and ``progress_cb`` gets a 0..1 fraction during each
    ffmpeg encode.

    ``speed`` speeds the speaker up: ``"auto"`` (default) measures the speech
    rate from the .srt and speeds slow speakers up to ~160 wpm (capped at
    1.3x); an explicit float 1.0-2.0 forces a factor (1.0 disables). Windows
    are grown to ``target * speed`` source seconds so the OUTPUT clip still
    matches ``target_seconds``.

    Subtitles are BURNED into the clips, so by default no per-clip ``.srt`` is
    left next to the ``.mp4`` (players would auto-load the same-basename
    sidecar and show every caption twice). Pass ``keep_srt=True`` to keep the
    re-timed sidecar anyway (e.g. for platform uploads).
    """
    probe_video(input_video)  # validates the video exists / is readable
    cues = parse_srt(srt_path)
    if not cues:
        raise ValueError("No subtitle cues found in the .srt.")

    speed = _resolve_speed(speed, cues)
    wpm = _speech_rate_wpm(cues)
    if wpm is not None:
        if speed != 1.0:
            log(f"speech rate {wpm:.0f} wpm -> speeding the speaker up {speed:g}x")
        else:
            log(f"speech rate {wpm:.0f} wpm -> keeping natural pace")
    has_audio = _has_audio(input_video)
    # The target applies to the OUTPUT: take proportionally more source when
    # speeding up so the finished clip still runs ~target_seconds.
    effective_target = target_seconds * speed

    os.makedirs(output_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(input_video))[0]

    # Ask for a few extra candidates: picks can cluster in one passage, and the
    # overlap guard below then drops them. Best-first ordering means the extra
    # candidates only ever fill the quota, never displace a better pick.
    highlights = find_highlights(cues, max_clips=max_clips + CANDIDATE_SLACK, log=log)
    windows = [_build_window(cues, h["start_sec"], effective_target) for h in highlights]
    # A pick near the end of the talk cannot grow to the target: drop those
    # stub windows so the quota is filled with on-length clips, not 15s tails.
    viable = [i for i, w in enumerate(windows) if (w[1] - w[0]) >= 0.5 * effective_target]
    if not viable:
        viable = list(range(len(windows)))
    keep_idx = [viable[i] for i in _dedupe_windows([windows[i] for i in viable])][:max_clips]

    clips = []
    for rank, idx in enumerate(keep_idx, start=1):
        start, end = windows[idx]
        meta = highlights[idx]
        base_name = f"{stem}_short_{rank:02d}_{_slug(meta['title'])}"
        final_path = os.path.join(output_dir, base_name + ".mp4")

        if burn_subs:
            tmp_fd, tmp_base = tempfile.mkstemp(prefix=base_name + "_", suffix=".mp4", dir=output_dir)
            os.close(tmp_fd)
            # The per-clip .srt is consumed by the burn below. Keep it next to
            # the .mp4 only on request: a same-basename sidecar makes players
            # show the soft track on top of the burned-in captions (duplicates).
            if keep_srt:
                clip_srt = os.path.join(output_dir, base_name + ".srt")
                srt_cleanup = None
            else:
                srt_fd, clip_srt = tempfile.mkstemp(prefix=base_name + "_", suffix=".srt")
                os.close(srt_fd)
                srt_cleanup = clip_srt
            try:
                _cut_segment(input_video, start, end, tmp_base, vertical,
                             speed=speed, has_audio=has_audio, progress_cb=progress_cb)
                _write_clip_srt(cues, start, end, clip_srt, speed=speed)
                overlay_subtitles(
                    tmp_base, clip_srt, final_path, style_preset=style,
                    progress_cb=progress_cb,
                )
            finally:
                for tmp_file in (tmp_base, srt_cleanup):
                    if tmp_file and os.path.exists(tmp_file):
                        os.remove(tmp_file)
        else:
            _cut_segment(input_video, start, end, final_path, vertical,
                         speed=speed, has_audio=has_audio, progress_cb=progress_cb)

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
        log(f"[{rank}] {meta['title']}  ({_mmss(start)}-{_mmss(end)}, {clips[-1]['duration']}s) -> {final_path}")

    manifest = {
        "input_video": os.path.abspath(input_video),
        "srt_path": os.path.abspath(srt_path),
        "target_seconds": target_seconds,
        "speed": speed,
        "speech_rate_wpm": round(wpm, 1) if wpm is not None else None,
        "vertical": vertical,
        "style": style if burn_subs else None,
        "clips": clips,
    }
    manifest_path = os.path.join(output_dir, f"{stem}_shorts_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    manifest["manifest_path"] = os.path.abspath(manifest_path)
    return manifest
