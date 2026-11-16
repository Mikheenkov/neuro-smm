"""Сторис Telegram-канала через MTProto (Telethon) от имени аккаунта автора (§3.5).

Bot API публиковать истории в канал не умеет, поэтому здесь userbot. Разрешены только вызовы:
get_input_entity(канал), upload_file, stories.CanSendStory, stories.SendStory — больше этот код ничего не делает.
Сессия лежит в data/tg_story.session, создаётся один раз командой `python -m smm.setup_stories` (вход вводит автор).
"""

from __future__ import annotations

import random
from pathlib import Path

from smm.publishers import PublishError, PublishResult


class TGStoryPublisher:
    def __init__(self, api_id: int, api_hash: str, session: Path, channel: str, *, pinned: bool = False,
                 period: int = 86400):
        self.api_id, self.api_hash = api_id, api_hash
        self.session = session
        self.channel = channel  # username канала без @
        self.pinned = pinned
        self.period = period

    async def publish(self, card: Path, caption: str, msg_id: int, area: dict) -> PublishResult:
        from telethon import TelegramClient, errors
        from telethon.tl import functions, types

        if not self.session.exists():
            raise PublishError("сторис Telegram: нет сессии — выполните вход: python -m smm.setup_stories", retryable=False)
        client = TelegramClient(str(self.session.with_suffix("")), self.api_id, self.api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise PublishError("сторис Telegram: сессия больше не действует — войдите заново: "
                                   "python -m smm.setup_stories", retryable=False)
            peer = await client.get_input_entity(self.channel)
            try:
                await client(functions.stories.CanSendStoryRequest(peer=peer))
            except errors.RPCError as exc:
                raise PublishError(_explain(exc), retryable=False) from exc
            uploaded = await client.upload_file(str(card))
            channel = types.InputChannel(peer.channel_id, peer.access_hash)
            # Координаты зоны — в процентах от карточки, x/y — центр.
            coords = types.MediaAreaCoordinates(x=area["x"] * 100, y=area["y"] * 100, w=area["w"] * 100,
                                                h=area["h"] * 100, rotation=0)
            try:
                res = await client(functions.stories.SendStoryRequest(
                    peer=peer, media=types.InputMediaUploadedPhoto(file=uploaded),
                    privacy_rules=[types.InputPrivacyValueAllowAll()], pinned=self.pinned, period=self.period,
                    caption=caption, random_id=random.getrandbits(63),
                    media_areas=[types.InputMediaAreaChannelPost(coordinates=coords, channel=channel, msg_id=msg_id)]))
            except errors.RPCError as exc:
                raise PublishError(_explain(exc), retryable="FLOOD" in str(exc)) from exc
            story_id = next((u.id for u in getattr(res, "updates", []) if isinstance(u, types.UpdateStoryID)), None)
            if story_id is None:
                story_id = next((u.story.id for u in getattr(res, "updates", []) if isinstance(u, types.UpdateStory)), None)
            url = f"https://t.me/{self.channel}/s/{story_id}" if story_id else f"https://t.me/{self.channel}"
            return PublishResult(str(story_id or ""), url)
        finally:
            await client.disconnect()


def _explain(exc: Exception) -> str:
    s = str(exc)
    if "BOOSTS_REQUIRED" in s:
        return "сторис Telegram: каналу не хватает бустов (нужен 1-й уровень)"
    if "STORIES_TOO_MUCH" in s or "STORY_SEND_FLOOD" in s:
        return "сторис Telegram: лимит историй на сегодня исчерпан"
    if "ADMIN_REQUIRED" in s or "CHAT_ADMIN" in s:
        return "сторис Telegram: у аккаунта в сессии нет права публиковать истории в канале"
    if "PREMIUM" in s:
        return f"сторис Telegram: нужен Premium ({s[:120]})"
    return f"сторис Telegram: {type(exc).__name__}: {s[:200]}"
