"""Cut "phrase + translation" cues from a recording into mp3 chunks.

Scans an .srt for cues whose text is a PHRASE followed by its TRANSLATION —
one side Hungarian (recognised by its diacritics: á é í ó ö ő ú ü ű), the
other plain English/Latin, in either order, separated by a dash / colon /
slash / equals delimiter or by a sentence boundary:

    Jó reggelt — Good morning
    Good morning. Jó reggelt.
    Ház / house

Each such cue is cut from the source media (auto-detected next to the .srt,
or passed via --media) into {output}/audio_chunks/NNN_<slug>.mp3, and
{output}/chunks.md lists every phrase with its translation and a link to its
audio.

    .venv/bin/python scripts/chunk_phrases.py --input ~/Documents/hungarian/hungarian_1.srt
    .venv/bin/python scripts/chunk_phrases.py --input in.srt --output outdir --media video.mp4

Restore-safe: an mp3 that already exists is not re-cut unless --overwrite is
passed; chunks.md is always regenerated (it is local and cheap).
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
from pathlib import Path

from common.progress import Spinner
from common.text import format_mmss as _mmss
from common.text import slugify as _slug
from ffmpeg_skill.core import parse_srt, run_ffmpeg

_HUNGARIAN = set("áéíóöőúüűÁÉÍÓÖŐÚÜŰ")
# A delimiter counts only when surrounded by whitespace, so hyphenated words
# ("state-of-the-art") and clock times ("10:30") never split a phrase. The
# colon is the exception: it hugs the word in real usage ("Szeretlek: I love
# you"), so it only needs a space AFTER it.
_DELIM_RE = re.compile(r"\s+[-—–|/=+]+\s+|:\s+")
_SENT_RE = re.compile(r"^(.+?[.!?])\s+(.+)$")

_MIN_CHUNK = 0.4   # seconds; shorter cues are noise, not phrases
_MAX_CHUNK = 30.0  # seconds; a phrase + translation never runs longer
_MEDIA_EXTS = (".mp3", ".m4a", ".wav", ".aac", ".flac", ".mp4", ".mov", ".mkv", ".avi")


def _hu_chars(text: str) -> int:
    return sum(ch in _HUNGARIAN for ch in text)


def _letters(text: str) -> int:
    return sum(ch.isalpha() for ch in text)


def _split_pair(text: str):
    """Return (phrase, translation) when the text is 'phrase + translation'.

    Accepted shapes: two parts around a spaced delimiter, or two sentences.
    One side Hungarian (recognised by its diacritics: á é í ó ö ő ú ü ű) and
    the other plain English/Latin -> the Hungarian side is the phrase. A
    delimiter pair with BOTH sides plain is also accepted when both parts
    look like short phrases (<= 6 words each) — Hungarian often writes without
    diacritics ("Az alma: The apple"); the first side is then the phrase.
    Sentence pairs always need a Hungarian side, so ordinary consecutive
    English sentences never pair up.
    """
    text = " ".join(text.replace("\n", " ").split())
    if not 4 <= len(text) <= 160:
        return None

    parts = None
    m = _DELIM_RE.search(text)
    if m:
        parts = (text[: m.start()], text[m.end():])
    else:
        m2 = _SENT_RE.match(text)
        if m2:
            parts = (m2.group(1), m2.group(2))
    if not parts:
        return None

    a, b = parts
    if _letters(a) < 2 or _letters(b) < 2:
        return None
    ha, hb = _hu_chars(a) > 0, _hu_chars(b) > 0
    if ha and hb:
        return None  # both sides accented: ambiguous, not a clean pair
    if ha:
        return a.strip(), b.strip()
    if hb:
        return b.strip(), a.strip()
    # both sides plain Latin: accept only explicit delimiters, short sides,
    # and not near-duplicates ("RAG - retrieval augmented generation" style).
    if m is None:
        return None
    wa, wb = a.lower().split(), b.lower().split()
    if len(wa) > 6 or len(wb) > 6:
        return None
    if set(wa) & set(wb):
        return None
    return a.strip(), b.strip()


def _find_media(srt_path: Path, media=None) -> Path:
    """The media carrying the audio: explicit --media, or the same-stem file
    next to the .srt (the pipeline's extract_audio leaves <video>.mp3 there)."""
    if media:
        p = Path(media).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"Media file not found: {p}")
        return p
    stem = str(srt_path.with_suffix(""))
    for ext in _MEDIA_EXTS:
        candidate = Path(stem + ext)
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"No media found next to {srt_path.name} (tried {', '.join(_MEDIA_EXTS)}); pass --media."
    )


