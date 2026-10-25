"""Environment-driven settings. Secrets are only ever read from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"


def parse_models(raw: str) -> tuple[str, ...]:
    models = tuple(m.strip() for m in raw.split(",") if m.strip())
    return models or (DEFAULT_MODEL,)


@dataclass(frozen=True)
class Settings:
    discord_token: str
    openrouter_api_key: str
    openrouter_models: tuple[str, ...]  # first = primary, rest = OpenRouter fallbacks
    openrouter_base_url: str
    vip_role_id: int | None
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
            vip_role_id=int(vip) if vip.isdigit() else None,
            db_path=db if db.is_absolute() else ROOT / db,
        )
