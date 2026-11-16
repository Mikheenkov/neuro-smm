"""Однократный вход в Telegram для сторис канала (сессия Telethon от аккаунта автора).

На сервере:
    docker compose run --rm -it smm python -m smm.setup_stories

Спросит номер телефона, код из Telegram и, если включён, облачный пароль — их вводит сам автор.
Сессия сохраняется в data/tg_story.session (права 600) и даёт полный доступ к аккаунту: не копируйте её.
В конце проверяет, что аккаунт может публиковать истории в канале.
"""

from __future__ import annotations

import asyncio
import os

from smm.config import load_config


async def main() -> None:
    from telethon import TelegramClient, functions

    cfg = load_config()
    session = cfg.data_dir / "tg_story.session"
    channel = (cfg.equivalents.get("own_tg_channel") or {}).get("username")
    client = TelegramClient(str(session.with_suffix("")), int(cfg.secret("TG_API_ID")), cfg.secret("TG_API_HASH"),
                            device_model="Neuro SMM stories", app_version="1.0")
    await client.start()
    os.chmod(session, 0o600)
    me = await client.get_me()
    print(f"Вход выполнен: {me.first_name} (@{me.username}), Premium: {bool(me.premium)}")
    peer = await client.get_input_entity(channel)
    st = await client(functions.premium.GetBoostsStatusRequest(peer=peer))
    print(f"Канал @{channel}: уровень {st.level}, бустов {st.boosts}")
    try:
        await client(functions.stories.CanSendStoryRequest(peer=peer))
        print("Публиковать истории в канал можно ✓")
    except Exception as exc:  # noqa: BLE001 — показываем автору как есть
        print(f"Публиковать истории пока нельзя: {exc}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
