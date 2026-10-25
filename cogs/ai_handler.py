"""Message pipeline: context capture, style learning, trigger detection, LLM call, dispatch."""
from __future__ import annotations

import json
import logging
import random
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import discord
from discord.ext import commands
from openai import AsyncOpenAI

from cogs.executor import Executor, is_silent, parse_response
from utils.censor import censor
from utils.config import ROOT, Settings
from utils.memory import Memory

log = logging.getLogger("capricorn.ai")

BOT_NAME_RE = re.compile(r"(?<!\w)(capricorn|capri|con bot|con ai|ai này|bot này)(?!\w)", re.IGNORECASE)
HOSTILE_RE = re.compile(
    r"(?<!\w)(ngu|đần|óc chó|ngáo|rác|vô dụng|lỏ|đm|dm|dcm|đcm|vcl|vl|cc|cút|im đi|câm|"
    r"bố láo|láo|dở hơi|khùng|điên|stupid|dumb|trash|garbage|useless|shut up|stfu|fuck)(?!\w)",
    re.IGNORECASE,
)
ANNOYANCE_THRESHOLD = 50.0
ANNOYANCE_AFTER_PUNISH = 20.0
RANDOM_CHANCE = 0.05
SPAM_COUNT, SPAM_WINDOW = 6, 8.0
USER_COOLDOWN = 4.0
FILLER_RE = re.compile(r"(?<!\w)(ê+|ơi|oi|alo|hey|hi|yo|này|nè|đâu|dậy|ra đây)(?!\w)", re.IGNORECASE)
MENTION_STRIP_RE = re.compile(r"<[@#!&]+\d+>")
SYSTEM_PROMPT_PATH = ROOT / "prompts" / "capricorn_system.txt"
# server_vibe.local.txt (git-ignored) overrides the committed sample so real chat logs stay private
VIBE_PATHS = (ROOT / "prompts" / "server_vibe.local.txt", ROOT / "prompts" / "server_vibe.txt")
VIBE_NOTE = (
    "Các ví dụ dưới đây chỉ để học GIỌNG ĐIỆU (few-shot). Quy tắc phân quyền, [SILENT], mã lệnh "
    "và việc che từ chửi thề bằng *** ở trên vẫn được ưu tiên tuyệt đối."
)


def load_system_prompt() -> str:
    prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    for path in VIBE_PATHS:
        if path.exists():
            vibe = path.read_text(encoding="utf-8").strip()
            if vibe:
                prompt += f"\n\n{VIBE_NOTE}\n\n{vibe}\n"
            break
    return prompt


@dataclass
class Flags:
    direct: bool = False
    hostile: bool = False  # insulting / trash-talking Capricorn
    spam: bool = False
    random_chime: bool = False
    empty_ping: bool = False  # bare tag / bare name with nothing else

    def triggered(self) -> bool:
        return self.direct or self.hostile or self.spam or self.random_chime or self.empty_ping


