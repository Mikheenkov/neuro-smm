from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

Role = Literal["editor", "judge", "regen"]
ErrorClass = Literal["limit", "overloaded", "auth", "schema", "refusal", "other"]


@dataclass
class LLMResult:
    data: dict
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd_est: float = 0.0
    duration_ms: int = 0
    extra: dict = field(default_factory=dict)


class LLMError(Exception):
    def __init__(self, message: str, error_class: ErrorClass = "other", *, retry_after: float | None = None):
        super().__init__(message)
        self.error_class = error_class
        self.retry_after = retry_after


class LLMProvider(Protocol):
    name: str

    def generate(self, system: str, payload: dict, schema: dict, *, role: Role, model: str) -> LLMResult: ...
