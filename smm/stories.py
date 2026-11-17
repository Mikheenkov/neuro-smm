"""Карточка сторис 1080×1920 (Telegram и Instagram) по образцу автора storystyle.png.

- Фон — картинка поста, если она есть (первая фотография). Нет картинки — случайное фото автора из
  фотобанка (photobank/). Фон всегда занимает весь кадр. Вертикальная картинка просто масштабируется;
  горизонтальную или квадратную OpenAI Images пересобирает в вертикальную (фото — перекадрирует,
  заголовки, даты, списки — перекомпонует). Не получилось — картинка обрезается на весь кадр.
- Тонировка #936A58: от левого нижнего угла вправо и вверх. Внизу, под текстом, почти сплошной цвет,
  вверху фото естественное.
- Внизу — текст (заголовок поста или ключевая мысль), под ним в самом низу — стикер «Ссылка на пост».
- Слева вверху — знак автора (ломаная из трёх пиков).

Фото выбирается один раз на пост и запоминается: предпросмотр и публикация показывают одну и ту же картинку.
Рисует HTML-шаблон в браузере (шрифт с кириллицей из Google Fonts), поэтому код не зависит от шрифтов контейнера.
"""

from __future__ import annotations

import base64
import json
import logging
import random
from html import escape
from pathlib import Path

log = logging.getLogger(__name__)

W, H = 1080, 1920
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
TINT = "147,106,88"  # #936A58

# Стикер-ссылка в самом низу (пиксели карточки). Telegram: сюда ставим кликабельную зону «перейти к посту»,
# VK: сюда перетаскиваем настоящий стикер-ссылку VK (тогда на карточке его не рисуем).
STICKER = {"left": 96, "top": 1682, "width": 480, "height": 90}
CTA_BOX = {"x": (STICKER["left"] + STICKER["width"] / 2) / W, "y": (STICKER["top"] + STICKER["height"] / 2) / H,
           "w": STICKER["width"] / W, "h": STICKER["height"] / H}  # центр и размеры в долях карточки
# Стикер-ссылка VK «Ссылка на пост» шириной ≈ 58% карточки: центр ставим так, чтобы левый край совпал с текстом.
VK_STICKER_BOX = {**CTA_BOX, "x": STICKER["left"] / W + 0.295}

_LINK_ICON = ('<svg viewBox="0 0 24 24" width="44" height="44" fill="none" stroke="#2F72EA" stroke-width="2.4" '
              'stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.07 0l3-3a5 5 0 0 0-7.07-7.07l-1.5 1.5"/>'
              '<path d="M14 11a5 5 0 0 0-7.07 0l-3 3a5 5 0 0 0 7.07 7.07l1.5-1.5"/></svg>')
_LOGO = ('<svg class="logo" viewBox="0 0 160 42" width="160" height="42" fill="none" stroke="#1c1c1c" stroke-width="3.6" '
         'stroke-linecap="round" stroke-linejoin="round"><polyline points="2,40 28,3 54,40 80,3 106,40 132,3 158,40"/></svg>')


class StoryError(Exception):
    pass


RECOMPOSE_PROMPT = (
    "Recompose this image into a vertical 2:3 portrait image that fills the whole frame, for a phone story. "
    "If it is a photograph: reframe it vertically around the main subject, extending the surroundings naturally "
    "where needed; keep every person exactly as in the original — same face, features, glasses, hair, expression, "
    "pose and clothes; do not beautify, restyle or change identity. "
    "If it contains graphic or text elements (headlines, dates, numbers, lists, labels, logos, screenshots of pages): "
    "rearrange them to fit the vertical layout, keeping every word, number and date exactly as written, in the same "
    "language and visual style; do not invent new text. "
    "Keep the lower 35% of the frame calm background without any text or important details: a caption will be "
    "placed there. Keep the top-left corner (top 10% of the height, left 30% of the width) empty: a logo goes there. "
    "No borders, no letterboxing, no blurred padding, no added watermarks."
)


def recompose(image: Path, out: Path, api_key: str, *, model: str = "gpt-image-2", timeout: float = 240) -> Path:
    """Горизонтальную или квадратную картинку поста пересобирает в вертикальную на весь кадр (OpenAI Images edits):
    фото — перекадрирует, заголовки, даты и списки — перекомпонует. Результат PNG 1024×1536."""
    import httpx
    mime = {".png": "image/png", ".webp": "image/webp"}.get(image.suffix.lower(), "image/jpeg")
    try:
        r = httpx.post("https://api.openai.com/v1/images/edits", timeout=timeout,
                       headers={"Authorization": f"Bearer {api_key}"},
                       data={"model": model, "prompt": RECOMPOSE_PROMPT, "size": "1024x1536", "quality": "high",
                             "n": "1"},
                       files={"image[]": (image.name, image.read_bytes(), mime)})
    except httpx.HTTPError as exc:
        raise StoryError(f"OpenAI Images недоступен: {type(exc).__name__}") from exc
    if r.status_code != 200:
        raise StoryError(f"OpenAI Images вернул {r.status_code}: {r.text[:200]}")
    b64 = (r.json().get("data") or [{}])[0].get("b64_json")
    if not b64:
        raise StoryError("OpenAI Images не вернул картинку")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(b64))
    return out


def pick_photo(library: Path, used: list[str]) -> Path:
    """Случайное фото, по возможности из тех, что ещё не использовались; иначе — то, что было давнее всех."""
    photos = [p for p in sorted(library.glob("*")) if p.suffix.lower() in PHOTO_EXT]
    if not photos:
        raise StoryError(f"в фотобанке {library} нет фотографий (jpg, png, webp)")
    order = {name: i for i, name in enumerate(used)}  # used — от старых к новым
    fresh = [p for p in photos if p.name not in order]
    if fresh:
        return random.choice(fresh)
    return min(photos, key=lambda p: order[p.name])


