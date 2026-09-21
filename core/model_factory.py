"""Model factory for creating LLM instances and model identifiers based on configuration."""

import os
from typing import Any, Optional

import structlog

from core.model_identifier import create_model_identifier

logger = structlog.get_logger(__name__)

_MODEL_PREFIX_MAP = {
    "deepseek": "openai",
    "gpt": "openai",
    "o1": "openai",
    "claude": "anthropic",
    "poolside": "openai",
}


def _detect_provider(model_name: str) -> str:
    """Detect the LLM provider from the model name prefix."""
    if not model_name:
        raise ValueError("model_name is required to detect provider")
    prefix = (model_name.split("/")[0] if "/" in model_name else model_name.split("-")[0]).lower()
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
    env_base = os.environ.get("AI_GATEWAY_BASE_URL")
    if env_base:
        return env_base
    if "base_url" in extra_kwargs or "base_url" in os.environ:
        return None  # already explicitly set
    if model_name.startswith("deepseek"):
        return "https://api.deepseek.com"
    return None


def _with_context_profile(model: Any) -> Any:
    """Attach ``max_input_tokens`` so context compression can size itself.

    deepagents picks summarization thresholds from the model's profile: with one
    it triggers at a FRACTION of the window (85%, keeping 10%); without one it
    falls back to a fixed 170,000-token trigger. That fallback is not a
    conservative default — it is a number unrelated to the model in use, and for
    any window smaller than it, summarization can never fire before the provider
    rejects the request. Nothing warns; the middleware is installed and inert.

    Models reached through a gateway carry no profile, because LangChain keys
    profiles off provider model ids and a gateway's names are its own. So the
    window is supplied here, from the deployment that knows it.

    Absent or unparseable, the profile is left alone rather than guessed: a wrong
    window is worse than none, since compression would then size itself to a
    budget the provider does not honour.
    """
    raw = os.environ.get("AUTO_COMPACT_WINDOW")
    if not raw:
        return model
    try:
        window = int(raw)
    except ValueError:
        logger.warning("auto_compact_window_not_an_integer", value=raw)
        return model
    if window <= 0:
        logger.warning("auto_compact_window_not_positive", value=window)
        return model

    profile = dict(getattr(model, "profile", None) or {})
    # Never overwrite a real profile — a provider that publishes its own limits
    # knows them better than an environment variable does.
    if profile.get("max_input_tokens"):
        return model
    profile["max_input_tokens"] = window
    try:
        model.profile = profile
    except Exception:  # noqa: BLE001 - pydantic models may forbid assignment
        logger.warning("auto_compact_window_profile_not_settable", model=type(model).__name__)
        return model
    logger.info("auto_compact_window_applied", max_input_tokens=window)
    return model


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

        return _with_context_profile(ChatAnthropic(**kwargs))
    if provider == "openai":
        base_url = _resolve_openai_base_url(model_name, extra_kwargs)
        if base_url:
            kwargs["base_url"] = base_url
            kwargs["use_responses_api"] = False
        from langchain_openai import ChatOpenAI

        return _with_context_profile(ChatOpenAI(**kwargs))
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

        api_key = os.environ.get("AI_GATEWAY_API_KEY")
        if not api_key:
            raise ValueError("AI_GATEWAY_API_KEY is not set")
        if model.startswith("deepseek") and "extra_body" not in extra_kwargs:
            extra_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        return _create_model_for_provider(prov, model, api_key=api_key, **extra_kwargs)
