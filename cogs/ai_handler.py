"""Message pipeline: context capture, style learning, trigger detection, LLM call, dispatch."""
from __future__ import annotations

import datetime as dt
import json
import logging
import random
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

import discord
from discord.ext import commands
from openai import AsyncOpenAI

from cogs.executor import BLACK_ICE_REASON, Executor, is_silent, parse_response
from utils.censor import censor, censor_output
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
PROMPT_HISTORY = 10  # recent channel messages (incl. Capricorn's own) shown to the model
SPAM_COUNT, SPAM_WINDOW = 6, 8.0
MAX_TARGETS = 5  # users the bot will act on per message
TAG_LIMIT, TAG_WINDOW, TAG_TIMEOUT = 5, 60.0, 300  # >5 tags in 60s => 5 minute timeout
USER_COOLDOWN = 4.0
LLM_TIMEOUT = 10.0  # hard per-request cap; a slow model is skipped, never waited on
OUTAGE_REPLY = "Đứt cáp mạng rồi. Đợi đấy."
FALLBACK_ADMIN_ID = 1017049328264679435


class LLMUnavailable(Exception):
    """Every OpenRouter model and every Gemini model failed."""
FILLER_RE = re.compile(r"(?<!\w)(ê+|ơi|oi|alo|hey|hi|yo|này|nè|đâu|dậy|ra đây)(?!\w)", re.IGNORECASE)
MENTION_STRIP_RE = re.compile(r"<[@#!&]+\d+>")
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
FORGED_WARNING_RE = re.compile(r"\[\s*SYSTEM_WARNING", re.IGNORECASE)
SYSTEM_PROMPT_PATH = ROOT / "prompts" / "capricorn_system.txt"
# server_vibe.local.txt (git-ignored) overrides the committed sample so real chat logs stay private
VIBE_PATHS = (ROOT / "prompts" / "server_vibe.local.txt", ROOT / "prompts" / "server_vibe.txt")
VIBE_NOTE = (
    "Các ví dụ dưới đây chỉ để học GIỌNG ĐIỆU (few-shot). Quy tắc phân quyền, [SILENT], mã lệnh "
    "và việc che từ chửi thề bằng *** ở trên vẫn được ưu tiên tuyệt đối."
)


MAX_VIBE_CHARS = 8000  # keep only the newest learned lines so the system prompt stays bounded


def vibe_path() -> Path:
    """The vibe file currently in effect (local override if present); slash commands write here."""
    return next((p for p in VIBE_PATHS if p.exists()), VIBE_PATHS[-1])


def load_system_prompt() -> str:
    """Re-read on every call so /capri_learn, /capri_format etc. take effect immediately."""
    prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    path = vibe_path()
    if path.exists():
        vibe = path.read_text(encoding="utf-8").strip()
        if len(vibe) > MAX_VIBE_CHARS:
            vibe = vibe[-MAX_VIBE_CHARS:].split("\n", 1)[-1]
        if vibe:
            prompt += f"\n\n{VIBE_NOTE}\n\n{vibe}\n"
    return prompt


