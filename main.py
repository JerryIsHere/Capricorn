"""Capricorn - Discord bot entry point."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

from utils.config import Settings
from utils.memory import Memory

EXTENSIONS = ("cogs.executor", "cogs.ai_handler")


class Capricorn(commands.Bot):
    def __init__(self, settings: Settings, memory: Memory):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        # No prefix/slash commands: everything happens through on_message in the cogs.
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.settings = settings
        self.memory = memory

    async def setup_hook(self) -> None:
        for ext in EXTENSIONS:
            await self.load_extension(ext)

    async def on_ready(self) -> None:
        logging.getLogger("capricorn").info("Online as %s (%s)", self.user, self.user.id)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    if not settings.discord_token:
        raise SystemExit("DISCORD_TOKEN is not set (see .env.example).")
    bot = Capricorn(settings, Memory(settings.db_path))
    try:
        asyncio.run(bot.start(settings.discord_token))
    except KeyboardInterrupt:
        pass
    finally:
        bot.memory.close()


if __name__ == "__main__":
    main()
