"""Unified LLM client factory for Nebius and Vision endpoints."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_nebius_client: OpenAI | None = None
_nebius_model: str | None = None

_vision_client: OpenAI | None = None
_vision_model: str | None = None


def get_nebius_client() -> tuple[OpenAI, str]:
    """Return a shared OpenAI client configured for the Nebius LLM endpoint."""
    global _nebius_client, _nebius_model
    if _nebius_client is not None and _nebius_model is not None:
        return _nebius_client, _nebius_model

    load_dotenv()
    _nebius_model = os.getenv("NEBIUS_MODEL", "zai-org/GLM-5.3-Flash")
    _nebius_client = OpenAI(
        base_url=os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/"),
        api_key=os.environ["NEBIUS_API_KEY"],
    )
    return _nebius_client, _nebius_model


def get_vision_client() -> tuple[OpenAI, str]:
    """Return an OpenAI client configured for the Vision model endpoint."""
    global _vision_client, _vision_model
    if _vision_client is not None and _vision_model is not None:
        return _vision_client, _vision_model

    load_dotenv()
    api_key = os.getenv("VISION_API_KEY") or os.environ.get("NEBIUS_API_KEY", "")
    base_url = os.getenv("VISION_BASE_URL") or os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/")
    _vision_model = os.getenv("VISION_MODEL", "moonshotai/Kimi-K2.6")
    _vision_client = OpenAI(base_url=base_url, api_key=api_key)
    return _vision_client, _vision_model