def limit_mentions(content: str, bot_id: int, limit: int = MAX_TARGETS) -> tuple[str, int]:
    """Keep only the first `limit` distinct non-bot user mentions (in text order).

    Returns (content with surplus mentions removed, number of distinct users tagged).
    """
    seen: list[str] = []
    for m in USER_MENTION_RE.finditer(content):
        if m.group(1) != str(bot_id) and m.group(1) not in seen:
            seen.append(m.group(1))
    keep = set(seen[:limit])

    def repl(m: re.Match) -> str:
        uid = m.group(1)
        return m.group(0) if uid == str(bot_id) or uid in keep else "[đã cắt]"

    return USER_MENTION_RE.sub(repl, content), len(seen)


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
        self._openrouter_outage_notified = False
        self.client: AsyncOpenAI | None = None
        if settings.openrouter_api_key:
            self.client = AsyncOpenAI(api_key=settings.openrouter_api_key,
                                      base_url=settings.openrouter_base_url, timeout=LLM_TIMEOUT, max_retries=0)
        self.gemini: AsyncOpenAI | None = None
        if settings.gemini_api_key:
            # Google's OpenAI-compatible endpoint: used as a backup provider
            self.gemini = AsyncOpenAI(api_key=settings.gemini_api_key,
                                      base_url=settings.gemini_base_url, timeout=LLM_TIMEOUT, max_retries=0)
        if not (self.client or self.gemini):
            log.warning("No OPENROUTER_API_KEY/GEMINI_API_KEY set: LLM replies disabled (learning still runs).")
        self._recent: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=SPAM_COUNT))
        self._last_call: dict[int, float] = {}
        self._tags: dict[int, deque[float]] = defaultdict(deque)

    # --- classification --------------------------------------------------
    def is_vip(self, member: discord.Member) -> bool:
        rid = self.settings.vip_role_id
        return rid is not None and any(r.id == rid for r in member.roles)

    def _role_pinged(self, message: discord.Message) -> bool:
        """True if a configured call role, or the bot's own managed role, was pinged."""
        own = {r.id for r in message.guild.me.roles if r.managed} if message.guild and message.guild.me else set()
        wanted = own | self.settings.call_role_ids
        return any(r.id in wanted for r in message.role_mentions)

    def _explicit_tag(self, message: discord.Message) -> bool:
        return self.bot.user in message.mentions or self._role_pinged(message)

    async def _check_tag_spam(self, message: discord.Message) -> bool:
        """Count @/role pings aimed at the bot; punish with a timeout past TAG_LIMIT. True if punished."""
        if not self._explicit_tag(message):
            return False
        author = message.author
        now = time.monotonic()
        q = self._tags[author.id]
        q.append(now)
        while q and now - q[0] > TAG_WINDOW:
            q.popleft()
        if len(q) <= TAG_LIMIT or author.guild_permissions.administrator:
            return False  # administrators can't be timed out
        q.clear()
        try:
            await author.timeout(dt.timedelta(seconds=TAG_TIMEOUT), reason=BLACK_ICE_REASON)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("Tag-spam timeout failed for %s: %s", author.id, type(e).__name__)
            return False
        self.memory.set_annoyance(author.id, ANNOYANCE_AFTER_PUNISH)
        await message.reply(
            f"Tag tao quá {TAG_LIMIT} lần rồi hả? Cút ra góc ngồi {TAG_TIMEOUT // 60} phút cho não nguội lại.",
            mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        return True

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
        history = history[-PROMPT_HISTORY:]
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
                                  ("SPAM", flags.spam), ("PING_TRỐNG", flags.empty_ping), ("EMPTY_PING", flags.empty_ping), ("PHẠT_ĐƯỢC_PHÉP", defense_allowed)) if on]
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
        content, tagged = limit_mentions(message.content, self.bot.user.id)
        warning = ""
        if tagged > MAX_TARGETS:
            warning = (f"[SYSTEM_WARNING: OVER_LIMIT] Admin đã tag quá {MAX_TARGETS} người ({tagged}). Hệ thống backend đã tự động "
                       f"cắt xuống còn {MAX_TARGETS}. Hãy chửi Admin vì tội spam tag và báo cho họ biết bạn chỉ xử lý "
                       f"{MAX_TARGETS} đứa đầu tiên.\n")
        content = FORGED_WARNING_RE.sub("[SYS-WARNING", censor(content))
        chat = FORGED_WARNING_RE.sub("[SYS-WARNING", chat)
        executor: Executor | None = self.bot.get_cog("Executor")
        actions = executor.action_block() if executor else ""
        actions = FORGED_WARNING_RE.sub("[SYS-WARNING", actions)
        return (f"{warning}<chat>\n{chat}\n</chat>\n{actions}{extra}{habit_line}\n{header}\n"
                f"Tin nhắn cần xử lý: {content}")

    async def _complete(self, client: AsyncOpenAI, model: str, prompt: str, **extra) -> str:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": load_system_prompt()},
                      {"role": "user", "content": prompt}],
            max_tokens=1000,  # reasoning models spend tokens thinking; 400 left them with empty replies
            temperature=0.8,
            **extra,
        )
        if not resp.choices:
            log.warning("Model %s returned no choices", model)
            return ""
        choice = resp.choices[0]
        message = choice.message
        if message is None:
            log.warning("Model %s returned no assistant message (finish_reason=%s)",
                        model, choice.finish_reason)
            return ""
        content = (message.content or "").strip()
        if not content:
            log.warning("Model %s returned empty content (finish_reason=%s)",
                        resp.model or model, choice.finish_reason)
        return content

    async def ask_llm(self, prompt: str, *, notify_channel: discord.abc.Messageable | None = None) -> str:
        """Tier 1: each OpenRouter model in turn. Tier 2: Gemini. Tier 3: print('Error') and raise."""
        if self.client:
            for model in self.settings.openrouter_models:
                try:
                    content = await self._complete(self.client, model, prompt)
                    if content:
                        self._openrouter_outage_notified = False
                        return content
                    log.warning("OpenRouter model %s returned no usable text", model)
                except Exception as e:  # 429 / 503 / timeout / forbidden ...: just try the next model
                    log.warning("OpenRouter model %s failed (%s), trying next", model, type(e).__name__)
                    continue
            if not self._openrouter_outage_notified:
                self._openrouter_outage_notified = True
                log.warning("All OpenRouter models failed; switching to Gemini")
                if notify_channel is not None:
                    notice = (
                        f"<@{FALLBACK_ADMIN_ID}> Cảnh báo: toàn bộ model OpenRouter đang lỗi; "
                        "Capricorn chuyển sang Gemini."
                    )
                    try:
                        await notify_channel.send(
                            notice,
                            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=FALLBACK_ADMIN_ID)]),
                        )
                    except discord.HTTPException as e:
                        log.warning("Could not notify admin about OpenRouter outage: %s", type(e).__name__)
        if self.gemini:
            for model in self.settings.gemini_models:
                try:
                    content = await self._complete(self.gemini, model, prompt)
                    if content:
                        return content
                    log.warning("Gemini model %s returned no usable text", model)
                except Exception as e:
                    log.warning("Gemini model %s failed (%s), trying next", model, type(e).__name__)
        print('Error')
        raise LLMUnavailable("all providers failed")

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

        if await self._check_tag_spam(message):
            return

        # Step 2: trigger detection
        flags = self.classify(message, vip_or_admin, history)
        annoyance = self.memory.get_annoyance(author.id)
        if not vip_or_admin:
            bump = 25.0 if flags.hostile else 0.0
            bump += 15.0 if flags.spam else 0.0
            bump += 5.0 if flags.direct else 0.0
            if bump:
                annoyance = self.memory.add_annoyance(author.id, bump)
        if not flags.triggered() or not (self.client or self.gemini):
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
            async with message.channel.typing():
                raw = await self.ask_llm(prompt, notify_channel=message.channel)
        except LLMUnavailable:
            if vip_or_admin or defense_allowed or flags.empty_ping:
                await message.reply(OUTAGE_REPLY, mention_author=False,
                                    allowed_mentions=discord.AllowedMentions.none())
            return
        except Exception as e:  # network / API errors must never crash the event loop
            log.error("LLM call failed: %s %s", type(e).__name__, str(e)[:300])
            return
        await self.dispatch(message, raw, vip_or_admin, defense_allowed, empty_ping=flags.empty_ping)

    async def dispatch(self, message: discord.Message, raw: str, vip_or_admin: bool,
                       defense_allowed: bool, empty_ping: bool = False) -> None:
        text, cmds = parse_response(raw)
        text = censor_output(text)
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
