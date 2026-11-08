"""Pure-Python FFmpeg/FFprobe helpers for the ffmpeg-skill MCP server.

Design notes
------------
The Homebrew ffmpeg on this machine is built WITHOUT libass and WITHOUT
drawtext, so the classic `subtitles=...:force_style=...` and `drawtext`
recipes from the guide cannot run here. Instead we implement the
"overlay subtitles" idea directly:

  1. Parse an .srt file into timed cues.
  2. Rasterise each cue to a transparent PNG (rounded, semi-transparent
     box + centred text) with Pillow, sized relative to the video.
  3. Composite the cue PNGs onto full-frame transparent stills (one per
     interval of the subtitle timeline) and feed those stills to a SINGLE
     `overlay` filter through the concat demuxer, encoding with VideoToolbox.

One input per cue does NOT scale: ffmpeg exhausts its per-input
decoders/threads around a few hundred inputs (observed at ~370), so the
filtergraph is kept at a constant two inputs no matter how many cues the
.srt contains.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Hardware encoding (Apple Silicon). Quality is controlled by -q:v, not -crf.
# ---------------------------------------------------------------------------
VIDEO_ENCODER = "h264_videotoolbox"
VIDEO_QUALITY = "65"

# Candidate system fonts on macOS (Pillow loads the .ttf/.ttc directly, so we do
# not rely on fontconfig).
_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
]


@dataclass
class Cue:
    index: int
    start: float  # seconds
    end: float    # seconds
    text: str     # may contain newlines


# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------
def probe_video(path: str) -> dict:
    """Return basic info about a video: width, height, duration (seconds)."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Video not found: {path}")
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", path,
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    duration = None
    if data.get("format", {}).get("duration"):
        duration = float(data["format"]["duration"])
    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "duration": duration,
    }


