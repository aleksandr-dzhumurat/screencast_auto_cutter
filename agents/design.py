"""Design sub-agent: picks a subtitle colour palette from a shot's dominant colour."""

from __future__ import annotations

import json
import re
from functools import lru_cache

from common.client import get_nebius_client

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]
Palette = tuple[RGBA, RGBA, RGBA | None]  # (box, text, outline)

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
    "dominant hue, but keep it dark and muted so white text stays readable "
    "(think of a darkened, slightly desaturated version of the dominant colour).\n"
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

_DEFAULT_BOX: RGBA = (0, 0, 0, 160)
_DEFAULT_TEXT: RGBA = (245, 245, 245, 255)
_DEFAULT_OUTLINE: RGBA | None = (0, 0, 0, 120)


def _rel_luminance(rgb: RGB) -> float:
    def chan(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(a: RGB, b: RGB) -> float:
    la, lb = _rel_luminance(a), _rel_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _enforce_legibility(box_rgb: RGB, text_rgb: RGB) -> RGB:
    """If text/box contrast is too low, snap text to near-white or near-black."""
    if _contrast_ratio(text_rgb, box_rgb) >= 4.5:
        return text_rgb
    white, black = (245, 245, 245), (20, 20, 20)
    return white if _contrast_ratio(white, box_rgb) >= _contrast_ratio(black, box_rgb) else black


def _deterministic_palette(dom: RGB) -> Palette:
    """Offline fallback: a darkened tint of the dominant colour + contrast text."""
    box_rgb = tuple(int(c * 0.32) for c in dom)
    text_rgb = _enforce_legibility(box_rgb, (245, 245, 245))
    outline = (0, 0, 0, 130) if text_rgb[0] > 128 else (255, 255, 255, 140)
    return (*box_rgb, 170), (*text_rgb, 255), outline


def _clamp(v, lo, hi) -> int:
    return max(lo, min(hi, int(v)))


def _parse_palette(raw: str, dom: RGB) -> Palette:
    """Parse the model's JSON, clamp values and enforce contrast."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in response")
    data = json.loads(match.group(0))

    box = data["box"]
    text = data["text"]
    box_rgb = (_clamp(box[0], 0, 255), _clamp(box[1], 0, 255), _clamp(box[2], 0, 255))
    box_alpha = _clamp(data.get("box_alpha", 165), 140, 190)
    text_rgb = (_clamp(text[0], 0, 255), _clamp(text[1], 0, 255), _clamp(text[2], 0, 255))
    text_rgb = _enforce_legibility(box_rgb, text_rgb)

    outline = data.get("outline")
    if outline is None:
        outline_rgba: RGBA | None = None
    else:
        outline_rgba = (
            _clamp(outline[0], 0, 255),
            _clamp(outline[1], 0, 255),
            _clamp(outline[2], 0, 255),
            140,
        )
    return (*box_rgb, box_alpha), (*text_rgb, 255), outline_rgba


@lru_cache(maxsize=256)
def _suggest_quantised(qr: int, qg: int, qb: int) -> Palette:
    """LLM call for a quantised colour bucket (cached). Falls back on any error."""
    dom = (qr, qg, qb)
    try:
        client, model = get_nebius_client()
        resp = client.chat.completions.create(
            model=model,
            temperature=0.4,
            messages=[
                {"role": "system", "content": DESIGN_SYSTEM_PROMPT},
                {"role": "user", "content": f"Dominant colour behind the subtitle: RGB{dom}."},
            ],
        )
        return _parse_palette(resp.choices[0].message.content or "", dom)
    except Exception as exc:  # noqa: BLE001 - never let design break rendering
        print(f"[design_agent] falling back to deterministic palette ({exc})")
        return _deterministic_palette(dom)


def suggest_palette(dominant_rgb: RGB) -> Palette:
    """Return a (box_rgba, text_rgba, outline_rgba) palette for a dominant colour."""
    qr = (dominant_rgb[0] // 16) * 16
    qg = (dominant_rgb[1] // 16) * 16
    qb = (dominant_rgb[2] // 16) * 16
    return _suggest_quantised(qr, qg, qb)


if __name__ == "__main__":
    for sample in [(30, 60, 120), (200, 180, 40), (120, 20, 20), (240, 240, 240)]:
        print(sample, "->", suggest_palette(sample))

