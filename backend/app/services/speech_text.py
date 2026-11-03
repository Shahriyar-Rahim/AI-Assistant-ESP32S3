"""Turns model output into plain text that is safe to speak and to show on the small display."""

from __future__ import annotations

import re
import unicodedata

_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_CITATION = re.compile(r"\[\d{1,3}\]|\[citation needed\]", re.I)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s*(?:[-*•–—]|\d{1,2}[.)])\s+", re.M)
_EMPHASIS = re.compile(r"[*_~`^|<>{}\\]+")
_PARENS_ONLY_SYMBOLS = re.compile(r"\(\s*\)")
_EMOJI_RANGES = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]")


def clean_for_speech(text: str, limit: int = 600) -> str:
    """Removes markdown, links, citations, emoji and symbol runs; keeps sentences readable aloud."""
    text = _CODE_BLOCK.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    text = _CITATION.sub("", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    text = _EMOJI_RANGES.sub("", text)
    text = _EMPHASIS.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("&", " and ").replace("@", " at ").replace("=", " equals ")
    text = text.replace(";", ",").replace(":", ",").replace("—", ", ").replace("–", ", ")
    text = re.sub(r"\s*\n+\s*", ". ", text)
    text = _PARENS_ONLY_SYMBOLS.sub("", text)
    text = re.sub(r"[^\w\s.,!?'%$°/+-]", " ", text)
    text = re.sub(r"([.,!?])(?:\s*[.,])+", r"\1", text)
    text = re.sub(r"\s+([.,!?])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" .,")
    if not text:
        return ""
    if len(text) > limit:
        cut = text[:limit]
        stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[: stop + 1] if stop > limit // 2 else cut.rsplit(" ", 1)[0]
    return text if text[-1] in ".!?" else text + "."
