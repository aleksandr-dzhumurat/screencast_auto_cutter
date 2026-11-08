"""Design sub-agent: backward-compatible wrapper around agents.design."""

from agents.design import (
    DESIGN_SYSTEM_PROMPT,
    RGB,
    RGBA,
    Palette,
    suggest_palette,
)

__all__ = ["DESIGN_SYSTEM_PROMPT", "RGB", "RGBA", "Palette", "suggest_palette"]
