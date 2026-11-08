"""One-command pipeline: video -> subtitles -> auto-cut -> pop-up overlay -> timecodes.

Chains the stages into a single CLI so you only provide the input video
and an output directory:

    .venv/bin/python scripts/pipeline.py --input input/video1772263817.mp4 --output-dir output

All artifacts of one video live in their own subdirectory named after the
video: output/<video_name>/...

Stages (all artifacts land in --output-dir/<video_name>/):
    1. transcribe        <stem>.srt                   (Whisper, MLX)
    2. auto-cut gaps     <stem>_cut.mp4               (remove no-speech gaps)
    3. overlay pop-ups   <stem>_final.mp4/_final.srt  (styled subtitles burned in)
    4. timecodes         <stem>_final_timecodes.md    (Nebius sub-agent, 5-6 key
                                                      moments on the final timeline)
    5. screenshots        screenshots/00000000s.jpg ... (one frame of the
                                                      INPUT video every 2s,
                                                      named by its timestamp)

Use --no-autocut to skip stage 2, --trim N for a fast preview, --style to
pick the subtitle look, --no-timecodes to skip stage 4 and --no-screenshots
to skip stage 5. --skip-autocut is a fast mode: only the transcript .srt and
screenshots/ are produced — no re-encoding at all.
"""

from __future__ import annotations

# Running from a subdirectory: make the project root importable
# (docs/refactoring_plan.md) so `agents`, `common`, `ffmpeg_skill` and
# `scripts` resolve no matter where the command is invoked from.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import os
import re
import sys
import time

from agents.timecodes import generate_timecodes
from common.progress import Spinner
from ffmpeg_skill import autocut_by_subtitles, overlay_subtitles
from ffmpeg_skill.core import probe_video, run_ffmpeg
from scripts.transcribe import DEFAULT_MODEL, SUPPORTED_LANGUAGES
from scripts.transcribe import transcribe as transcribe_to_srt

STYLES = ["designer", "adaptive", "modern_box", "youtube_pop"]


def _banner(step: str, msg: str) -> None:
    print(f"\n=== {step} === {msg}")


def _shot_name(t: float, integral: bool) -> str:
    """Filename for the frame captured at `t` seconds (zero-padded to sort).

    One format per run — decided by whether the interval is a whole number —
    so all names share a width and sort chronologically.
    """
    if integral:
        return f"{round(t):08d}s.jpg"
    return f"{t:011.3f}s.jpg"


def _capture_screenshots(
    input_video: str,
    out_dir: str,
    interval: float = 2.0,
    progress_cb=None,
) -> dict:
    """Capture one JPEG frame from the input video every `interval` seconds.

    Files are named after their timestamp in the INPUT video (00000000s.jpg
    for t=0, 00000002s.jpg for t=2, ...) rather than by order, so the name
    says when the frame was taken.
    """
    os.makedirs(out_dir, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", input_video,
        "-vf", f"fps=1/{interval:g}",
        "-q:v", "4",
        os.path.join(out_dir, "%08d.jpg"),
    ]
    info = probe_video(input_video)
    run_ffmpeg(cmd, total_seconds=info["duration"], progress_cb=progress_cb,
               label="ffmpeg screenshots")
    # ffmpeg's image2 muxer numbers frames 1..N (frame k = t (k-1)*interval);
    # rename to the frame's timestamp in seconds.
    integral = float(interval).is_integer()
    sequential = sorted(
        f for f in os.listdir(out_dir) if re.fullmatch(r"\d{8}\.jpg", f)
    )
    for k, name in enumerate(sequential):
        os.rename(os.path.join(out_dir, name),
                  os.path.join(out_dir, _shot_name(k * interval, integral)))
    count = len([f for f in os.listdir(out_dir) if f.endswith(".jpg")])
    return {
        "directory": os.path.abspath(out_dir),
        "count": count,
        "interval": interval,
    }


