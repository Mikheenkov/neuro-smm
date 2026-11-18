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
    # Только буквы и цифры: сети показывают эмодзи картинками, и в innerText их нет.
    text = re.sub(r"[^\w\s]|_", " ", re.sub(r"https?://\S+", "", text or ""))
    return re.sub(r"\s+", " ", text).strip().lower()


async def _pause(lo: float = 0.8, hi: float = 2.5) -> None:
    await asyncio.sleep(random.uniform(lo, hi))  # человеческий темп, без серий мгновенных действий


def _btn(page: Page, *names: str):
    """Кнопка по видимому названию (англ. или рус. интерфейс). VK в ноябре 2026 убрал data-testid —
    опираемся на то, что видит человек."""
    pattern = re.compile("^(" + "|".join(re.escape(n) for n in names) + ")$", re.I)
    return page.get_by_role("button", name=pattern)


class BrowserPublisher:
    network = ""

    def __init__(self, *, shots_dir: Path, cdp_host: str = "browser"):
        self.shots_dir = shots_dir
        self.cdp_host = cdp_host

    async def publish(self, rendered: dict, *, images: list[Path] | None = None, rehearsal: bool = False) -> PublishResult:
        return await self._run(lambda page: self._publish(page, rendered["text"], images or [], rehearsal))

    async def _run(self, scenario) -> PublishResult:
        async with _LOCK:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(_cdp_url(self.cdp_host), timeout=30000)
                page = await browser.contexts[0].new_page()
                try:
                    await page.set_viewport_size({"width": 1400, "height": 1000})
                    return await scenario(page)
                except PublishError:
                    await self._shot(page, "error")
                    raise
                except Exception as exc:
                    shot = await self._shot(page, "error")
                    raise PublishError(f"{self.network}: сбой сценария браузера ({type(exc).__name__}: {str(exc)[:150]}); "
                                       f"снимок экрана: {shot}", retryable=True) from exc  # таймауты и сбои страницы чаще временные;
                    # дубля не будет: перед публикацией VK и Facebook проверяют, нет ли поста уже на стене
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
        # Ссылка-дата каждого поста ведёт на /wall<id>_<номер>; пост — ближайший [data-post-id].
        date_link = f'a[href*="/wall{self.user_id}_"]'
        try:
            await page.locator(date_link).first.wait_for(timeout=40000)
        except Exception:
            if await page.get_by_text(re.compile(r"^(no posts yet|на стене пока нет записей)", re.I)).count():
                return []
            raise PublishError("VK: стена не загрузилась (не нашёл посты)", retryable=True)
        return await page.evaluate("""(sel) => {
            const seen = new Set(), out = [];
            for (const a of document.querySelectorAll(sel)) {
                const post = a.closest('[data-post-id]');
                const id = post ? post.dataset.postId : a.getAttribute('href');
                if (seen.has(id) || !/wall-?\\d+_\\d+$/.test(a.getAttribute('href') || '')) continue;
                seen.add(id);
                out.push([a.href, (post && post.innerText) || '']);
                if (out.length >= 5) break;
            }
            return out;
        }""", date_link)

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
        await _btn(page, "Create post", "Создать пост").first.click(timeout=40000)
        dialog = page.locator("[role=dialog]").filter(has=page.locator("[contenteditable=true]"))
        box = dialog.locator("[contenteditable=true]").first
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
            await dialog.locator('input[type=file][accept^="image/"]').first.set_input_files(
                [str(p) for p in images[:10]])
            await page.wait_for_timeout(4000 + 2500 * len(images[:10]))
        await _pause()
        await box.click()
        await page.keyboard.insert_text(text)
        await _pause()
        if _norm(await box.inner_text())[:80] != _norm(text)[:80]:
            raise PublishError("VK: текст вставился не полностью — публикацию остановил", retryable=False)
        await _btn(page, "Next", "Далее").last.click()
        submit = _btn(page, "Publish", "Опубликовать").last
        await submit.wait_for(timeout=20000)
        if rehearsal:
            shot = await self._shot(page, "rehearsal")
            return PublishResult("", None, {"rehearsal": True, "shot": shot}, ["репетиция: дошёл до кнопки «Publish», не нажимал"])
        await _pause(1.5, 3)
        await submit.click()
        await submit.wait_for(state="detached", timeout=60000)
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
        await dlg.wait_for(timeout=60000)  # окно создания поста у Facebook иногда грузится долго
        box = dlg.locator("[contenteditable=true][role=textbox]").first
        await _pause()
        await box.click()
        await page.keyboard.press("Control+A")  # Facebook мог сохранить черновик прошлой попытки
        await page.keyboard.press("Delete")
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
        post_btn = None
        for _ in range(20):  # кнопка «Post» на экране «Post settings»; берём видимую
            cands = page.get_by_role("button", name=self._POST)
            for i in range(await cands.count()):
                if await cands.nth(i).is_visible():
                    post_btn = cands.nth(i)
            if post_btn:
                break
            await page.wait_for_timeout(1000)
        if post_btn is None:
            raise PublishError("Facebook: не нашёл кнопку «Post»", retryable=False)
        if rehearsal:
            shot = await self._shot(page, "rehearsal")
            return PublishResult("", None, {"rehearsal": True, "shot": shot}, ["репетиция: дошёл до кнопки «Post», не нажимал"])
        await _pause(1.5, 3)
        await post_btn.click()
        await page.locator("[role=dialog]:has([contenteditable=true])").wait_for(state="detached", timeout=90000)
        await page.wait_for_timeout(8000)
        await self._recent_posts(page)
        url = await self.permalink(page, text)
        if not url:
            return PublishResult("", self.profile_url, {}, ["пост опубликован, но прямую ссылку найти не удалось — ссылка на профиль"])
        return PublishResult(url, url)

    async def permalink(self, page: Page, text: str) -> str | None:
        """Ссылка на пост: Facebook подставляет настоящий адрес в ссылку времени публикации только при наведении."""
        head = _norm(text)[:40]
        arts = page.locator("[role=article], [aria-posinset]")
        for i in range(min(await arts.count(), 6)):
            art = arts.nth(i)
            if head not in _norm(await art.inner_text()):
                continue
            anchors = art.locator("a[role=link]")
            for j in range(min(await anchors.count(), 15)):
                a = anchors.nth(j)
                try:
                    await a.hover(timeout=2000)
                    await page.wait_for_timeout(400)
                except Exception:
                    continue
                href = await a.get_attribute("href") or ""
                if re.search(r"/posts/|pfbid|story_fbid|permalink", href):
                    return href.split("?")[0]
            return None
        return None


