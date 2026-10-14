"""Публикация через браузер на сервере (VK — личная страница, Facebook — личный профиль).

Chromium работает в контейнере browser, вход в аккаунты сделал автор руками. Здесь — только
предсказуемые сценарии: открыть редактор, вставить готовый текст, приложить фото, опубликовать,
найти ссылку на пост. Капчи и проверки входа не обходим: останавливаемся и зовём автора.

rehearsal=True — «репетиция»: всё заполняется до последнего экрана, делается снимок, публикации нет.
"""

from __future__ import annotations

import asyncio
import random
import re
import socket
from pathlib import Path

from playwright.async_api import Page, async_playwright

from smm.publishers import PublishError, PublishResult

_LOCK = asyncio.Lock()  # один браузерный сценарий за раз


def _cdp_url(host: str = "browser", port: int = 9223) -> str:
    # Chromium принимает отладочные подключения только с Host = IP или localhost.
    return f"http://{socket.gethostbyname(host)}:{port}"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"https?://\S+", "", text or "")).strip().lower()


async def _pause(lo: float = 0.8, hi: float = 2.5) -> None:
    await asyncio.sleep(random.uniform(lo, hi))  # человеческий темп, без серий мгновенных действий


class BrowserPublisher:
    network = ""

    def __init__(self, *, shots_dir: Path, cdp_host: str = "browser"):
        self.shots_dir = shots_dir
        self.cdp_host = cdp_host

    async def publish(self, rendered: dict, *, images: list[Path] | None = None, rehearsal: bool = False) -> PublishResult:
        async with _LOCK:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(_cdp_url(self.cdp_host), timeout=30000)
                page = await browser.contexts[0].new_page()
                try:
                    await page.set_viewport_size({"width": 1400, "height": 1000})
                    return await self._publish(page, rendered["text"], images or [], rehearsal)
                except PublishError:
                    await self._shot(page, "error")
                    raise
                except Exception as exc:
                    shot = await self._shot(page, "error")
                    raise PublishError(f"{self.network}: сбой сценария браузера ({type(exc).__name__}: {str(exc)[:150]}); "
                                       f"снимок экрана: {shot}", retryable=False) from exc
                finally:
                    await page.close()

    async def _shot(self, page: Page, tag: str) -> str:
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        path = self.shots_dir / f"{self.network}-{tag}-{random.randint(1000, 9999)}.png"
        try:
            await page.screenshot(path=str(path))
        except Exception:
            return "не удалось"
        return path.name

    async def _publish(self, page: Page, text: str, images: list[Path], rehearsal: bool) -> PublishResult:
        raise NotImplementedError


class VKPublisher(BrowserPublisher):
    network = "vk"

    def __init__(self, user_id: str, **kw):
        super().__init__(**kw)
        self.user_id = user_id

    async def _check_login(self, page: Page) -> None:
        if "login" in page.url or "id.vk" in page.url or await page.locator("input[type=password]").count():
            raise PublishError("VK: нужен вход — откройте браузер на сервере и войдите в VK", retryable=False)

    async def _recent_posts(self, page: Page) -> list[tuple[str, str]]:
        """(ссылка, текст) последних своих постов на стене."""
        await page.goto(f"https://vk.ru/wall{self.user_id}?owner=1", wait_until="domcontentloaded")
        await self._check_login(page)
        await page.locator("[data-testid=post_date_block_preview]").first.wait_for(timeout=40000)
        return await page.evaluate("""() => [...document.querySelectorAll('[data-testid=post_date_block_preview]')].slice(0, 5).map(a => {
            const post = a.closest('[data-post-id], .post, article') || a.parentElement.parentElement.parentElement.parentElement;
            return [a.href, (post && post.innerText) || ''];
        })""")

    def _match(self, posts: list[tuple[str, str]], text: str) -> str | None:
        head = _norm(text)[:60]
        return next((href for href, body in posts if head and head[:40] in _norm(body)), None)

    async def _publish(self, page: Page, text: str, images: list[Path], rehearsal: bool) -> PublishResult:
        existing = self._match(await self._recent_posts(page), text)
        if existing and not rehearsal:  # защита от дубля после сбоя или повтора
            return PublishResult(existing.rsplit("/", 1)[-1], existing.replace("vk.com", "vk.ru"),
                                 {"dedup": True}, ["пост уже был на стене — повторно не публиковал"])
        await page.goto(f"https://vk.ru/id{self.user_id}", wait_until="domcontentloaded")
        await self._check_login(page)
        await page.locator("[data-testid=posting_create_post_button]").click(timeout=40000)
        box = page.locator("[data-testid=posting_base_screen_input_message]")
        # Если остался черновик (например, после репетиции), VK спрашивает «открыть черновик?» — начинаем заново.
        start_over = page.get_by_role("button", name=re.compile(r"^(start over|начать заново)$", re.I))
        try:
            await box.or_(start_over).first.wait_for(timeout=20000)
        except Exception:
            raise PublishError("VK: редактор поста не открылся", retryable=True)
        if await start_over.count() and await start_over.first.is_visible():
            await start_over.first.click()
        await box.wait_for(timeout=20000)
        if images:
            await page.locator("[data-testid=posting_base_screen_download_from_device]").set_input_files(
                [str(p) for p in images[:10]])
            await page.wait_for_timeout(4000 + 2500 * len(images[:10]))
        await _pause()
        await box.click()
        await page.keyboard.insert_text(text)
        await _pause()
        if _norm(await box.inner_text())[:80] != _norm(text)[:80]:
            raise PublishError("VK: текст вставился не полностью — публикацию остановил", retryable=False)
        await page.locator("[data-testid=posting_base_screen_next]").click()
        submit = page.locator("[data-testid=posting_submit_button]")
        await submit.wait_for(timeout=20000)
        if rehearsal:
            shot = await self._shot(page, "rehearsal")
            return PublishResult("", None, {"rehearsal": True, "shot": shot}, ["репетиция: дошёл до кнопки «Publish», не нажимал"])
        await _pause(1.5, 3)
        await submit.click()
        await page.locator("[data-testid=posting_submit_button]").wait_for(state="detached", timeout=60000)
        await page.wait_for_timeout(4000)
        url = self._match(await self._recent_posts(page), text)
        if not url:
            return PublishResult("", f"https://vk.ru/id{self.user_id}", {}, ["пост опубликован, но ссылку на него найти не удалось"])
        url = url.replace("vk.com", "vk.ru")
        return PublishResult(url.rsplit("/", 1)[-1], url)


