"""Regex pre-processing: mask profanity in user text before it reaches the LLM."""
from __future__ import annotations

import re
import unicodedata

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


# --- output safety net: hide the vowel of any profanity the model forgot to mask -------------
_OUT_ROOTS = _ROOTS + (r"mẹ",)
_OUT_SUFFIXES = r"(?:ing|ed|er|ers|s|in)"
OUTPUT_PROFANITY_RE = re.compile(
    rf"(?<![\w*])((?:{'|'.join(_OUT_ROOTS)}))({_OUT_SUFFIXES})?(?![\w*])", re.IGNORECASE | re.UNICODE
)
_VOWELS = set("aeiou")


def _is_vowel(ch: str) -> bool:
    return unicodedata.normalize("NFD", ch)[0].lower() in _VOWELS


def mask_vowel(word: str) -> str:
    """fuck -> f*ck, đéo -> đ*o, lồn -> l*n, mẹ -> m*. No vowel (vcl): keep first letter, star the rest."""
    for i, ch in enumerate(word):
        if _is_vowel(ch):
            return word[:i] + "*" + word[i + 1:]  # one star per hidden character
    return word[0] + "*" * (len(word) - 1)


def censor_output(text: str) -> str:
    """Post-process bot output: mask unmasked profanity by hiding its vowel (already-starred words are left alone)."""
    return OUTPUT_PROFANITY_RE.sub(lambda m: mask_vowel(m.group(0)), text)


def escape_markdown_stars(text: str) -> str:
    """Escape every '*' so censor masks (f*ck, đ***) render literally instead of triggering Discord bold/italic."""
    return re.sub(r"(?<!\\)\*", r"\\*", text)


def safe_output(text: str) -> str:
    """censor_output + markdown-safe stars; use this right before sending to Discord."""
    return escape_markdown_stars(censor_output(text))
