"""Parses SYS_CMD tags out of LLM output and executes them against Discord.

Authority is always derived from the real message author, never from the LLM's text:
  * administrators may run any command;
  * anyone else may only trigger Capricorn's auto-defense: a <=60s timeout / mute / deafen
    on THEMSELVES, and only when the pipeline flagged them as hostile or overly annoying.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass

import discord
from discord.ext import commands

log = logging.getLogger("capricorn.executor")

CMD_RE = re.compile(r"\[SYS_CMD:\s*([A-Za-z_]+)\s*((?:\|[^\]\|]*)*)\]")
SILENT_RE = re.compile(r"\[\s*SILENT\s*\]", re.IGNORECASE)
AUTO_DEFENSE_MAX_SECONDS = 60
MAX_TIMEOUT_SECONDS = 28 * 24 * 3600

ADMIN_ONLY = {"SCAN", "ROLE", "CHANNEL_PERM", "MOVE_VOICE"}
KNOWN = ADMIN_ONLY | {"TIMEOUT", "MUTE", "DEAF"}


@dataclass(frozen=True)
class Command:
    name: str
    args: tuple[str, ...]


def parse_response(text: str) -> tuple[str, list[Command]]:
    """Return (clean_text, commands). Command tags and [SILENT] are removed from the text."""
    cmds = [
        Command(m.group(1).upper(), tuple(a.strip() for a in m.group(2).split("|")[1:]))
        for m in CMD_RE.finditer(text)
    ]
    clean = SILENT_RE.sub("", CMD_RE.sub("", text)).strip()
    return clean, cmds


def is_silent(text: str) -> bool:
    return bool(SILENT_RE.search(text)) and not CMD_RE.sub("", SILENT_RE.sub("", text)).strip()


def _id(value: str) -> int | None:
    value = value.strip().strip("<@!&#>")
    return int(value) if value.isdigit() else None


def _on(value: str) -> bool | None:
    v = value.strip().upper()
    return True if v == "ON" else False if v == "OFF" else None


class Executor(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    async def execute(self, message: discord.Message, cmds: list[Command], *,
                      defense_allowed: bool = False) -> list[str]:
        """Run commands; returns a list of log lines describing what happened."""
        results: list[str] = []
        author = message.author
        guild = message.guild
        if guild is None or not isinstance(author, discord.Member):
            return results
        is_admin = author.guild_permissions.administrator
        for cmd in cmds[:5]:
            if cmd.name not in KNOWN:
                results.append(f"unknown command {cmd.name}")
                continue
            if not is_admin and not self._defense_ok(cmd, author, defense_allowed):
                log.warning("Denied %s from %s (admin=%s, defense=%s)",
                            cmd.name, author.id, is_admin, defense_allowed)
                results.append(f"denied {cmd.name}")
                continue
            try:
                results.append(await self._run(cmd, message, guild, is_admin))
            except discord.Forbidden:
                results.append(f"forbidden {cmd.name}")
            except (discord.HTTPException, ValueError, IndexError) as e:
                log.warning("Command %s failed: %s", cmd, e)
                results.append(f"failed {cmd.name}: {e}")
        return results

    @staticmethod
    def _defense_ok(cmd: Command, author: discord.Member, defense_allowed: bool) -> bool:
        if not defense_allowed or cmd.name not in {"TIMEOUT", "MUTE", "DEAF"}:
            return False
        if not cmd.args or _id(cmd.args[0]) != author.id:
            return False
        if cmd.name in {"MUTE", "DEAF"}:
            return len(cmd.args) > 1 and _on(cmd.args[1]) is True
        return True

    async def _member(self, guild: discord.Guild, raw: str) -> discord.Member:
        uid = _id(raw)
        if uid is None:
            raise ValueError(f"invalid user id {raw!r}")
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound:
                raise ValueError(f"user {uid} not in server") from None
        return member

    async def _run(self, cmd: Command, message: discord.Message,
                   guild: discord.Guild, is_admin: bool) -> str:
        a = cmd.args
        reason = f"Capricorn: requested by {message.author} ({message.author.id})"
        if cmd.name == "SCAN":
            member = await self._member(guild, a[0])
            await message.channel.send(embed=build_scan_embed(member),
                                       allowed_mentions=discord.AllowedMentions.none())
            return f"scanned {member.id}"

        if cmd.name == "CHANNEL_PERM":
            action, channel_id, target_id = a[0].upper(), _id(a[1]), _id(a[2])
            channel = guild.get_channel(channel_id) if channel_id else None
            target = None
            if target_id:
                target = guild.get_member(target_id) or guild.get_role(target_id)
            if channel is None or target is None or action not in {"LOCK", "UNLOCK"}:
                raise ValueError("bad CHANNEL_PERM arguments")
            # LOCK denies sending/viewing; UNLOCK clears the overrides (back to inherited)
            value = False if action == "LOCK" else None
            await channel.set_permissions(target, send_messages=value, view_channel=value, reason=reason)
            return f"{action} {channel.id} for {target.id}"

        if cmd.name == "ROLE":
            action = a[0].upper()
            member = await self._member(guild, a[1])
            if member.id == guild.owner_id:
                raise ValueError("target is protected")
            role = discord.utils.find(lambda r: r.name.lower() == a[2].lower(), guild.roles)
            if role is None or role.is_default() or role.managed:
                raise ValueError("role not found or not assignable")
            if role.permissions.administrator and message.author.id != guild.owner_id:
                raise ValueError("refusing to grant/remove an administrator role")
            if action == "ADD":
                await member.add_roles(role, reason=reason)
            elif action == "REMOVE":
                await member.remove_roles(role, reason=reason)
            else:
                raise ValueError("expected ADD/REMOVE")
            return f"role {action} {role.id} {member.id}"
        member = await self._member(guild, a[0])
        if member.id == guild.owner_id or (member.id == self.bot.user.id):
            raise ValueError("target is protected")

        if cmd.name == "TIMEOUT":
            seconds = int(a[1])
            seconds = max(0, min(seconds, MAX_TIMEOUT_SECONDS if is_admin else AUTO_DEFENSE_MAX_SECONDS))
            until = dt.timedelta(seconds=seconds) if seconds else None
            await member.timeout(until, reason=reason)
            return f"timeout {member.id} {seconds}s"

        if cmd.name in {"MUTE", "DEAF"}:
            state = _on(a[1])
            if state is None:
                raise ValueError("expected ON/OFF")
            if member.voice is None:
                raise ValueError("target not in voice")
            kw = {"mute": state} if cmd.name == "MUTE" else {"deafen": state}
            await member.edit(reason=reason, **kw)
            return f"{cmd.name} {member.id} {state}"

        if cmd.name == "MOVE_VOICE":
            if member.voice is None:
                raise ValueError("target not in voice")
            dest_name = a[1]
            if dest_name.upper() == "NONE":
                await member.move_to(None, reason=reason)
                return f"disconnected {member.id}"
            dest = discord.utils.find(lambda c: c.name.lower() == dest_name.lower(), guild.voice_channels)
            if dest is None:
                raise ValueError(f"voice channel {dest_name!r} not found")
            await member.move_to(dest, reason=reason)
            return f"moved {member.id} to {dest.id}"

        raise ValueError("unhandled command")


def build_scan_embed(member: discord.Member) -> discord.Embed:
    embed = discord.Embed(
        title="▌NCPD // NETRUNNER SCAN",
        description=f"`TARGET LOCKED` · {member.mention}",
        color=0x00F0FF,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Tên hiển thị", value=discord.utils.escape_markdown(member.display_name), inline=True)
    embed.add_field(name="User ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="Tài khoản tạo", value=discord.utils.format_dt(member.created_at, "F"), inline=False)
    joined = discord.utils.format_dt(member.joined_at, "F") if member.joined_at else "N/A"
    embed.add_field(name="Tham gia server", value=joined, inline=False)
    roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
    text = " ".join(roles) or "—"
    embed.add_field(name=f"Roles ({len(roles)})", value=text[:1000], inline=False)
    embed.set_footer(text="CAPRICORN // HUD v2.077")
    return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Executor(bot))
