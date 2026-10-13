"""Находит канал MAX, куда добавлен бот, и записывает MAX_CHANNEL_ID в .env.

    uv run python -m smm.setup_max

Запускайте после того, как бот стал администратором канала. Если событие уже
прочитано ранее, удалите бота из администраторов и добавьте снова.
"""

from __future__ import annotations

import asyncio
import sys

from smm.config import ROOT, load_config, set_env_value
from smm.publishers.max_api import MaxPublisher


async def main() -> int:
    cfg = load_config()
    mx = MaxPublisher(cfg.secret("MAX_BOT_TOKEN"), None, ca_bundle=ROOT / "certs" / "russian_trusted.pem")
    me = await mx.me()
    print(f"Бот: @{me.get('username')} ({me.get('name') or me.get('first_name')})")
    marker = None
    found = []
    for _ in range(3):
        data = await mx.updates(marker=marker, timeout=5)
        marker = data.get("marker")
        for u in data.get("updates", []):
            if u.get("update_type") in ("bot_added", "chat_title_changed", "message_created"):
                chat_id = u.get("chat_id") or (u.get("message", {}).get("recipient") or {}).get("chat_id")
                kind = "канал" if u.get("is_channel") or (u.get("message", {}).get("recipient") or {}).get("chat_type") == "channel" else "чат"
                if chat_id and chat_id not in [c for c, _ in found]:
                    found.append((chat_id, kind))
        if not data.get("updates"):
            break
    # Проверяем тип каждого найденного чата: нужен именно канал, а не диалог с ботом.
    channels = []
    for chat_id, _ in found:
        r = await mx.client.get(f"/chats/{chat_id}")
        if r.status_code == 200 and r.json().get("type") == "channel":
            channels.append(chat_id)
            print(f"Канал: {r.json().get('title')} ({chat_id})")
    if not channels:
        print("Событий о добавлении бота не найдено. Добавьте бота администратором канала (или удалите и добавьте снова) и запустите ещё раз.")
        return 1
    if len(channels) > 1:
        print("Найдено несколько чатов, впишите нужный в .env вручную (MAX_CHANNEL_ID):")
        for c in channels:
            print(f"  {c}")
        return 1
    set_env_value(ROOT / ".env", "MAX_CHANNEL_ID", str(channels[0]))
    print(f"Готово: MAX_CHANNEL_ID={channels[0]} записан в .env")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
