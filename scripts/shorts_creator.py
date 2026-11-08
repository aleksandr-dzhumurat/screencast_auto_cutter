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

# Running from a subdirectory: make the project root importable
# (docs/refactoring_plan.md) so `agents`, `common`, `ffmpeg_skill` and
# `scripts` resolve no matter where the command is invoked from.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import os
import sys

from common.progress import Spinner
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
    p.add_argument("--speed", default="auto",
                   help="Speaker speed-up: 'auto' (default) speeds up slow speech to ~160 wpm "
                        "(max 1.3x); an explicit 1.0-2.0 forces a factor (1.0 disables).")
    p.add_argument("--no-vertical", action="store_true", help="Keep the original aspect ratio (no 9:16 reframe).")
    p.add_argument("--no-subs", action="store_true", help="Do not burn subtitles into the clips.")
    p.add_argument("--keep-srt", action="store_true",
                   help="Keep the per-clip .srt next to each clip (by default it is removed: "
                        "subtitles are burned in and players auto-load same-basename .srt "
                        "files, showing captions twice).")
    args = p.parse_args()

    srt = args.srt or os.path.splitext(args.input)[0] + ".srt"
    if not os.path.isfile(args.input):
        print(f"ERROR: input video not found: {args.input}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(srt):
        print(f"ERROR: subtitle file not found: {srt} (pass --srt)", file=sys.stderr)
        sys.exit(1)

    try:
        with Spinner(f"Shorts · {args.count} x ~{args.duration:.0f}s · style={args.style}") as spin:
            result = make_shorts(
                input_video=args.input,
                srt_path=srt,
                output_dir=args.output_dir,
                target_seconds=args.duration,
                max_clips=args.count,
                vertical=not args.no_vertical,
                style=args.style,
                burn_subs=not args.no_subs,
                speed=args.speed,
                keep_srt=args.keep_srt,
                log=spin.say,
                progress_cb=spin.set_progress,
            )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\n=== DONE ===")
    print(f"Created {len(result['clips'])} clip(s) in {args.output_dir}")
    print(f"Manifest: {result['manifest_path']}")


if __name__ == "__main__":
    main()