def fit_for(width: int | None, height: int | None) -> str:
    """cover — достаточно масштабировать; contain — при обрезке потерялось бы много, картинку надо пересобрать."""
    if not width or not height:
        return "cover"
    return "cover" if height / width >= 1.25 else "contain"


def card_html(photo: Path, text: str, sticker: str | None, *, link_icon: bool = True) -> str:
    mime = {".png": "image/png", ".webp": "image/webp"}.get(photo.suffix.lower(), "image/jpeg")
    img = base64.b64encode(photo.read_bytes()).decode()
    photo_html = f'<img class="photo" src="data:{mime};base64,{img}">'
    s = STICKER
    sticker_html = (f'<div class="sticker">{_LINK_ICON if link_icon else ""}<span>{escape(sticker)}</span></div>'
                    if sticker else "")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Golos+Text:wght@400;500&display=block" rel="stylesheet">
<style>
  html, body {{ margin: 0; width: {W}px; height: {H}px; overflow: hidden; background: rgb({TINT}); }}
  .photo {{ position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; object-position: center 30%; }}
  /* Тон: диагональ из левого нижнего угла вправо-вверх + основание внизу под текст. */
  .tint {{ position: absolute; inset: 0; background:
      linear-gradient(32deg, rgba({TINT},1) 0%, rgba({TINT},1) 24%, rgba({TINT},.8) 33%, rgba({TINT},.35) 44%, rgba({TINT},0) 56%),
      linear-gradient(0deg, rgba({TINT},.85) 0%, rgba({TINT},.55) 14%, rgba({TINT},0) 30%); }}
  .logo {{ position: absolute; left: 62px; top: 58px; }}
  .text {{ position: absolute; left: 96px; right: 130px; bottom: {H - s["top"] + 64}px;
    font: 400 54px/1.13 'Golos Text', sans-serif; color: #fff; letter-spacing: .2px; }}
  .sticker {{ position: absolute; left: {s["left"]}px; top: {s["top"]}px; min-width: {s["width"]}px; height: {s["height"]}px;
    box-sizing: border-box; padding: 0 34px 0 26px; display: flex; align-items: center; gap: 14px; border-radius: 22px;
    background: #fff; color: #2F72EA; font: 400 46px 'Golos Text', sans-serif; white-space: nowrap; }}
</style></head><body>
{photo_html}<div class="tint"></div>
{_LOGO}
<div class="text" id="t">{escape(text)}</div>
{sticker_html}
<script>
  document.fonts.ready.then(() => {{
    const t = document.getElementById('t'); let size = 54;
    while (t.offsetHeight > 420 && size > 38) {{ size -= 2; t.style.fontSize = size + 'px'; }}
    document.body.dataset.ready = '1';
  }});
</script></body></html>"""


async def render_card(page, photo: Path, text: str, sticker: str | None, out: Path, *, link_icon: bool = True) -> Path:
    """page — страница Playwright (локальный Chromium или серверный через CDP)."""
    await page.set_viewport_size({"width": W, "height": H})
    await page.set_content(card_html(photo, text, sticker, link_icon=link_icon), wait_until="networkidle", timeout=60000)
    await page.wait_for_function("document.body.dataset.ready === '1'", timeout=30000)
    out.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(out), type="jpeg", quality=92)
    return out


class StoryCards:
    """Фото на пост + карточки по площадкам. Какие фото уже были — в базе демона (чтобы не повторяться подряд)."""

    def __init__(self, library: Path, root: Path, db=None, *, openai_key: str | None = None,
                 model: str = "gpt-image-2"):
        self.library = library
        self.root = root
        self.db = db
        self.openai_key = openai_key
        self.model = model

    def folder(self, post_id: int) -> Path:
        return self.root / str(post_id)

    def photo(self, post_id: int) -> Path:
        marker = self.folder(post_id) / "photo.txt"
        if marker.exists():
            chosen = self.library / marker.read_text(encoding="utf-8").strip()
            if chosen.exists():
                return chosen
        used = json.loads(self.db.get("story_photos_used") or "[]") if self.db else []
        chosen = pick_photo(self.library, used)
        log.info("сторис поста %s: фото %s", post_id, chosen.name)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(chosen.name, encoding="utf-8")
        if self.db:
            used = [n for n in used if n != chosen.name] + [chosen.name]
            self.db.set("story_photos_used", json.dumps(used[-200:], ensure_ascii=False))
        return chosen

    def background(self, post_id: int, post_image: Path | None = None, fit: str = "cover") -> Path:
        """Фон на весь кадр. Картинка поста важнее фотобанка; горизонтальную пересобираем один раз на пост
        (кэш — предпросмотр и публикация показывают одно и то же и платим за пересборку один раз)."""
        if post_image is None:
            return self.photo(post_id)
        if fit == "cover":
            return post_image
        out = self.folder(post_id) / "post-vertical.png"
        if out.exists():
            return out
        if not self.openai_key:
            log.warning("сторис поста %s: OPENAI_API_KEY не задан — картинка поста будет обрезана", post_id)
            return post_image
        try:
            return recompose(post_image, out, self.openai_key, model=self.model)
        except StoryError as exc:
            log.warning("сторис поста %s: пересобрать картинку не удалось (%s) — обрезаю", post_id, exc)
            return post_image

    async def card(self, page, post_id: int, text: str, sticker: str | None, variant: str, *,
                   link_icon: bool = True, background: Path | None = None) -> Path:
        return await render_card(page, background or self.photo(post_id), text, sticker,
                                 self.folder(post_id) / f"card-{variant}.jpg", link_icon=link_icon)
