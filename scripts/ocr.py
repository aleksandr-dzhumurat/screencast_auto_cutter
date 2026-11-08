"""OCR the pipeline screenshots with a vision model.

For a pipeline output directory (`--input output/<video_name>`), finds the
`screenshots/` subdir and transcribes every image with a vision LLM (default:
`moonshotai/Kimi-K2.6`). Each transcription is written next to its screenshot
as `{SCREENSHOT_NAME}.md`, so `screenshots/00000001.jpg` becomes
`screenshots/00000001.md`.

    .venv/bin/python scripts/ocr.py --input output/video1772263817
    .venv/bin/python scripts/ocr.py --input output/video1772263817 --overwrite

Already-transcribed screenshots are skipped (pass --overwrite to redo them).
The directory may also be the screenshots dir itself.

For screenshots OF A BROWSER, pass --auto-crop: up to 10 random screenshots
are first sent to the vision model one by one to determine the main-content
rectangle (the embedded player window); the median answer is then used to
crop every screenshot (in memory — files are not modified) before OCR, so
browser chrome, sidebars and recommendation lists are excluded.

Configuration (.env): VISION_API_KEY (falls back to NEBIUS_API_KEY),
VISION_BASE_URL (defaults to the Nebius endpoint) and VISION_MODEL
(defaults to moonshotai/Kimi-K2.6).
"""

from __future__ import annotations

# Running from a subdirectory: make the project root importable
# (docs/refactoring_plan.md) so `agents`, `common`, `ffmpeg_skill` and
# `scripts` resolve no matter where the command is invoked from.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import base64
import io
import json
import os
import random
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI

from common.client import get_vision_client
from common.progress import Spinner

VISION_MODEL = "moonshotai/Kimi-K2.6"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
CROP_SAMPLES = 10
DEFAULT_PARALLEL = 5

OCR_PROMPT = (
    "Transcribe the text in this image exactly as it appears. "
    "The text may mix English and Hungarian, so do not translate or normalize anything. "
    "Preserve all Hungarian diacritics exactly: á, é, í, ó, ö, ő, ú, ü, ű "
    "(and their uppercase forms). Pay close attention to the difference between "
    "ö/ő and ü/ű, since the long forms use a double acute accent, not an umlaut. "
    "Preserve blanks and underscores as they are. Be precise: do not guess, "
    "autocorrect, or fill in missing letters, and do not replace a word with a "
    "similar-looking one from either language. If a character is unreadable, "
    "output [?] in its place. Output ONLY the transcribed text, as short and "
    "literal as possible, with no commentary, no explanation, and no analysis."
)

CROP_PROMPT = (
    "You are given a screenshot of a web browser page. Locate the rectangle of "
    "the MAIN CONTENT area: the embedded video player window. Exclude everything "
    "that is not the player: browser chrome (tabs, address bar, bookmarks bar), "
    "sidebars, recommendation or comments lists, and page background.\n\n"
    "The image is {width}x{height} pixels. Respond with ONLY a JSON object of "
    "this shape, in pixel coordinates of the original image:\n"
    '{{"x": <int>, "y": <int>, "width": <int>, "height": <int>}}\n'
    "(x, y) is the top-left corner of the player window."
)

def _get_client() -> OpenAI:
    client, _ = get_vision_client()
    return client


def _mime(suffix: str) -> str:
    return "image/png" if suffix.lower() == ".png" else "image/jpeg"


def _load_image_b64(image_path: Path, crop_rect=None) -> tuple:
    """Return (base64 data, mime) for the image, optionally cropped to a rect.

    The file on disk is never modified — the crop only affects what is sent
    to the model.
    """
    if crop_rect is None:
        return base64.b64encode(image_path.read_bytes()).decode("utf-8"), _mime(image_path.suffix)
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    x, y, w, h = crop_rect
    img = img.crop((x, y, x + w, y + h))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("utf-8"), "image/jpeg"


def ocr_image(image_path: Path, client: OpenAI, model: str, crop_rect=None) -> str:
    """Send one image (optionally pre-cropped) to the vision model."""
    image_data, mime = _load_image_b64(image_path, crop_rect)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{image_data}"},
                    },
                    {
                        "type": "text",
                        "text": OCR_PROMPT,
                    },
                ],
            }
        ],
        max_tokens=16384,
    )
    return (response.choices[0].message.content or "").strip()


