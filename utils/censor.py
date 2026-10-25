"""Regex pre-processing: mask profanity in user text before it reaches the LLM."""
from __future__ import annotations

import re

_ROOTS = (
    # English
    r"fuck\w*?", r"shit\w*?", r"bitch\w*?", r"asshole", r"bastard", r"dick(?:head)?", r"cunt", r"wtf", r"stfu",
    # Vietnamese
    r"đệt", r"địt", r"đụ", r"đéo", r"đ[mc]m?", r"dcm", r"dm", r"vcl", r"vkl", r"vl", r"cc", r"cặc", r"lồn",
    r"buồi", r"đĩ", r"đm", r"clgt", r"cl",
)
_SUFFIXES = r"(?:ing|ed|er|ers|s|in)"
PROFANITY_RE = re.compile(
    rf"(?<!\w)((?:{'|'.join(_ROOTS)}))({_SUFFIXES})?(?!\w)", re.IGNORECASE | re.UNICODE
)
_SIMPLE_ROOT = re.compile(r"^(fuck|shit|bitch)", re.IGNORECASE)


def _mask(m: re.Match) -> str:
    word, suffix = m.group(1), m.group(2) or ""
    # fucking -> f***ing, đệt -> đ***, vcl -> v***
    if _SIMPLE_ROOT.match(word) and not suffix:
        suffix = word[len(_SIMPLE_ROOT.match(word).group(1)):]
        suffix = suffix if suffix.lower() in {"ing", "ed", "er", "ers", "s", "in"} else ""
    return f"{word[0]}***{suffix}"


def censor(text: str) -> str:
    """Replace profanity with a masked form (first letter + '***')."""
    return PROFANITY_RE.sub(_mask, text)
