"""Telegram Bot API client (https://core.telegram.org/bots/api): text, photo and video alerts."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

# httpx logs full request URLs at INFO, and the Telegram URL contains the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

API = "https://api.telegram.org"
PHOTO_LIMIT = 10 * 1024 * 1024
VIDEO_LIMIT = 50 * 1024 * 1024


class TelegramClient:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    async def _post(self, method: str, data: dict, files: dict | None = None) -> bool:
        if not self.configured:
            logger.warning("Telegram is not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")
            return False
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(f"{API}/bot{self.token}/{method}", data={"chat_id": self.chat_id, **data}, files=files)
            if response.status_code != 200:
                logger.warning("Telegram %s failed: HTTP %s %s", method, response.status_code, response.text[:200])
                return False
            return True
        except httpx.HTTPError as error:
            logger.warning("Telegram %s failed: %s", method, type(error).__name__)
            return False

    async def send_message(self, text: str) -> bool:
        return await self._post("sendMessage", {"text": text[:4000]})

    async def send_photo(self, path: str | Path, caption: str = "") -> bool:
        file = Path(path)
        if not file.is_file() or file.stat().st_size > PHOTO_LIMIT:
            return await self.send_message(caption or "Photo unavailable")
        return await self._post("sendPhoto", {"caption": caption[:1000]}, {"photo": (file.name, file.read_bytes(), "image/jpeg")})

    async def send_video(self, path: str | Path, caption: str = "") -> bool:
        file = Path(path)
        if not file.is_file() or file.stat().st_size > VIDEO_LIMIT:
            return await self.send_message(f"{caption} (video is saved on the PC: {file.name})")
        return await self._post("sendVideo", {"caption": caption[:1000]}, {"video": (file.name, file.read_bytes(), "video/mp4")})