def _detect_rect_one(image_path: Path, client: OpenAI, model: str):
    """Ask the vision model for the main-content (player) rectangle of one image."""
    from PIL import Image

    with Image.open(image_path) as im:
        width, height = im.size
    image_data, mime = _load_image_b64(image_path)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{image_data}"},
                    },
                    {
                        "type": "text",
                        "text": CROP_PROMPT.format(width=width, height=height),
                    },
                ],
            }
        ],
        max_tokens=16384,  # reasoning models can spend thousands of tokens thinking
    )
    raw = response.choices[0].message.content or ""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("no JSON in response")
    data = json.loads(match.group(0))
    x, y = int(data["x"]), int(data["y"])
    w, h = int(data["width"]), int(data["height"])
    # Clamp to the image bounds; reject degenerate rectangles.
    x = max(0, min(x, width - 1))
    y = max(0, min(y, height - 1))
    w = max(1, min(w, width - x))
    h = max(1, min(h, height - y))
    return (x, y, w, h)


def detect_content_rect(
    images: list,
    client: OpenAI,
    model: str,
    max_samples: int = CROP_SAMPLES,
    parallel: int = DEFAULT_PARALLEL,
    log=print,
    on_sample=None,
):
    """Determine the main-content (player) rectangle from up to 10 random shots.

    Samples are sent to the vision model individually, `parallel` requests at
    a time; the final rectangle is the per-coordinate MEDIAN of the answers,
    which is robust to a few bad guesses. Returns (x, y, w, h).
    """
    sample = list(images) if len(images) <= max_samples else random.sample(list(images), max_samples)
    rects = []
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futures = {
            pool.submit(_detect_rect_one, img, client, model): img
            for img in sample
        }
        for fut in as_completed(futures):
            img = futures[fut]
            completed += 1
            if on_sample is not None:
                on_sample(completed, len(sample), img.name)
            try:
                rects.append(fut.result())
            except Exception as exc:  # noqa: BLE001 - one bad sample must not kill detection
                log(f"[auto-crop] {img.name}: detection failed ({exc})")
    if len(rects) < 3:
        raise RuntimeError(
            f"Could not determine the main-content rectangle ({len(rects)}/{len(sample)} valid samples)"
        )
    rect = tuple(int(statistics.median(c)) for c in zip(*rects))
    log(f"[auto-crop] main-content rectangle from {len(rects)} samples: "
        f"x={rect[0]} y={rect[1]} w={rect[2]} h={rect[3]}")
    return rect


def find_screenshots_dir(input_dir: Path) -> Path:
    """Locate the screenshots dir: <input>/screenshots, or the input dir itself
    when it directly contains images."""
    input_dir = input_dir.expanduser()
    if input_dir.is_file():
        raise FileNotFoundError(
            f"--input must be a directory, got the file: {input_dir}\n"
            "Run pipeline.py on the video first (it creates screenshots/), then "
            "point --input at the pipeline output dir (or the screenshots dir)."
        )
    candidate = input_dir / "screenshots"
    if candidate.is_dir():
        return candidate
    if any(p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES for p in input_dir.iterdir()):
        return input_dir
    raise FileNotFoundError(
        f"No screenshots found: no 'screenshots' subdir and no images in {input_dir}"
    )


