"""LLM-слой: провайдеры за общим интерфейсом, сборка промптов, схемы ответов."""

import os

from smm.llm.base import LLMError, LLMProvider, LLMResult

__all__ = ["LLMError", "LLMProvider", "LLMResult"]


def build_provider(cfg) -> LLMProvider:
    """Провайдер — первый в списке providers из config/llm.yaml."""
    name = cfg.llm["providers"][0]
    effort = cfg.llm.get("effort")
    if name == "openrouter":
        from smm.llm.openrouter import OpenRouterProvider
        return OpenRouterProvider(cfg.secret("OPENROUTER_API_KEY"), effort=effort)
    from smm.llm.anthropic_api import AnthropicApiProvider
    return AnthropicApiProvider(cfg.secret("ANTHROPIC_API_KEY"), effort=effort,
                                workspace_id=os.environ.get("ANTHROPIC_WORKSPACE_ID") or None)
