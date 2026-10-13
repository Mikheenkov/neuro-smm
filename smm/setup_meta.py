"""Получение долгоживущих токенов Meta. Токены пишутся сразу в .env, в консоль не выводятся.

    uv run python -m smm.setup_meta threads-check  # проверить токен из User Token Generator (проще всего)
    uv run python -m smm.setup_meta threads     # вход в Threads через браузер, токен на 60 дней
    uv run python -m smm.setup_meta facebook    # бессрочный токен Страницы из токена Graph API Explorer
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from smm.config import ROOT, load_config, set_env_value

ENV = ROOT / ".env"
THREADS_SCOPES = "threads_basic,threads_content_publish,threads_manage_replies"


def _get(url: str, **params) -> dict:
    r = httpx.get(url, params=params, timeout=30)
    data = r.json()
    if r.status_code != 200 or "error" in data:
        raise SystemExit(f"Ошибка Meta API: {data.get('error', data)}")
    return data


def setup_threads(redirect_uri: str) -> None:
    cfg = load_config()
    app_id, secret = cfg.secret("THREADS_APP_ID"), cfg.secret("THREADS_APP_SECRET")
    url = "https://threads.net/oauth/authorize?" + urlencode({
        "client_id": app_id, "redirect_uri": redirect_uri, "scope": THREADS_SCOPES, "response_type": "code",
    })
    print("1. Сейчас откроется браузер. Войдите в Threads и разрешите доступ приложению.")
    print(f"   Если браузер не открылся, откройте ссылку вручную:\n   {url}\n")
    webbrowser.open(url)
    print(f"2. Браузер перейдёт на {redirect_uri} — страница может показать ошибку, это нормально.")
    pasted = input("   Скопируйте полный адрес из адресной строки и вставьте сюда: ").strip()
    code = parse_qs(urlsplit(pasted).query).get("code", [pasted])[0].split("#")[0]

    r = httpx.post("https://graph.threads.net/oauth/access_token", data={
        "client_id": app_id, "client_secret": secret, "grant_type": "authorization_code",
        "redirect_uri": redirect_uri, "code": code,
    }, timeout=30)
    short = r.json()
    if "access_token" not in short:
        raise SystemExit(f"Не удалось обменять код: {short}")
    long = _get("https://graph.threads.net/access_token", grant_type="th_exchange_token",
                client_secret=secret, access_token=short["access_token"])
    me = _get("https://graph.threads.net/v1.0/me", fields="id,username", access_token=long["access_token"])

    set_env_value(ENV, "THREADS_ACCESS_TOKEN", long["access_token"])
    set_env_value(ENV, "THREADS_USER_ID", me["id"])
    set_env_value(ENV, "THREADS_TOKEN_OBTAINED_AT", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    days = int(long.get("expires_in", 0)) // 86400
    print(f"\nГотово: аккаунт @{me['username']}, токен записан в .env (действует {days} дн., обновлять будет демон).")


def check_threads() -> None:
    """Токен из User Token Generator уже вписан в .env: проверяем его и дописываем user_id."""
    cfg = load_config()
    token = cfg.secret("THREADS_ACCESS_TOKEN")
    me = _get("https://graph.threads.net/v1.0/me", fields="id,username", access_token=token)
    set_env_value(ENV, "THREADS_USER_ID", me["id"])
    set_env_value(ENV, "THREADS_TOKEN_OBTAINED_AT", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    print(f"Готово: токен рабочий, аккаунт @{me['username']} (id {me['id']}) записан в .env.")


def setup_facebook(page_id: str | None) -> None:
    cfg = load_config()
    app_id, secret = cfg.secret("META_APP_ID"), cfg.secret("META_APP_SECRET")
    short = cfg.secret("FB_USER_TOKEN")
    long = _get("https://graph.facebook.com/oauth/access_token", grant_type="fb_exchange_token",
                client_id=app_id, client_secret=secret, fb_exchange_token=short)["access_token"]
    pages = _get("https://graph.facebook.com/me/accounts", fields="id,name,tasks,access_token",
                 access_token=long).get("data", [])
    if not pages:
        raise SystemExit("Токен не видит ни одной Страницы. Проверьте, что при выдаче токена выбрана ваша Страница.")
    if page_id:
        pages = [p for p in pages if p["id"] == page_id]
    if len(pages) != 1:
        print("Найдено несколько Страниц, запустите снова с --page-id:")
        for p in pages:
            print(f"  {p['id']}  {p['name']}")
        raise SystemExit(1)
    page = pages[0]
    if "CREATE_CONTENT" not in page.get("tasks", []):
        raise SystemExit(f"У токена нет права публиковать на Странице «{page['name']}» (CREATE_CONTENT).")
    info = _get("https://graph.facebook.com/debug_token", input_token=page["access_token"],
                access_token=f"{app_id}|{secret}")["data"]
    set_env_value(ENV, "FB_PAGE_ID", page["id"])
    set_env_value(ENV, "FB_PAGE_TOKEN", page["access_token"])
    set_env_value(ENV, "FB_USER_TOKEN", "")
    expires = "бессрочный" if not info.get("expires_at") else datetime.fromtimestamp(info["expires_at"]).isoformat()
    print(f"Готово: Страница «{page['name']}» ({page['id']}), токен записан в .env ({expires}).")
    print(f"Разрешения: {', '.join(info.get('scopes', []))}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("threads")
    t.add_argument("--redirect", default="https://localhost/", help="Redirect URI, указанный в настройках приложения")
    sub.add_parser("threads-check", help="проверить токен из User Token Generator")
    f = sub.add_parser("facebook")
    f.add_argument("--page-id")
    args = ap.parse_args()
    if args.cmd == "threads":
        setup_threads(args.redirect)
    elif args.cmd == "threads-check":
        check_threads()
    else:
        setup_facebook(args.page_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
