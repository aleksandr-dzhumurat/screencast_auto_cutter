"""Generate an .srt subtitle file from a video/audio file using Whisper.

Apple Silicon oriented: uses `mlx_whisper` (MLX backend) for fast local
transcription, mirroring the approach in the reference implementation
(https://github.com/aleksandr-dzhumurat/mindbase_layer, src/.../utils/media.py).

Pipeline:
  1. If the input is a video (or non-mp3 audio), extract audio to mp3 with ffmpeg.
  2. Transcribe the audio with Whisper in the requested language (`ru` or `en`).
  3. Write a SubRip (.srt) file that the overlay agent can consume.

Usage:
    .venv/bin/python scripts/transcribe.py --input input/video1772263817.mp4 --language en
    .venv/bin/python scripts/transcribe.py --input input/video1772263817.mp4 --language ru \
        --output input/subtitles.srt

The produced .srt is compatible with `ffmpeg_skill.overlay_subtitles` and the
LangGraph agent in `agent.py`.
"""

from __future__ import annotations

# Running from a subdirectory: make the project root importable
# (docs/refactoring_plan.md) so `agents`, `common`, `ffmpeg_skill` and
# `scripts` resolve no matter where the command is invoked from.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import subprocess
import sys
import unicodedata
from collections.abc import Callable
from pathlib import Path

try:
    import mlx_whisper
except ImportError:  # pragma: no cover - import guard
    mlx_whisper = None

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - optional dependency
    def tqdm(iterable, **_kwargs):  # type: ignore
        return iterable

# Default MLX Whisper model. "medium" balances quality and speed; use
# "mlx-community/whisper-large-v3" for best accuracy, "whisper-small" for speed.
DEFAULT_MODEL = "mlx-community/whisper-medium"

SUPPORTED_LANGUAGES = {"ru", "en"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}


def _resolve_input(path: str) -> Path:
    """Resolve an input path, tolerating macOS NFC/NFD filename normalization."""
    candidate = Path(unicodedata.normalize("NFC", str(path)))
    if candidate.is_file():
        return candidate
    candidate = Path(unicodedata.normalize("NFD", str(path)))
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(f"Input file not found: {path}")


def extract_audio(input_file: Path, log: Callable[[str], None] = print) -> Path:
    """Extract/convert the input's audio track to mp3. Skips if it already exists."""
    output_file = input_file.with_suffix(".mp3")
    if output_file.is_file():
        log(f"Audio already exists: {output_file}")
        return output_file

    log(f"Extracting audio: {input_file.name} -> {output_file.name}")
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(input_file),
            "-vn", "-acodec", "libmp3lame", "-q:a", "2",
            "-y", str(output_file),
        ],
        check=True,
    )
    return output_file


def srt_timestamp(seconds: float) -> str:
    """Format seconds as an SRT timestamp: HH:MM:SS,mmm."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = round((seconds - int(seconds)) * 1000)
    if ms == 1000:  # rounding edge
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments, srt_path: Path, min_chars: int = 1, show_bar: bool = True) -> int:
    """Write Whisper segments to an .srt file. Returns the number of cues written."""
    idx = 1
    with open(srt_path, "w", encoding="utf-8") as fh:
        for seg in tqdm(segments, total=len(segments), desc="Writing .srt", disable=not show_bar):
            text = (seg.get("text") or "").strip()
            if len(text) < min_chars:
                continue
            fh.write(f"{idx}\n")
            fh.write(f"{srt_timestamp(seg['start'])} --> {srt_timestamp(seg['end'])}\n")
            fh.write(f"{text}\n\n")
            idx += 1
    return idx - 1


def transcribe(
    input_path: str,
    language: str = "en",
    output: str | None = None,
    model: str = DEFAULT_MODEL,
    min_chars: int = 1,
    overwrite: bool = False,
    log: Callable[[str], None] = print,
    show_bar: bool = True,
) -> Path:
    """Transcribe a video/audio file to an .srt subtitle file.

    Args:
        input_path: path to a video or audio file.
        language:   "ru" or "en".
        output:     destination .srt path (defaults to the input stem + .srt).
        model:      MLX Whisper model repo id.
        min_chars:  drop segments shorter than this many characters.
        overwrite:  re-transcribe even if the .srt already exists.
        log:        where informational messages go (the pipeline routes them
                    through its spinner; default: print).
        show_bar:   show the tqdm bar while writing the .srt (the pipeline
                    disables it in favour of its spinner).
    """
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"language must be one of {sorted(SUPPORTED_LANGUAGES)}, got {language!r}")

    source = _resolve_input(input_path)
    srt_path = Path(output) if output else source.with_suffix(".srt")
    srt_path.parent.mkdir(parents=True, exist_ok=True)

    if srt_path.is_file() and not overwrite:
        log(f"SRT already exists (use --overwrite to regenerate): {srt_path}")
        return srt_path

    # Extract audio if the input is a video or a non-mp3 audio file.
    if source.suffix.lower() in AUDIO_SUFFIXES and source.suffix.lower() == ".mp3":
        audio_path = source
    else:
        audio_path = extract_audio(source, log=log)

    if mlx_whisper is None:
        raise RuntimeError(
            "mlx_whisper is not installed. Install it with:\n"
            "    VIRTUAL_ENV=.venv uv pip install mlx-whisper"
        )

    log(f"Transcribing [{language}] with {model}")
    log("Loading model and processing audio (first run downloads the model)...")
    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=model,
        language=language,
    )

    count = write_srt(result["segments"], srt_path, min_chars=min_chars, show_bar=show_bar)
    log(f"Saved {count} subtitle cues to: {srt_path}")
    return srt_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an .srt from a video/audio file with Whisper.")
    parser.add_argument("--input", "-i", required=True, help="Path to the video or audio file.")
    parser.add_argument("--language", "-l", default="en", choices=sorted(SUPPORTED_LANGUAGES),
                        help="Spoken language of the audio (ru or en).")
    parser.add_argument("--output", "-o", default=None, help="Output .srt path (defaults next to the input).")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL, help="MLX Whisper model repo id.")
    parser.add_argument("--min-chars", type=int, default=1, help="Drop segments shorter than this many characters.")
    parser.add_argument("--overwrite", action="store_true", help="Regenerate the .srt even if it exists.")
    args = parser.parse_args()

    try:
        transcribe(
            input_path=args.input,
            language=args.language,
            output=args.output,
            model=args.model,
            min_chars=args.min_chars,
            overwrite=args.overwrite,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
