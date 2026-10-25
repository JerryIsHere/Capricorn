"""Data purifier: sanitise raw chat / OCR / subtitle text before it is stored in server_vibe.txt."""
from __future__ import annotations

import re

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
CUSTOM_EMOJI_RE = re.compile(r"<a?:[a-zA-Z0-9_]+:\d+>")
USER_TAG_RE = re.compile(r"<@!?\d+>")
REPEAT_RE = re.compile(r"(.)\1{2,}", re.DOTALL)
# Control tokens must never survive into the system prompt via learned data
CONTROL_TOKEN_RE = re.compile(r"\[\s*(?:SYS_CMD|SYSTEM_WARNING|SILENT|EMPTY_PING)[^\]]*\]", re.IGNORECASE)

SRT_INDEX_RE = re.compile(r"^\d+\s*$")
SRT_TIME_RE = re.compile(r"\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}")
HTML_TAG_RE = re.compile(r"<[^>]+>")
MIN_LINE_LEN = 2


def sample_line(text: str) -> str:
    return f'- Mẫu câu tham khảo: "{text}"'


def clean_chat_data(text: str) -> str:
    """Strip URLs, custom emoji, tags and spam; drop lines shorter than 2 characters."""
    text = URL_RE.sub("", text)
    text = CUSTOM_EMOJI_RE.sub("", text)
    text = USER_TAG_RE.sub("mày", text)
    text = CONTROL_TOKEN_RE.sub("", text)
    text = REPEAT_RE.sub(lambda m: m.group(1) * 2, text)
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if len(line) >= MIN_LINE_LEN)


def parse_srt_subtitles(srt_content: str) -> str:
    """Extract spoken lines from .srt content, cleaned and formatted as sample-sentence lines."""
    kept: list[str] = []
    for line in srt_content.lstrip("\ufeff").splitlines():
        if SRT_INDEX_RE.match(line) or SRT_TIME_RE.search(line):
            continue
        line = HTML_TAG_RE.sub("", line).strip().lstrip("-– ").strip()
        if line:
            kept.append(line)
    cleaned = clean_chat_data("\n".join(kept))
    return "\n".join(sample_line(line) for line in cleaned.splitlines())
