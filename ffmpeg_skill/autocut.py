"""Subtitle-driven auto-cut: remove no-speech gaps using the .srt as a VAD.

The subtitles already tell us when the speaker talks: each cue is a speech
interval, and the gaps between cues are silence/pauses. This module keeps the
speech intervals (with a little padding so words are not clipped), drops long
gaps, and re-encodes a tighter video.

Crucially, cutting changes the timeline, so it also writes a NEW .srt whose
timestamps match the cut video. That cut video + re-timed .srt is the input to
the pop-up overlay stage (`ffmpeg_skill.overlay_subtitles`).

Pipeline position:
    transcribe.py  ->  autocut_by_subtitles  ->  overlay_subtitles
"""

from __future__ import annotations

import os
import tempfile

from .core import VIDEO_ENCODER, VIDEO_QUALITY, parse_srt, probe_video, run_ffmpeg

Interval = tuple[float, float]


def _srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = round((seconds - int(seconds)) * 1000)
    if ms == 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _build_keep_intervals(
    cues,
    duration: float,
    pad: float,
    min_gap: float,
) -> list[Interval]:
    """Expand each cue by `pad`, then merge intervals whose gap is < `min_gap`.

    The remaining gaps (>= min_gap) are what gets cut. Small gaps are preserved
    so the result does not feel unnaturally clipped.
    """
    spans = []
    for c in cues:
        s = max(0.0, c.start - pad)
        e = min(duration, c.end + pad)
        if e > s:
            spans.append([s, e])
    spans.sort()

    merged: list[list[float]] = []
    for s, e in spans:
        if merged and s - merged[-1][1] < min_gap:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _make_time_mapper(keep: list[Interval]):
    """Return a function mapping original-timeline seconds -> cut-timeline seconds."""
    segs = []
    cum = 0.0
    for s, e in keep:
        segs.append((s, e, cum))
        cum += e - s

    def map_t(t: float) -> float:
        for s, e, off in segs:
            if s <= t <= e:
                return off + (t - s)
        # t falls in a removed gap: clamp to the nearest kept boundary.
        for s, e, off in segs:
            if t < s:
                return off  # start of the next kept segment
        last_s, last_e, last_off = segs[-1]
        return last_off + (last_e - last_s)

    return map_t


def _write_remapped_srt(cues, keep: list[Interval], out_srt: str) -> int:
    map_t = _make_time_mapper(keep)
    idx = 1
    os.makedirs(os.path.dirname(os.path.abspath(out_srt)) or ".", exist_ok=True)
    with open(out_srt, "w", encoding="utf-8") as fh:
        for c in cues:
            ns, ne = map_t(c.start), map_t(c.end)
            if ne <= ns:
                continue
            fh.write(f"{idx}\n")
            fh.write(f"{_srt_timestamp(ns)} --> {_srt_timestamp(ne)}\n")
            fh.write(f"{c.text}\n\n")
            idx += 1
    return idx - 1


def autocut_by_subtitles(
    input_video: str,
    srt_path: str,
    output_video: str,
    output_srt: str | None = None,
    pad: float = 0.25,
    min_gap: float = 0.7,
    progress_cb=None,
) -> dict:
    """Cut no-speech gaps from a video using its subtitles as the speech map.

    Parameters
    ----------
    input_video  : source video.
    srt_path     : subtitles describing where speech happens (original timeline).
    output_video : destination for the tightened video.
    output_srt   : destination for the re-timed subtitles (defaults to
                   output_video stem + ".srt"). Feed this to overlay_subtitles.
    pad          : seconds of padding kept around each cue (avoids clipping words).
    min_gap      : only gaps of at least this many seconds are removed; shorter
                   pauses are preserved so pacing stays natural.
    progress_cb  : optional callable(fraction 0..1) fed from ffmpeg's
                   ``-progress`` output during the re-encode.

    Returns a summary including the kept-segment count, removed seconds and the
    paths to the cut video and the re-timed .srt.
    """
    info = probe_video(input_video)
    duration = info["duration"]
    if duration is None:
        raise RuntimeError("Could not determine video duration.")

    cues = parse_srt(srt_path)
    if not cues:
        raise ValueError("No subtitle cues found; nothing to base the cut on.")

    keep = _build_keep_intervals(cues, duration, pad=pad, min_gap=min_gap)
    if not keep:
        raise ValueError("No speech intervals derived from subtitles.")

    kept_duration = sum(e - s for s, e in keep)
    removed = max(0.0, duration - kept_duration)

    if output_srt is None:
        output_srt = os.path.splitext(output_video)[0] + ".srt"
    os.makedirs(os.path.dirname(os.path.abspath(output_video)) or ".", exist_ok=True)

    # Build the trim+concat filtergraph (written to a script file to stay well
    # within command-length limits when there are many segments).
    parts = []
    concat_inputs = ""
    for i, (s, e) in enumerate(keep):
        parts.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
        concat_inputs += f"[v{i}][a{i}]"
    filtergraph = ";".join(parts) + f";{concat_inputs}concat=n={len(keep)}:v=1:a=1[outv][outa]"

    script_fd, script_path = tempfile.mkstemp(prefix="autocut_", suffix=".txt")
    try:
        with os.fdopen(script_fd, "w") as fh:
            fh.write(filtergraph)

        cmd = [
            "ffmpeg", "-hide_banner", "-y", "-i", input_video,
            "-filter_complex_script", script_path,
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", VIDEO_ENCODER, "-q:v", VIDEO_QUALITY,
            "-c:a", "aac", "-movflags", "+faststart",
            output_video,
        ]
        run_ffmpeg(cmd, total_seconds=duration, progress_cb=progress_cb, label="ffmpeg auto-cut")
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass

    cues_written = _write_remapped_srt(cues, keep, output_srt)
    out_info = probe_video(output_video)

    return {
        "output_video": os.path.abspath(output_video),
        "output_srt": os.path.abspath(output_srt),
        "segments_kept": len(keep),
        "original_duration": round(duration, 2),
        "cut_duration": round(out_info["duration"], 2) if out_info["duration"] else None,
        "removed_seconds": round(removed, 2),
        "cues": cues_written,
    }
