"""SQLite storage: channel context window, per-user style (Engram) and annoyance score."""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path

CONTEXT_WINDOW = 12
CONTEXT_KEEP = 50  # rows retained per channel
MAX_STYLE_ROWS = 150  # rows retained per user
ANNOYANCE_MAX = 100.0
ANNOYANCE_DECAY_PER_MIN = 2.0

EMOJI_RE = re.compile(
    r"<a?:\w+:\d+>|[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2B55]"
)
WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
URL_RE = re.compile(r"https?://\S+")
MENTION_RE = re.compile(r"<[@#!&]+\d+>")
STOPWORDS = {
    "và", "là", "của", "có", "không", "cho", "với", "một", "những", "này", "đó",
    "the", "and", "you", "that", "for", "are", "this", "with", "have", "not",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    author_name TEXT NOT NULL,
    content TEXT NOT NULL,
    is_bot INTEGER NOT NULL DEFAULT 0,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id);
CREATE TABLE IF NOT EXISTS styles (
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    token TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    last_seen REAL NOT NULL,
    PRIMARY KEY (user_id, kind, token)
);
CREATE TABLE IF NOT EXISTS annoyance (
    user_id INTEGER PRIMARY KEY,
    score REAL NOT NULL,
    updated REAL NOT NULL
);
"""


def extract_style(content: str) -> list[tuple[str, str]]:
    """Return (kind, token) pairs: word / phrase (bigram) / emoji."""
    text = MENTION_RE.sub(" ", URL_RE.sub(" ", content))
    out: list[tuple[str, str]] = [("emoji", e) for e in EMOJI_RE.findall(text)]
    words = [w.lower() for w in WORD_RE.findall(EMOJI_RE.sub(" ", text))]
    out += [("word", w) for w in words if w not in STOPWORDS and 1 < len(w) <= 20]
    out += [
        ("phrase", f"{a} {b}")
        for a, b in zip(words, words[1:])
        if a not in STOPWORDS and b not in STOPWORDS and len(a) + len(b) <= 30
    ]
    return out


class Memory:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- channel context -------------------------------------------------
    def add_message(self, channel_id: int, author_id: int, author_name: str,
                    content: str, is_bot: bool = False) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO messages(channel_id, author_id, author_name, content, is_bot, ts)"
                " VALUES (?,?,?,?,?,?)",
                (channel_id, author_id, author_name, content[:1000], int(is_bot), time.time()),
            )
            self._db.execute(
                "DELETE FROM messages WHERE channel_id=? AND id NOT IN "
                "(SELECT id FROM messages WHERE channel_id=? ORDER BY id DESC LIMIT ?)",
                (channel_id, channel_id, CONTEXT_KEEP),
            )

    def recent_messages(self, channel_id: int, limit: int = CONTEXT_WINDOW) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT author_id, author_name, content, is_bot, ts FROM messages "
                "WHERE channel_id=? ORDER BY id DESC LIMIT ?",
                (channel_id, limit),
            ).fetchall()
        return [
            {"author_id": r[0], "author_name": r[1], "content": r[2], "is_bot": bool(r[3]), "ts": r[4]}
            for r in reversed(rows)
        ]

    # --- style / engram --------------------------------------------------
    def learn_style(self, user_id: int, content: str) -> None:
        pairs = extract_style(content)
        if not pairs:
            return
        now = time.time()
        with self._lock, self._db:
            self._db.executemany(
                "INSERT INTO styles(user_id, kind, token, count, last_seen) VALUES (?,?,?,1,?) "
                "ON CONFLICT(user_id, kind, token) DO UPDATE SET count=count+1, last_seen=excluded.last_seen",
                [(user_id, k, t, now) for k, t in pairs],
            )
            self._db.execute(
                "DELETE FROM styles WHERE user_id=? AND rowid NOT IN "
                "(SELECT rowid FROM styles WHERE user_id=? ORDER BY count DESC, last_seen DESC LIMIT ?)",
                (user_id, user_id, MAX_STYLE_ROWS),
            )

    def style_summary(self, user_id: int, min_count: int = 3, limit: int = 8) -> str:
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, token FROM styles WHERE user_id=? AND count>=? "
                "ORDER BY count DESC, last_seen DESC LIMIT ?",
                (user_id, min_count, limit),
            ).fetchall()
        return ", ".join(f"{t}" for _, t in rows) if rows else "chưa rõ"

    # --- annoyance -------------------------------------------------------
    def _current_annoyance(self, user_id: int, now: float) -> float:
        row = self._db.execute(
            "SELECT score, updated FROM annoyance WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            return 0.0
        return max(0.0, row[0] - (now - row[1]) / 60.0 * ANNOYANCE_DECAY_PER_MIN)

    def get_annoyance(self, user_id: int) -> float:
        with self._lock:
            return self._current_annoyance(user_id, time.time())

    def add_annoyance(self, user_id: int, amount: float) -> float:
        now = time.time()
        with self._lock, self._db:
            score = min(ANNOYANCE_MAX, self._current_annoyance(user_id, now) + amount)
            self._db.execute(
                "INSERT INTO annoyance(user_id, score, updated) VALUES (?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET score=excluded.score, updated=excluded.updated",
                (user_id, score, now),
            )
        return score

    def set_annoyance(self, user_id: int, value: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO annoyance(user_id, score, updated) VALUES (?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET score=excluded.score, updated=excluded.updated",
                (user_id, value, time.time()),
            )
