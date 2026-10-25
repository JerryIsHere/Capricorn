"""Admin-only slash commands that feed / reset the server voice profile (prompts/server_vibe.txt)."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.ai_handler import AIHandler, vibe_path
from cogs.executor import BLACK_ICE_REASON, Executor, parse_response
from utils.censor import censor, censor_output
from utils.purifier import clean_chat_data, parse_srt_subtitles, sample_line
from utils.vision import extract_chat_from_image, is_image

log = logging.getLogger("capricorn.admin")

ADMIN_ID = 1017049328264679435
DENY_MSG = "Quyền đ*** đâu mà đòi đụng vào hệ thống của tao? Cút!"
MAX_FEED_BYTES = 1_000_000
MAX_LINES_PER_FEED = 2000
MAX_SCAN_MESSAGES = 5000  # hard cap on messages read per /capri_clone run
MAX_CLONE_CHANNELS = 30
MAX_TIMEOUT_MINUTES = 28 * 24 * 60


def format_message(content: str) -> list[str]:
    """Clean one chat message into sample-sentence lines (empty if nothing useful is left)."""
    return [sample_line(line) for line in clean_chat_data(content).splitlines()]


def append_vibe(lines: list[str]) -> int:
    """Append (already purified) lines to the vibe file; returns how many were written."""
    lines = [censor(line) for line in lines if line.strip()][:MAX_LINES_PER_FEED]
    if not lines:
        return 0
    path = vibe_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_nl = path.exists() and path.stat().st_size > 0 and not path.read_bytes().endswith(b"\n")
    with path.open("a", encoding="utf-8") as f:
        f.write(("\n" if needs_nl else "") + "\n".join(lines) + "\n")
    return len(lines)


def _skippable(msg: discord.Message) -> bool:
    content = msg.content.strip()
    return msg.author.bot or not content or content.startswith(("!", "/"))


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _guard(self, interaction: discord.Interaction) -> bool:
        """Defer first (Discord gives only 3s), then enforce the admin hard-lock."""
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id == ADMIN_ID:
            return True
        await interaction.followup.send(DENY_MSG, ephemeral=True)
        return False

    @app_commands.command(name="capri_learn", description="Học văn phong từ các tin nhắn gần nhất trong kênh")
    @app_commands.guild_only()
    async def capri_learn(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 500] = 50):
        if not await self._guard(interaction):
            return
        lines: list[str] = []
        async for msg in interaction.channel.history(limit=amount):
            if _skippable(msg):
                continue
            lines.extend(reversed(format_message(msg.content)))
        lines.reverse()  # newest-first scan -> oldest-first file order
        n = append_vibe(lines)
        await interaction.followup.send(f"Nuốt xong {n} dòng rác của tụi mày. Óc tao bẩn thêm rồi đấy.", ephemeral=True)

    @app_commands.command(name="capri_clone", description="Sao chép văn phong của một thành viên")
    @app_commands.describe(channel="Kênh cần quét (mặc định: kênh hiện tại)",
                           all_channels="Quét mọi kênh chữ bot đọc được (chậm hơn)")
    @app_commands.guild_only()
    async def capri_clone(self, interaction: discord.Interaction, target_user: discord.Member,
                          amount: app_commands.Range[int, 1, 200] = 50,
                          channel: Optional[discord.TextChannel] = None, all_channels: bool = False):
        if not await self._guard(interaction):
            return
        guild = interaction.guild
        if all_channels:
            me = guild.me
            channels = [c for c in guild.text_channels
                        if (p := c.permissions_for(me)).view_channel and p.read_message_history][:MAX_CLONE_CHANNELS]
        else:
            channels = [channel or interaction.channel]
        budget = min(amount * 5, MAX_SCAN_MESSAGES) if not all_channels else MAX_SCAN_MESSAGES
        scanned = 0
        got: list[tuple[dt.datetime, list[str]]] = []
        for ch in channels:
            if len(got) >= amount or scanned >= budget:
                break
            try:
                async for msg in ch.history(limit=min(amount * 5, budget - scanned)):
                    scanned += 1
                    if msg.author.id != target_user.id or _skippable(msg):
                        continue
                    formatted = format_message(msg.content)
                    if formatted:
                        got.append((msg.created_at, formatted))
                    if len(got) >= amount:
                        break
            except discord.Forbidden:
                continue
        # keep the newest `amount` messages overall, then write oldest -> newest
        got = sorted(got, key=lambda x: x[0], reverse=True)[:amount]
        lines = [line for _, fl in sorted(got, key=lambda x: x[0]) for line in fl]
        n = append_vibe(lines)
        note = "" if len(got) >= amount else f" (mới gom {len(got)}/{amount}, đã quét {scanned} tin — nó ít nói quá)"
        await interaction.followup.send(
            f"Hút xong nơ-ron của {target_user.display_name}: {len(got)} câu ({n} dòng){note}.", ephemeral=True)

    @app_commands.command(name="capri_scan", description="Đọc ảnh chụp đoạn chat và học văn phong")
    @app_commands.guild_only()
    async def capri_scan(self, interaction: discord.Interaction, image: discord.Attachment):
        if not await self._guard(interaction):
            return
        if not is_image(image.filename, image.content_type):
            await interaction.followup.send("Đây đ*** phải ảnh. Mù à?", ephemeral=True)
            return
        ai: AIHandler | None = self.bot.get_cog("AIHandler")
        if not ai or not ai.client:
            await interaction.followup.send("Chưa có OpenRouter key, quét bằng niềm tin à?", ephemeral=True)
            return
        text = await extract_chat_from_image(ai.client, self.bot.settings.vision_models, image.url)
        # OCR output is "Name: content"; keep only the content
        lines = [sample_line(body.strip() if sep else line.strip())
                 for line in text.splitlines()
                 for _, sep, body in [line.partition(": ")] if (body if sep else line).strip()]
        n = append_vibe(lines)
        if not n:
            await interaction.followup.send("Soi nát cái ảnh mà chẳng đọc được chữ nào. Ảnh gì như c***.", ephemeral=True)
            return
        await interaction.followup.send(f"Quét xong ảnh, nạp {n} dòng thị giác vào đầu tao.", ephemeral=True)

    @app_commands.command(name="capri_feed", description="Nạp dữ liệu thủ công: văn bản, .txt hoặc phụ đề .srt")
    @app_commands.guild_only()
    async def capri_feed(self, interaction: discord.Interaction, raw_text: Optional[str] = None,
                         file: Optional[discord.Attachment] = None):
        if not await self._guard(interaction):
            return
        if not raw_text and not file:
            await interaction.followup.send("Không đưa gì thì tao nuốt cái đ*** gì?", ephemeral=True)
            return
        chunks: list[str] = []
        if raw_text:
            chunks.append("\n".join(sample_line(x) for x in clean_chat_data(raw_text).splitlines()))
        if file:
            name = file.filename.lower()
            if not name.endswith((".txt", ".srt")) or file.size > MAX_FEED_BYTES:
                await interaction.followup.send("Chỉ nhận .txt / .srt dưới 1MB thôi.", ephemeral=True)
                return
            content = (await file.read()).decode("utf-8-sig", errors="replace")
            chunks.append(parse_srt_subtitles(content) if name.endswith(".srt") 
                          else "\n".join(sample_line(x) for x in clean_chat_data(content).splitlines()))
        n = append_vibe([line for c in chunks for line in c.splitlines()])
        await interaction.followup.send(f"Nuốt xong kịch bản: {n} dòng. Tiêu hóa chưa nổi thì tao ợ ra.", ephemeral=True)

    @app_commands.command(name="capri_format", description="Xóa sạch văn phong đã học")
    @app_commands.guild_only()
    async def capri_format(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        with vibe_path().open("w", encoding="utf-8"):
            pass
        await interaction.followup.send(
            "Đã xóa sạch mớ rác rưởi tụi mày nhét vào đầu tao. Băng thông lại sạch sẽ.", ephemeral=True)

    @app_commands.command(name="capri_fry", description="Khóa mõm một thành viên và chửi nó")
    @app_commands.guild_only()
    async def capri_fry(self, interaction: discord.Interaction, target_user: discord.Member,
                        minutes: app_commands.Range[int, 1, MAX_TIMEOUT_MINUTES] = 10):
        if not await self._guard(interaction):
            return
        try:
            await target_user.timeout(dt.timedelta(minutes=minutes), reason=BLACK_ICE_REASON)
        except discord.Forbidden:
            await interaction.followup.send("Role thằng này cao hơn tao, khóa mõm đ*** nổi.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"Khóa mõm hỏng: {e.status}", ephemeral=True)
            return
        insult = ""
        ai: AIHandler | None = self.bot.get_cog("AIHandler")
        if ai:
            prompt = (
                f"[NHIỆM VỤ ADMIN] Thằng {target_user.display_name} vừa bị khóa mõm {minutes} phút. "
                "Viết ĐÚNG 1 câu chửi trực diện, cực kỳ toxic, che mọi từ tục bằng ***. "
                "Không dùng [SILENT], không kèm mã lệnh."
            )
            try:
                insult, _ = parse_response(await ai.ask_llm(prompt))
            except Exception as e:
                log.warning("Fry insult generation failed: %s", type(e).__name__)
        insult = censor_output(censor(insult.replace("@", ""))).strip()[:500] or f"Ngồi im {minutes} phút đi, đồ rác mạng."
        executor: Executor | None = self.bot.get_cog("Executor")
        if executor:
            executor.record(f"Tao vừa khóa mõm (timeout) {target_user.mention} ({target_user.display_name}) {minutes} phút bằng /capri_fry")
        await interaction.channel.send(
            f"{target_user.mention} {insult}",
            allowed_mentions=discord.AllowedMentions(users=[target_user]))
        await interaction.followup.send(f"Đã khóa mõm {target_user.display_name} {minutes} phút.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
