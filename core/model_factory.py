"""Model factory for creating LLM instances and model identifiers based on configuration."""

import os
from typing import Any, Optional

from core.model_identifier import create_model_identifier

_MODEL_PREFIX_MAP = {
    "deepseek": "openai",
    "gpt": "openai",
    "o1": "openai",
    "claude": "anthropic",
}


def _detect_provider(model_name: str) -> str:
    """Detect the LLM provider from the model name prefix."""
    if not model_name:
        raise ValueError("model_name is required to detect provider")
    prefix = model_name.split("-")[0].lower()
    result = _MODEL_PREFIX_MAP.get(prefix)
    if not result:
        raise ValueError(
            f"Cannot detect provider from model name '{model_name}'. "
            f"Set LLM_PROVIDER env var or use a known model prefix "
            f"({', '.join(sorted(_MODEL_PREFIX_MAP))})"
        )
    return result


def _resolve_openai_base_url(model_name: str, extra_kwargs: dict[str, Any]) -> str | None:
    """Resolve OpenAI-compatible base URL from env or model prefix."""
    env_base = os.environ.get("OPENAI_BASE_URL")
    if env_base:
        return env_base
    if "base_url" in extra_kwargs or "base_url" in os.environ:
        return None  # already explicitly set
    if model_name.startswith("deepseek"):
        return "https://api.deepseek.com"
    return None


def _create_model_for_provider(
    provider: str, model_name: str, api_key: str, **extra_kwargs: Any
) -> Any:
    """Create the appropriate LLM model based on provider and model name."""
    kwargs: dict[str, Any] = {"model": model_name, "api_key": api_key, **extra_kwargs}

    timeout_seconds = os.environ.get("LLM_TIMEOUT_SECONDS")
    if timeout_seconds is not None:
        kwargs["timeout"] = int(timeout_seconds)
    max_retries = os.environ.get("LLM_MAX_RETRIES")
    if max_retries is not None:
        kwargs["max_retries"] = int(max_retries)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(**kwargs)
    if provider == "openai":
        base_url = _resolve_openai_base_url(model_name, extra_kwargs)
        if base_url:
            kwargs["base_url"] = base_url
            kwargs["use_responses_api"] = False
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(**kwargs)
    raise ValueError(f"Unsupported provider: {provider}")


class ModelFactory:
    """Factory for creating LLM model instances."""

    @staticmethod
    def resolve_model_identifier(
        provider: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        """Resolve model identifier string, checking env var overrides first.

        Returns a "provider:model_name" string suitable for passing to
        deepagents' create_deep_agent(model=...), which enables HarnessProfile
        resolution.

        Args:
            provider: Provider name from config (e.g. "openai", "anthropic").
            model_name: Model name from config (e.g. "gpt-4.1-mini").

        Returns:
            Model identifier string in "provider:model_name" format.
        """
        model = os.environ.get("LLM_MODEL_NAME") or model_name
        if not model:
            raise ValueError(
                "No model name specified. Set LLM_MODEL_NAME env var "
                "or provide model_name in agent definition"
            )
        prov = os.environ.get("LLM_PROVIDER") or provider or _detect_provider(model)
        return create_model_identifier(prov, model)

    @staticmethod
    def create_model(
        provider: Optional[str] = None,
        model_name: Optional[str] = None,
        **extra_kwargs: Any,
    ) -> Any:
        # Env vars take precedence over agent definition
        model = os.environ.get("LLM_MODEL_NAME") or model_name
        if not model:
            raise ValueError(
                "No model name specified. Set LLM_MODEL_NAME env var "
                "or provide model_name in agent definition"
            )

        # Model name determines provider (agent definition's provider field
        # may be incorrect for cross-provider models like deepseek via OpenAI API)
        prov = os.environ.get("LLM_PROVIDER") or _detect_provider(model)

        if model.startswith("deepseek"):
            api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
            if "extra_body" not in extra_kwargs:
                extra_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        else:
            api_key = (
                os.environ.get("OPENAI_API_KEY")
                or os.environ.get("DEEPSEEK_API_KEY")
                or os.environ.get("ANTHROPIC_API_KEY")
            )
        if not api_key:
            raise ValueError(
                "No API key found. Set OPENAI_API_KEY, DEEPSEEK_API_KEY, or ANTHROPIC_API_KEY"
            )

        return _create_model_for_provider(prov, model, api_key=api_key, **extra_kwargs)
