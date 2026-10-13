"""Сетевые помощники для политики ссылок: раскрытие сокращателей, проверка доступности,
текст чужого публичного TG-поста для пересказа (§2.4)."""

from __future__ import annotations

import re
from functools import lru_cache
from html import unescape

import httpx

from smm.filters import VPN_RE

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/128 Safari/537.36"}


@lru_cache(maxsize=512)
def resolve(url: str) -> str:
    try:
        r = httpx.head(url, follow_redirects=True, timeout=5, headers=_UA, max_redirects=5)
        return str(r.url)
    except httpx.HTTPError:
        return url


@lru_cache(maxsize=512)
def reachable(url: str) -> bool:
    try:
        r = httpx.head(url, follow_redirects=True, timeout=5, headers=_UA)
        if r.status_code == 405:  # HEAD не поддерживается
            r = httpx.get(url, follow_redirects=True, timeout=5, headers=_UA)
        return r.status_code < 400 or r.status_code in (401, 403)  # 403 — антибот, но сайт жив
    except httpx.HTTPError:
        return False


_MSG_TEXT = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_STOP = re.compile(r"\berid\b|реклама", re.I)


def tg_post_text(username: str, msg_id: int, max_chars: int = 1500, *, filtered: bool = True) -> str | None:
    """Текст публичного поста из веб-виджета t.me. Ссылки выкидываются: это только данные для пересказа."""
    try:
        r = httpx.get(f"https://t.me/{username}/{msg_id}", params={"embed": "1", "mode": "tme"}, timeout=10, headers=_UA)
    except httpx.HTTPError:
        return None
    m = _MSG_TEXT.search(r.text)
    if not m:
        return None
    html = re.sub(r"<br\s*/?>", "\n", m.group(1))
    text = unescape(re.sub(r"<[^>]+>", "", html)).strip()
    text = re.sub(r"https?://\S+", "", text)
    if filtered and (VPN_RE.search(text) or _STOP.search(text)):
        return None
    return text[:max_chars] or None
