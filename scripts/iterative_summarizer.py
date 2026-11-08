"""Iterative phrase summarizer over the OCR'd screenshots.

Walks the `{SCREENSHOT_NAME}.md` files produced by `ocr.py` (in chronological
order — the filenames are timestamps), keeps a running buffer of distinct
phrases, and asks one LLM prompt per file:

    * extract only the meaningful PHRASES from the OCR text,
    * compare them with the phrases already in the buffer,
    * return ONLY the ones that are new — if nothing is new, proceed to the
      next file.

The final buffer is written as a .md file (default
`<video_name>_phrases.md` next to the screenshots dir).

    .venv/bin/python scripts/iterative_summarizer.py --input output/<video_name>
    .venv/bin/python scripts/iterative_summarizer.py --input output/<video_name> --overwrite

Identical OCR texts (a slide that did not change between frames) are skipped
without an LLM call — "already exists" also applies to whole screenshots.
"""

from __future__ import annotations

# Running from a subdirectory: make the project root importable
# (docs/refactoring_plan.md) so `agents`, `common`, `ffmpeg_skill` and
# `scripts` resolve no matter where the command is invoked from.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import re
import sys
from pathlib import Path

from common.client import get_nebius_client as _get_client
from common.progress import Spinner

SUMMARY_SYSTEM_PROMPT = (
    "You maintain a running list of distinct phrases from OCR'd lecture "
    "screenshots. You are given the CURRENT BUFFER of phrases already "
    "collected and the OCR TEXT of the next screenshot.\n\n"
    "1. Extract only the meaningful PHRASES from the OCR text: key terms, "
    "titles, formulas, definitions, notable statements. Ignore OCR noise "
    "(timestamps, watermarks, page furniture) and ignore fragments of "
    "phrases already covered by a longer phrase in the buffer.\n"
    "2. Compare each phrase with the buffer: if the same phrase (or the "
    "same meaning) is already there, it is NOT new.\n"
    "3. Output ONLY the new phrases, one per line — no numbering, no "
    "bullets, no commentary.\n\n"
    "If nothing is new (or the OCR text has nothing meaningful), output "
    "exactly: NOTHING_NEW"
)


STRUCTURE_SYSTEM_PROMPT = (
    "You are a language-course editor. You are given a numbered list of "
    "phrases (Hungarian course material; some entries are Hungarian, some "
    "English, some both sides of a pair). Turn them into a study document.\n\n"
    "Rules:\n"
    "1. Group the phrases into THEMES (e.g. Greetings, Numbers, Food & Drink, "
    "Useful Expressions) and output one '# <Theme>' heading per group.\n"
    "2. Each phrase becomes a subsection heading: '## <n>. <Hungarian phrase>' "
    "(use the English phrase in the heading only when no Hungarian form "
    "exists for it).\n"
    "3. Under each heading, exactly three bullet lines:\n"
    "* origin (english): the English phrase\n"
    "* translation (Hungarian): the Hungarian phrase\n"
    "* translation (pronunciation): how to pronounce the Hungarian phrase, "
    "respelled for an English reader (e.g. 'yo REH-gelt'), stressed syllable "
    "in CAPS\n"
    "4. Pair the Hungarian and English versions of the same phrase together; "
    "keep every phrase from the list exactly once — do not invent new ones.\n"
    "5. Output ONLY the markdown document, starting directly with the first "
    "'# <Theme>' heading — no commentary, no code fences."
)

STRUCTURE_CHUNK = 30  # phrases per LLM call (keeps the prompt bounded)


def _clean_line(line: str) -> str:
    stripped = line.strip()
    for prefix in ("- ", "* ", "• "):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):].strip()
    return stripped


