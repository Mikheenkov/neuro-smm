"""MAX Bot API (§3.1): загрузка медиа → POST /messages в канал с HTML и кнопками-ссылками."""

from __future__ import annotations

import asyncio
import re
import ssl
from pathlib import Path

import httpx

from smm.publishers import PublishError, PublishResult

API = "https://platform-api2.max.ru"


def max_ssl_context(ca_bundle: Path) -> ssl.SSLContext:
    """Системные сертификаты + «Russian Trusted Root/Sub CA» — только для клиента MAX."""
    ctx = ssl.create_default_context()
    if ca_bundle.exists():
        ctx.load_verify_locations(ca_bundle)
    return ctx


class MaxPublisher:
    def __init__(self, token: str, chat_id: int | None, *, ca_bundle: Path, client: httpx.AsyncClient | None = None):
        self.chat_id = chat_id
        self.client = client or httpx.AsyncClient(
            base_url=API, timeout=120, headers={"Authorization": token}, verify=max_ssl_context(ca_bundle))

    async def _json(self, r: httpx.Response) -> dict:
        try:
            data = r.json()
        except ValueError:
            data = {"message": r.text[:300]}
        if r.status_code >= 400:
            raise PublishError(f"MAX API {r.status_code}: {data.get('code', '')} {data.get('message', '')}".strip(),
                               retryable=r.status_code >= 500 or r.status_code == 429)
        return data

    async def me(self) -> dict:
        return await self._json(await self.client.get("/me"))

    async def updates(self, marker: int | None = None, timeout: int = 0) -> dict:
        params = {"timeout": timeout}
        if marker is not None:
            params["marker"] = marker
        return await self._json(await self.client.get("/updates", params=params))

    async def upload(self, path: Path, kind: str = "image") -> dict:
        slot = await self._json(await self.client.post("/uploads", params={"type": kind}))
        async with httpx.AsyncClient(timeout=300) as up:
            r = await up.post(slot["url"], files={"data": (path.name, path.read_bytes())})
        uploaded = await self._json(r) if r.content else {}
        # Для image токен приходит в ответе загрузки, для video/file — в ответе /uploads.
        token = slot.get("token") or uploaded.get("token")
        if not token and isinstance(uploaded.get("photos"), dict):
            token = next(iter(uploaded["photos"].values()), {}).get("token")
        if not token:
            raise PublishError(f"MAX: не получен токен загрузки для {path.name}", retryable=True)
        return {"type": kind, "payload": {"token": token}}

    async def publish(self, rendered: dict, *, images: list[Path] | None = None, videos: list[Path] | None = None) -> PublishResult:
        if not self.chat_id:
            raise PublishError("MAX: не задан MAX_CHANNEL_ID (бот ещё не добавлен в канал)", retryable=False)
        attachments = [await self.upload(p, "image") for p in (images or [])[:12]]
        attachments += [await self.upload(p, "video") for p in (videos or [])]
        if rendered.get("buttons"):
            attachments.append({"type": "inline_keyboard", "payload": {"buttons": [
                [{"type": "link", "text": b["label"], "url": b["url"]}] for b in rendered["buttons"]]}})
        body = {"text": rendered["text"], "format": "html", "attachments": attachments or None}
        params = {"chat_id": self.chat_id, "disable_link_preview": str(bool(rendered.get("disable_link_preview"))).lower()}

        delay = 1.0
        for _ in range(8):  # attachment.not.ready: медиа ещё обрабатывается (до ~2 мин)
            r = await self.client.post("/messages", params=params, json=body)
            if r.status_code == 400 and "attachment.not.ready" in r.text:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            data = await self._json(r)
            msg = data.get("message", data)
            mid = (msg.get("body") or {}).get("mid") or ""
            warnings = [] if msg.get("url") else ["MAX не вернул публичную ссылку на пост"]
            return PublishResult(mid, msg.get("url"), {"mid": mid}, warnings)
        raise PublishError("MAX: вложения так и не обработались за 2 минуты")


def _norm(text: str) -> str:
    return re.sub(r"[^\wё]+", " ", (text or "").lower()).strip()


def find_post_url(token: str, chat_id: int, ca_bundle: Path, text: str, *, max_pages: int = 10) -> str | None:
    """Ищет в истории канала MAX пост с тем же началом текста, что у поста Telegram. Возвращает его ссылку."""
    needle = _norm(text)[:80]
    if len(needle) < 20:
        return None
    to = None
    with httpx.Client(base_url=API, timeout=30, headers={"Authorization": token}, verify=max_ssl_context(ca_bundle)) as c:
        for _ in range(max_pages):
            params = {"chat_id": chat_id, "count": 100}
            if to is not None:
                params["to"] = to
            r = c.get("/messages", params=params)
            if r.status_code != 200:
                return None
            msgs = r.json().get("messages") or []
            for m in msgs:
                if _norm((m.get("body") or {}).get("text", "")).startswith(needle[:60]) and m.get("url"):
                    return m["url"]
            if len(msgs) < 100:
                return None
            to = min(m.get("timestamp", 0) for m in msgs) - 1
    return None
