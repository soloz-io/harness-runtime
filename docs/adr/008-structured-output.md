# ADR-008: Structured Output — ToolStrategy, DeepSeek Thinking, and Response Mapping

**Date:** 2026-06-19
**Status:** Proposed

## Context

Many workflow actions require structured output: the AI must return typed data (e.g., `{approved: boolean, feedback: string}`) that edge routers and downstream transitions can read.

LLMs produce structured output in two ways:
1. **Tool-call based** (ToolStrategy): The model calls a fake tool whose schema defines the output structure. This is reliable and works with all OpenAI-compatible models.
2. **JSON mode**: The model outputs JSON as plain text. This is less reliable but works with models that don't support tool calling.

Additionally, DeepSeek reasoning models have a "thinking" mode that conflicts with tool calling (ToolStrategy requires `tool_choice`, which is incompatible with thinking). The harness must disable thinking when structured output is requested with a DeepSeek model.

Structured output values in the acrylic topology must be spread into typed state fields so conditional edges (`state["approved"]`) can read them.

## Decision

### ToolStrategy for Structured Output

When an agent definition includes `response_format` (a JSON schema dict), `build_tool_strategy()` wraps it in a `ToolStrategy` instance:

```python
from langchain.agents.structured_output import ToolStrategy
strategy = ToolStrategy(schema=response_format)
```

This is passed to `create_deep_agent()` or `create_agent()` as the `response_format` parameter.

### DeepSeek Thinking Disabled

When the model is a DeepSeek variant and `response_format` is present, `resolve_structured_output_model()` disables thinking mode:

```python
if needs_thinking_disabled(model_identifier, response_format):
    extra_body = {"thinking": {"type": "disabled"}}
```

This is injected via `extra_body` in the `ChatOpenAI` constructor call.

### Monkey-Patch for Reasoning Content

A monkey-patch in `structured_output.py` overrides `langchain_openai.chat_models.base._convert_message_to_dict` to preserve DeepSeek's `reasoning_content` in `additional_kwargs`. Without this, assistant messages with tool calls that include reasoning content will fail serialization.

### StructuredOutputMappingMiddleware (Acrylic Topology Only)

The acrylic topology uses `StructuredOutputMappingMiddleware` to spread `state["structured_response"]` fields into individually-typed state fields:

```python
def after_model(self, state, runtime):
    sr = state.get("structured_response")
    if sr and isinstance(sr, dict):
        return dict(sr)  # Spreads fields into state
```

This is critical for conditional edge routing. For example, if the response format defines `{approved: boolean, feedback: string}`, the middleware ensures `state["approved"]` and `state["feedback"]` are accessible to `eval()`-based edge conditions.

The star topology does not use this middleware because conditional edges do not exist in that path.

## Consequences

### Positive

- Structured output works reliably via ToolStrategy (tool-call based) for all OpenAI-compatible models
- DeepSeek models work with structured output (thinking mode disabled)
- Edge routers in acrylic topology can read typed fields from structured output
- The monkey-patch unblocks tool calls with DeepSeek reasoning models

### Negative

- The monkey-patch on `langchain_openai` is fragile — it could break with package updates
- ToolStrategy internally uses `tool_choice`, which increases token usage
- DeepSeek thinking being globally disabled for structured output may reduce output quality on reasoning tasks

## References

- `core/structured_output.py`: `build_tool_strategy()`, `resolve_structured_output_model()`, `StructuredOutputMappingMiddleware`, monkey-patch
- `core/start_topology.py`: Structured output passed to `create_deep_agent()` for star topology
- `core/node_compiler.py`: Structured output passed to `create_agent()` for acrylic topology
