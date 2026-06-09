"""
Model Identifier Module for Agent Executor.

This module provides functionality for creating model identifier strings
that are compatible with LangGraph and deepagents frameworks.

Functions:
    - create_model_identifier: Create model identifier string for LangGraph

References:
    - Requirements: Req. 3.1 (Stateful Graph Execution)
    - Design: Section 3.2.1 (Model Factory)
"""

import structlog

logger = structlog.get_logger(__name__)


def create_model_identifier(provider: str, model_name: str) -> str:
    """
    Create a model identifier string for LangGraph.

    LangGraph accepts model identifiers in the format "provider:model_name"
    (e.g., "openai:gpt-4o", "anthropic:claude-3-opus").

    This function validates the provider and constructs the identifier string.

    Args:
        provider: LLM provider name (openai, anthropic, ollama)
        model_name: Model identifier (e.g., "gpt-4o", "claude-3-opus")

    Returns:
        Formatted model identifier string

    Raises:
        ValueError: If provider or model_name is empty

    Example:
        >>> create_model_identifier("openai", "gpt-4o")
        "openai:gpt-4o"

        >>> create_model_identifier("anthropic", "claude-3-opus")
        "anthropic:claude-3-opus"

    References:
        - Requirements: Req. 3.1
        - Design: Section 3.2.1 (Model Factory)
    """
    if not provider or not provider.strip():
        raise ValueError("Provider name cannot be empty")

    if not model_name or not model_name.strip():
        raise ValueError("Model name cannot be empty")

    provider = provider.strip().lower()
    model_name = model_name.strip()

    # Validate supported providers
    supported_providers = ["openai", "anthropic", "ollama"]
    if provider not in supported_providers:
        logger.warning(
            "unsupported_provider",
            provider=provider,
            supported_providers=supported_providers
        )

    model_identifier = f"{provider}:{model_name}"

    logger.debug(
        "model_identifier_created",
        provider=provider,
        model_name=model_name,
        identifier=model_identifier
    )

    return model_identifier