# ---------------------------------------------------------------------------
# SRT parsing
# ---------------------------------------------------------------------------
_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def _ts_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def parse_srt(path: str) -> list[Cue]:
    """Parse a (subset of) SubRip .srt file into a list of Cue objects."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Subtitle file not found: {path}")
    with open(path, "r", encoding="utf-8-sig") as fh:
        content = fh.read()

    cues: list[Cue] = []
    # Split into blocks separated by blank lines.
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [ln.rstrip() for ln in block.splitlines() if ln.strip() != ""]
        if not lines:
            continue
        # Optional numeric index line.
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


# ---------------------------------------------------------------------------
# FFmpeg runner with optional progress reporting
# ---------------------------------------------------------------------------
def run_ffmpeg(
    cmd: list[str],
    total_seconds: float | None = None,
    progress_cb=None,
    label: str = "ffmpeg",
) -> None:
    """Run an ffmpeg command; optionally report encoding progress (0..1).

    When ``progress_cb`` is given, ffmpeg is started with ``-progress pipe:1``
    and every ``out_time_ms`` update is converted to a completion fraction
    (processed seconds / ``total_seconds``) and forwarded to the callback.
    Without a callback the behaviour is identical to the previous plain
    ``subprocess.run`` (used by the MCP server path).
    """
    if progress_cb is None:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"{label} failed:\n{proc.stderr[-2000:]}")
        return

    full_cmd = cmd[:1] + ["-progress", "pipe:1", "-nostdin"] + cmd[1:]
    # stderr goes to a file: reading only stdout would risk the pipe filling
    # up and blocking ffmpeg, but we still want the tail for error messages.
    with tempfile.TemporaryFile("w+") as err_fh:
        proc = subprocess.Popen(full_cmd, stdout=subprocess.PIPE, stderr=err_fh, text=True)
        try:
            for line in proc.stdout:
                if not line.startswith("out_time_ms=") or not total_seconds:
                    continue
                try:
                    done = int(line.split("=", 1)[1].strip()) / 1_000_000.0
                except ValueError:
                    continue
                progress_cb(max(0.0, min(1.0, done / total_seconds)))
            proc.wait()
        finally:
            if proc.poll() is None:  # caller bailed out mid-encode
                proc.kill()
                proc.wait()
        if proc.returncode != 0:
            err_fh.seek(0)
            raise RuntimeError(f"{label} failed:\n{err_fh.read()[-2000:]}")


# ---------------------------------------------------------------------------
# Subtitle rasterisation (Pillow)
# ---------------------------------------------------------------------------
def _find_font() -> str:
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    raise RuntimeError("No usable system font found for subtitle rendering.")


# Visual presets: (text RGBA, box RGBA, outline RGBA)
_PRESETS = {
    "modern_box": {
        "text": (255, 255, 255, 255),
        "box": (0, 0, 0, 153),      # black @ 0.6
        "outline": None,
        "font_scale": 0.042,
    },
    "youtube_pop": {
        "text": (255, 255, 0, 255),  # yellow
        "box": (0, 0, 0, 102),       # black @ 0.4
        "outline": (0, 0, 0, 255),
        "font_scale": 0.050,
    },
}


def _wrap_text(draw, text: str, font, max_width: int) -> str:
    """Word-wrap text (respecting explicit newlines) to fit max_width pixels."""
    from PIL import ImageDraw  # noqa: F401  (ensures Pillow present)

    wrapped_lines: list[str] = []
    for raw_line in text.split("\n"):
        words = raw_line.split()
        if not words:
            wrapped_lines.append("")
            continue
        line = words[0]
        for word in words[1:]:
            candidate = f"{line} {word}"
            width = draw.textbbox((0, 0), candidate, font=font)[2]
            if width <= max_width:
                line = candidate
            else:
                wrapped_lines.append(line)
                line = word
        wrapped_lines.append(line)
    return "\n".join(wrapped_lines)


def _luminance(rgb) -> float:
    """Perceived luminance of an (R, G, B) tuple on a 0-255 scale."""
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def _sample_dominant_color(
    video_path: str,
    t: float,
    vid_w: int,
    vid_h: int,
    margin_v: int = 40,
) -> tuple[int, int, int] | None:
    """Average colour of the frame region where the subtitle will sit, at time t.

    Grabs a single frame, crops the bottom-centre band behind the subtitle and
    downscales it to 1x1 so the lone pixel is the region's average colour.
    Returns None if sampling fails (the caller falls back to the default look).
    """
    cw = max(2, int(vid_w * 0.6))
    ch = max(2, int(vid_h * 0.18))
    x = max(0, (vid_w - cw) // 2)
    y = max(0, vid_h - ch - margin_v)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, t):.3f}", "-i", video_path,
        "-frames:v", "1",
        "-vf", f"crop={cw}:{ch}:{x}:{y},scale=1:1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True)
    data = proc.stdout
    if proc.returncode != 0 or len(data) < 3:
        return None
    return (data[0], data[1], data[2])


def _derive_adaptive_colors(dom: tuple[int, int, int]):
    """Turn a shot's dominant colour into a legible, harmonising subtitle palette.

    Keeps the box dark and semi-transparent (so text stays readable) but tinted
    with the shot's hue, and auto-picks white or dark text for contrast. This
    preserves the "dark box, light text" feel of the default while blending the
    box into each scene.

    Returns (box_rgba, text_rgba, outline_rgba).
    """
    # Darken the dominant colour toward black, keeping its hue -> tinted box.
    box_rgb = tuple(int(c * 0.32) for c in dom)
    box = (*box_rgb, 170)  # ~0.67 alpha
    if _luminance(box_rgb) > 140:
        text = (15, 15, 15, 255)
        outline = (255, 255, 255, 140)
    else:
        text = (255, 255, 255, 255)
        outline = (0, 0, 0, 130)
    return box, text, outline


def render_cue_png(
    text: str,
    video_width: int,
    video_height: int,
    out_path: str,
    style_preset: str = "modern_box",
    box_color=None,
    text_color=None,
    outline_color=None,
) -> tuple[int, int]:
    """Render a single subtitle cue to a transparent PNG.

    The optional ``box_color`` / ``text_color`` / ``outline_color`` RGBA tuples
    override the preset (used by the per-cue "adaptive" styling).

    Returns the (width, height) of the produced image.
    """
    from PIL import Image, ImageDraw, ImageFont

    preset = _PRESETS.get(style_preset, _PRESETS["modern_box"])
    box_fill = box_color if box_color is not None else preset["box"]
    text_fill = text_color if text_color is not None else preset["text"]
    outline_fill = outline_color if outline_color is not None else preset["outline"]
    font_path = _find_font()
    font_size = max(18, int(video_height * preset["font_scale"]))
    try:
        font = ImageFont.truetype(font_path, font_size)
    except OSError:
        font = ImageFont.load_default()

    pad_x = int(font_size * 0.8)
    pad_y = int(font_size * 0.5)
    radius = int(font_size * 0.5)
    # Keep the subtitle box within ~86% of the frame width.
    max_box_width = int(video_width * 0.86)
    max_text_width = max_box_width - 2 * pad_x

    # Measure on a scratch image.
    scratch = Image.new("RGBA", (10, 10))
    sdraw = ImageDraw.Draw(scratch)
    wrapped = _wrap_text(sdraw, text, font, max_text_width)

    # Measure the wrapped block.
    bbox = sdraw.multiline_textbbox((0, 0), wrapped, font=font, align="center", spacing=int(font_size * 0.25))
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    box_w = int(min(max_box_width, text_w + 2 * pad_x))
    box_h = int(text_h + 2 * pad_y)

    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, box_w - 1, box_h - 1], radius=radius, fill=box_fill)

    # Center the text block inside the box.
    tx = (box_w - text_w) / 2 - bbox[0]
    ty = (box_h - text_h) / 2 - bbox[1]
    draw.multiline_text(
        (tx, ty),
        wrapped,
        font=font,
        fill=text_fill,
        align="center",
        spacing=int(font_size * 0.25),
        stroke_width=2 if outline_fill else 0,
        stroke_fill=outline_fill,
    )

    img.save(out_path)
    return box_w, box_h


# ---------------------------------------------------------------------------
# Overlay subtitles onto a video
# ---------------------------------------------------------------------------
def overlay_subtitles(
    input_video: str,
    srt_path: str,
    output_video: str,
    style_preset: str = "modern_box",
    trim_seconds: float | None = None,
    margin_v: int = 40,
    progress_cb=None,
) -> dict:
    """Burn subtitles onto a video in a single overlay pass.

    Every cue is rasterised to a small transparent PNG (rounded box +
    centred text). The cue PNGs are then composited onto full-frame
    transparent stills -- one still per interval between cue start/end
    boundaries, where the set of visible cues is constant -- and the stills
    are played back at the right times via the concat demuxer. The whole
    burn therefore needs exactly one ffmpeg invocation with two inputs and
    one `overlay` filter, regardless of cue count.

    Parameters
    ----------
    input_video   : source video path.
    srt_path      : SubRip subtitle file.
    output_video  : destination path (re-encoded with VideoToolbox).
    style_preset  : "modern_box" (default, grey box + white text),
                    "youtube_pop", "adaptive" (box tinted per-shot to the
                    scene's dominant colour via a deterministic rule), or
                    "designer" (a Nebius design sub-agent picks the per-shot
                    palette using colour-pairing theory).
    trim_seconds  : if set, only render the first N seconds (fast preview).
    margin_v      : bottom margin in pixels for the subtitle box.
    progress_cb   : optional callable(fraction 0..1) fed from ffmpeg's
                    ``-progress`` output during the final encode (used by the
                    pipeline's spinner; the MCP path passes nothing).
    """
    from PIL import Image

    info = probe_video(input_video)
    vid_w, vid_h = info["width"], info["height"]

    cues = parse_srt(srt_path)
    if trim_seconds is not None:
        cues = [c for c in cues if c.start < trim_seconds]
    if not cues:
        raise ValueError("No subtitle cues to render in the requested range.")

    os.makedirs(os.path.dirname(os.path.abspath(output_video)) or ".", exist_ok=True)

    # Both "adaptive" and "designer" keep the modern_box geometry but recolour
    # the box per cue from the dominant colour of the shot behind it:
    #   * adaptive  - deterministic darkened tint (offline, no LLM).
    #   * designer  - a Nebius design sub-agent picks the palette using
    #                 colour-pairing theory (with the deterministic fallback).
    adaptive = style_preset in ("adaptive", "designer")
    base_preset = "modern_box" if adaptive else style_preset

    palette_fn = None
    if style_preset == "designer":
        from agents.design import suggest_palette as palette_fn  # lazy import

    tmpdir = tempfile.mkdtemp(prefix="ffmpeg_skill_subs_")
    written: list[str] = []
    try:
        # 1) Rasterise every cue to a small PNG with its final geometry.
        rendered: list[tuple[str, int, int, float, float]] = []
        for i, cue in enumerate(cues):
            png_path = os.path.join(tmpdir, f"cue_{i:04d}.png")
            written.append(png_path)
            box_color = text_color = outline_color = None
            if adaptive:
                # Sample near the middle of the cue's window for a representative colour.
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
            if end > cue.start:
                rendered.append((png_path, w, h, cue.start, end))

        if not rendered:
            raise ValueError("No subtitle cues with a positive duration in the requested range.")

        # 2) Build the subtitle timeline as full-frame stills. Inside an
        #    interval between consecutive cue boundaries the visible set of
        #    cues is constant, so one still per interval is exact.
        bounds = {0.0}
        for _, _, _, start, end in rendered:
            bounds.add(start)
            bounds.add(end)
        bounds = sorted(b for b in bounds if b >= 0)

        blank_path = os.path.join(tmpdir, "blank.png")
        written.append(blank_path)
        Image.new("RGBA", (vid_w, vid_h), (0, 0, 0, 0)).save(blank_path)

        frames: list[tuple[str, float]] = []  # (png path, hold duration)
        for b_start, b_end in itertools.pairwise(bounds):
            duration = b_end - b_start
            if duration <= 0:
                continue
            active = [
                (path, w, h) for path, w, h, start, end in rendered
                if start <= b_start < end
            ]
            if not active:
                frames.append((blank_path, duration))
                continue
            frame_path = os.path.join(tmpdir, f"frame_{len(frames):04d}.png")
            written.append(frame_path)
            canvas = Image.new("RGBA", (vid_w, vid_h), (0, 0, 0, 0))
            # Stack simultaneous cues from the bottom margin upwards.
            y_offset = 0
            for path, w, h in active:
                cue_img = Image.open(path)
                x = int((vid_w - w) / 2)
                y = max(0, vid_h - h - margin_v - y_offset)
                canvas.paste(cue_img, (x, y), cue_img)
                y_offset += h + int(margin_v * 0.5)
            canvas.save(frame_path)
            frames.append((frame_path, duration))

        if not frames:
            raise ValueError("Empty subtitle timeline.")

        # 3) Play the stills at the right times with the concat demuxer and
        #    composite them with a single overlay filter.
        list_path = os.path.join(tmpdir, "frames.ffconcat")
        written.append(list_path)
        with open(list_path, "w", encoding="utf-8") as fh:
            fh.write("ffconcat version 1.0\n")
            for frame_path, duration in frames:
                fh.write(f"file '{frame_path}'\nduration {duration:.3f}\n")
            # Trailing entry so the concat demuxer applies the last duration;
            # overlay's eof_action=pass makes whatever follows irrelevant.
            fh.write(f"file '{blank_path}'\n")

        cmd = [
            "ffmpeg", "-hide_banner",
            "-i", input_video,
            "-f", "concat", "-safe", "0", "-i", list_path,
        ]
        if trim_seconds is not None:
            cmd += ["-t", str(trim_seconds)]
        cmd += [
            "-filter_complex",
            "[0:v][1:v]overlay=eof_action=pass,format=yuv420p[outv]",
            "-map", "[outv]",
            "-map", "0:a?",
            "-c:v", VIDEO_ENCODER,
            "-q:v", VIDEO_QUALITY,
            "-c:a", "aac",
            "-movflags", "+faststart",
            "-y", output_video,
        ]

        encode_total = trim_seconds if trim_seconds is not None else info["duration"]
        run_ffmpeg(cmd, total_seconds=encode_total, progress_cb=progress_cb, label="ffmpeg")
    finally:
        # Clean up temporary PNGs.
        for path in written:
            try:
                os.remove(path)
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
