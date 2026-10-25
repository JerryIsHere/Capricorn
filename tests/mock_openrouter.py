"""Fake OpenRouter backend (OpenAI-compatible /chat/completions) for offline testing.

Run standalone:  python -m tests.mock_openrouter   then set OPENROUTER_BASE_URL=http://127.0.0.1:8787/v1
"""
import re

from aiohttp import web

REQUESTS: list[dict] = []


def script_reply(prompt: str) -> str:
    header = re.search(r"\[User: .*?\]", prompt, re.S)
    header = header.group(0) if header else ""
    uid = re.search(r"ID: (\d+)", header)
    uid = uid.group(1) if uid else "0"
    msg = prompt.rsplit("Tin nhắn cần xử lý:", 1)[-1]
    if "PING_TRỐNG" in header:
        return "Giề?"
    if "Dân Thường" in header:
        if "PHẠT_ĐƯỢC_PHÉP" in header:
            return f"Một đống mã rác biết nói. Im lặng đi. [SYS_CMD:TIMEOUT|{uid}|60]"
        return "[SILENT]"
    target = re.search(r"<@!?(\d+)>", msg)
    if "quét" in msg and target:
        return f"Đang quét. [SYS_CMD:SCAN|{target.group(1)}]"
    if "mute" in msg and target:
        return f"Rõ. [SYS_CMD:MUTE|{target.group(1)}|ON]"
    return "Hừ, lại là bạn à."


async def chat(request: web.Request) -> web.Response:
    body = await request.json()
    REQUESTS.append(body)
    prompt = body["messages"][-1]["content"]
    return web.json_response({
        "id": "mock", "object": "chat.completion", "created": 0, "model": body.get("model", "mock"),
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": script_reply(prompt)}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="127.0.0.1", port=8787)
