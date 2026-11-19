"""Légende TikTok prête à copier/coller : titre, phrase descriptive (SEO), hashtags."""

from __future__ import annotations

_MAX_CAPTION = 2200  # limite TikTok


def build_caption(hook: str, hashtags: list[str], description: str = "") -> str:
    tags = " ".join(h if h.startswith("#") else f"#{h}" for h in hashtags)
    body = hook.strip()
    if description and description.strip().lower() != body.lower():
        body = f"{body}\n{description.strip()}"
    caption = f"{body}\n\n{tags}".strip() if tags else body
    return caption[:_MAX_CAPTION]
