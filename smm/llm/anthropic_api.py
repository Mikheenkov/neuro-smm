"""Основной провайдер: Anthropic Messages API со структурированным выводом."""

from __future__ import annotations

import json
import time

import anthropic

from smm.llm.base import LLMError, LLMResult, Role

# $ за 1M токенов: вход, выход, чтение кэша. Источник: прайс Anthropic (09.2026).
PRICES = {
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def estimate_cost(model: str, usage) -> float:
    p_in, p_out, p_cache = PRICES.get(model, (4.00, 20.00, 0.20))
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return round(
        (usage.input_tokens * p_in + cache_write * p_in * 1.25 + cache_read * p_cache + usage.output_tokens * p_out) / 1e6,
        6,
    )


class AnthropicApiProvider:
    name = "anthropic_api"

    def __init__(self, api_key: str, *, effort: dict[str, str] | None = None, max_tokens: int = 16000,
                 timeout: float = 300.0, workspace_id: str | None = None):
        # Ключ организации без привязки к workspace требует явно указать workspace в каждом запросе.
        headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2, default_headers=headers)
        self.effort = effort or {"editor": "medium", "judge": "medium", "regen": "medium"}
        self.max_tokens = max_tokens

    def generate(self, system: str, payload: dict, schema: dict, *, role: Role, model: str) -> LLMResult:
        started = time.monotonic()
        try:
            response = self.client.beta.messages.create(
                model=model,
                max_tokens=self.max_tokens,
                # Стабильный системный промпт кэшируется: полезно для перегенераций внутри одного прогона.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                }],
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort.get(role, "medium"),
                               "format": {"type": "json_schema", "schema": schema}},
                # При отказе классификатора запрос сам перезапускается на рекомендованной модели.
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as exc:
            raise LLMError(f"ключ Anthropic API не принят: {exc.message}", "auth") from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(f"нет доступа: {exc.message}", "auth") from exc
        except anthropic.RateLimitError as exc:
            retry_after = float(exc.response.headers.get("retry-after", "60"))
            raise LLMError("лимит запросов Anthropic API", "limit", retry_after=retry_after) from exc
        except anthropic.BadRequestError as exc:
            if "credit balance" in exc.message:
                raise LLMError("на балансе Anthropic API нет средств — пополните в Console → Billing", "auth") from exc
            # 400: схема, превышение бюджета, неверные параметры — повтор не поможет
            raise LLMError(f"неверный запрос: {exc.message}", "schema" if "schema" in exc.message else "other") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code == 529 or exc.status_code >= 500:
                raise LLMError(f"Anthropic API перегружен ({exc.status_code})", "overloaded") from exc
            raise LLMError(f"ошибка Anthropic API {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"нет соединения с Anthropic API: {exc}", "overloaded") from exc

        if response.stop_reason == "refusal":
            raise LLMError("модель отказалась отвечать", "refusal")
        if response.stop_reason == "max_tokens":
            raise LLMError("ответ обрезан по max_tokens", "schema")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise LLMError("в ответе нет текстового блока", "schema")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"ответ не JSON: {exc}", "schema") from exc

        served_model = response.model or model
        return LLMResult(
            data=data,
            provider=self.name,
            model=served_model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cost_usd_est=estimate_cost(served_model, response.usage),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