def extract_new_phrases(ocr_text: str, buffer_text: str, client, model) -> list[str]:
    """Ask the model which phrases of `ocr_text` are NOT in `buffer_text` yet."""
    user = (
        "CURRENT BUFFER:\n"
        f"{buffer_text if buffer_text.strip() else '(empty)'}\n\n"
        f"OCR TEXT:\n{ocr_text}"
    )
    resp = client.chat.completions.create(
        model=model,
        temperature=0.2,
        messages=[
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
    )
    raw = (resp.choices[0].message.content or "").strip()
    if not raw or raw.upper() == "NOTHING_NEW":
        return []
    phrases = [_clean_line(line) for line in raw.splitlines() if line.strip()]
    # Tolerate a chatty model: drop a stray NOTHING_NEW among real phrases.
    return [p for p in phrases if p.upper() != "NOTHING_NEW"]


def find_ocr_dir(input_dir: Path) -> Path:
    """Locate the dir with the OCR .md files: <input>/screenshots by default,
    or the input dir itself when it directly contains .md files."""
    candidate = input_dir / "screenshots"
    if candidate.is_dir():
        return candidate
    if any(p.is_file() and p.suffix == ".md" for p in input_dir.iterdir()):
        return input_dir
    raise FileNotFoundError(
        f"No OCR .md files found: no 'screenshots' subdir and no .md files in {input_dir}"
    )


def build_structured_phrases(
    phrases: list,
    client,
    model: str,
    chunk: int = STRUCTURE_CHUNK,
    log=print,
) -> str:
    """Turn the flat phrase buffer into a themed, structured study document.

    The buffer is sent to the LLM in chunks of `chunk` phrases (bounded
    prompt); each response is a set of '# <Theme>' sections. Chunks are
    independent, so themes may repeat across chunk boundaries for very large
    buffers.
    """
    parts = []
    for start in range(0, len(phrases), chunk):
        batch = phrases[start:start + chunk]
        # numbering continues across chunks so the final document reads as one list
        user = "Phrases:\n" + "\n".join(
            f"{j}. {p}" for j, p in enumerate(batch, start=start + 1)
        )
        resp = client.chat.completions.create(
            model=model,
            temperature=0.3,
            messages=[
                {"role": "system", "content": STRUCTURE_SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        text = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", text).strip()
        if text:
            parts.append(text)
        log(f"[structure] {min(start + len(batch), len(phrases))}/{len(phrases)} phrases structured")
    return "\n\n".join(parts)


def run_iterative_summary(
    input_dir: str,
    output: str | None = None,
    overwrite: bool = False,
    log=print,
    on_file=None,
) -> dict:
    """Build the deduplicated phrase buffer from the OCR .md files.

    Iterates the files in name order (timestamps sort chronologically),
    feeding each OCR text and the current buffer to one LLM prompt; only new
    phrases are appended. The buffer is finally written to `output`.

    LAST STEP — structured study document: the buffer is turned into
    `<output>_structured.md` (i.e. <stem>_phrases_structured.md) by one more
    LLM prompt per chunk of phrases: themes ('# <Theme>'), each phrase as a
    '## <n>. <phrase>' subsection with bullets for the English origin, the
    Hungarian translation and an English-respelled pronunciation. When the
    phrases file already exists, its content is used and the structured file
    is still generated if missing (restore-safe).

    RESTORE MODE: progress is checkpointed after every file
    (`<output>.progress.json` holds the processed files and the buffer), so
    an interrupted run resumes where it stopped — already-summarized files
    cost no API call. The checkpoint is removed once the final .md is
    written; `--overwrite` starts over from scratch.
    """
    ocr_dir = find_ocr_dir(Path(input_dir))
    video_dir = ocr_dir.parent if ocr_dir.name == "screenshots" else ocr_dir
    stem = video_dir.name
    out_path = Path(output) if output else video_dir / f"{stem}_phrases.md"
    progress_path = out_path.with_suffix(".progress.json")
    structured_path = out_path.with_name(out_path.stem + "_structured.md")

    client = None
    model = None

    def _client_and_model():
        nonlocal client, model
        if client is None:
            client, model = _get_client()
        return client, model

    if out_path.exists() and not overwrite:
        # Phrases already collected: reuse them (restore) and go straight to
        # the structured step, which may still be missing.
        buffer = [ln.strip() for ln in out_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        log(f"Phrases file already exists ({len(buffer)} phrases): {out_path} — using it")
        regenerated = False
        llm_calls, duplicates, restored, md_files = 0, 0, 0, []
    else:
        md_files = sorted(ocr_dir.glob("*.md"))
        if out_path.parent == ocr_dir:
            md_files = [p for p in md_files if p != out_path]
        if not md_files:
            raise ValueError(f"No .md files to summarize in {ocr_dir}")

        # Restore an interrupted run: reload the checkpoint (disk only, no API).
        processed: set[str] = set()
        buffer: list[str] = []
        if not overwrite and progress_path.is_file():
            try:
                data = json.loads(progress_path.read_text(encoding="utf-8"))
                processed = {str(n) for n in data.get("processed", [])}
                buffer = [str(p) for p in data.get("buffer", [])]
            except ValueError:
                processed, buffer = set(), []
            else:
                log(f"Restore mode: resuming after {len(processed)} summarized file(s), "
                    f"buffer already holds {len(buffer)} phrases.")
        # Rebuild the identical-text skip set from the already-processed files.
        seen_texts = set()
        for name in processed:
            p = ocr_dir / name
            if p.is_file():
                try:
                    seen_texts.add(p.read_text(encoding="utf-8").strip())
                except OSError:
                    pass

        client, model = _client_and_model()
        restored = len(processed)
        log(f"Summarizing {len(md_files)} OCR file(s) from {ocr_dir} with {model}"
            + (f" ({restored} already done, no API calls)" if restored else ""))

        def _checkpoint() -> None:
            progress_path.write_text(
                json.dumps({"processed": sorted(processed), "buffer": buffer}),
                encoding="utf-8",
            )

        llm_calls = 0
        duplicates = 0
        for i, md in enumerate(md_files, start=1):
            if on_file is not None:
                on_file(i, len(md_files), md.name, len(buffer))
            if md.name in processed:
                continue
            text = md.read_text(encoding="utf-8").strip()
            if not text or text in seen_texts:
                duplicates += 1
                processed.add(md.name)
                _checkpoint()
                continue
            seen_texts.add(text)
            try:
                new = extract_new_phrases(text, "\n".join(buffer), client, model)
            except Exception as exc:  # noqa: BLE001 - one bad file must not kill the run
                log(f"[summarizer] {md.name} failed: {exc}")
                continue  # not checkpointed: a re-run retries it
            llm_calls += 1
            if new:
                buffer.extend(new)
            processed.add(md.name)
            _checkpoint()

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(buffer) + ("\n" if buffer else ""), encoding="utf-8")
        progress_path.unlink(missing_ok=True)  # finished — nothing left to restore
        regenerated = True

    # Last step: structured study document from the buffer.
    structured_done = False
    if buffer and (overwrite or not structured_path.is_file()):
        client, model = _client_and_model()
        log(f"Structuring {len(buffer)} phrase(s) into {structured_path.name} with {model}")
        doc = build_structured_phrases(buffer, client, model, log=log)
        if doc:
            structured_path.write_text(doc + "\n", encoding="utf-8")
            structured_done = True
    elif structured_path.is_file():
        log(f"Structured file already exists: {structured_path}")

    return {
        "phrases_md": str(out_path),
        "structured_md": str(structured_path) if structured_done or structured_path.is_file() else None,
        "structured_new": structured_done,
        "regenerated": regenerated,
        "files": len(md_files),
        "llm_calls": llm_calls,
        "identical_skipped": duplicates,
        "restored": restored,
        "phrases": len(buffer),
    }


def main() -> None:
    p = argparse.ArgumentParser(
        description="Deduplicated phrase buffer (.md) from OCR'd screenshots, "
                    "plus a structured study document with themes and pronunciation.",
    )
    p.add_argument("--input", "-i", required=True,
                   help="Pipeline output dir (must contain screenshots/*.md), or the OCR dir itself.")
    p.add_argument("--output", "-o", default=None,
                   help="Destination .md for the phrase buffer "
                        "(default: <video_name>_phrases.md next to the screenshots).")
    p.add_argument("--overwrite", action="store_true", help="Regenerate even if the file exists.")
    args = p.parse_args()

    try:
        with Spinner("Phrases · iterative summarizer") as spin:
            result = run_iterative_summary(
                input_dir=args.input,
                output=args.output,
                overwrite=args.overwrite,
                log=spin.say,
                on_file=lambda i, n, name, total: spin.update(
                    f"Phrases {i}/{n} · {name} · buffer {total}"),
            )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\n=== DONE ===")
    if not result["regenerated"]:
        print(f"Phrases   : {result['phrases_md']} (kept existing)")
    else:
        print(f"Phrases   : {result['phrases_md']}")
        print(f"Files     : {result['files']}  LLM calls: {result['llm_calls']}"
              f"  identical skipped: {result['identical_skipped']}")
        if result.get("restored"):
            print(f"Restored  : {result['restored']} file(s) resumed without API calls")
        print(f"Phrases   : {result['phrases']}")
    if result["structured_md"]:
        status = "new" if result["structured_new"] else "kept existing"
        print(f"Structured: {result['structured_md']} ({status})")


if __name__ == "__main__":
    main()
