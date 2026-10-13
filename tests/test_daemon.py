"""Демон без сети: приём поста, альбом, правка, адаптация на фейковой модели, публикация на фейковых издателях."""

import asyncio
import json

import pytest
from aiogram.types import Message

from smm.adapt import Adapter
from smm.config import load_config
from smm.daemon import SMMDaemon
from smm.db import now_iso
from smm.publishers import PublishError, PublishResult

from test_adapt import ENTITIES, GOOD_MAX, TEXT, THREADS, FakeProvider

CHANNEL, AUTHOR = -1001424181684, 203996119


def channel_msg(msg_id: int, text: str = "", entities=None, *, photo: str | None = None, group: str | None = None,
                caption: bool = False) -> Message:
    d = {"message_id": msg_id, "date": 1_790_000_000, "chat": {"id": CHANNEL, "type": "channel", "title": "t"}}
    if group:
        d["media_group_id"] = group
    if photo:
        d["photo"] = [{"file_id": f"f-{photo}", "file_unique_id": photo, "width": 1280, "height": 720, "file_size": 1000}]
    if text:
        d["caption" if (caption or photo) else "text"] = text
        if entities:
            d["caption_entities" if (caption or photo) else "entities"] = entities
    return Message.model_validate(d)


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("SMM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST-token")
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", str(CHANNEL))
    monkeypatch.setenv("TELEGRAM_AUTHOR_USER_ID", str(AUTHOR))
    d = SMMDaemon(load_config())
    d.notes = []
    d.published = []

    async def notify(text):
        d.notes.append(text)

    d.cards = []

    async def send_card(post_id, chat_id=None):
        d.cards.append(f"Пост {d.db.post(post_id)['tg_msg_id']}")

    async def publish_one(net, rendered, media, done):
        d.published.append((net, rendered))
        if net == "max" and getattr(d, "max_fails", False):
            raise PublishError("MAX упал", retryable=False)
        return PublishResult(f"{net}-1", f"https://example/{net}/1", {"part_ids": ["t1"]} if net == "threads" else {})

    d.notify = notify
    d.send_card = send_card
    d._publish_one = publish_one
    d.adapter = lambda: Adapter(d.cfg, FakeProvider({"max": GOOD_MAX, "threads": THREADS}, {"max": "pass", "threads": "pass"}),
                                prefilters=False)  # как в настоящем демоне
    d.post_input = lambda text, entities, media: __import__("smm.adapt", fromlist=["PostInput"]).PostInput(text, entities, media)
    return d


async def test_full_cycle_publish_and_report(daemon):
    await daemon.on_channel_post(channel_msg(10, TEXT, ENTITIES))
    post = daemon.db.post_by_msg(CHANNEL, 10)
    assert post["state"] == "new"  # ждёт команды автора
    assert "Пост 10" in daemon.cards[-1]

    await daemon.queue(post["id"])
    await daemon.run_post(post["id"])
    post = daemon.db.post(post["id"])
    assert post["state"] == "done"
    assert [n for n, _ in daemon.published] == ["threads", "max"]  # порядок публикации
    assert daemon.db.copy_url(10, "max") == "https://example/max/1"  # пригодится для ссылок на этот пост
    assert "✓ Threads: https://example/threads/1" in daemon.notes[-1]


async def test_album_is_one_post_and_edit_changes_hash(daemon):
    await daemon.on_channel_post(channel_msg(20, "подпись", photo="a", group="g1"))
    await daemon.on_channel_post(channel_msg(21, photo="b", group="g1"))
    post = daemon.db.post_by_msg(CHANNEL, 20)
    assert len(json.loads(post["media_json"])) == 2 and daemon.db.post_by_msg(CHANNEL, 21) is None

    old_hash = post["content_hash"]
    await daemon.on_edited_post(channel_msg(20, "подпись исправлена", photo="a", group="g1"))
    post = daemon.db.post(post["id"])
    assert post["content_hash"] != old_hash and post["text"] == "подпись исправлена"
    assert len(json.loads(post["media_json"])) == 2


