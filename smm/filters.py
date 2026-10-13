"""Код-фильтры до LLM (§1.4): реклама и VPN/обход блокировок."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

_AD_PATTERNS = [
    r"\berid\b",
    r"(^|\s)#?реклама\b",
    r"на\s+правах\s+рекламы",
    r"партн[её]рский\s+материал",
    r"\bрекламодатель\b",
    r"\bИНН\s*:?\s*\d{10,12}\b",
    r"\bОГРН(ИП)?\s*:?\s*\d{13,15}\b",
]
_AD_RE = re.compile("|".join(_AD_PATTERNS), re.I | re.M)

VPN_WORDS = [
    r"\bvpn\b",
    r"\bвпн\b",
    r"\bвпн-",
    r"обход\w*\s+блокиров\w*",
    r"обойти\s+блокиров\w*",
    r"прокси\s+для",
    r"\bnordvpn\b",
    r"\bexpressvpn\b",
    r"\bsurfshark\b",
    r"\bprotonvpn\b",
    r"\bamnezia\w*\b",
    r"\boutline\s+vpn\b",
    r"\bшадоусокс\b|\bshadowsocks\b",
    r"\bv2ray\b|\bvless\b|\bxray\b",
]
VPN_RE = re.compile("|".join(VPN_WORDS), re.I)


@dataclass
class FilterResult:
    skip: bool
    reason: str | None = None
    evidence: str | None = None


def check_ad(text: str, urls: list[str]) -> FilterResult:
    m = _AD_RE.search(text)
    if m:
        return FilterResult(True, "ad", m.group(0).strip())
    for url in urls:
        if any(k.lower() == "erid" for k, _ in parse_qsl(urlsplit(url).query)):
            return FilterResult(True, "ad", f"erid в ссылке {url}")
    return FilterResult(False)


def check_vpn(text: str, link_classes: list[str]) -> FilterResult:
    m = VPN_RE.search(text)
    if m:
        return FilterResult(True, "vpn", m.group(0))
    if "circumvention" in link_classes:
        return FilterResult(True, "vpn", "ссылка на VPN/средство обхода")
    return FilterResult(False)