def _prompt_choice(label: str, choices: list[str], default: str) -> str:
    """Interactively ask for one of `choices`; falls back to `default` when no TTY."""
    options = " / ".join(f"[{i + 1}] {c}" for i, c in enumerate(choices))
    hint = f" (Enter = {default})" if default else ""
    try:
        answer = input(f"{label}: {options}{hint}\n> ").strip()
    except EOFError:
        answer = ""
    if not answer:
        return default
    if answer in choices:
        return answer
    if answer.isdigit() and 1 <= int(answer) <= len(choices):
        return choices[int(answer) - 1]
    print(f"Invalid choice {answer!r}, using {default!r}.")
    return default


def run_pipeline(
    input_video: str,
    output_dir: str,
    language: str = "en",
    style: str = "designer",
    do_autocut: bool = True,
    trim_seconds=None,
    model: str = DEFAULT_MODEL,
    pad: float = 0.25,
    min_gap: float = 0.7,
    overwrite: bool = False,
    timecodes: bool = True,
    keep_srt: bool = False,
    screenshots: bool = True,
    screenshot_interval: float = 2.0,
    skip_autocut: bool = False,
) -> dict:
    if not os.path.isfile(input_video):
        raise FileNotFoundError(f"Input video not found: {input_video}")
    stem = os.path.splitext(os.path.basename(input_video))[0]
    # Every artifact of one video lives in its own subdirectory, named after
    # the video, inside --output-dir.
    video_dir = os.path.join(output_dir, stem)
    os.makedirs(video_dir, exist_ok=True)
    if skip_autocut:
        # Fast mode: transcript (+ screenshots) only — no re-encoding at all.
        steps = 1 + (1 if screenshots else 0)
    else:
        steps = 3 + (1 if timecodes else 0) + (1 if screenshots else 0)

    srt_path = os.path.join(video_dir, f"{stem}.srt")
    cut_video = os.path.join(video_dir, f"{stem}_cut.mp4")
    # The re-timed subtitles match the FINAL video's timeline (and the cut
    # video's), so they are named after the end product, not the stage.
    final_srt = os.path.join(video_dir, f"{stem}_final.srt")
    final_video = os.path.join(video_dir, f"{stem}_final.mp4")
    screenshots_dir = os.path.join(video_dir, "screenshots")

    t0 = time.time()

    # 1) Transcribe -------------------------------------------------------
    _banner(f"1/{steps} transcribe", f"{input_video} [{language}]")
    with Spinner(f"Whisper MLX · transcribing [{language}]") as spin:
        transcribe_to_srt(
            input_path=input_video,
            language=language,
            output=srt_path,
            model=model,
            overwrite=overwrite,
            log=spin.say,
            show_bar=False,
        )

    # Fast mode (--skip-autocut): transcript (+ screenshots) only. No auto-cut,
    # no subtitle burn, no timecodes — nothing is re-encoded.
    if skip_autocut:
        shots_summary = None
        if screenshots:
            _banner(f"2/{steps} screenshots", f"one frame every {screenshot_interval:g}s")
            with Spinner("Screenshots · extracting frames") as spin:
                shots_summary = _capture_screenshots(
                    input_video, screenshots_dir,
                    interval=screenshot_interval, progress_cb=spin.set_progress,
                )
        elapsed = time.time() - t0
        summary = {
            "output_dir": os.path.abspath(video_dir),
            "final_video": None,
            "final_subtitles": None,
            "cut_video": None,
            "transcript": os.path.abspath(srt_path),
            "timecodes_md": None,
            "screenshots": shots_summary["directory"] if shots_summary else None,
            "screenshot_count": shots_summary["count"] if shots_summary else 0,
            "cues_rendered": 0,
            "style": None,
            "elapsed_seconds": round(elapsed, 1),
        }
        print("\n=== DONE ===")
        print(f"Transcript  : {summary['transcript']}")
        if shots_summary:
            print(f"Screenshots : {shots_summary['directory']} ({shots_summary['count']} frames)")
        print(f"Time        : {summary['elapsed_seconds']}s")
        return summary

    # 2) Auto-cut no-speech gaps -----------------------------------------
    if do_autocut:
        _banner(f"2/{steps} auto-cut", "removing no-speech gaps")
        with Spinner("Auto-cut · re-encoding with FFmpeg (VideoToolbox)") as spin:
            cut = autocut_by_subtitles(
                input_video=input_video,
                srt_path=srt_path,
                output_video=cut_video,
                output_srt=final_srt,
                pad=pad,
                min_gap=min_gap,
                progress_cb=spin.set_progress,
            )
        print(
            f"kept {cut['segments_kept']} segments, "
            f"removed {cut['removed_seconds']}s "
            f"({cut['original_duration']}s -> {cut['cut_duration']}s)"
        )
        overlay_input_video = cut["output_video"]
        overlay_input_srt = cut["output_srt"]
    else:
        _banner(f"2/{steps} auto-cut", "skipped (--no-autocut)")
        overlay_input_video = input_video
        overlay_input_srt = srt_path

    # 3) Overlay pop-up subtitles ----------------------------------------
    _banner(f"3/{steps} overlay", f"style={style}" + (f", preview {trim_seconds}s" if trim_seconds else ""))
    with Spinner(f"Burning subtitles · style={style}") as spin:
        result = overlay_subtitles(
            input_video=overlay_input_video,
            srt_path=overlay_input_srt,
            output_video=final_video,
            style_preset=style,
            trim_seconds=trim_seconds,
            progress_cb=spin.set_progress,
        )

    elapsed = time.time() - t0

    # 4) Timecodes — LLM sub-agent over the .srt matching the FINAL video --
    tc_summary = None
    if timecodes:
        final_subs = final_srt if do_autocut else srt_path
        timecodes_md = os.path.join(video_dir, f"{stem}_final_timecodes.md")
        _banner(f"4/{steps} timecodes", "picking key moments (Nebius sub-agent)")
        with Spinner("Timecodes · Nebius sub-agent") as spin:
            tc_summary = generate_timecodes(final_subs, timecodes_md, log=spin.say)

    # 5) Screenshots — one JPEG from the INPUT video every `interval` seconds
    shots_summary = None
    if screenshots:
        _banner(f"{steps}/{steps} screenshots", f"one frame every {screenshot_interval:g}s")
        with Spinner("Screenshots · extracting frames") as spin:
            shots_summary = _capture_screenshots(
                input_video, screenshots_dir,
                interval=screenshot_interval, progress_cb=spin.set_progress,
            )

    # Subtitles are burned into the final video, so the re-timed sidecar .srt
    # would only make players draw every caption twice (same-basename
    # auto-load). Remove it unless explicitly kept for downstream tools.
    final_subs_kept = True
    if do_autocut and not keep_srt:
        try:
            os.remove(final_srt)
            final_subs_kept = False
        except OSError:
            pass

    # Which .srt (if any) matches the final video's timeline and survives?
    if do_autocut:
        final_subs_path = os.path.abspath(final_srt) if final_subs_kept else None
    else:
        final_subs_path = os.path.abspath(srt_path)  # transcript = final timeline

    summary = {
        "output_dir": os.path.abspath(video_dir),
        "final_video": result["output_video"],
        "final_subtitles": final_subs_path,
        "cut_video": os.path.abspath(cut_video) if do_autocut else None,
        "transcript": os.path.abspath(srt_path),
        "timecodes_md": tc_summary["output_path"] if tc_summary else None,
        "screenshots": shots_summary["directory"] if shots_summary else None,
        "screenshot_count": shots_summary["count"] if shots_summary else 0,
        "cues_rendered": result["cues_rendered"],
        "style": style,
        "elapsed_seconds": round(elapsed, 1),
    }
    print("\n=== DONE ===")
    print(f"Final video : {summary['final_video']}")
    if summary["final_subtitles"]:
        print(f"Subtitles   : {summary['final_subtitles']}")
    else:
        print("Subtitles   : burned in (no sidecar .srt)")
    if do_autocut:
        print(f"Cut video   : {summary['cut_video']}")
        print(f"Transcript  : {summary['transcript']}")
    if tc_summary:
        print(f"Timecodes   : {tc_summary['output_path']} ({tc_summary['count']} moments)")
    if shots_summary:
        print(f"Screenshots : {shots_summary['directory']} ({shots_summary['count']} frames)")
    print(f"Style       : {style}   Cues: {summary['cues_rendered']}   Time: {summary['elapsed_seconds']}s")
    return summary


