"""Загрузка конфигурации: YAML из config/ и секреты из окружения (.env)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_dotenv(path: Path) -> None:
    """Минимальный .env: KEY=VALUE, без перезаписи уже заданных переменных."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = re.split(r"(^|\s)#", value, maxsplit=1)[0]  # комментарий в конце строки
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def set_env_value(path: Path, key: str, value: str) -> None:
    """Записывает KEY=value в .env, сохраняя остальные строки и порядок."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


@dataclass
class Config:
    root: Path = ROOT
    domains: dict = field(default_factory=dict)
    domain_overrides: dict = field(default_factory=dict)
    equivalents: dict = field(default_factory=dict)
    networks: dict = field(default_factory=dict)
    llm: dict = field(default_factory=dict)
    daemon: dict = field(default_factory=dict)

    @property
    def prompts_dir(self) -> Path:
        return self.root / "prompts"

    @property
    def data_dir(self) -> Path:
        return Path(os.environ.get("SMM_DATA_DIR", self.root / "data"))

    def secret(self, name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise KeyError(f"не задан секрет {name} (см. .env.example)")
        return value

    def enabled_networks(self) -> list[str]:
        return [n for n, spec in self.networks.items() if isinstance(spec, dict) and spec.get("enabled")]


def load_config(root: Path = ROOT) -> Config:
    load_dotenv(root / ".env")
    c = root / "config"
    return Config(
        root=root,
        domains=_load_yaml(c / "domains.yaml"),
        domain_overrides=_load_yaml(c / "domain_overrides.yaml"),
        equivalents=_load_yaml(c / "equivalents.yaml"),
        networks=_load_yaml(c / "networks.yaml"),
        llm=_load_yaml(c / "llm.yaml"),
        daemon=_load_yaml(c / "daemon.yaml"),
    )