class AIHandler(commands.Cog):
    def __init__(self, bot: commands.Bot, settings: Settings, memory: Memory):
        self.bot = bot
        self.settings = settings
        self.memory = memory
        self.system_prompt = load_system_prompt()
        self.client: AsyncOpenAI | None = None
        if settings.openrouter_api_key:
            self.client = AsyncOpenAI(api_key=settings.openrouter_api_key,
                                      base_url=settings.openrouter_base_url, timeout=30.0)
        else:
            log.warning("OPENROUTER_API_KEY not set: LLM replies disabled (learning still runs).")
        self._recent: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=SPAM_COUNT))
        self._last_call: dict[int, float] = {}

    # --- classification --------------------------------------------------
    def is_vip(self, member: discord.Member) -> bool:
        rid = self.settings.vip_role_id
        return rid is not None and any(r.id == rid for r in member.roles)

    def _role_pinged(self, message: discord.Message) -> bool:
        """True if a configured call role, or the bot's own managed role, was pinged."""
        own = {r.id for r in message.guild.me.roles if r.managed} if message.guild and message.guild.me else set()
        wanted = own | self.settings.call_role_ids
        return any(r.id in wanted for r in message.role_mentions)

    def _is_reply_to_bot(self, message: discord.Message) -> bool:
        ref = message.reference
        if not ref:
            return False
        resolved = ref.resolved or ref.cached_message
        return isinstance(resolved, discord.Message) and resolved.author.id == self.bot.user.id

    def classify(self, message: discord.Message, vip_or_admin: bool, history: list[dict]) -> Flags:
        content = message.content
        bot_user = self.bot.user
        flags = Flags()
        flags.direct = (
            bot_user in message.mentions
            or self._role_pinged(message)
            or self._is_reply_to_bot(message)
            or bool(BOT_NAME_RE.search(content))
        )
        stripped = FILLER_RE.sub(" ", BOT_NAME_RE.sub(" ", MENTION_STRIP_RE.sub(" ", content)))
        flags.empty_ping = flags.direct and not re.search(r"\w", stripped)
        # hostile only counts when it plausibly targets the bot: a bot reference now or in recent context
        if HOSTILE_RE.search(content):
            recent = history[-6:-1]
            if flags.direct or any(BOT_NAME_RE.search(m["content"]) or m["is_bot"] for m in recent):
                flags.hostile = True

        now = time.monotonic()
        q = self._recent[message.author.id]
        q.append(now)
        flags.spam = len(q) == SPAM_COUNT and now - q[0] <= SPAM_WINDOW

        if vip_or_admin and random.random() < RANDOM_CHANCE:
            wall = time.time()
            lively = [m for m in history if not m["is_bot"] and wall - m["ts"] <= 60]
            if len(lively) >= 4 and len({m["author_id"] for m in lively}) >= 2:
                flags.random_chime = True
        return flags

    # --- prompt ----------------------------------------------------------
    def build_prompt(self, message: discord.Message, role_label: str, history: list[dict],
                     flags: Flags, annoyance: float, defense_allowed: bool) -> str:
        author = message.author
        lines = []
        others: list[int] = []
        for m in history:
            lines.append(f"{m['author_name']}{' (Capricorn)' if m['is_bot'] else ''}: {censor(m['content'])}")
            if not m["is_bot"] and m["author_id"] != author.id and m["author_id"] not in others:
                others.append(m["author_id"])
        names = {m["author_id"]: m["author_name"] for m in history}
        server_style = "; ".join(
            f"{names[u]}: {self.memory.style_summary(u)}" for u in others[:2]
        )
        voice = "có" if getattr(author, "voice", None) else "không"
        cflags = [f for f, on in (("GỌI_BOT", flags.direct), ("NÓI_XẤU", flags.hostile),
                                  ("SPAM", flags.spam), ("PING_TRỐNG", flags.empty_ping), ("PHẠT_ĐƯỢC_PHÉP", defense_allowed)) if on]
        header = (
            f"[User: {author.display_name} | ID: {author.id} | Role: {role_label} | "
            f"Style hay dùng: {self.memory.style_summary(author.id)} | "
            f"Annoyance: {annoyance:.0f}/100 | Đang ở Voice: {voice} | Cờ: {','.join(cflags) or 'không'}]"
        )
        extra = f"\nStyle các thành viên khác (có thể nhại): {server_style}" if server_style else ""
        habits = [censor(t) for t in self.memory.recent_user_messages(message.channel.id, author.id)]
        habit_line = ""
        if habits:
            quoted = ", ".join(json.dumps(t[:200], ensure_ascii=False) for t in habits)
            habit_line = f"\n[THÓI QUEN CHAT CỦA USER ĐỂ BẮT CHƯỚC]: {quoted}"
        chat = "\n".join(lines).replace("</chat>", "")
        return (f"<chat>\n{chat}\n</chat>{extra}{habit_line}\n{header}\n"
                f"Tin nhắn cần xử lý: {censor(message.content)}")

    async def ask_llm(self, prompt: str) -> str:
        assert self.client is not None
        primary, *fallbacks = self.settings.openrouter_models
        fallbacks = fallbacks[:3]  # OpenRouter rejects more than 3 fallback models
        resp = await self.client.chat.completions.create(
            model=primary,
            messages=[{"role": "system", "content": self.system_prompt},
                      {"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.8,
            # OpenRouter-native routing: falls through this list if the primary is rate-limited/down
            extra_body={"models": fallbacks} if fallbacks else None,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            log.warning("Model %s returned empty content", resp.model)
        return content

    # --- event -----------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not isinstance(message.author, discord.Member):
            return
        if not message.content.strip():
            return
        author = message.author
        # Step 1: always record context + learn style
        self.memory.add_message(message.channel.id, author.id, author.display_name, message.content)
        self.memory.learn_style(author.id, message.content)

        is_admin = author.guild_permissions.administrator
        vip_or_admin = is_admin or self.is_vip(author)
        history = self.memory.recent_messages(message.channel.id)

        # Step 2: trigger detection
        flags = self.classify(message, vip_or_admin, history)
        annoyance = self.memory.get_annoyance(author.id)
        if not vip_or_admin:
            bump = 25.0 if flags.hostile else 0.0
            bump += 15.0 if flags.spam else 0.0
            bump += 5.0 if flags.direct else 0.0
            if bump:
                annoyance = self.memory.add_annoyance(author.id, bump)
        if not flags.triggered() or self.client is None:
            return
        now = time.monotonic()
        if now - self._last_call.get(author.id, 0.0) < USER_COOLDOWN:
            return
        self._last_call[author.id] = now

        # Step 3: role-based behaviour
        defense_allowed = (not vip_or_admin) and (flags.hostile or annoyance >= ANNOYANCE_THRESHOLD)
        role_label = "VIP/Admin" if vip_or_admin else "Dân Thường"
        prompt = self.build_prompt(message, role_label, history, flags, annoyance, defense_allowed)
        try:
            raw = await self.ask_llm(prompt)
        except Exception as e:  # network / API errors must never crash the event loop
            log.error("OpenRouter call failed: %s %s", type(e).__name__, str(e)[:300])
            return
        await self.dispatch(message, raw, vip_or_admin, defense_allowed, empty_ping=flags.empty_ping)

    async def dispatch(self, message: discord.Message, raw: str, vip_or_admin: bool,
                       defense_allowed: bool, empty_ping: bool = False) -> None:
        text, cmds = parse_response(raw)
        if not vip_or_admin and not defense_allowed:
            if not (empty_ping and text):
                return  # commoners get ignored unless they crossed the line
            cmds = []  # a bare ping earns a curt reply, never actions
        if is_silent(raw) or (not text and not cmds):
            return
        if text:
            sent = await message.reply(text[:1900], mention_author=False,
                                       allowed_mentions=discord.AllowedMentions.none())
            self.memory.add_message(message.channel.id, self.bot.user.id, "Capricorn", text, is_bot=True)
            log.debug("sent reply %s", sent.id)
        executor: Executor | None = self.bot.get_cog("Executor")
        if cmds and executor:
            results = await executor.execute(message, cmds, defense_allowed=defense_allowed)
            log.info("commands: %s", results)
            if not vip_or_admin and any(r.startswith(("timeout", "MUTE", "DEAF")) for r in results):
                self.memory.set_annoyance(message.author.id, ANNOYANCE_AFTER_PUNISH)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AIHandler(bot, bot.settings, bot.memory))  # type: ignore[attr-defined]