def main() -> None:
    p = argparse.ArgumentParser(
        description="Video -> subtitles -> auto-cut -> styled pop-up overlay (one command).",
    )
    p.add_argument("--input", "-i", required=True, help="Input video file.")
    p.add_argument("--output-dir", "-o", default="output", help="Directory for all artifacts (default: output).")
    p.add_argument("--language", "-l", default=None, choices=sorted(SUPPORTED_LANGUAGES),
                   help="Spoken language: en or ru (default: en; asked interactively if omitted).")
    p.add_argument("--style", "-s", default=None, choices=STYLES,
                   help="Subtitle style preset (default: designer; asked interactively if omitted).")
    p.add_argument("--no-autocut", action="store_true", help="Skip the silence auto-cut stage.")
    p.add_argument("--skip-autocut", action="store_true",
                   help="Fast mode: generate only the transcript .srt and screenshots/ "
                        "(no auto-cut, no subtitle burn, no timecodes — nothing is re-encoded).")
    p.add_argument("--no-timecodes", action="store_true",
                   help="Skip the key-moments timecode agent (stage 4).")
    p.add_argument("--keep-srt", action="store_true",
                   help="Keep the re-timed <stem>_final.srt next to the final video "
                        "(by default it is removed: subtitles are burned in and players "
                        "auto-load same-basename .srt files, showing captions twice).")
    p.add_argument("--no-screenshots", action="store_true",
                   help="Skip the input-video screenshots (stage 5).")
    p.add_argument("--screenshot-interval", type=float, default=2.0, metavar="SECONDS",
                   help="Seconds between screenshots of the input video (default: 2).")
    p.add_argument("--trim", type=float, default=None, metavar="SECONDS",
                   help="Render only the first N seconds (fast preview).")
    p.add_argument("--model", "-m", default=DEFAULT_MODEL, help="MLX Whisper model repo id.")
    p.add_argument("--pad", type=float, default=0.25, help="Auto-cut: seconds kept around each cue.")
    p.add_argument("--min-gap", type=float, default=0.7, help="Auto-cut: only gaps this long or longer are removed.")
    p.add_argument("--overwrite", action="store_true", help="Re-transcribe even if the .srt exists.")
    args = p.parse_args()

    language = args.language
    if language is None:
        language = _prompt_choice("Spoken language", sorted(SUPPORTED_LANGUAGES), default="en")
    style = args.style
    if style is None and not args.skip_autocut:  # no burn in fast mode -> no style needed
        style = _prompt_choice("Subtitle style", STYLES, default="designer")

    try:
        run_pipeline(
            input_video=args.input,
            output_dir=args.output_dir,
            language=language,
            style=style,
            do_autocut=not args.no_autocut,
            trim_seconds=args.trim,
            model=args.model,
            pad=args.pad,
            min_gap=args.min_gap,
            overwrite=args.overwrite,
            timecodes=not args.no_timecodes,
            keep_srt=args.keep_srt,
            screenshots=not args.no_screenshots,
            screenshot_interval=args.screenshot_interval,
            skip_autocut=args.skip_autocut,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