def run_ocr(
    input_dir: str,
    overwrite: bool = False,
    auto_crop: bool = False,
    parallel: int = DEFAULT_PARALLEL,
    log=print,
    on_image=None,
) -> dict:
    """Transcribe every screenshot into a same-named .md next to it.

    RESTORE MODE (default): a screenshot whose .md already exists is skipped
    WITHOUT any API call, so an interrupted run is resumed by simply running
    the same command again. `--overwrite` re-OCRs everything.

    `parallel` requests run concurrently (default 5; 1 = sequential). The
    per-image calls are independent, so results and failures are unaffected
    by the order in which they complete.

    With `auto_crop`, the detected rectangle is cached in
    `screenshots/crop_rect.json` and restored on the next run (delete that
    file to force re-detection; `--overwrite` reuses the cache too — the
    rectangle describes the page layout, not the OCR).

    A failing image is reported and does not abort the batch (its .md is not
    written, so a re-run retries it). `on_image(done, total, name)` is called
    per image so the CLI can drive its spinner.
    """
    parallel = max(1, int(parallel))
    shots_dir = find_screenshots_dir(Path(input_dir))
    images = sorted(p for p in shots_dir.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    if not images:
        raise ValueError(f"No screenshot images found in {shots_dir}")

    pending = [img for img in images if overwrite or not img.with_suffix(".md").exists()]
    existing = len(images) - len(pending)
    if not pending:
        log(f"Restore mode: all {len(images)} screenshot(s) already transcribed — "
            f"nothing to do, no API calls (use --overwrite to re-OCR).")
        return {
            "screenshots_dir": str(shots_dir),
            "images": len(images),
            "transcribed": 0,
            "skipped": len(images),
            "failed": [],
            "crop_rect": None,
            "restored": True,
            "parallel": parallel,
        }

    client = _get_client()
    model = os.getenv("VISION_MODEL", VISION_MODEL)
    todo = f"{len(pending)} to transcribe"
    if existing:
        todo += f", {existing} already exist (skipped, no API calls)"
    log(f"OCR {shots_dir}: {todo} — model {model}, {parallel} request(s) in parallel")

    crop_rect = None
    if auto_crop:
        cache_path = shots_dir / "crop_rect.json"
        if cache_path.is_file():
            crop_rect = tuple(json.loads(cache_path.read_text()))
            log(f"[auto-crop] restored cached rectangle x={crop_rect[0]} y={crop_rect[1]} "
                f"w={crop_rect[2]} h={crop_rect[3]} (delete crop_rect.json to re-detect)")
        else:
            crop_rect = detect_content_rect(
                images, client, model, parallel=parallel, log=log,
                on_sample=lambda i, n, name: on_image and on_image(i, n, f"crop-detect {name}"),
            )
            cache_path.write_text(json.dumps(crop_rect))

    done, failed = 0, []
    completed = 0
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {
            pool.submit(ocr_image, img, client, model, crop_rect): img
            for img in pending
        }
        for fut in as_completed(futures):
            img = futures[fut]
            completed += 1
            if on_image is not None:
                on_image(completed, len(pending), img.name)
            try:
                text = fut.result()
                img.with_suffix(".md").write_text(text + "\n", encoding="utf-8")
                done += 1
            except Exception as exc:  # noqa: BLE001 - one bad image must not kill the batch
                failed.append((img.name, str(exc)))
                log(f"[ocr] {img.name} failed: {exc}")

    if done == 0 and failed:
        raise RuntimeError(f"All {len(failed)} OCR calls failed; first error: {failed[0][1]}")

    return {
        "screenshots_dir": str(shots_dir),
        "images": len(images),
        "transcribed": done,
        "skipped": existing,
        "failed": failed,
        "crop_rect": crop_rect,
        "restored": False,
        "parallel": parallel,
    }


def _has_pending_ocr(input_dir: str, overwrite: bool) -> bool:
    """Would this run actually OCR anything? (Only then is the auto-crop
    question worth asking.)"""
    try:
        shots_dir = find_screenshots_dir(Path(input_dir))
    except (FileNotFoundError, NotADirectoryError):
        return True  # bad input: let run_ocr raise the proper error
    images = [p for p in shots_dir.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES]
    if not images:
        return True
    return overwrite or any(not p.with_suffix(".md").exists() for p in images)


def _ask_auto_crop() -> bool:
    """Interactive y/N question; defaults to No on empty input, EOF or no TTY."""
    try:
        if not sys.stdin.isatty():
            return False
        answer = input(
            "Screenshots of a browser page? Detect the player area and crop "
            "before OCR? [y/N] "
        ).strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes", "д", "да")


def main() -> None:
    p = argparse.ArgumentParser(
        description="OCR pipeline screenshots to {SCREENSHOT_NAME}.md files (vision model).",
    )
    p.add_argument("--input", "-i", required=True,
                   help="Pipeline output dir (must contain screenshots/), or the screenshots dir itself.")
    p.add_argument("--overwrite", action="store_true", help="Re-OCR even if the .md already exists.")
    p.add_argument("--auto-crop", action="store_true",
                   help="First send up to 10 random screenshots to the vision model (one by one) to "
                        "determine the main-content rectangle (the player window), then crop every "
                        "screenshot to that rectangle before OCR - excludes browser chrome and sidebars. "
                        "When omitted, you are asked interactively (y/N, default N) if there is OCR work to do.")
    p.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL, metavar="N",
                   help=f"How many vision requests run concurrently (default: {DEFAULT_PARALLEL}; 1 = sequential).")
    args = p.parse_args()

    # --auto-crop omitted? Ask (y/N, default N) when OCR work is pending.
    auto_crop = args.auto_crop
    if not auto_crop and _has_pending_ocr(args.input, args.overwrite):
        auto_crop = _ask_auto_crop()

    try:
        with Spinner("OCR · vision model") as spin:
            result = run_ocr(
                input_dir=args.input,
                overwrite=args.overwrite,
                auto_crop=auto_crop,
                parallel=args.parallel,
                log=spin.say,
                on_image=lambda done, total, name: spin.update(f"OCR {done}/{total} · {name}"),
            )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\n=== DONE ===")
    print(f"Directory   : {result['screenshots_dir']}")
    if result["crop_rect"]:
        x, y, w, h = result["crop_rect"]
        print(f"Auto-crop   : x={x} y={y} w={w} h={h}")
    print(f"Transcribed : {result['transcribed']} / {result['images']}"
          + (f" (skipped {result['skipped']} existing)" if result["skipped"] else ""))
    if result["failed"]:
        print(f"Failed      : {len(result['failed'])} (re-run to retry them)")


if __name__ == "__main__":
    main()
