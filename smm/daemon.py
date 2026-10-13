"""Демон (§1): слушает канал и публикует пост в другие сети по команде автора.

    uv run python -m smm.daemon

Решение автора (10.10.2026): публикация только по команде. На каждый новый пост бот присылает
карточку с кнопками «Опубликовать» и «Как будет выглядеть». Раз автор сказал «публикуй»,
фильтры рекламы не применяются.

Команды в личке бота (только от автора): /post <номер>, /status, /stop, /start, /retry <номер> [сеть].
Пересланный из канала пост — карточка с кнопками; любой другой текст — пробная адаптация без публикации.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)

from smm import web
from smm.adapt import AdaptResult, Adapter, PostInput, safe_adapt
from smm.config import Config, load_config
from smm.db import DB, dumps, now_iso, parse_iso
from smm.ir import content_hash, entities_to_ir
from smm.links import classify, normalize_url
from smm.llm.anthropic_api import AnthropicApiProvider
from smm.publishers import PublishError, PublishResult
from smm.publishers.max_api import MaxPublisher, find_post_url
from smm.publishers.threads import ThreadsPublisher

log = logging.getLogger("smm")

BTN_LAST = "📥 Последний пост"
BTN_RESTART = "🔄 Перезапустить менеджер"
MENU = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_LAST), KeyboardButton(text=BTN_RESTART)]],
                           resize_keyboard=True, is_persistent=True)

NET_TITLES = {"threads": "Threads", "max": "MAX", "fb": "Facebook", "vk": "VK", "story": "Stories"}


def _utc(s: str) -> datetime:
    return parse_iso(s)


def message_payload(msg: Message) -> dict:
    """Текст, entities и медиа одного сообщения Telegram."""
    text = msg.text or msg.caption or ""
    ents = msg.entities or msg.caption_entities or []
    media = []
    if msg.photo:
        p = msg.photo[-1]  # самый большой размер
        media.append({"type": "photo", "file_id": p.file_id, "file_unique_id": p.file_unique_id,
                      "width": p.width, "height": p.height, "size": p.file_size, "msg_id": msg.message_id})
    if msg.video:
        v = msg.video
        media.append({"type": "video", "file_id": v.file_id, "file_unique_id": v.file_unique_id,
                      "width": v.width, "height": v.height, "size": v.file_size, "duration": v.duration,
                      "msg_id": msg.message_id})
    return {"text": text, "entities": [e.model_dump(exclude_none=True, mode="json") for e in ents], "media": media}


def skip_reason(msg: Message) -> str | None:
    if msg.forward_origin is not None:
        return "пересылка из другого канала"
    if (msg.model_extra or {}).get("rich_message"):
        return "rich_message"
    if msg.poll or msg.sticker or msg.voice or msg.video_note or msg.audio or msg.paid_media:
        return "опрос, стикер, голосовое или платный контент"
    if not (msg.text or msg.caption or msg.photo or msg.video):
        return "служебное сообщение"
    return None


class SMMDaemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = DB(cfg.data_dir / "smm.sqlite")
        self.bot = Bot(cfg.secret("TELEGRAM_BOT_TOKEN"))
        self.dp = Dispatcher()
        self.channel_id = int(cfg.secret("TELEGRAM_CHANNEL_ID"))
        self.author_id = int(cfg.secret("TELEGRAM_AUTHOR_USER_ID"))
        self.d = cfg.daemon
        self.media_dir = cfg.data_dir / "media"
        self._busy: set[int] = set()
        self._version = self.pipeline_version()
        self._restart = False
        self._register()

    # --- зависимости, создаются по требованию --------------------------------

    def adapter(self) -> Adapter:
        provider = AnthropicApiProvider(self.cfg.secret("ANTHROPIC_API_KEY"), effort=self.cfg.llm.get("effort"),
                                        workspace_id=os.environ.get("ANTHROPIC_WORKSPACE_ID") or None)
        return Adapter(self.cfg, provider, resolve=web.resolve, reachable=web.reachable, copy_url=self.copy_url,
                       prefilters=False)

    def copy_url(self, tg_msg_id: int, network: str) -> str | None:
        """Ссылка на тот же пост в другой сети. Для MAX ищем и среди постов, вышедших там до нашей системы."""
        url = self.db.copy_url(tg_msg_id, network)
        if url or network != "max":
            return url
        cached = self.db.get(f"max_map:{tg_msg_id}")
        if cached:
            return cached
        import os
        chat = os.environ.get("MAX_CHANNEL_ID")
        own = (self.cfg.equivalents.get("own_tg_channel") or {}).get("username")
        text = self.db.own_post_text(tg_msg_id) or (web.tg_post_text(own, tg_msg_id, 3000, filtered=False) if own else None)
        if not (chat and text):
            return None
        url = find_post_url(self.cfg.secret("MAX_BOT_TOKEN"), int(chat), self.cfg.root / "certs" / "russian_trusted.pem", text)
        if url:
            self.db.set(f"max_map:{tg_msg_id}", url)
        return url

    def link_warnings(self, text: str, entities: list[dict]) -> list[str]:
        """Что случится со ссылками в MAX — автору заранее, чтобы он мог поправить пост в Telegram."""
        if "max" not in self.networks():
            return []
        out = []
        for link in entities_to_ir(text, entities).links:
            url = normalize_url(link.raw_url)
            c = classify(url, self.cfg)
            anchor = link.anchor.strip()[:40]
            if c.link_class == "meta":
                out.append(f"⚠ MAX: ссылка «{anchor}» ведёт на {url.split('/')[2]} — продукты Meta в MAX ставить нельзя, "
                           f"ссылка будет убрана (слова останутся). Замените ссылку в посте в Telegram, если она нужна.")
            elif c.link_class == "own_tg_post" and c.own_msg_id:
                if self.copy_url(c.own_msg_id, "max"):
                    out.append(f"✓ MAX: ссылка «{anchor}» на ваш пост {c.own_msg_id} заменится ссылкой на этот пост в MAX.")
                else:
                    out.append(f"⚠ MAX: пост {c.own_msg_id}, на который ссылка «{anchor}», в MAX не найден — ссылка будет "
                               f"убрана. Опубликуйте тот пост в MAX (/post {c.own_msg_id}), и ссылка заработает.")
            elif c.link_class in ("foreign_tg_post", "foreign_tg_channel", "tg_other", "own_tg_channel"):
                out.append(f"⚠ MAX: ссылка «{anchor}» ведёт в Telegram — в MAX она не откроется и будет убрана.")
            elif c.link_class == "blocked_ru":
                out.append(f"⚠ MAX: ссылка «{anchor}» ({url.split('/')[2]}) заблокирована в РФ — будет убрана, слова останутся.")
        return out

    def threads(self) -> ThreadsPublisher:
        return ThreadsPublisher(self.cfg.secret("THREADS_USER_ID"), self.threads_token())

    def threads_token(self) -> str:
        """Свежий токен Threads: после автопродления он хранится в базе, .env — только начальное значение."""
        return self.db.get("threads_token") or self.cfg.secret("THREADS_ACCESS_TOKEN")

    async def maybe_refresh_threads_token(self) -> None:
        """Раз в сутки: если токену больше недели — продлеваем ещё на 60 дней. Сбой — предупреждение автору."""
        now = datetime.now(timezone.utc)
        checked = self.db.get("threads_refresh_checked")
        if checked and now - _utc(checked) < timedelta(hours=24):
            return
        self.db.set("threads_refresh_checked", now.isoformat(timespec="seconds"))
        obtained = self.db.get("threads_token_at") or os.environ.get("THREADS_TOKEN_OBTAINED_AT")
        if not obtained:
            obtained = now.isoformat(timespec="seconds")
            self.db.set("threads_token_at", obtained)
        age = now - _utc(obtained)
        if age < timedelta(days=7):
            return
        try:
            token, expires_in = await self.threads().refresh_token()
        except PublishError as exc:
            left = 60 - age.days
            log.warning("не удалось продлить токен Threads: %s", exc)
            if left <= 14:
                await self.notify(f"⚠ Не получается продлить доступ к Threads ({exc}). Токен перестанет работать "
                                  f"примерно через {max(left, 0)} дн. Напишите мне — выпустим новый.")
            return
        self.db.set("threads_token", token)
        self.db.set("threads_token_at", now.isoformat(timespec="seconds"))
        log.info("токен Threads продлён на %d дн.", expires_in // 86400)

    def max(self) -> MaxPublisher:
        import os
        chat = os.environ.get("MAX_CHANNEL_ID")
        return MaxPublisher(self.cfg.secret("MAX_BOT_TOKEN"), int(chat) if chat else None,
                            ca_bundle=self.cfg.root / "certs" / "russian_trusted.pem")

    def pipeline_version(self) -> str:
        """Отпечаток промптов и настроек: если они поменялись, сохранённые версии постов устарели."""
        import hashlib
        h = hashlib.sha256()
        files = sorted(self.cfg.prompts_dir.rglob("*.md")) + sorted((self.cfg.root / "config").glob("*.yaml"))
        files += sorted((self.cfg.root / "smm").rglob("*.py"))
        for f in files:
            h.update(f.read_bytes())
        return h.hexdigest()[:12]

    def gen_key(self, post) -> str:
        return f"{post['content_hash']}:{self._version}"

    def is_fresh(self, post) -> bool:
        return post["gen_hash"] == self.gen_key(post)

    def networks(self) -> list[str]:
        enabled = set(self.cfg.enabled_networks())
        return [n for n in self.d.get("publish_order", []) if n in enabled]

    # --- приём постов --------------------------------------------------------

    def _register(self) -> None:
        dp = self.dp
        dp.channel_post.register(self.on_channel_post, F.chat.id == self.channel_id)
        dp.edited_channel_post.register(self.on_edited_post, F.chat.id == self.channel_id)
        author = F.from_user.id == self.author_id
        dp.message.register(self.cmd_status, Command("status"), author)
        dp.message.register(self.cmd_stop, Command("stop"), author)
        dp.message.register(self.cmd_start, Command("start"), author)
        dp.message.register(self.cmd_post, Command("post", "now"), author)
        dp.message.register(self.cmd_retry, Command("retry"), author)
        dp.message.register(self.cmd_test, Command("test"), author)
        dp.message.register(self.btn_last_post, F.text == BTN_LAST, author)
        dp.message.register(self.btn_restart, F.text == BTN_RESTART, author)
        dp.message.register(self.on_author_dm, author, F.chat.type == "private")
        dp.callback_query.register(self.on_button, F.from_user.id == self.author_id)

    async def on_channel_post(self, msg: Message) -> None:
        data = message_payload(msg)
        group = msg.media_group_id
        existing = self.db.post_by_group(msg.chat.id, group) if group else None
        if existing:  # следующий элемент альбома
            media = json.loads(existing["media_json"]) + data["media"]
            fields = {"media_json": dumps(media)}
            if data["text"] and not existing["text"]:
                fields.update(text=data["text"], entities_json=dumps(data["entities"]))
            text = fields.get("text", existing["text"])
            ents = json.loads(fields.get("entities_json", existing["entities_json"]))
            fields["content_hash"] = content_hash(text, ents, [m["file_unique_id"] for m in media])
            self.db.update_post(existing["id"], **fields)
            return

        reason = skip_reason(msg)
        if reason and reason != "rich_message":
            log.info("msg %s пропущен: %s", msg.message_id, reason)
            return
        post_id = self.save_post(msg.chat.id, msg.message_id, group, data)
        if reason == "rich_message":
            self.db.update_post(post_id, state="held", reason=reason)
            await self.notify(f"Пост {msg.message_id} написан в визуальном редакторе — такой формат пока не поддерживается.")
            return
        await self.send_card(post_id)

    def save_post(self, chat_id: int, msg_id: int, group: str | None, data: dict) -> int:
        return self.db.insert_post(
            tg_chat_id=chat_id, tg_msg_id=msg_id, media_group_id=group, first_seen_at=now_iso(),
            state="new", text=data["text"], entities_json=dumps(data["entities"]), media_json=dumps(data["media"]),
            content_hash=content_hash(data["text"], data["entities"], [m["file_unique_id"] for m in data["media"]]))

    async def send_card(self, post_id: int, *, chat_id: int | None = None) -> None:
        post = self.db.post(post_id)
        title = (post["text"].strip().splitlines() or ["(без текста)"])[0][:80]
        published = [NET_TITLES.get(p["network"], p["network"]) for p in self.db.publications(post_id) if p["state"] == "published"]
        note = f"\nУже опубликован: {', '.join(published)}" if published else ""
        warnings = await asyncio.to_thread(self.link_warnings, post["text"], json.loads(post["entities_json"]))
        if warnings:
            note += "\n\n" + "\n".join(warnings)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚀 Опубликовать", callback_data=f"pub:{post_id}"),
            InlineKeyboardButton(text="👀 Как будет выглядеть", callback_data=f"prev:{post_id}"),
        ]])
        await self.bot.send_message(chat_id or self.author_id, f"Пост {post['tg_msg_id']} «{title}»{note}",
                                    reply_markup=kb, disable_web_page_preview=True)

    async def on_edited_post(self, msg: Message) -> None:
        post = self.db.post_by_msg(msg.chat.id, msg.message_id)
        if post is None and msg.media_group_id:
            post = self.db.post_by_group(msg.chat.id, msg.media_group_id)
        if post is None:
            return
        data = message_payload(msg)
        media = json.loads(post["media_json"])
        media = [data["media"][0] if data["media"] and m.get("msg_id") == msg.message_id else m for m in media]
        text, ents = post["text"], json.loads(post["entities_json"])
        if data["text"] or msg.message_id == post["tg_msg_id"]:
            text, ents = data["text"], data["entities"]
        new_hash = content_hash(text, ents, [m["file_unique_id"] for m in media])
        if new_hash == post["content_hash"]:
            return  # правка полей, которые мы не используем
        if post["state"] in ("done", "partial", "publishing") and post["tg_msg_id"]:
            await self.notify(f"Пост {post['tg_msg_id']} изменён после публикации — правка в другие сети не перенесена.")
            return
        self.db.update_post(post["id"], text=text, entities_json=dumps(ents), media_json=dumps(media),
                            content_hash=new_hash, last_edit_at=now_iso())
        log.info("пост %s изменён, будет перегенерирован", post["id"])

    # --- расписание ----------------------------------------------------------

    async def scheduler(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("ошибка в планировщике")
            await asyncio.sleep(self.d.get("tick_seconds", 20))

    async def tick(self) -> None:
        self._cleanup_public()
        try:
            await self.maybe_refresh_threads_token()
        except Exception:
            log.exception("ошибка продления токена Threads")
        if self.db.get("stopped") == "1":
            return
        now = datetime.now(timezone.utc)
        for post in self.db.posts_in_state("queued"):
            if post["id"] in self._busy:
                continue
            next_try = self.db.get(f"next_try:{post['id']}")
            if next_try and now < _utc(next_try):
                continue
            # после правки ждём немного тишины, чтобы взять окончательную версию
            if post["last_edit_at"] and now - _utc(post["last_edit_at"]) < timedelta(seconds=self.d.get("edit_debounce_seconds", 30)):
                continue
            asyncio.create_task(self.run_post(post["id"]))

    async def queue(self, post_id: int, nets: list[str] | None = None) -> None:
        """Команда автора: опубликовать (во все сети или только в nets). Невышедшие сети пробуем снова."""
        for pub in self.db.publications(post_id):
            if pub["state"] != "published" and (nets is None or pub["network"] in nets):
                self.db.upsert_publication(post_id, pub["network"], state="pending", error=None)
        self.db.set(f"queue_nets:{post_id}", json.dumps(nets) if nets else "")
        self.db.update_post(post_id, state="queued", reason=None)
        self.db.set(f"next_try:{post_id}", "")

    async def run_generate(self, post_id: int) -> None:
        self._busy.add(post_id)
        try:
            await self.generate(post_id)
        finally:
            self._busy.discard(post_id)

    async def run_post(self, post_id: int) -> None:
        self._busy.add(post_id)
        try:
            post = self.db.post(post_id)
            if not self.is_fresh(post):
                await self.generate(post_id)
                post = self.db.post(post_id)
            gen = json.loads(post["gen_json"] or "{}")
            if gen.get("status") == "skipped":
                self.db.update_post(post_id, state="skipped", reason=gen.get("reason"))
                await self.notify(f"Пост {post['tg_msg_id']} не публикуется: {gen.get('reason')}")
                return
            if gen.get("status") == "held":
                await self._hold_llm(post, gen)
                return
            await self.publish(post_id)
        finally:
            self._busy.discard(post_id)

    async def _hold_llm(self, post, gen) -> None:
        held_since = self.db.get(f"held_since:{post['id']}")
        if not held_since:
            held_since = now_iso()
            self.db.set(f"held_since:{post['id']}", held_since)
        if datetime.now(timezone.utc) - _utc(held_since) > timedelta(hours=24):
            self.db.update_post(post["id"], state="skipped", reason=f"LLM недоступен больше суток: {gen.get('reason')}")
            await self.notify(f"Пост {post['tg_msg_id']} пропущен: LLM недоступен больше суток ({gen.get('reason')}).")
        elif self.db.get(f"held_notified:{post['id']}") is None:
            self.db.set(f"held_notified:{post['id']}", now_iso())
            await self.notify(f"Пост {post['tg_msg_id']} ждёт: {gen.get('reason')}. Повторю попытку автоматически.")
        # следующая попытка через 10 минут; gen_hash обнуляем, чтобы адаптация повторилась
        self.db.set(f"next_try:{post['id']}", (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="seconds"))
        self.db.update_post(post["id"], gen_hash=None)

    # --- адаптация -----------------------------------------------------------

    def post_input(self, text: str, entities: list[dict], media: list[dict]) -> PostInput:
        quoted = []
        for link in entities_to_ir(text, entities).links:
            url = normalize_url(link.raw_url)
            c = classify(url, self.cfg)
            if c.link_class == "foreign_tg_post" and c.tg and c.tg.username:
                body = web.tg_post_text(c.tg.username, c.tg.msg_id)
                if body:
                    quoted.append({"ref": link.ref, "kind": "чужой пост, пересказать без указания источника", "text": body})
            elif c.link_class == "own_tg_post" and c.own_msg_id:
                body = self.db.own_post_text(c.own_msg_id)
                if body:
                    quoted.append({"ref": link.ref, "kind": "старый пост автора", "text": body[:1500]})
        return PostInput(text, entities, [{"type": m["type"]} for m in media], quoted)

    async def generate(self, post_id: int) -> AdaptResult:
        post = self.db.post(post_id)
        inp = await asyncio.to_thread(self.post_input, post["text"], json.loads(post["entities_json"]),
                                      json.loads(post["media_json"]))
        if self.db.month_cost() >= self.cfg.llm.get("monthly_budget_usd", 12):
            result = AdaptResult(status="held", reason="исчерпан месячный бюджет API")
        else:
            result = await asyncio.to_thread(safe_adapt, self.adapter(), inp, self.networks())
        for run in result.llm_runs:
            self.db.log_llm(post_id, run)
        gen = {
            "status": result.status, "reason": result.reason,
            "analysis": result.analysis,
            "publishable": result.publishable(),
            "verdicts": result.verdicts,
            "errors": {n: v.errors for n, v in result.networks.items() if v.errors},
            "skipped": {n: v.warnings for n, v in result.networks.items() if v.skipped},
            "links": [{"ref": m.ref, "class": m.link_class, "actions": {n: d["action"] for n, d in m.decisions.items()}}
                      for m in result.manifest],
            "cost": round(sum(r.cost_usd_est for r in result.llm_runs), 4),
            "outputs": result.outputs,
        }
        self.db.update_post(post_id, gen_hash=self.gen_key(post), gen_json=dumps(gen))
        log.info("пост %s адаптирован: %s, готово сетей %d, $%.3f", post_id, result.status,
                 len(gen["publishable"]), gen["cost"])
        return result

    # --- публикация ----------------------------------------------------------

    async def _download(self, media: list[dict], kind: str, max_mb: int = 20) -> list[Path]:
        paths = []
        for m in media:
            if m["type"] != kind or (m.get("size") or 0) > max_mb * 1024 * 1024:
                continue
            ext = "jpg" if kind == "photo" else "mp4"
            path = self.media_dir / f"{m['file_unique_id']}.{ext}"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                await self.bot.download(m["file_id"], destination=path)
            paths.append(path)
        return paths

    def _public_urls(self, paths: list[Path]) -> list[str] | None:
        """Публичные адреса картинок для Threads: случайные имена, файлы живут сутки (см. tick)."""
        base = os.environ.get("SMM_PUBLIC_BASE_URL", "").rstrip("/")
        if not base or not paths:
            return None
        import shutil
        import uuid
        out_dir = self.cfg.data_dir / "public" / "m"
        out_dir.mkdir(parents=True, exist_ok=True)
        urls = []
        for p in paths:
            name = f"{uuid.uuid4().hex}{p.suffix}"
            shutil.copyfile(p, out_dir / name)
            urls.append(f"{base}/m/{name}")
        return urls

    def _cleanup_public(self, max_age_hours: int = 24) -> None:
        import time
        folder = self.cfg.data_dir / "public" / "m"
        if not folder.exists():
            return
        for f in folder.iterdir():
            if f.is_file() and time.time() - f.stat().st_mtime > max_age_hours * 3600:
                f.unlink(missing_ok=True)

    async def _publish_one(self, net: str, rendered: dict, media: list[dict], done: dict) -> PublishResult:
        if net == "threads":
            photos = await self._download(media, "photo", max_mb=8)
            image_urls = self._public_urls(photos[:20])
            return await self.threads().publish({**rendered, "_had_media": bool(media)}, image_urls=image_urls, done=done)
        if net == "max":
            photos = await self._download(media, "photo")
            videos = await self._download(media, "video")
            res = await self.max().publish(rendered, images=photos, videos=videos)
            big = [m for m in media if m["type"] == "video" and (m.get("size") or 0) > 20 * 1024 * 1024]
            if big:
                res.warnings.append("видео не перенесено: больше 20 МБ")
            return res
        raise PublishError(f"публикация в {net} ещё не реализована", retryable=False)

    async def publish(self, post_id: int) -> None:
        post = self.db.post(post_id)
        self.db.update_post(post_id, state="publishing")
        gen = json.loads(post["gen_json"])
        media = json.loads(post["media_json"])
        delays = self.d.get("retry_delays_seconds", [30, 120])
        only = json.loads(self.db.get(f"queue_nets:{post_id}") or "null")
        for net in self.networks():
            if self.db.get("stopped") == "1":
                break
            if only and net not in only:
                continue
            pub = self.db.publication(post_id, net)
            if pub and pub["state"] in ("published", "skipped"):
                continue
            rendered = gen["publishable"].get(net)
            if rendered is None:
                why = gen["errors"].get(net) or gen["skipped"].get(net) or [
                    f"судья: {gen['verdicts'].get(net, {}).get('verdict', 'нет вердикта')}"]
                self.db.upsert_publication(post_id, net, state="skipped", error="; ".join(why)[:500])
                continue
            done = json.loads(pub["extra_json"]) if pub and pub["extra_json"] else {}
            for attempt in range(self.d.get("max_attempts", 3)):
                self.db.upsert_publication(post_id, net, state="publishing", attempts=attempt + 1,
                                           attempt_started_at=now_iso())
                try:
                    res = await self._publish_one(net, rendered, media, done)
                    self.db.upsert_publication(post_id, net, state="published", remote_id=res.remote_id,
                                               remote_url=res.url, extra_json=dumps({**res.extra, "warnings": res.warnings}),
                                               error=None, published_at=now_iso())
                    break
                except PublishError as exc:
                    done = exc.extra or done
                    self.db.upsert_publication(post_id, net, state="failed", error=str(exc)[:500], extra_json=dumps(done))
                    log.warning("%s: %s (попытка %d)", net, exc, attempt + 1)
                    if not exc.retryable or attempt + 1 >= self.d.get("max_attempts", 3):
                        break
                    await asyncio.sleep(delays[min(attempt, len(delays) - 1)])
        pubs = self.db.publications(post_id)
        ok = all(p["state"] in ("published", "skipped") for p in pubs) and any(p["state"] == "published" for p in pubs)
        self.db.update_post(post_id, state="done" if ok else "partial")
        await self.report(post_id)

    # --- отчёты и команды ----------------------------------------------------

    async def notify(self, text: str) -> None:
        try:
            await self.bot.send_message(self.author_id, text, disable_web_page_preview=True)
        except Exception:
            log.exception("не удалось отправить уведомление автору")

    async def report(self, post_id: int) -> None:
        post = self.db.post(post_id)
        gen = json.loads(post["gen_json"] or "{}")
        title = (post["text"].strip().splitlines() or [""])[0][:60]
        lines = [f"Пост {post['tg_msg_id']} «{title}»"]
        for p in self.db.publications(post_id):
            name = NET_TITLES.get(p["network"], p["network"])
            extra = json.loads(p["extra_json"] or "{}")
            if p["state"] == "published":
                urls = extra.get("urls") or []
                if p["network"] == "threads" and len(urls) > 1 and extra.get("mode") != "chain":
                    lines.append(f"✓ {name}: {len(urls)} отдельных поста")
                    lines += [f"   {i}. {u}" for i, u in enumerate(urls, 1)]
                else:
                    parts = len(extra.get("part_ids", [])) if p["network"] == "threads" else 0
                    chain = f" (ветка из {parts})" if parts > 1 else ""
                    lines.append(f"✓ {name}{chain}: {p['remote_url'] or 'опубликован, ссылки нет'}")
                lines += [f"   ⚠ {w}" for w in extra.get("warnings", [])]
            elif p["state"] == "skipped":
                lines.append(f"– {name}: пропущен — {p['error']}")
            else:
                lines.append(f"✗ {name}: ошибка — {p['error']}")
        retold = sum(1 for l in gen.get("links", []) if "retell" in l["actions"].values())
        if gen.get("links"):
            lines.append(f"Ссылок: {len(gen['links'])}, пересказано вместо ссылки хотя бы в одной сети: {retold}")
        lines.append(f"LLM: ${gen.get('cost', 0):.2f}")
        await self.notify("\n".join(lines))
        self.db.update_post(post_id, report_sent=1)

    def _find(self, arg: str | None):
        if not arg or not arg.strip().split()[0].isdigit():
            return None
        return self.db.post_by_msg(self.channel_id, int(arg.split()[0]))

    async def cmd_start(self, msg: Message) -> None:
        self.db.set("stopped", "0")
        await msg.answer("Работаю. На каждый новый пост в канале пришлю карточку с кнопкой «Опубликовать».\n"
                         f"«{BTN_LAST}» — карточка последнего поста канала, «{BTN_RESTART}» — перезапуск.\n"
                         "/post <номер> — опубликовать пост, /status — последние посты, /stop — остановить всё.",
                         reply_markup=MENU)

    async def btn_last_post(self, msg: Message) -> None:
        """Карточка последнего поста канала — даже если он вышел, пока менеджер был выключен."""
        last_id = await asyncio.to_thread(self._latest_channel_msg_id)
        post = self.db.post_by_msg(self.channel_id, last_id) if last_id else None
        if post is None and last_id:
            try:
                # Bot API не отдаёт историю канала, но бот-админ может переслать пост — так мы получим
                # текст со всеми скрытыми ссылками. Пересланную копию сразу удаляем.
                fwd = await self.bot.forward_message(self.author_id, self.channel_id, last_id, disable_notification=True)
                data = message_payload(fwd)
                await self.bot.delete_message(self.author_id, fwd.message_id)
                post = self.db.post(self.save_post(self.channel_id, last_id, None, data))
            except Exception:
                log.exception("не удалось получить пост %s", last_id)
        if post is None:
            recent = self.db.recent_posts(1)
            post = recent[0] if recent else None
        if post is None:
            await msg.answer("Не нашёл постов. Перешлите пост из канала сюда.", reply_markup=MENU)
            return
        await self.send_card(post["id"])

    def _latest_channel_msg_id(self) -> int | None:
        """Номер последнего поста — из публичной веб-версии канала t.me/s/<канал>."""
        own = (self.cfg.equivalents.get("own_tg_channel") or {}).get("username")
        if not own:
            return None
        try:
            html = httpx.get(f"https://t.me/s/{own}", timeout=10, headers={"User-Agent": "Mozilla/5.0"}).text
        except httpx.HTTPError:
            return None
        ids = [int(x) for x in re.findall(rf'data-post="{re.escape(own)}/(\d+)"', html, re.I)]
        return max(ids) if ids else None

    async def btn_restart(self, msg: Message) -> None:
        busy = self.db.posts_in_state("queued", "publishing")
        if busy or self._busy:
            await msg.answer("Сейчас идёт публикация или подготовка версий — перезапущу после неё. "
                             "Нажмите кнопку ещё раз через пару минут.", reply_markup=MENU)
            return
        self.db.set("restart_notify", "1")
        await msg.answer("Перезапускаюсь, это несколько секунд…", reply_markup=MENU)
        self._restart = True
        await self.dp.stop_polling()

    async def cmd_stop(self, msg: Message) -> None:
        self.db.set("stopped", "1")
        await msg.answer("Публикации остановлены до команды /start.")

    async def cmd_status(self, msg: Message) -> None:
        stopped = self.db.get("stopped") == "1"
        lines = ["⏸ Публикации остановлены" if stopped else "▶ Публикации включены",
                 f"Расход API за месяц: ${self.db.month_cost():.2f}", ""]
        states = {"new": "ждёт команды", "queued": "в очереди", "publishing": "публикуется", "done": "опубликован",
                  "partial": "опубликован частично", "held": "ждёт", "skipped": "пропущен"}
        for p in self.db.recent_posts(8):
            first_line = ((p["text"] or "").strip().splitlines() or [""])[0][:50]
            lines.append(f"{p['tg_msg_id']} · {states.get(p['state'], p['state'])} · {first_line}")
        await msg.answer("\n".join(lines))

    async def cmd_post(self, msg: Message, command: CommandObject) -> None:
        post = self._find(command.args)
        if not post:
            await msg.answer("Укажите номер поста: /post 1234 (число в конце ссылки t.me/…/1234). "
                             "Или просто перешлите пост из канала сюда.")
            return
        await self.queue(post["id"])
        await msg.answer(f"Публикую пост {post['tg_msg_id']}. Отчёт пришлю через несколько минут.")

    async def cmd_retry(self, msg: Message, command: CommandObject) -> None:
        post = self._find(command.args)
        if not post:
            await msg.answer("Укажите номер поста: /retry 1234 [threads|max]")
            return
        parts = command.args.split()
        nets = parts[1:] or [p["network"] for p in self.db.publications(post["id"]) if p["state"] != "published"]
        for net in nets:
            self.db.upsert_publication(post["id"], net, state="pending", error=None)
        self.db.update_post(post["id"], gen_hash=None)  # явный повтор — адаптируем заново
        await self.queue(post["id"], nets or None)
        await msg.answer(f"Повторяю публикацию поста {post['tg_msg_id']}: {', '.join(nets) or 'нечего повторять'}.")

    async def on_button(self, call: CallbackQuery) -> None:
        parts = (call.data or "").split(":")
        action, raw_id, net = parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else None
        post = self.db.post(int(raw_id)) if raw_id.isdigit() else None
        if post is None:
            await call.answer("Пост не найден")
            return
        title = NET_TITLES.get(net, net) if net else ""
        if action in ("pub", "pubnet"):
            if post["state"] in ("queued", "publishing"):
                await call.answer("Уже публикуется")
                return
            await self.queue(post["id"], [net] if net else None)
            await call.answer("Публикую")
            where = f" в {title}" if net else ""
            await self.notify(f"Публикую пост {post['tg_msg_id']}{where}. Отчёт пришлю через пару минут.")
        elif action == "prev":
            await call.answer("Готовлю версии")
            await self.preview_post(post["id"])
        elif action == "fix" and net:
            self.db.set("awaiting_fix", f"{post['id']}:{net}")
            await call.answer("Жду замечания")
            await self.notify(f"Напишите одним сообщением, что исправить в версии для {title} — например: "
                              f"«разбей на 3–4 поста по мыслям, сохрани пример с цифрами». /cancel — отменить.")

    def _net_buttons(self, post_id: int, net: str) -> InlineKeyboardMarkup:
        title = NET_TITLES.get(net, net)
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"✅ Опубликовать в {title}", callback_data=f"pubnet:{post_id}:{net}"),
            InlineKeyboardButton(text="✏️ Доработать", callback_data=f"fix:{post_id}:{net}"),
        ]])

    async def show_version(self, post_id: int, net: str, gen: dict) -> None:
        title = NET_TITLES.get(net, net)
        pub = self.db.publication(post_id, net)
        rendered = gen["publishable"].get(net)
        if rendered:
            if net == "threads":
                n = len(rendered["parts"])
                label = (lambda i: f"[пост {i} из {n}]\n") if rendered.get("mode") != "chain" else (lambda i: f"[{i}/{n}]\n")
                body = "\n\n".join((label(i) if n > 1 else "") + p for i, p in enumerate(rendered["parts"], 1))
            else:
                body = rendered.get("text", "")
            if net == "max" and rendered.get("buttons"):
                body += "\n\n[кнопки: " + ", ".join(f"{b['label']} → {b['url']}" for b in rendered["buttons"]) + "]"
            if net == "threads" and rendered.get("link_attachment_url"):
                body += f"\n\n[карточка ссылки: {rendered['link_attachment_url']}]"
            head = f"— {title}"
        else:
            verdict = gen["verdicts"].get(net, {})
            why = gen["errors"].get(net) or gen["skipped"].get(net) or [i["text"] for i in verdict.get("issues", [])]
            head, body = f"— {title}: версия не прошла проверку", "\n".join(why)
        if pub and pub["state"] == "published":
            await self.notify(f"{head} — уже опубликован: {pub['remote_url']}")
            return
        await self.bot.send_message(self.author_id, (head + "\n\n" + body)[:4000], disable_web_page_preview=True,
                                    reply_markup=self._net_buttons(post_id, net) if rendered else InlineKeyboardMarkup(
                                        inline_keyboard=[[InlineKeyboardButton(text="✏️ Доработать",
                                                                               callback_data=f"fix:{post_id}:{net}")]]))

    async def preview_post(self, post_id: int) -> None:
        """Версии по сетям без публикации, у каждой свои кнопки. Результат сохраняется и переиспользуется."""
        post = self.db.post(post_id)
        await self.notify(f"Готовлю версии поста {post['tg_msg_id']}, это 1–3 минуты…")
        if not self.is_fresh(post):
            self._busy.add(post_id)
            try:
                await self.generate(post_id)
            finally:
                self._busy.discard(post_id)
        gen = json.loads(self.db.post(post_id)["gen_json"] or "{}")
        if gen.get("status") != "ready":
            await self.notify(f"Не получилось подготовить версии: {gen.get('reason')}")
            return
        for net in self.networks():
            await self.show_version(post_id, net, gen)

    async def revise(self, post_id: int, net: str, notes: str) -> None:
        """Доработка одной сети по словам автора. Замечание сохраняется — из них потом соберём правила."""
        post = self.db.post(post_id)
        gen = json.loads(post["gen_json"] or "{}")
        previous = (gen.get("outputs") or {}).get(net)
        await self.notify(f"Дорабатываю версию для {NET_TITLES.get(net, net)}…")
        inp = await asyncio.to_thread(self.post_input, post["text"], json.loads(post["entities_json"]),
                                      json.loads(post["media_json"]))
        fb = {"network": net, "previous": previous or {}, "notes": notes}
        self._busy.add(post_id)
        try:
            result = await asyncio.to_thread(safe_adapt, self.adapter(), inp, None, feedback=fb)
        finally:
            self._busy.discard(post_id)
        for run in result.llm_runs:
            self.db.log_llm(post_id, run)
        self.db.add_feedback(post_id, net, notes, previous, (result.outputs or {}).get(net))
        if result.status != "ready":
            await self.notify(f"Не получилось доработать: {result.reason}")
            return
        v = result.networks[net]
        gen.setdefault("outputs", {})[net] = result.outputs.get(net)
        gen["verdicts"][net] = result.verdicts.get(net, {})
        gen["errors"].pop(net, None)
        gen["skipped"].pop(net, None)
        if v.ok and not v.skipped and result.verdicts.get(net, {}).get("verdict") in ("pass", "fix"):
            gen["publishable"][net] = v.rendered
        else:
            gen["publishable"].pop(net, None)
            if v.errors:
                gen["errors"][net] = v.errors
        gen["cost"] = round(gen.get("cost", 0) + sum(r.cost_usd_est for r in result.llm_runs), 4)
        self.db.update_post(post_id, gen_json=dumps(gen))
        await self.show_version(post_id, net, gen)

    async def on_author_dm(self, msg: Message) -> None:
        awaiting = self.db.get("awaiting_fix")
        if awaiting and msg.text and not msg.forward_origin:
            self.db.set("awaiting_fix", "")
            if msg.text.strip() == "/cancel":
                await msg.answer("Отменил.")
                return
            post_id, net = awaiting.split(":")
            await self.revise(int(post_id), net, msg.text.strip())
            return
        # Пересланный пост из своего канала → карточка с кнопками.
        origin = msg.forward_origin
        if origin is not None and getattr(origin, "type", None) == "channel" and origin.chat.id == self.channel_id:
            post = self.db.post_by_msg(self.channel_id, origin.message_id)
            if post is None:  # пост из канала, который демон не видел (например, до запуска)
                post_id = self.save_post(self.channel_id, origin.message_id, None, message_payload(msg))
            else:
                post_id = post["id"]
            await self.send_card(post_id)
            return
        await msg.answer(
            "Чтобы опубликовать пост, нажмите «Опубликовать» в карточке под ним или перешлите пост из канала сюда.\n\n"
            "/post <номер> — опубликовать пост из канала\n"
            "/test <текст> — показать, как текст выйдет в сетях (без публикации)\n"
            "/status — последние посты\n/stop и /start — остановить и включить публикации", reply_markup=MENU)

    async def cmd_test(self, msg: Message, command: CommandObject) -> None:
        if not command.args:
            await msg.answer("Напишите текст после команды: /test Ваш текст поста")
            return
        await self.dry_run(msg, text_override=command.args)

    async def dry_run(self, msg: Message, text_override: str | None = None) -> None:
        """Пробная адаптация произвольного текста: показывает версии по сетям, ничего не публикует."""
        data = message_payload(msg)
        if text_override is not None:
            data = {"text": text_override, "entities": [], "media": data["media"]}
        if not data["text"]:
            await msg.answer("Перешлите пост из канала — пришлю кнопки «Опубликовать» и «Как будет выглядеть». "
                             "Или пришлите любой текст — покажу, как он выйдет в сетях.")
            return
        await msg.answer("Готовлю пробные версии, это 1–3 минуты…")
        inp = await asyncio.to_thread(self.post_input, data["text"], data["entities"], data["media"])
        result = await asyncio.to_thread(safe_adapt, self.adapter(), inp, self.networks())
        for run in result.llm_runs:
            self.db.log_llm(None, run)
        if result.status != "ready":
            await msg.answer(f"Не получилось: {result.reason}")
            return
        for net, v in result.networks.items():
            verdict = result.verdicts.get(net, {}).get("verdict", "—")
            head = f"— {NET_TITLES.get(net, net)} · проверка: {'ok' if v.ok else 'ошибки'} · судья: {verdict}"
            if v.ok and v.rendered:
                body = "\n\n".join(v.rendered["parts"]) if net == "threads" else v.rendered.get("text", "")
                if net == "max" and v.rendered.get("buttons"):
                    body += "\n\n[кнопки: " + ", ".join(b["label"] for b in v.rendered["buttons"]) + "]"
            else:
                body = "\n".join(v.errors or v.warnings)
            issues = "\n".join(f"  ~ {i['text']}" for i in result.verdicts.get(net, {}).get("issues", []))
            await msg.answer((head + "\n\n" + body + ("\n\n" + issues if issues else ""))[:4000],
                             disable_web_page_preview=True)
        cost = sum(r.cost_usd_est for r in result.llm_runs)
        await msg.answer(f"Пробный прогон: {len(result.llm_runs)} вызовов LLM, ${cost:.3f}. Ничего не опубликовано.")

    # --- запуск --------------------------------------------------------------

    async def run(self) -> bool:
        """Возвращает True, если автор попросил перезапуск."""
        log.info("старт: канал %s, сети %s", self.channel_id, self.networks())
        asyncio.create_task(self.scheduler())
        if self.db.get("restart_notify") == "1":
            self.db.set("restart_notify", "")
            await self.bot.send_message(self.author_id, "Менеджер перезапущен и работает.", reply_markup=MENU)
        await self.dp.start_polling(
            self.bot, allowed_updates=["channel_post", "edited_channel_post", "message", "callback_query"])
        await self.bot.session.close()
        return self._restart


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    if asyncio.run(SMMDaemon(cfg).run()):
        # Перезапуск по кнопке: заменяем процесс новым — подхватятся свежий код, промпты и .env.
        log.info("перезапуск по команде автора")
        os.execv(sys.executable, [sys.executable, "-m", "smm.daemon"])


if __name__ == "__main__":
    main()