async def test_forward_skipped_but_ad_word_is_published_on_command(daemon):
    fwd = channel_msg(30, "чужое").model_copy(update={"forward_origin": {"type": "hidden_user", "date": 1, "sender_user_name": "x"}})
    await daemon.on_channel_post(fwd)
    assert daemon.db.post_by_msg(CHANNEL, 30) is None

    # Пост про рекламу публикуется: команду дал автор, фильтр рекламы не применяется.
    await daemon.on_channel_post(channel_msg(31, TEXT + " Реклама на улице была смешная.", ENTITIES))
    post = daemon.db.post_by_msg(CHANNEL, 31)
    await daemon.queue(post["id"])
    await daemon.run_post(post["id"])
    assert daemon.db.post(post["id"])["state"] == "done"


async def test_partial_failure_reports_error(daemon):
    daemon.max_fails = True
    await daemon.on_channel_post(channel_msg(40, TEXT, ENTITIES))
    post = daemon.db.post_by_msg(CHANNEL, 40)
    await daemon.queue(post["id"])
    await daemon.run_post(post["id"])
    assert daemon.db.post(post["id"])["state"] == "partial"
    assert "✗ MAX: ошибка — MAX упал" in daemon.notes[-1]


async def test_stop_blocks_publication(daemon):
    daemon.db.set("stopped", "1")
    await daemon.on_channel_post(channel_msg(50, TEXT, ENTITIES))
    post = daemon.db.post_by_msg(CHANNEL, 50)
    await daemon.queue(post["id"])
    await daemon.tick()
    assert daemon.published == []


async def test_new_post_is_not_published_without_command(daemon):
    await daemon.on_channel_post(channel_msg(60, TEXT, ENTITIES))
    await daemon.tick()
    await asyncio.sleep(0)
    assert daemon.published == []


async def test_publish_single_network_and_revise_by_author_notes(daemon, monkeypatch):
    sent = []

    async def send_message(chat_id, text, **kw):
        sent.append((text, kw.get("reply_markup")))

    monkeypatch.setattr(daemon.bot, "send_message", send_message)
    await daemon.on_channel_post(channel_msg(70, TEXT, ENTITIES))
    post = daemon.db.post_by_msg(CHANNEL, 70)

    await daemon.preview_post(post["id"])
    threads_msg = next(t for t, kb in sent if t.startswith("— Threads"))
    assert threads_msg
    buttons = [b.callback_data for t, kb in sent if kb for row in kb.inline_keyboard for b in row]
    assert f"pubnet:{post['id']}:max" in buttons and f"fix:{post['id']}:threads" in buttons

    # Публикуем только MAX.
    await daemon.queue(post["id"], ["max"])
    await daemon.run_post(post["id"])
    assert [n for n, _ in daemon.published] == ["max"]

    # Доработка Threads по словам автора: замечание уходит модели и сохраняется.
    revised = {**THREADS, "parts": ['Первая мысль. <a ref="L1">Цены</a>.', "Claude 5.5 подешевел до $4 за миллион токенов.", "Вторая мысль."],
               "topic_tags": ["ИИ", "Бизнес", "Нейросети"]}
    daemon.adapter = lambda: Adapter(daemon.cfg, FakeProvider({"threads": revised}, {"threads": "pass"}), prefilters=False)
    await daemon.revise(post["id"], "threads", "разбей на 3 поста по мыслям")
    gen = json.loads(daemon.db.post(post["id"])["gen_json"])
    assert len(gen["publishable"]["threads"]["parts"]) == 3
    assert daemon.db.conn.execute("SELECT notes FROM feedback").fetchone()["notes"] == "разбей на 3 поста по мыслям"
    assert "[пост 1 из 3]" in sent[-1][0]

    await daemon.queue(post["id"], ["threads"])
    await daemon.run_post(post["id"])
    assert [n for n, _ in daemon.published] == ["max", "threads"]
    assert daemon.published[-1][1]["parts"][2] == "Вторая мысль."


async def test_threads_token_refreshed_weekly_and_stored_in_db(daemon, monkeypatch):
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "old")
    monkeypatch.setenv("THREADS_USER_ID", "42")
    monkeypatch.setenv("THREADS_TOKEN_OBTAINED_AT", "2026-01-01T00:00:00+00:00")
    calls = []

    async def fake_refresh(self):
        calls.append(self.token)
        return "new-token", 60 * 86400

    monkeypatch.setattr("smm.publishers.threads.ThreadsPublisher.refresh_token", fake_refresh)
    await daemon.maybe_refresh_threads_token()
    assert calls == ["old"] and daemon.threads_token() == "new-token"
    await daemon.maybe_refresh_threads_token()  # повторно в тот же день — не продлеваем
    assert calls == ["old"]
