"""Environment-driven settings. Secrets are only ever read from the environment."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "poolside/laguna-xs-2.1:free"


def parse_models(raw: str) -> tuple[str, ...]:
    # Tolerate commas, whitespace/newlines, quotes and bash-array parentheses
    parts = (p.strip("\"'()") for p in re.split(r"[,\s]+", raw))
    models = tuple(dict.fromkeys(p for p in parts if p))
    return models or (DEFAULT_MODEL,)


@dataclass(frozen=True)
class Settings:
    discord_token: str
    openrouter_api_key: str
    openrouter_models: tuple[str, ...]  # first = primary, rest = OpenRouter fallbacks
    openrouter_base_url: str
    gemini_api_key: str
    gemini_model: str
    gemini_base_url: str
    vip_role_id: int | None
    call_role_ids: frozenset[int]  # extra roles that, when pinged, summon the bot
    db_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        vip = os.getenv("VIP_ROLE_ID", "").strip()
        db = Path(os.getenv("CAPRICORN_DB_PATH", "data/capricorn.db"))
        return cls(
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            openrouter_models=parse_models(os.getenv("OPENROUTER_MODELS", "")),
            openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "").strip()
            or "https://openrouter.ai/api/v1",
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", "").strip() or "gemini-2.0-flash",
            gemini_base_url=os.getenv("GEMINI_BASE_URL", "").strip()
            or "https://generativelanguage.googleapis.com/v1beta/openai/",
            vip_role_id=int(vip) if vip.isdigit() else None,
            call_role_ids=frozenset(
                int(x) for x in re.split(r"[,\s]+", os.getenv("CAPRICORN_CALL_ROLE_IDS", "")) if x.isdigit()
            ),
            db_path=db if db.is_absolute() else ROOT / db,
        )