def _cut_chunk(media: Path, start: float, end: float, out_path: Path) -> None:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(media),
        "-vn", "-c:a", "libmp3lame", "-q:a", "4",
        str(out_path),
    ]
    run_ffmpeg(cmd, label="ffmpeg audio chunk")


def run_chunk_phrases(
    input_srt: str,
    output_dir: str | None = None,
    media: str | None = None,
    overwrite: bool = False,
    log=print,
    on_chunk=None,
) -> dict:
    """Cut every 'phrase + translation' cue into {output}/audio_chunks/*.mp3
    and write {output}/chunks.md with the phrase list."""
    srt_path = Path(input_srt).expanduser()
    if not srt_path.is_file():
        raise FileNotFoundError(f"SRT file not found: {srt_path}")
    cues = parse_srt(str(srt_path))
    if not cues:
        raise ValueError(f"No cue entries found in {srt_path}")

    out_dir = Path(output_dir).expanduser() if output_dir else srt_path.parent
    chunks_dir = out_dir / "audio_chunks"
    os.makedirs(chunks_dir, exist_ok=True)
    media_path = _find_media(srt_path, media)

    pairs = []
    for cue in cues:
        pair = _split_pair(cue.text)
        if pair is None:
            continue
        if not _MIN_CHUNK <= cue.end - cue.start <= _MAX_CHUNK:
            continue
        pairs.append((cue, pair[0], pair[1]))
    if not pairs:
        raise ValueError(
            f"No 'phrase + translation' cues found among {len(cues)} cues "
            "(looked for one Hungarian side + one English/Latin side)."
        )
    log(f"Found {len(pairs)} phrase chunk(s) in {len(cues)} cues; cutting from {media_path.name}")

    cut, skipped = 0, 0
    rows = []
    for i, (cue, phrase, translation) in enumerate(pairs, start=1):
        if on_chunk is not None:
            on_chunk(i, len(pairs), phrase)
        name = f"{i:03d}_{_slug(phrase)}.mp3"
        chunk_path = chunks_dir / name
        if chunk_path.exists() and not overwrite:
            skipped += 1  # restore mode: existing mp3s are not re-cut
        else:
            _cut_chunk(media_path, cue.start, cue.end, chunk_path)
            cut += 1
        rows.append((i, cue.start, phrase, translation, name))

    md_lines = [
        f"# Phrase chunks — {media_path.stem}",
        "",
        f"Source: `{media_path.name}` · srt: `{srt_path.name}` · {len(pairs)} chunk(s)",
        "",
        "| # | Start | Phrase | Translation | Audio |",
        "|---|-------|--------|-------------|-------|",
    ]
    for i, start, phrase, translation, name in rows:
        md_lines.append(
            f"| {i} | {_mmss(start)} | {phrase} | {translation} | [mp3](audio_chunks/{name}) |"
        )
    md_path = out_dir / "chunks.md"
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    return {
        "chunks_md": str(md_path),
        "audio_chunks_dir": str(chunks_dir),
        "chunks": len(pairs),
        "cut": cut,
        "skipped_existing": skipped,
        "media": str(media_path),
    }


def main() -> None:
    p = argparse.ArgumentParser(
        description="Cut 'phrase + translation' cues (.srt) into mp3 chunks + chunks.md.",
    )
    p.add_argument("--input", "-i", required=True, help="The .srt file to scan.")
    p.add_argument("--output", "-o", default=None,
                   help="Output dir for audio_chunks/ and chunks.md (default: the .srt's dir).")
    p.add_argument("--media", "-m", default=None,
                   help="Source video/audio to cut the mp3s from "
                        "(default: same-stem media file next to the .srt).")
    p.add_argument("--overwrite", action="store_true", help="Re-cut mp3s that already exist.")
    args = p.parse_args()

    try:
        with Spinner("Chunks · cutting phrases") as spin:
            result = run_chunk_phrases(
                input_srt=args.input,
                output_dir=args.output,
                media=args.media,
                overwrite=args.overwrite,
                log=spin.say,
                on_chunk=lambda i, n, phrase: spin.update(f"Chunk {i}/{n} · {phrase}"),
            )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\n=== DONE ===")
    print(f"Chunks     : {result['chunks']} (cut {result['cut']}"
          + (f", skipped {result['skipped_existing']} existing" if result["skipped_existing"] else "") + ")")
    print(f"Audio dir  : {result['audio_chunks_dir']}")
    print(f"List       : {result['chunks_md']}")


if __name__ == "__main__":
    main()