async def _largest(loc) -> dict:
    """Холст редактора историй — самый большой canvas в окне редактора."""
    boxes = [b for b in [await loc.nth(i).bounding_box() for i in range(await loc.count())] if b]
    if not boxes:
        raise PublishError("VK: не нашёл холст редактора историй", retryable=True)
    return max(boxes, key=lambda b: b["width"] * b["height"])


async def run_in_browser(fn, cdp_host: str = "browser"):
    """Любая работа в серверном Chromium (например, отрисовка карточки сторис) — под тем же замком."""
    async with _LOCK:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(_cdp_url(cdp_host), timeout=30000)
            page = await browser.contexts[0].new_page()
            try:
                return await fn(page)
            finally:
                await page.close()


class VKStoryPublisher(VKPublisher):
    """Сторис на личной странице VK: картинка + стикер «ссылка» на пост в VK, перетащенный на место кнопки."""
    network = "story_vk"
    _EDITOR = "https://vk.ru/feed?w=story_publisher%3Fentrypoint%3Dstories_feed"

    async def publish_story(self, card: Path, link_url: str, link_text: str, cta_box: dict, *,
                            rehearsal: bool = False) -> PublishResult:
        return await self._run(lambda page: self._story(page, card, link_url, link_text, cta_box, rehearsal))

    async def _story(self, page: Page, card: Path, link_url: str, link_text: str, cta_box: dict,
                     rehearsal: bool) -> PublishResult:
        await page.goto(self._EDITOR, wait_until="domcontentloaded")
        await self._check_login(page)
        file_input = page.locator('input[type=file]').first
        try:
            await file_input.wait_for(state="attached", timeout=40000)
        except Exception:
            raise PublishError("VK: редактор историй не открылся", retryable=True)
        await file_input.set_input_files(str(card))
        stickers = _btn(page, "Stickers", "Стикеры").first
        try:
            await stickers.wait_for(timeout=40000)
        except Exception:
            raise PublishError("VK: редактор историй не открыл картинку", retryable=True)
        editor = page.locator(".vkitInternalModalBox").filter(has=stickers)
        await page.wait_for_timeout(4000)
        await _pause()
        await stickers.click()
        await page.get_by_text(re.compile(r"^(add link|добавить ссылку)$", re.I)).click(timeout=20000)
        url_input = page.locator('input[type=text][placeholder*="://"]').last
        await url_input.fill(link_url)
        await page.locator('input[type=text]:not([placeholder*="://"])').last.fill(link_text[:30])
        await _pause()
        await _btn(page, "Add", "Добавить").last.click()
        await page.wait_for_timeout(2500)
        # Стикер появляется в центре холста — переносим его туда, где на карточке оставлено место под кнопку.
        box = await _largest(editor.locator("canvas"))
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        tx, ty = box["x"] + box["width"] * cta_box["x"], box["y"] + box["height"] * cta_box["y"]
        await page.mouse.move(cx, cy)
        await page.mouse.down()
        for i in range(1, 16):
            await page.mouse.move(cx + (tx - cx) * i / 15, cy + (ty - cy) * i / 15)
            await page.wait_for_timeout(40)
        await page.mouse.up()
        await page.wait_for_timeout(1500)
        publish = _btn(page, "Publish", "Опубликовать").last
        if rehearsal:
            shot = await self._shot(page, "rehearsal")
            return PublishResult("", None, {"rehearsal": True, "shot": shot}, ["репетиция: дошёл до кнопки «Publish», не нажимал"])
        await _pause(1.5, 3)
        await publish.click()
        try:
            await editor.wait_for(state="detached", timeout=90000)
        except Exception:
            raise PublishError("VK: история не опубликовалась за 90 секунд", retryable=False)
        url = f"https://vk.ru/id{self.user_id}"
        return PublishResult("", url, {}, [])


