"""Threads API (§3.4): контейнер → ожидание статуса → публикация → permalink; цепочки через reply_to_id."""

from __future__ import annotations

import asyncio

import httpx

from smm.publishers import PublishError, PublishResult

API = "https://graph.threads.net/v1.0"


class ThreadsPublisher:
    def __init__(self, user_id: str, token: str, *, client: httpx.AsyncClient | None = None):
        self.user_id = user_id
        self.token = token
        self.client = client or httpx.AsyncClient(timeout=60)

    async def _call(self, method: str, path: str, **params) -> dict:
        params["access_token"] = self.token
        r = await self.client.request(method, f"{API}/{path}", params=params)
        data = r.json()
        if r.status_code >= 400 or "error" in data:
            err = data.get("error", {})
            retryable = r.status_code >= 500 or err.get("code") in (1, 2, 4, 17, 32, 613)
            raise PublishError(f"Threads API: {err.get('message', data)}", retryable=retryable)
        return data

    async def _wait_ready(self, container_id: str, timeout: float = 120) -> None:
        # Сразу после создания контейнер может быть ещё «не найден» — это не ошибка, а задержка Meta.
        await asyncio.sleep(3)
        waited = 3.0
        while waited < timeout:
            try:
                status = (await self._call("GET", container_id, fields="status,error_message")).get("status")
            except PublishError as exc:
                if "does not exist" not in str(exc):
                    raise
                status = None
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(f"контейнер Threads в статусе {status}", retryable=False)
            await asyncio.sleep(5)
            waited += 5
        raise PublishError("контейнер Threads не готов за 2 минуты")

    async def _publish_container(self, container: str) -> str:
        for attempt in range(4):
            try:
                return (await self._call("POST", f"{self.user_id}/threads_publish", creation_id=container))["id"]
            except PublishError as exc:
                if "does not exist" not in str(exc) or attempt == 3:
                    raise
                await asyncio.sleep(5 * (attempt + 1))
        raise AssertionError("unreachable")

    async def publish(self, rendered: dict, *, image_urls: list[str] | None = None, done: dict | None = None) -> PublishResult:
        """rendered — результат валидатора: parts, link_attachment_url, topic_tag, mode.
        mode=separate: каждый элемент parts — отдельный пост; mode=chain — ветка через reply_to_id.
        done — прогресс прошлой попытки {"part_ids": [...]} — продолжаем с первого неопубликованного."""
        parts: list[str] = rendered["parts"]
        chain = rendered.get("mode") == "chain"
        part_ids: list[str] = list((done or {}).get("part_ids", []))
        warnings = []
        if image_urls is None and rendered.get("_had_media"):
            warnings.append("фото не перенесено: нужен публичный адрес картинки (появится на сервере)")
        link = rendered.get("link_attachment_url")
        # карточка ссылки — к тому посту, где эта ссылка есть в тексте (иначе к первому)
        card_at = next((i for i, p in enumerate(parts) if link and link in p), 0)

        for i in range(len(part_ids), len(parts)):
            params = {"text": parts[i], "media_type": "TEXT"}
            if chain and i > 0:
                params["reply_to_id"] = part_ids[-1]
            else:
                if i == 0 and image_urls:
                    params.update(media_type="IMAGE", image_url=image_urls[0])
                elif i == card_at and link:
                    params["link_attachment"] = link
                if rendered.get("topic_tag"):
                    params["topic_tag"] = rendered["topic_tag"]
            try:
                container = (await self._call("POST", f"{self.user_id}/threads", **params))["id"]
                await self._wait_ready(container)
                published = await self._publish_container(container)
            except PublishError as exc:
                exc.extra = {"part_ids": part_ids}
                raise
            part_ids.append(published)
            if i + 1 < len(parts):
                await asyncio.sleep(3)

        urls = [(await self._call("GET", pid, fields="permalink")).get("permalink") for pid in part_ids]
        return PublishResult(part_ids[0], urls[0], {"part_ids": part_ids, "urls": urls, "mode": "chain" if chain else "separate"},
                             warnings)

    async def refresh_token(self) -> tuple[str, int]:
        r = await self.client.get("https://graph.threads.net/refresh_access_token",
                                  params={"grant_type": "th_refresh_token", "access_token": self.token})
        data = r.json()
        if "access_token" not in data:
            raise PublishError(f"Threads: не удалось обновить токен: {data.get('error', data)}", retryable=True)
        return data["access_token"], int(data.get("expires_in", 0))
