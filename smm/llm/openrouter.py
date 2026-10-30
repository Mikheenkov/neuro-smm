"""Провайдер через OpenRouter: те же модели Claude по OpenAI-совместимому API со структурированным выводом."""

from __future__ import annotations

import json
import time

import httpx

from smm.llm.base import LLMError, LLMResult, Role

BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, api_key: str, *, effort: dict[str, str] | None = None, max_tokens: int = 16000,
                 timeout: float = 300.0, transport: httpx.BaseTransport | None = None):
        self.client = httpx.Client(base_url=BASE_URL, timeout=timeout, transport=transport,
                                   headers={"Authorization": f"Bearer {api_key}", "X-Title": "Neuro SMM"})
        self.effort = effort or {"editor": "medium", "judge": "medium", "regen": "medium"}
        self.max_tokens = max_tokens

    def generate(self, system: str, payload: dict, schema: dict, *, role: Role, model: str) -> LLMResult:
        started = time.monotonic()
        body = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": [
                # Стабильный системный промпт кэшируется: полезно для перегенераций внутри одного прогона.
                {"role": "system", "content": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "reasoning": {"effort": self.effort.get(role, "medium")},
            "response_format": {"type": "json_schema", "json_schema": {"name": role, "strict": True, "schema": schema}},
            "provider": {"require_parameters": True},  # только площадки, которые умеют строгую схему
            "usage": {"include": True},
        }
        try:
            response = self.client.post("/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"нет соединения с OpenRouter: {exc}", "overloaded") from exc
        try:
            answer = response.json()
        except ValueError as exc:
            raise LLMError(f"OpenRouter вернул не JSON (HTTP {response.status_code})", "overloaded") from exc
        # OpenRouter может прислать ошибку и с HTTP 200, когда площадка отказала уже после начала ответа.
        error = answer.get("error")
        if error or response.status_code >= 400:
            _raise_for_error(error or {}, response)

        choice = (answer.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        if message.get("refusal") or choice.get("native_finish_reason") == "refusal":
            raise LLMError("модель отказалась отвечать", "refusal")
        if choice.get("finish_reason") == "length":
            raise LLMError("ответ обрезан по max_tokens", "schema")
        text = message.get("content")
        if not text:
            raise LLMError("в ответе нет текста", "schema")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"ответ не JSON: {exc}", "schema") from exc

        usage = answer.get("usage") or {}
        return LLMResult(
            data=data,
            provider=self.name,
            model=answer.get("model") or model,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            cache_read_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            cost_usd_est=usage.get("cost") or 0.0,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def _raise_for_error(error: dict, response: httpx.Response) -> None:
    code = error.get("code") or response.status_code
    message = error.get("message") or response.text[:300]
    if code == 401:
        raise LLMError(f"ключ OpenRouter не принят: {message}", "auth")
    if code == 402:
        raise LLMError(f"на ключе OpenRouter кончились средства или лимит: {message}", "auth")
    if code == 403:
        raise LLMError(f"нет доступа: {message}", "auth")
    if code == 429:
        retry_after = float(response.headers.get("retry-after", "60"))
        raise LLMError("лимит запросов OpenRouter", "limit", retry_after=retry_after)
    if code == 400:
        raise LLMError(f"неверный запрос: {message}", "schema" if "schema" in str(message) else "other")
    if isinstance(code, int) and code >= 500:
        raise LLMError(f"OpenRouter перегружен ({code}): {message}", "overloaded")
    raise LLMError(f"ошибка OpenRouter {code}: {message}")