class FBStoryPublisher(FBPublisher):
    """Сторис в личном профиле Facebook. Стикера-ссылки в веб-версии нет — только картинка."""
    network = "story_fb"
    _SHARE = re.compile(r"^(share to story|поделиться в истории|опубликовать в истории)$", re.I)

    async def publish_story(self, card: Path, *, rehearsal: bool = False) -> PublishResult:
        return await self._run(lambda page: self._story(page, card, rehearsal))

    async def _story(self, page: Page, card: Path, rehearsal: bool) -> PublishResult:
        await page.goto("https://www.facebook.com/stories/create", wait_until="domcontentloaded")
        await self._check_login(page)
        file_input = page.locator("input[type=file]").first
        try:
            await file_input.wait_for(state="attached", timeout=40000)
        except Exception:
            raise PublishError("Facebook: страница создания истории не открылась", retryable=True)
        await _pause()
        await file_input.set_input_files(str(card))
        share = page.get_by_role("button", name=self._SHARE).or_(page.get_by_text(self._SHARE)).first
        try:
            await share.wait_for(timeout=60000)
        except Exception:
            raise PublishError("Facebook: картинка истории не загрузилась", retryable=True)
        await page.wait_for_timeout(3000)
        if rehearsal:
            shot = await self._shot(page, "rehearsal")
            return PublishResult("", None, {"rehearsal": True, "shot": shot}, ["репетиция: дошёл до «Share to story», не нажимал"])
        await _pause(1.5, 3)
        await share.click()
        try:
            await page.wait_for_url(lambda u: "stories/create" not in u, timeout=90000)
        except Exception:
            raise PublishError("Facebook: история не опубликовалась за 90 секунд", retryable=False)
        return PublishResult("", self.profile_url, {}, [])
