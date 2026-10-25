"""Capricorn - Discord bot entry point."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

from utils.config import Settings
from utils.memory import Memory

EXTENSIONS = ("cogs.executor", "cogs.ai_handler", "cogs.admin")


class Capricorn(commands.Bot):
    def __init__(self, settings: Settings, memory: Memory):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        # Chat behaviour lives in on_message; admin tooling is exposed as slash commands (cogs.admin).
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.settings = settings
        self.memory = memory

    async def setup_hook(self) -> None:
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        log = logging.getLogger("capricorn")
        try:
            synced = await self.tree.sync()
            log.info("Synced %d global slash commands: %s", len(synced), [c.name for c in synced])
        except discord.HTTPException as e:
            log.error("Global slash sync failed: %s", e)

    async def on_ready(self) -> None:
        log = logging.getLogger("capricorn")
        log.info("Online as %s (%s)", self.user, self.user.id)
        # Commands are global only; wipe any per-guild copies from earlier versions so they don't show twice
        for guild in self.guilds:
            try:
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
            except discord.HTTPException as e:
                log.warning("Could not clear guild commands for %s: %s", guild.id, e)


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
