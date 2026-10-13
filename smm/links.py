"""Политика ссылок (§2 архитектуры): нормализация, классификация, решения по сетям.

Модель реальных URL не видит и не пишет: она получает манифест с ref и действием
для каждой сети, а URL подставляет рендерер.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from smm.config import Config

NETWORKS = ("max", "vk", "fb", "threads")
RU_NETWORKS = ("max", "vk")

_TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|yclid|si|igshid|mc_cid|mc_eid|ref_src)$")
_TG_HOSTS = {"t.me", "telegram.me", "telegram.dog"}


def normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host in _TG_HOSTS:
        host = "t.me"
    if host == "vk.com" or host.endswith(".vk.com"):
        host = host[: -len("vk.com")] + "vk.ru"
    netloc = host + (f":{parts.port}" if parts.port else "")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k)])
    path = parts.path if parts.path != "/" else ""
    return urlunsplit(("https" if parts.scheme in ("http", "https") else parts.scheme, netloc, path, query, parts.fragment))


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _suffix_match(host: str, domains: list[str]) -> bool:
    for d in domains:
        d = d.lower()
        if "/" in d:  # домен с путём, например proton.me/vpn
            continue
        if host == d or host.endswith("." + d):
            return True
    return False


def _path_match(url: str, domains: list[str]) -> bool:
    bare = url.split("://", 1)[-1]
    return any("/" in d and bare.startswith(d) for d in domains)


@dataclass
class TgLink:
    kind: str  # post | channel | invite | bot | other
    username: str | None = None
    internal_id: str | None = None
    msg_id: int | None = None


def parse_tg(url: str) -> TgLink | None:
    parts = urlsplit(url)
    if host_of(url) == "telegra.ph":
        return TgLink("other")
    if host_of(url) != "t.me":
        return None
    segs = [s for s in parts.path.split("/") if s]
    if not segs:
        return TgLink("other")
    if segs[0] == "s" and len(segs) > 1:  # t.me/s/<ch>/<id> — веб-превью
        segs = segs[1:]
    if segs[0].startswith("+") or segs[0] == "joinchat":
        return TgLink("invite")
    if segs[0] == "c" and len(segs) >= 3 and segs[2].isdigit():
        return TgLink("post", internal_id=segs[1], msg_id=int(segs[2]))
    name = segs[0]
    if "start" in dict(parse_qsl(parts.query)) or name.lower().endswith("bot"):
        return TgLink("bot", username=name)
    if len(segs) >= 2 and segs[1].isdigit():
        return TgLink("post", username=name, msg_id=int(segs[1]))
    return TgLink("channel", username=name)


@dataclass
class Classified:
    link_class: str
    own_msg_id: int | None = None
    tg: TgLink | None = None


def classify(url: str, cfg: Config) -> Classified:
    """url — уже нормализованный и раскрытый."""
    own = cfg.equivalents.get("own_tg_channel") or {}
    own_user = (own.get("username") or "").lower()
    own_internal = str(own.get("internal_id") or "")

    tg = parse_tg(url)
    if tg:
        is_own = (tg.username and tg.username.lower() == own_user) or (
            tg.internal_id and own_internal and tg.internal_id == own_internal
        )
        if tg.kind == "post":
            return Classified("own_tg_post" if is_own else "foreign_tg_post", tg.msg_id if is_own else None, tg)
        if tg.kind == "channel":
            return Classified("own_tg_channel" if is_own else "foreign_tg_channel", tg=tg)
        return Classified("tg_other", tg=tg)

    bare = url.split("://", 1)[-1]
    for prefix in cfg.equivalents.get("own_profile_prefixes") or []:
        if bare.startswith(prefix):
            return Classified("own_profile")

    host = host_of(url)
    overrides = cfg.domain_overrides
    for cls, domains in overrides.items():
        if _suffix_match(host, domains or []) or _path_match(url, domains or []):
            return Classified(cls)
    d = cfg.domains
    if _suffix_match(host, d.get("circumvention", [])) or _path_match(url, d.get("circumvention", [])):
        return Classified("circumvention")
    for cls in ("meta", "blocked_ru", "geo_blocked_by_service", "ok_foreign", "ru_site"):
        if _suffix_match(host, d.get(cls, [])):
            return Classified(cls)
    return Classified("unknown")


def is_shortener(url: str, cfg: Config) -> bool:
    return _suffix_match(host_of(url), cfg.domains.get("shorteners", []))


# --- Матрица решений (§2.3) -------------------------------------------------
# keep | map | retell | name | drop

def decide(link_class: str, network: str, *, has_copy: bool = False, reachable: bool = True,
           has_profile: bool = False) -> str:
    action = _decide(link_class, network, has_copy=has_copy, reachable=reachable, has_profile=has_profile)
    # MAX — почти дословная копия поста (решение автора 12.10.2026): ничего не пересказываем,
    # вместо этого убираем ссылку, оставляя слова; автор получает предупреждение.
    if network == "max" and action == "retell":
        return "name"
    return action


def _decide(link_class: str, network: str, *, has_copy: bool, reachable: bool, has_profile: bool) -> str:
    ru = network in RU_NETWORKS
    if link_class == "circumvention":
        return "drop"  # пост целиком уходит в skipped:vpn раньше, это страховка
    if link_class == "own_tg_post":
        return "map" if has_copy else "retell"
    if link_class in ("own_tg_channel", "foreign_tg_channel", "tg_other"):
        return "drop"
    if link_class == "foreign_tg_post":
        return "retell"
    if link_class == "own_profile":
        return "map" if has_profile else "drop"
    if link_class == "meta":
        return "name" if ru else "keep"
    if link_class == "blocked_ru":
        return "name" if ru else "keep"
    if link_class in ("geo_blocked_by_service", "ok_foreign"):
        return "keep"
    if link_class in ("ru_site", "unknown"):
        # reachable — ответ HEAD с сервера; для ru_site в Threads важен доступ из-за рубежа
        return "keep" if reachable else "name"
    return "name"


@dataclass
class ManifestLink:
    ref: str
    anchor: str
    raw_url: str
    final_url: str
    link_class: str
    own_msg_id: int | None = None
    decisions: dict[str, dict] = field(default_factory=dict)  # net → {action, target_url, target_kind}
    quoted_source: str | None = None

    def for_editor(self) -> dict:
        """То, что видит модель: без реальных URL."""
        return {
            "ref": self.ref,
            "anchor": self.anchor,
            "class": self.link_class,
            "domain": host_of(self.final_url) if self.link_class not in ("own_tg_post", "foreign_tg_post") else None,
            "actions": {net: {"action": d["action"], "target_kind": d.get("target_kind")} for net, d in self.decisions.items()},
            "quoted_source": self.quoted_source,
        }


_TARGET_KIND = {
    "own_tg_post": "ваш пост в этой сети",
    "own_profile": "ваш профиль в этой сети",
    "meta": "страница в сервисе Meta",
    "geo_blocked_by_service": "сайт сервиса",
    "ok_foreign": "внешний сайт",
    "ru_site": "сайт",
    "unknown": "сайт",
    "blocked_ru": "внешний сайт",
}


def build_manifest(
    links: list,  # list[smm.ir.LinkRef]
    cfg: Config,
    *,
    resolve: Callable[[str], str] = lambda u: u,
    reachable: Callable[[str], bool] = lambda u: True,
    copy_url: Callable[[int, str], str | None] = lambda msg_id, net: None,
    networks: tuple[str, ...] = NETWORKS,
) -> list[ManifestLink]:
    """resolve — раскрытие сокращателей; copy_url(msg_id, net) — URL копии своего поста в сети."""
    profiles = cfg.equivalents.get("profile_by_network") or {}
    manifest = []
    for link in links:
        url = normalize_url(link.raw_url)
        if is_shortener(url, cfg):
            url = normalize_url(resolve(url))
        c = classify(url, cfg)
        ml = ManifestLink(link.ref, link.anchor, link.raw_url, url, c.link_class, c.own_msg_id)
        for net in networks:
            target = None
            copy = copy_url(c.own_msg_id, net) if c.link_class == "own_tg_post" and c.own_msg_id else None
            profile = profiles.get(net)
            action = decide(c.link_class, net, has_copy=bool(copy), reachable=reachable(url), has_profile=bool(profile))
            if action == "keep":
                target = url
            elif action == "map":
                target = copy if c.link_class == "own_tg_post" else profile
            ml.decisions[net] = {"action": action, "target_url": target, "target_kind": _TARGET_KIND.get(c.link_class)}
        manifest.append(ml)
    return manifest
