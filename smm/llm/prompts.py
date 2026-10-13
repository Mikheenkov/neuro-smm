"""Сборка системных промптов из Markdown-файлов prompts/ (§5.1).

Порядок фиксирован: роль → голос → ссылки → сети → эталоны. Файлы без вендорной
специфики, поэтому те же тексты подходят и для другого провайдера.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BuiltPrompt:
    text: str
    prompt_hash: str


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _examples(prompts: Path, network: str, limit: int) -> list[str]:
    folder = prompts / "examples" / network
    if not folder.exists():
        return []
    return [_read(p) for p in sorted(folder.glob("*.md"))[:limit]]


def _section(title: str, body: str) -> str:
    return f"# {title}\n\n{body}" if body else ""


def build_editor_prompt(prompts: Path, networks: list[str], *, examples_per_network: int = 3) -> BuiltPrompt:
    parts = [
        _read(prompts / "editor" / "role.md"),
        _section("Голос автора", _read(prompts / "voice" / "author-voice.md")),
        _section("Глоссарий", _read(prompts / "voice" / "glossary.md")),
        _section("Запреты и правила автора", _read(prompts / "voice" / "taboo.md")),
        _section("Как обращаться со ссылками", _read(prompts / "links" / "link-rules.md")),
    ]
    for net in networks:
        parts.append(_read(prompts / "networks" / f"{net}.md"))
        ex = _examples(prompts, net, examples_per_network)
        if ex:
            parts.append(f"## Эталоны для сети {net}\n\n" + "\n\n---\n\n".join(ex))
    return _finish(parts)


def build_judge_prompt(prompts: Path, networks: list[str]) -> BuiltPrompt:
    parts = [
        _read(prompts / "judge" / "role.md"),
        _read(prompts / "judge" / "checklist.md"),
        _section("Голос автора", _read(prompts / "voice" / "author-voice.md")),
    ]
    parts += [_read(prompts / "networks" / f"{net}.md") for net in networks]
    return _finish(parts)


def _finish(parts: list[str]) -> BuiltPrompt:
    text = "\n\n".join(p for p in parts if p)
    return BuiltPrompt(text, hashlib.sha256(text.encode()).hexdigest()[:16])


def load_glossary(prompts: Path) -> dict[str, list[str]]:
    """Строки вида «Claude = Клод, Клауд» из voice/glossary.md."""
    out: dict[str, list[str]] = {}
    for line in _read(prompts / "voice" / "glossary.md").splitlines():
        line = line.strip().lstrip("-* ").strip()
        if "=" in line and not line.startswith("#"):
            canon, variants = line.split("=", 1)
            out[canon.strip()] = [v.strip() for v in variants.split(",") if v.strip()]
    return out
