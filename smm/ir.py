"""Внутреннее представление (IR) поста.

IR — текст на простых тегах: <b> <i> <u> <s> <code> <blockquote>,
ссылки <a ref="L1">анкор</a> и упоминания <m ref="M1">@ник</m>.
Реальные URL в IR не попадают: они лежат в манифесте ссылок по ref.

Смещения entities в Telegram считаются в UTF-16 code units, поэтому
текст сначала переводится в UTF-16 и режется по этим смещениям.
"""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html import escape

ALLOWED_TAGS = {"b", "i", "u", "s", "code", "blockquote", "a", "m"}

# Тип entity Telegram → тег IR. None — тег не нужен, текст остаётся как есть.
_TAG_BY_TYPE = {
    "bold": "b",
    "italic": "i",
    "underline": "u",
    "strikethrough": "s",
    "code": "code",
    "pre": "code",
    "blockquote": "blockquote",
    "expandable_blockquote": "blockquote",
    "text_link": "a",
    "url": "a",
    "mention": "m",
}


@dataclass
class Entity:
    type: str
    offset: int
    length: int
    url: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Entity":
        return cls(type=d["type"], offset=d["offset"], length=d["length"], url=d.get("url"))


@dataclass
class LinkRef:
    ref: str
    anchor: str
    raw_url: str
    entity_type: str  # text_link | url


@dataclass
class MentionRef:
    ref: str
    username: str  # без «@»


@dataclass
class IRResult:
    ir: str
    links: list[LinkRef] = field(default_factory=list)
    mentions: list[MentionRef] = field(default_factory=list)


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-le")


def _slice16(buf: bytes, start: int, end: int) -> str:
    return buf[start * 2 : end * 2].decode("utf-16-le")


def entities_to_ir(text: str, entities: list[Entity] | list[dict] | None) -> IRResult:
    ents = [e if isinstance(e, Entity) else Entity.from_dict(e) for e in (entities or [])]
    ents = [e for e in ents if e.type in _TAG_BY_TYPE and e.length > 0]
    # Внешние раньше внутренних: по началу, затем длиннее — первым.
    ents.sort(key=lambda e: (e.offset, -e.length))

    buf = _utf16(text)
    total = len(buf) // 2
    result = IRResult(ir="")

    # Позиции открытия/закрытия. Частичные перекрытия обрезаем по родителю.
    opens: dict[int, list[tuple[Entity, int]]] = {}
    stack_end: list[int] = []
    clipped: list[tuple[Entity, int, int]] = []
    for e in ents:
        start, end = e.offset, min(e.offset + e.length, total)
        while stack_end and stack_end[-1] <= start:
            stack_end.pop()
        if stack_end:
            end = min(end, stack_end[-1])
        if end <= start:
            continue
        clipped.append((e, start, end))
        stack_end.append(end)

    for e, start, end in clipped:
        opens.setdefault(start, []).append((e, end))

    out: list[str] = []
    stack: list[tuple[str, int]] = []  # (закрывающий тег, позиция конца)
    link_n = mention_n = 0
    boundaries = sorted({0, total, *opens.keys(), *(end for _, _, end in clipped)})

    for i, pos in enumerate(boundaries):
        while stack and stack[-1][1] == pos:
            out.append(stack.pop()[0])
        for e, end in opens.get(pos, []):
            tag = _TAG_BY_TYPE[e.type]
            if tag == "a":
                link_n += 1
                ref = f"L{link_n}"
                anchor = _slice16(buf, pos, end)
                url = e.url if e.type == "text_link" else anchor.strip()
                result.links.append(LinkRef(ref, anchor, url or "", e.type))
                out.append(f'<a ref="{ref}">')
            elif tag == "m":
                mention_n += 1
                ref = f"M{mention_n}"
                result.mentions.append(MentionRef(ref, _slice16(buf, pos, end).lstrip("@")))
                out.append(f'<m ref="{ref}">')
            else:
                out.append(f"<{tag}>")
            stack.append((f"</{tag}>", end))
        if i + 1 < len(boundaries):
            out.append(escape(_slice16(buf, pos, boundaries[i + 1]), quote=False))

    while stack:
        out.append(stack.pop()[0])
    result.ir = "".join(out)
    return result


def content_hash(text: str, entities: list[dict] | None, media_ids: list[str] | None = None) -> str:
    """Хэш содержимого: одинаковый для правок, не меняющих текст, разметку и медиа."""
    norm_entities = sorted(
        ({k: e.get(k) for k in ("type", "offset", "length", "url")} for e in entities or []),
        key=lambda e: (e["offset"], e["length"], e["type"]),
    )
    payload = json.dumps(
        {"text": text.strip(), "entities": norm_entities, "media": sorted(media_ids or [])},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class IRError(ValueError):
    pass


def parse_ir(ir: str) -> ET.Element:
    """Разбирает IR как XML и проверяет, что в нём только разрешённые теги."""
    try:
        root = ET.fromstring(f"<root>{ir}</root>")
    except ET.ParseError as exc:
        raise IRError(f"IR не разбирается как XML: {exc}") from exc
    for el in root.iter():
        if el is root:
            continue
        if el.tag not in ALLOWED_TAGS:
            raise IRError(f"запрещённый тег <{el.tag}>")
        if el.tag in ("a", "m") and not el.get("ref"):
            raise IRError(f"<{el.tag}> без ref")
    return root


def ir_plain_text(ir: str) -> str:
    """Текст без тегов: для подсчёта длины и проверки фактов."""
    return "".join(parse_ir(ir).itertext())


def ir_refs(ir: str) -> list[str]:
    return [el.get("ref") for el in parse_ir(ir).iter() if el.tag in ("a", "m")]