class FBPublisher(BrowserPublisher):
    network = "fb"
    _OPENER = re.compile(r"what's on your mind|что у вас нового", re.I)
    _NEXT = re.compile(r"^(next|далее)$", re.I)
    _POST = re.compile(r"^(post|опубликовать)$", re.I)

    def __init__(self, profile_url: str, **kw):
        super().__init__(**kw)
        self.profile_url = profile_url.rstrip("/")

    async def _check_login(self, page: Page) -> None:
        if "login" in page.url or "checkpoint" in page.url or await page.locator("input[name=pass]").count():
            raise PublishError("Facebook: нужен вход или проверка «это вы?» — откройте браузер на сервере", retryable=False)

    async def _recent_posts(self, page: Page) -> list[tuple[str, str]]:
        await page.goto(self.profile_url, wait_until="domcontentloaded")
        await self._check_login(page)
        await page.wait_for_timeout(6000)
        return await page.evaluate("""() => [...document.querySelectorAll('[role=article], [aria-posinset]')].slice(0, 6).map(art => {
            const link = [...art.querySelectorAll('a[href]')].map(a => a.href).find(h => /\\/posts\\/|pfbid|story_fbid|permalink/.test(h)) || '';
            return [link.split('?')[0], art.innerText || ''];
        })""")

    def _match(self, posts: list[tuple[str, str]], text: str) -> str | None:
        head = _norm(text)[:40]
        return next((href or "?" for href, body in posts if head and head in _norm(body)), None)

    async def _publish(self, page: Page, text: str, images: list[Path], rehearsal: bool) -> PublishResult:
        existing = self._match(await self._recent_posts(page), text)
        if existing and not rehearsal:
            url = existing if existing != "?" else self.profile_url
            return PublishResult(url, url, {"dedup": True}, ["пост уже был в профиле — повторно не публиковал"])
        opener = page.get_by_role("button", name=self._OPENER).or_(page.get_by_text(self._OPENER)).first
        await opener.click(timeout=40000)
        dlg = page.locator("[role=dialog]:has([contenteditable=true])").last
        await dlg.wait_for(timeout=20000)
        box = dlg.locator("[contenteditable=true][role=textbox]").first
        await _pause()
        await box.click()
        await page.keyboard.insert_text(text)
        await _pause()
        if _norm(await box.inner_text())[:80] != _norm(text)[:80]:
            raise PublishError("Facebook: текст вставился не полностью — публикацию остановил", retryable=False)
        if images:
            await dlg.get_by_role("button", name=re.compile(r"photo.video|фото.видео", re.I)).first.click()
            await page.wait_for_timeout(1500)
            await dlg.locator("input[type=file]").first.set_input_files([str(p) for p in images[:10]])
            await page.wait_for_timeout(4000 + 2500 * len(images[:10]))
        nxt = dlg.get_by_role("button", name=self._NEXT)
        if await nxt.count():
            await nxt.first.click()
            await page.wait_for_timeout(2500)
        post_btn = page.locator("[role=dialog]").last.get_by_role("button", name=self._POST).first
        await post_btn.wait_for(timeout=20000)
        if rehearsal:
            shot = await self._shot(page, "rehearsal")
            return PublishResult("", None, {"rehearsal": True, "shot": shot}, ["репетиция: дошёл до кнопки «Post», не нажимал"])
        await _pause(1.5, 3)
        await post_btn.click()
        await page.locator("[role=dialog]:has([contenteditable=true])").wait_for(state="detached", timeout=90000)
        await page.wait_for_timeout(8000)
        url = self._match(await self._recent_posts(page), text)
        if not url or url == "?":
            return PublishResult("", self.profile_url, {}, ["пост опубликован, но прямую ссылку найти не удалось — ссылка на профиль"])
        return PublishResult(url, url)
