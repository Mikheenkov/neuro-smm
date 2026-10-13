"""Издатели: единственный код, который пишет во внешний мир. Модель к ним доступа не имеет."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PublishResult:
    remote_id: str
    url: str | None
    extra: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class PublishError(Exception):
    def __init__(self, message: str, *, retryable: bool = True, extra: dict | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.extra = extra or {}  # частичный прогресс (например, уже опубликованные части цепочки)
