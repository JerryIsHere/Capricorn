"""OCR via a free OpenRouter vision model: extract chat text from a screenshot URL."""
from __future__ import annotations

import logging

from openai import AsyncOpenAI

from utils.purifier import clean_chat_data

log = logging.getLogger("capricorn.vision")

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
OCR_PROMPT = (
    "Trích xuất chính xác toàn bộ nội dung đoạn chat trong bức ảnh này theo định dạng Tên: Nội dung. "
    "Bỏ qua giờ giấc và giao diện thừa."
)


def is_image(filename: str, content_type: str | None) -> bool:
    return filename.lower().endswith(IMAGE_EXTENSIONS) and (content_type or "image/").startswith("image/")


async def extract_chat_from_image(client: AsyncOpenAI, models: tuple[str, ...], image_url: str) -> str:
    """Try each vision model in order; return cleaned text, or "" if none produced any."""
    for model in models:
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]}],
                max_tokens=1500,
                temperature=0,
            )
            content = (resp.choices[0].message.content or "") if resp.choices and resp.choices[0].message else ""
            cleaned = clean_chat_data(content)
            if cleaned:
                return cleaned
            log.warning("Vision model %s returned no text", model)
        except Exception as e:
            log.warning("Vision model %s failed (%s), trying next", model, type(e).__name__)
    return ""
