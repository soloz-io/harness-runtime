# ADR-014: Event Streaming Protocol — v3 Raw Events Over Distributed Pipeline

**Date:** 2026-06-23
**Status:** Accepted

## Context

The harness-runtime streams agent execution events (coordinator text, tool calls, subagent output, interrupts) through a distributed pipeline: `executor → Redis → sandbox SSE → SDK → BFF → frontend`. The consumer is a browser UI that renders coordinator messages as a chat thread and specialist subagent cards with progressive content.

The initial implementation used `graph.astream(stream_mode=["values", "messages"])` (LangGraph v2 protocol). Subagent output arrived as a completed tool result, which was manually chunked into 60-character `tool-output-delta` events after the subagent finished. This prevented true token-level streaming for specialist content.

DeepAgents provides `graph.stream_events(version="v3")` which emits granular protocol events (`message-start`, `content-block-start`, `content-block-delta`, `content-block-finish`, `message-finish`, `tool-started`, `tool-finished`, `tool-output-delta`, `lifecycle`) at both coordinator and subagent namespaces.

Two consumption patterns are available:
1. **Typed projections** (`stream.subagents`, `stream.messages`, `stream.tool_calls`) — yields in-process Python handles (`SubagentRunStream`, `ChatModelStream`, `ToolCallStream`) with typed attributes and async pump callbacks.
2. **Raw protocol events** (`for event in stream`) — yields JSON-serializable `ProtocolEvent` dicts with `{method, params: {namespace, timestamp, data}}`.

The typed projections are documented as the primary deepagents API, but they produce non-serializable objects that cannot cross process boundaries.

## Decision

### Use raw protocol events (Pattern 2) as the sole consumption mechanism

The executor consumes `graph.stream_events(version="v3")` / `graph.astream_events(version="v3")` by iterating `run.__iter__()` / `run.__aiter__()` and routing each `ProtocolEvent` by `method` and `params.namespace`. Raw events are translated to the SSE/Redis publisher's existing method calls (`publish_stream_event_text`, `publish_assistant`, `publish_tool_output_delta`, `publish_tool_result`, etc.).

### Namespace-based routing replaces message diffing

`ProtocolEvent.params.namespace` distinguishes coordinator events (`[]`) from subagent events (non-empty). A `namespace → tool_call_id` mapping is built from `lifecycle` events' `cause.tool_call_id` field. Subagent `content-block-delta` events are routed to `publish_tool_output_delta` via this mapping, replacing the prior 60-character post-hoc chunking.

### No typed projection handles cross the Redis boundary

`SubagentRunStream`, `ChatModelStream`, and `ToolCallStream` (from `stream.subagents`, `stream.messages`, `stream.tool_calls`) are never serialized or published. They remain in-process objects containing pump callbacks and buffer state that are not JSON-serializable.

### Coordinator tool lifecycle retains `publish_assistant`

The existing `publish_assistant` method still emits `tool-started` for coordinator tool calls (via `tool_use` content blocks detected from raw `messages: content-block-start {type: "tool_call"}` events). The v3 `tools: tool-started` events from the coordinator namespace are *not* forwarded to avoid double-emission. The v3 `tools: tool-finished` and `tools: tool-output-delta` events are forwarded directly.

## Ownership

This ADR defines a streaming protocol architecture and does not own platform resources. For resource ownership, see ADR-039.

## Consequences

### Positive

- **True token-level subagent streaming**: Specialist content arrives as real-time `content-block-delta` events at the subagent namespace, mapped to `tool-output-delta` for the frontend. The prior 60-character chunking is eliminated.
- **No message diffing**: v3 events provide granular lifecycle per message and tool call. The executor no longer compares message list lengths or tracks `published_message_ids`.
- **No `_task_tool_call_ids` set**: The namespace mapping from lifecycle events replaces manual tracking of which tool calls are `task` invocations.
- **Architecture matches the docs**: The raw protocol event approach is explicitly documented as the fallback for exact arrival ordering (the official deepagents event-streaming page).
- **Pipeline compatible**: `ProtocolEvent` dicts are JSON-serializable, suitable for Redis/SSE transport. No typed handles leak across process boundaries.

### Negative

- **Coarser metadata than typed handles**: Subagent name, status, and cause are extracted from `lifecycle` events rather than available directly on `subagent.name` / `subagent.cause`. Equivalent data, one extra hop through the raw event log.
- **Duplicate routing state**: The `ns_to_tool_call` dict duplicates the `cause` information that `SubagentTransformer` already computes internally. This is necessary because we consume raw events rather than handles.
- **`tool-started` emission split**: Coordinator tool-started is emitted by `publish_assistant` (driven from `messages` events); tool-output-delta and tool-finished are emitted from raw `tools` events. Two event sources for one tool lifecycle.

## References

- `core/executor.py`: `_process_v3_event()`, `_handle_lifecycle()`, `_handle_subagent_message()`, `_handle_root_message()`, `_handle_root_tools()`
- `api/publisher.py`: `SSEEventPublisher.publish_message_finish()`
- `core/event_publisher.py`: `EventPublisher.publish_message_finish()` (ABC default)
- https://docs.langchain.com/oss/python/deepagents/event-streaming — official deepagents event-streaming docs, Pattern 2 (raw protocol events)
- `langgraph/stream/run_stream.py`: `GraphRunStream`, `AsyncGraphRunStream` — `__iter__` / `__aiter__` consuming raw `ProtocolEvent` dicts
- `langgraph/stream/_types.py`: `ProtocolEvent` type definition
- `langgraph/stream/transformers.py`: `LifecycleTransformer`, `SubgraphTransformer`, `MessagesTransformer`, `ToolCallTransformer`
- `langchain/agents/_subagent_transformer.py`: `SubagentTransformer` — builds `SubagentRunStream` handles with `cause` for tool_call_id mapping
- `deepagents/middleware/subagents.py`: `SubAgentMiddleware` — task tool implementation, subagent invocation via `runnable.invoke()`
