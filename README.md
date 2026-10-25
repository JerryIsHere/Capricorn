# Capricorn
a semi-AI Discord bot inspired bt Cyberpunk: Edgerunners 2. Features passive-aggressive moderation, adaptive speech mimicry, and natural language server controls powerred by OpenRouter


## Setup
```bash
pip install -r requirements.txt
cp .env.example .env     # fill DISCORD_TOKEN, OPENROUTER_API_KEY, OPENROUTER_MODEL, VIP_ROLE_ID
python main.py
```
In Codespaces, set the same names as **Codespaces secrets** instead of using a `.env` file. Enable the *Message Content*, *Server Members* intents in the Discord developer portal. Secrets are only read from the environment; `.env`, `*.db` and `data/` are git-ignored.

## How it works
- `main.py` – bot bootstrap (intents, cog loading). No slash commands.
- `cogs/ai_handler.py` – `on_message` pipeline: record context → learn style → trigger filter (mention/reply/keywords; hostile talk about the bot; 5% chime-in during lively VIP chat) → OpenRouter (`AsyncOpenAI`) → dispatch.
- `cogs/executor.py` – regex parser for `[SYS_CMD:...]` tags and Discord actions (`SCAN` HUD embed, `TIMEOUT`, `MUTE`, `DEAF`, `MOVE_VOICE`, `ROLE`, `CHANNEL_PERM`).
- `utils/memory.py` – SQLite: 12-message sliding window per channel, per-user style/engram, annoyance score (decays over time).
- `prompts/capricorn_system.txt` – system prompt.

Behaviour: VIP/Admin get replies and mimicry; commoners are ignored (`[SILENT]`, also enforced in code) unless they insult the bot or their annoyance score reaches 50, after which Capricorn may reply and apply a ≤60s timeout/mute/deafen **to that user only**.

**Security model:** the LLM's output is never trusted for authority. Admin commands (`SCAN`, `ROLE`, `CHANNEL_PERM`, `MOVE_VOICE`, long timeouts) require the *real* message author to have the Administrator permission; everyone else can only trigger self-targeted auto-defense. Administrator roles can't be granted via the bot, the server owner is protected, and bot replies never ping.

## Testing without API keys
`tests/mock_openrouter.py` is a fake OpenAI-compatible backend. Run `python -m pytest` (starts it automatically), or run it manually (`python -m tests.mock_openrouter`) and set `OPENROUTER_BASE_URL=http://127.0.0.1:8787/v1`.
