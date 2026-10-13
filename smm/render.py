"""Рендер IR в формат каждой сети. Реальные URL подставляются только здесь, по ref."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html import escape

from smm.ir import parse_ir

META_FOOTNOTE = "*Meta признана экстремистской организацией и запрещена в РФ"
_META_MENTION = re.compile(r"\b(instagram|facebook|инстаграм\w*|фейсбук\w*|whatsapp|ватсап\w*)\b", re.I)

_HTML_TAGS = {"b", "i", "u", "s", "code", "blockquote"}
_HASHTAG = re.compile(r"(?<![\w&#/])#(?!\d+\b)[\wё]+", re.I)


def strip_hashtags(text: str) -> str:
    """Хэштеги в MAX не работают. Строки только из хэштегов и хвосты из хэштегов в конце строки удаляются,
    а хэштег внутри фразы («про #AI и цены») становится обычным словом — иначе фраза ломается."""
    tail = re.compile(r"(\s*(?<![\w&/])#(?!\d+\b)[\wё]+[,.;]?)+\s*$", re.I)
    lines = []
    for line in text.split("\n"):
        if line.strip() and not re.sub(r"[\s,.;·|]+", "", _HASHTAG.sub("", line)):
            continue  # строка состояла только из хэштегов
        line = tail.sub("", line)
        lines.append(_HASHTAG.sub(lambda m: m.group(0)[1:], line).rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _targets(manifest: list, network: str) -> dict[str, str | None]:
    """ref → URL для этой сети (None — ссылку не ставим, остаётся только текст)."""
    out = {}
    for ml in manifest:
        d = ml.decisions.get(network, {})
        out[ml.ref] = d.get("target_url") if d.get("action") in ("keep", "map") else None
    return out


def _walk_html(el: ET.Element, targets: dict, out: list[str]) -> None:
    if el.text:
        out.append(escape(el.text, quote=False))
    for child in el:
        tag = child.tag
        if tag in _HTML_TAGS:
            out.append(f"<{tag}>")
            _walk_html(child, targets, out)
            out.append(f"</{tag}>")
        elif tag == "a" and targets.get(child.get("ref")):
            out.append(f'<a href="{escape(targets[child.get("ref")])}">')
            _walk_html(child, targets, out)
            out.append("</a>")
        elif tag == "m":
            out.append(escape("".join(child.itertext()).lstrip("@"), quote=False))
        else:
            _walk_html(child, targets, out)
        if child.tail:
            out.append(escape(child.tail, quote=False))


def _walk_plain(el: ET.Element, targets: dict, out: list[str]) -> None:
    if el.text:
        out.append(el.text)
    for child in el:
        if child.tag == "a":
            anchor = "".join(child.itertext())
            url = targets.get(child.get("ref"))
            if not url:
                out.append(anchor)
            elif _same_url(anchor, url):
                out.append(url)
            else:
                out.append(f"{anchor} ({url})")
        elif child.tag == "m":
            out.append("".join(child.itertext()).lstrip("@"))
        else:
            _walk_plain(child, targets, out)
        if child.tail:
            out.append(child.tail)


def _same_url(anchor: str, url: str) -> bool:
    strip = lambda s: re.sub(r"^https?://(www\.)?", "", s.strip()).rstrip("/").lower()
    return strip(anchor) == strip(url) or strip(anchor) in strip(url) and "." in anchor


def needs_meta_footnote(plain_text: str) -> bool:
    return bool(_META_MENTION.search(plain_text))


def render_max(text_ir: str, manifest: list, buttons: list[dict] | None = None, *, meta_footnote: bool = True,
               has_media: bool = False) -> dict:
    root = parse_ir(text_ir)
    targets = _targets(manifest, "max")
    parts: list[str] = []
    _walk_html(root, targets, parts)
    html = strip_hashtags("".join(parts))
    plain = "".join(root.itertext())
    if meta_footnote and needs_meta_footnote(plain) and META_FOOTNOTE not in plain:
        html += f"\n\n<i>{escape(META_FOOTNOTE)}</i>"
    btns = [{"label": b["label"], "url": targets[b["ref"]]} for b in buttons or [] if targets.get(b.get("ref"))]
    n_links = sum(1 for v in targets.values() if v)
    return {"text": html, "buttons": btns, "disable_link_preview": has_media or n_links > 1}


def render_plain(text_ir: str, manifest: list, network: str, *, meta_footnote: bool = False,
                 hashtags: list[str] | None = None, footer: str | None = None) -> str:
    root = parse_ir(text_ir)
    parts: list[str] = []
    _walk_plain(root, _targets(manifest, network), parts)
    text = "".join(parts).strip()
    if meta_footnote and needs_meta_footnote(text) and META_FOOTNOTE not in text:
        text += f"\n\n{META_FOOTNOTE}"
    if footer:
        text += f"\n\n{footer}"
    if hashtags:
        text += "\n\n" + " ".join(h if h.startswith("#") else f"#{h}" for h in hashtags)
    return text


def render_threads(parts_ir: list[str], manifest: list) -> list[str]:
    return [render_plain(p, manifest, "threads") for p in parts_ir]
