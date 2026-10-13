"""Инварианты фактов (§5.4): числа, даты и названия из исходника должны дожить
до каждой версии или быть явно перечислены моделью в dropped_facts.
Новые числа и латинские названия, которых не было в исходнике, — ошибка.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_MONTH_RE = r"(январ[ья]|феврал[ья]|марта?|апрел[ья]|ма[йя]|июн[ья]|июл[ья]|августа?|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])"

_MULT = [
    (r"трлн|триллион\w*", 10**12),
    (r"млрд|миллиард\w*", 10**9),
    (r"млн|миллион\w*", 10**6),
    (r"тыс\.?|тысяч\w*", 10**3),
    # латинские сокращения — только слитно с числом: 5k, 2M, 3bn
    (r"(?<=\d)(?:bn|b)\b", 10**9),
    (r"(?<=\d)m\b", 10**6),
    (r"(?<=\d)k\b", 10**3),
]
_NUM = r"(?<![\w.,])(\d{1,3}(?:[   ]\d{3})+|\d+(?:[.,]\d+)?)"
_NUM_RE = re.compile(_NUM + r"(?:\s?(" + "|".join(m for m, _ in _MULT) + r"))?", re.I)
_DATE_NUM_RE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?![\d.])")
_DATE_WORD_RE = re.compile(r"(?<!\d)(\d{1,2})\s+" + _MONTH_RE, re.I)
_LATIN_RE = re.compile(r"(?<![\w-])[A-Za-z][A-Za-z0-9]*(?:[.\-+][A-Za-z0-9]+)*")

_WORD_NUMS = {
    "ноль": 0, "один": 1, "одна": 1, "одно": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "сто": 100,
    "тысяча": 1000, "миллион": 10**6, "миллиард": 10**9,
}

# Латинские слова, которые не считаются «новыми фактами».
DEFAULT_WHITELIST = {
    "ai", "ii", "telegram", "max", "vk", "facebook", "threads", "instagram", "meta", "it",
    "ok", "pdf", "url", "faq", "ceo", "cto", "smm", "hr", "b2b", "b2c", "api", "ui", "ux",
}


def _num_value(raw: str, mult: str | None) -> float:
    v = float(re.sub(r"[   ]", "", raw).replace(",", "."))
    if mult:
        for pat, k in _MULT:
            if re.fullmatch(pat.replace("(?<=\\d)", ""), mult, re.I):
                v *= k
                break
    return round(v, 6)


def _month(word: str) -> int:
    w = word.lower()
    for stem, n in sorted(_MONTHS.items(), key=lambda kv: -len(kv[0])):
        if w.startswith(stem):
            return n
    return 0


def extract_dates(text: str) -> set[str]:
    out = set()
    for d, m, _y in _DATE_NUM_RE.findall(text):
        if 1 <= int(d) <= 31 and 1 <= int(m) <= 12:
            out.add(f"{int(d)}.{int(m)}")
    for d, mon in _DATE_WORD_RE.findall(text):
        out.add(f"{int(d)}.{_month(mon)}")
    return out


def extract_numbers(text: str) -> set[float]:
    stripped = _DATE_NUM_RE.sub(" ", text)
    stripped = _DATE_WORD_RE.sub(lambda m: " " + m.group(2), stripped)
    nums = {_num_value(raw, mult) for raw, mult in _NUM_RE.findall(stripped)}
    for word in re.findall(r"[а-яё]+", text.lower()):
        if word in _WORD_NUMS:
            nums.add(float(_WORD_NUMS[word]))
    return nums


def extract_latin(text: str) -> set[str]:
    return {t for t in _LATIN_RE.findall(text) if len(t) >= 2}


@dataclass
class Invariants:
    numbers: set[float] = field(default_factory=set)
    dates: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)  # латиница + сущности из анализа

    def total(self) -> int:
        return len(self.numbers) + len(self.dates) + len(self.names)

    def as_list(self) -> list[str]:
        nums = [_fmt(n) for n in sorted(self.numbers)]
        return nums + sorted(self.dates) + sorted(self.names)


def build_invariants(source_text: str, entity_names: list[str] | None = None) -> Invariants:
    source_text = re.sub(r"(?<![\w&#/])#[\wё]+", " ", source_text)  # хэштеги — не факты
    names = {t for t in extract_latin(source_text) if t.lower() not in DEFAULT_WHITELIST}
    for name in entity_names or []:
        name = clean_name(name)
        if name and _name_in(name, source_text):
            names.add(name)
    # Годы считаем датами, а не «обычными» числами: в тексте их часто пишут иначе.
    return Invariants(numbers=extract_numbers(source_text), dates=extract_dates(source_text), names=names)


@dataclass
class InvariantCheck:
    missing: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    dropped_ratio: float = 0.0

    def errors(self, max_dropped_ratio: float) -> list[str]:
        errs = [f"fact_missing: «{m}» нет в тексте и нет в dropped_facts" for m in self.missing]
        errs += [f"fact_new: «{n}» нет в исходнике" for n in self.new]
        if self.dropped_ratio > max_dropped_ratio:
            errs.append(f"too_many_dropped: выброшено {self.dropped_ratio:.0%} фактов, лимит {max_dropped_ratio:.0%}")
        return errs


_QUOTES = "«»\"“”„‘’'`"


def clean_name(name: str) -> str:
    return name.strip().strip(_QUOTES).strip()


def _stem(word: str) -> str:
    """Грубая основа русского слова: падежи меняют окончание («Андрей» → «Андрея»)."""
    w = word.lower()
    if re.search(r"[а-яё]", w) and len(w) > 4:
        return w[: max(4, len(w) - 2)]
    return w


def _name_in(name: str, text: str) -> bool:
    """Название есть в тексте с точностью до кавычек и падежных окончаний."""
    low = re.sub(f"[{re.escape(_QUOTES)}]", "", text.lower())
    words = re.findall(r"[\wё.+-]+", clean_name(name).lower())
    return bool(words) and all(_stem(w) in low for w in words)


def _aliases(name: str, glossary: dict[str, list[str]]) -> list[str]:
    al = [name]
    for canon, variants in glossary.items():
        if name.lower() == canon.lower() or name.lower() in (v.lower() for v in variants):
            al += [canon, *variants]
    return al


def check_invariants(
    inv: Invariants,
    output_text: str,
    dropped_facts: list[str],
    *,
    allowed_extra_text: str = "",
    glossary: dict[str, list[str]] | None = None,
    whitelist: set[str] | None = None,
) -> InvariantCheck:
    """allowed_extra_text — quoted_sources и пересказы своих постов: их факты разрешены."""
    glossary = glossary or {}
    wl = DEFAULT_WHITELIST | {w.lower() for w in (whitelist or set())}
    out_low = output_text.lower()
    dropped_low = " ".join(dropped_facts).lower()
    out_nums, out_dates = extract_numbers(output_text), extract_dates(output_text)
    dropped_nums, dropped_dates = extract_numbers(dropped_low), extract_dates(dropped_low)

    res = InvariantCheck()
    dropped = 0
    for n in inv.numbers:
        if n in out_nums:
            continue
        if n in dropped_nums:
            dropped += 1
        else:
            res.missing.append(_fmt(n))
    for d in inv.dates:
        if d in out_dates:
            continue
        if d in dropped_dates:
            dropped += 1
        else:
            res.missing.append(d)
    for name in inv.names:
        als = _aliases(name, glossary)
        if any(_name_in(a, output_text) for a in als):
            continue
        if any(_name_in(a, dropped_low) for a in als):
            dropped += 1
        else:
            res.missing.append(name)

    allowed_nums = inv.numbers | extract_numbers(allowed_extra_text)
    allowed_dates = inv.dates | extract_dates(allowed_extra_text)
    allowed_names = {n.lower() for n in inv.names} | {t.lower() for t in extract_latin(allowed_extra_text)}
    for g_canon, g_vars in glossary.items():
        if g_canon.lower() in allowed_names or any(v.lower() in allowed_names for v in g_vars):
            allowed_names |= {g_canon.lower(), *(v.lower() for v in g_vars)}
    res.new += [_fmt(n) for n in sorted(out_nums - allowed_nums) if not _is_list_marker(n, output_text)]
    res.new += sorted(out_dates - allowed_dates)
    res.new += sorted(
        t for t in extract_latin(output_text)
        if t.lower() not in allowed_names and t.lower() not in wl
        and not any(t.lower() in a.lower() for a in allowed_names)
    )
    res.dropped_ratio = dropped / inv.total() if inv.total() else 0.0
    return res


def _is_list_marker(n: float, text: str) -> bool:
    """Номера частей «1/3» и пункты списка «1.» не считаются фактами."""
    if n != int(n) or n > 20:
        return False
    k = int(n)
    return bool(re.search(rf"(^|\n)\s*{k}[.)]\s|\b{k}/\d+\b|\b\d+/{k}\b", text))


def _fmt(n: float) -> str:
    return str(int(n)) if n == int(n) else f"{n:g}"
