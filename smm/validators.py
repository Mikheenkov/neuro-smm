"""Валидаторы версий по сетям (§1.5, §3). Рендерят ответ редактора в готовый payload.

Ошибки (`errors`) — то, что нельзя публиковать: лимиты сети, битая разметка, правила автора (Telegram, VPN).
При ошибках сеть перегенерируется. Подозрения (`notes`) — выдуманные числа и названия, потерянные ссылки:
код их только подсказывает судье, а решает судья, глядя на исходник."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from smm.config import Config
from smm.filters import VPN_RE
from smm.invariants import Invariants, check_invariants
from smm.ir import IRError, ir_plain_text, ir_refs, parse_ir
from smm.links import host_of
from smm.render import render_max, render_plain, render_threads

_URL_IN_TEXT = re.compile(r"https?://|www\.|\bt\.me/|\b[a-z0-9-]+\.(ru|com|org|net|io|ai|me|dev|co)(/|\b)", re.I)
_TG_MENTION = re.compile(r"телеграм\w*|telegram|\bтг\b|\btg\b|т\.\s?me|\bt\.me\b", re.I)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
_TOPIC_TAG = re.compile(r"^[^.&]{1,50}$")


@dataclass
class ValidationResult:
    network: str
    ok: bool = True
    skipped: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rendered: dict = field(default_factory=dict)

    def err(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)


def threads_len(text: str) -> int:
    """Консервативный подсчёт: эмодзи за 4 символа [проверить реальный подсчёт Threads]."""
    return len(text) + 3 * len(_EMOJI.findall(text))


def _check_ir(res: ValidationResult, ir: str, refs_allowed: set[str], label: str,
              url_anchor_hosts: dict[str, str] | None = None) -> str | None:
    """url_anchor_hosts: ref → домен ссылки, которую в этой сети можно оставить. Если у автора ссылка была
    голым адресом, анкором остаётся сам адрес — это разрешено, но только адрес той же ссылки."""
    try:
        root = parse_ir(ir)
    except IRError as exc:
        res.err(f"{label}: {exc}")
        return None
    for ref in ir_refs(ir):
        if ref not in refs_allowed:
            res.err(f"{label}: ref {ref} нет в манифесте")
    plain = ir_plain_text(ir)
    check_text = plain
    for el in root.iter("a"):
        anchor = "".join(el.itertext())
        host = (url_anchor_hosts or {}).get(el.get("ref"))
        if host and _URL_IN_TEXT.search(anchor) and host.removeprefix("www.") in anchor.lower():
            check_text = check_text.replace(anchor, " ", 1)
    if _URL_IN_TEXT.search(check_text):
        res.err(f"{label}: в тексте есть URL или домен — ссылки ставятся только через <a ref>")
    return plain


def _check_text_rules(res: ValidationResult, plain: str, *, allow_tg: bool) -> None:
    if not allow_tg and _TG_MENTION.search(plain):
        res.err(f"упоминание Telegram: «{_TG_MENTION.search(plain).group(0)}» — в этой сети Telegram не упоминаем")
    m = VPN_RE.search(plain)
    if m:
        res.err(f"запрещённая тема VPN/обход блокировок: «{m.group(0)}»")


def _external_urls(urls: list[str]) -> list[str]:
    meta = ("facebook.com", "fb.com", "instagram.com", "threads.com", "threads.net", "whatsapp.com", "wa.me")
    return [u for u in urls if not any(host_of(u) == d or host_of(u).endswith("." + d) for d in meta)]


def validate(
    network: str,
    out: dict,
    manifest: list,
    inv: Invariants,
    cfg: Config,
    *,
    mention_refs: set[str] = frozenset(),
    allowed_extra_text: str = "",
    glossary: dict | None = None,
    has_media: bool = False,
    author_edit: bool = False,
) -> ValidationResult:
    res = ValidationResult(network)
    spec = cfg.networks.get(network, {})
    if out.get("skip"):
        res.skipped = True
        res.warnings.append(f"редактор пропустил сеть: {out.get('skip_reason')}")
        return res

    refs_allowed = {ml.ref for ml in manifest} | set(mention_refs)
    url_anchor_hosts = {ml.ref: host_of(ml.decisions[network]["target_url"]) for ml in manifest
                        if ml.decisions.get(network, {}).get("action") in ("keep", "map")
                        and ml.decisions[network].get("target_url")}
    dropped = [f"{d['fact']}" for d in out.get("dropped_facts") or []]

    if network == "threads":
        if "posts" in out:  # новая форма: ключевые мысли → посты
            posts = out.get("posts") or []
            thoughts = out.get("key_thoughts") or []
            if not thoughts:
                res.err("threads: не выделены ключевые мысли (key_thoughts)")
            if len(posts) != len(thoughts):
                res.err(f"threads: мыслей {len(thoughts)}, а постов {len(posts)} — нужен ровно один пост на мысль")
            out = {**out, "parts": [p_.get("text_ir", "") for p_ in posts],
                   "topic_tags": [p_.get("topic_tag", "") for p_ in posts]}
        parts = out.get("parts") or []
        if not parts:
            res.err("threads: нет ни одной части")
        limit = spec.get("max_posts", 3) if spec.get("mode", "separate") == "separate" else spec.get("max_parts", 5)
        if len(parts) > limit:
            res.err(f"threads: постов {len(parts)}, максимум {limit}")
        plains = [p for i, ir in enumerate(parts) if (p := _check_ir(res, ir, refs_allowed, f"часть {i + 1}", url_anchor_hosts)) is not None]
        attach_plain = ""
        if out.get("text_attachment_ir"):
            attach_plain = _check_ir(res, out["text_attachment_ir"], refs_allowed, "text_attachment", url_anchor_hosts) or ""
        if not res.ok:
            return res
        texts = render_threads(parts, manifest)
        for i, t in enumerate(texts):
            if threads_len(t) > spec.get("part_len", 500):
                res.err(f"часть {i + 1}: {threads_len(t)} символов, лимит {spec.get('part_len', 500)}")
            if len(set(re.findall(r"https?://\S+", t))) > spec.get("max_urls_per_part", 5):
                res.err(f"часть {i + 1}: больше {spec.get('max_urls_per_part', 5)} ссылок")
            if re.search(r"(^|\s)#\w", t):
                res.err(f"пост {i + 1}: хэштеги в тексте Threads не ставим (есть topic_tag)")
            if spec.get("mode", "separate") == "separate" and re.match(r"\s*\(?\d+\s*/\s*\d+", t):
                res.err(f"пост {i + 1}: нумерация «1/3» — посты отдельные, не цепочка")
        tags = [t.lstrip("#").strip() for t in (out.get("topic_tags") or [])]
        if len(tags) != len(parts):
            res.err(f"topic_tags: нужен ровно один хэштег на каждый пост ({len(parts)}), а их {len(tags)}")
        for t in tags:
            if not _TOPIC_TAG.match(t):
                res.err(f"хэштег «{t}» невалиден (1–50 символов, без точек и &)")
        link_att = out.get("link_attachment_ref")  # к посту с фото карточку не прикрепляем — это решает издатель
        full_plain = "\n".join(plains) + "\n" + attach_plain
        _check_text_rules(res, full_plain, allow_tg=False)
        res.rendered = {
            "parts": texts,
            "text_attachment": render_plain(out["text_attachment_ir"], manifest, "threads") if out.get("text_attachment_ir") else None,
            "link_attachment_url": next((ml.decisions["threads"]["target_url"] for ml in manifest if ml.ref == link_att), None),
            "topic_tags": tags,
            "mode": spec.get("mode", "separate"),
            "key_thoughts": [t.get("thought") for t in out.get("key_thoughts") or []],
        }
    elif network == "story":
        # Текст карточки — он же подпись к сторис (решение автора): отдельного caption нет.
        title = (out.get("card_title") or "").strip()
        if not title:
            res.err("story: пустой card_title")
        if len(title) > spec.get("card_title_max", 90):
            res.err(f"story: card_title {len(title)} символов, лимит {spec.get('card_title_max', 90)}")
        _check_text_rules(res, title, allow_tg=False)  # карточка уходит и в VK, и в Facebook
        res.rendered = {"card_title": title, "caption": title}
        return res  # инварианты для Stories не проверяем: это тизер, не пересказ
    else:
        ir = out.get("text_ir") or ""
        full_plain = _check_ir(res, ir, refs_allowed, network, url_anchor_hosts)
        if full_plain is None:
            return res
        _check_text_rules(res, full_plain, allow_tg=False)
        if network == "max":
            buttons = out.get("buttons") or []
            if len(buttons) > spec.get("max_buttons", 2):
                res.err(f"max: кнопок {len(buttons)}, максимум {spec.get('max_buttons', 2)}")
            for b in buttons:
                if b.get("ref") not in refs_allowed:
                    res.err(f"max: кнопка ссылается на неизвестный ref {b.get('ref')}")
                if len(b.get("label", "")) > spec.get("button_label_max", 30):
                    res.err(f"max: подпись кнопки «{b.get('label')}» длиннее {spec.get('button_label_max', 30)}")
            res.rendered = render_max(ir, manifest, buttons, meta_footnote=cfg.networks.get("meta_footnote", True),
                                      has_media=has_media)
            if len(res.rendered["text"]) > spec.get("max_len", 4000):
                res.err(f"max: {len(res.rendered['text'])} символов с разметкой, лимит {spec.get('max_len', 4000)}")
        else:  # vk, fb
            hashtags = out.get("hashtags") or []
            if len(hashtags) > spec.get("max_hashtags", 2):
                res.err(f"{network}: хэштегов {len(hashtags)}, максимум {spec.get('max_hashtags', 2)}")
            text = render_plain(ir, manifest, network, meta_footnote=network == "vk" and cfg.networks.get("meta_footnote", True),
                                hashtags=hashtags)
            urls = re.findall(r"https?://\S+", text)
            if any(host_of(u) == "t.me" for u in urls):
                res.err(f"{network}: ссылка на t.me запрещена")
            limit = spec.get("max_external_urls")  # по умолчанию ссылок сколько угодно, как у автора
            if limit and len(_external_urls(urls)) > limit:
                res.err(f"{network}: внешних ссылок {len(_external_urls(urls))}, максимум {limit}")
            if len(text) > spec.get("max_len", 4000):
                res.err(f"{network}: {len(text)} символов, лимит {spec.get('max_len', 4000)}")
            res.rendered = {"text": text}

    _check_links_kept(res, network, out, manifest, spec)
    chk = check_invariants(inv, full_plain, dropped, allowed_extra_text=allowed_extra_text, glossary=glossary)
    # Пропуски фактов — нормальная адаптация (решение автора 05.11.2026); включается check_missing_facts.
    # Автор сам попросил убрать часть текста — тоже не ошибка.
    if not spec.get("check_missing_facts", False) or author_edit:
        chk.missing, chk.dropped_ratio = [], 0.0
    for e in chk.errors(spec.get("max_dropped_ratio", 1.0)):
        res.note(e)
    return res


def _check_links_kept(res: ValidationResult, network: str, out: dict, manifest: list, spec: dict) -> None:
    """Ссылки — важная часть поста: всё, что разрешено оставить (keep/map), должно остаться в тексте.
    Для MAX засчитывается и кнопка. Ссылки с одним адресом считаются одной (автор мог разбить анкор
    на «🌹 » и «Вечеринка»). Потеря — подсказка судье, а не отказ. Threads берёт отдельные мысли — там не проверяем."""
    kept = [ml for ml in manifest if ml.decisions.get(network, {}).get("action") in ("keep", "map")]
    if not kept or network == "threads":
        return
    irs = list(out.get("parts") or []) if network == "threads" else [out.get("text_ir") or ""]
    if network == "threads" and out.get("text_attachment_ir"):
        irs = [*irs, out["text_attachment_ir"]]
    present: set[str] = set()
    for ir in irs:
        try:
            present.update(ir_refs(ir))
        except IRError:
            return  # ошибку разметки уже сообщили выше
    if network == "max":
        present.update(b.get("ref") for b in out.get("buttons") or [])
    url = {ml.ref: ml.decisions[network].get("target_url") or ml.ref for ml in kept}
    present_urls = {url[r] for r in present if r in url}
    missing, seen = [], set()
    for ml in kept:
        if url[ml.ref] not in present_urls and url[ml.ref] not in seen:
            seen.add(url[ml.ref])
            missing.append(ml)
    limit = spec.get("max_external_urls")
    required = min(len(seen | present_urls), limit) if limit else len(seen | present_urls)
    if len(present_urls) < required:
        for ml in missing:
            res.note(f"пропала ссылка {ml.ref} («{ml.anchor.strip()}»)")
