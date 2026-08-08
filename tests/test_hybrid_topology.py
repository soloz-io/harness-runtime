"""Unit tests for the hybrid topology builder.

The hybrid builder dispatches each specialist to one of three compilers:
declarative ``SubAgent`` (default), nested deep agent (``mode``),
or nested acrylic subgraph (``mode``).  These tests verify the dispatch and
the resulting subagent mix without constructing real LLM models.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from core.factory import build_agent_from_definition
from core.topology.acrylic_topology import AcrylicTopologyBuilder
from core.topology.hybrid_topology import HybridTopologyBuilder

SENTINEL_RUNNABLE = object()
SENTINEL_SUBGRAPH = object()


def _hybrid_definition() -> dict[str, Any]:
    return {
        "topology": "hybrid",
        "tool_definitions": [],
        "nodes": [
            {
                "id": "orchestrator",
                "type": "Orchestrator",
                "config": {
                    "name": "orchestrator",
                    "system_prompt": "you are the orchestrator",
                    "model": {"model_name": "gpt-4o-mini"},
                    "tools": [],
                },
            },
            {
                "id": "plain",
                "type": "Specialist",
                "config": {
                    "name": "plain-agent",
                    "system_prompt": "plain specialist",
                    "model": {"model_name": "gpt-4o-mini"},
                    "tools": [],
                },
            },
            {
                "id": "deep",
                "type": "Specialist",
                "config": {
                    "name": "deep-agent",
                    "system_prompt": "nested deep agent",
                    "model": {"model_name": "gpt-4o-mini"},
                    "tools": [],
                    "mode": "deepagent",
                    "subagents": [
                        {
                            "name": "inner-a",
                            "system_prompt": "inner subagent",
                            "model": {"model_name": "gpt-4o-mini"},
                            "tools": [],
                        }
                    ],
                },
            },
            {
                "id": "sub",
                "type": "Specialist",
                "config": {
                    "name": "sub-agent",
                    "system_prompt": "nested subgraph",
                    "model": {"model_name": "gpt-4o-mini"},
                    "tools": [],
                    "mode": "subgraph",
                    "state_schema": {"approved": {"type": "bool"}},
                    "nodes": [
                        {
                            "id": "n1",
                            "type": "Specialist",
                            "config": {
                                "name": "n1",
                                "system_prompt": "node one",
                                "model": {"model_name": "gpt-4o-mini"},
                                "tools": [],
                            },
                        },
                        {
                            "id": "n2",
                            "type": "Specialist",
                            "config": {
                                "name": "n2",
                                "system_prompt": "node two",
                                "model": {"model_name": "gpt-4o-mini"},
                                "tools": [],
                            },
                        },
                    ],
                    "edges": [{"source": "n1", "target": "n2", "type": "data"}],
                },
            },
        ],
        "edges": [],
    }


def test_dispatch_mixed_specialists(monkeypatch: Any) -> None:
    """Specialists route to declarative / nested deep agent / subgraph."""
    deep_agent_calls: list[tuple[dict[str, Any], str | None, list[Any] | None]] = []
    subgraph_calls: list[dict[str, Any]] = []
    subagent_builds: list[dict[str, Any]] = []

    import core.topology.hybrid_topology as hybrid_mod

    def fake_build_subagent(
        config: dict[str, Any],
        available_tools: dict[str, Any],
        *,
        skills: list[str] | None = None,
        tools_spec: Any = None,
    ) -> dict[str, Any]:
        subagent_builds.append(config)
        return {"name": config.get("name"), "declarative": True}

    def fake_build_deep_agent_runnable(
        config: dict[str, Any],
        available_tools: dict[str, Any],
        *,
        checkpointer: Any = None,
        node_id: str | None = None,
        subagents: list[Any] | None = None,
        skills: list[str] | None = None,
        composite_backend: Any = None,
        backend: Any = None,
        tools_ctx: Any = None,
    ) -> Any:
        deep_agent_calls.append((config, node_id, subagents))
        return SENTINEL_RUNNABLE

    def fake_acrylic_build(
        self: Any,
        definition: dict[str, Any],
        available_tools: dict[str, Any],
        checkpointer: Any,
        **kwargs: Any,
    ) -> Any:
        subgraph_calls.append(definition)
        return SENTINEL_SUBGRAPH

    monkeypatch.setattr(hybrid_mod, "build_subagent", fake_build_subagent)
    monkeypatch.setattr(hybrid_mod, "build_deep_agent_runnable", fake_build_deep_agent_runnable)
    monkeypatch.setattr(AcrylicTopologyBuilder, "build", fake_acrylic_build)

    result = HybridTopologyBuilder().build(_hybrid_definition(), {}, MemorySaver())

    assert result is SENTINEL_RUNNABLE

    # Nested deep agent builds during specialist compilation; the orchestrator
    # is built last.
    by_node: dict[str, tuple[dict[str, Any], list[Any] | None]] = {
        node_id: (config, subagents) for config, node_id, subagents in deep_agent_calls
    }
    assert set(by_node) == {"orchestrator", "deep"}

    orchestrator_config, subagents = by_node["orchestrator"]
    assert len(subagents) == 3

    plain, deep_spec, subgraph_spec = subagents
    assert plain == {"name": "plain-agent", "declarative": True}

    assert deep_spec["name"] == "deep-agent"
    assert deep_spec["runnable"] is SENTINEL_RUNNABLE

    assert subgraph_spec["name"] == "sub-agent"
    assert subgraph_spec["runnable"] is SENTINEL_SUBGRAPH

    # Nested deep agent forwarded its own subagent spec through build_subagent.
    _, deep_subagents = by_node["deep"]
    assert len(deep_subagents) == 1
    assert {b.get("name") for b in subagent_builds} == {"plain-agent", "inner-a"}

    # Nested subgraph received nodes + edges + state_schema.
    assert subgraph_calls[0]["nodes"][0]["id"] == "n1"
    assert subgraph_calls[0]["edges"][0]["source"] == "n1"
    assert subgraph_calls[0]["state_schema"] == {"approved": {"type": "bool"}}


def test_missing_orchestrator_falls_back_to_first_node(monkeypatch: Any) -> None:
    """Without an orchestrator node, the first node becomes the orchestrator."""
    definition = _hybrid_definition()
    definition["nodes"] = [node for node in definition["nodes"] if node["id"] != "orchestrator"]

    deep_agent_calls: list[tuple[dict[str, Any], str | None, list[Any] | None]] = []

    import core.topology.hybrid_topology as hybrid_mod

    def fake_build_subagent(
        config: dict[str, Any],
        available_tools: dict[str, Any],
        *,
        skills: list[str] | None = None,
        tools_spec: Any = None,
    ) -> dict[str, Any]:
        return {"name": config.get("name"), "declarative": True}

    def fake_build_deep_agent_runnable(
        config: dict[str, Any],
        available_tools: dict[str, Any],
        *,
        checkpointer: Any = None,
        node_id: str | None = None,
        subagents: list[Any] | None = None,
        skills: list[str] | None = None,
        composite_backend: Any = None,
        backend: Any = None,
        tools_ctx: Any = None,
    ) -> Any:
        deep_agent_calls.append((config, node_id, subagents))
        return SENTINEL_RUNNABLE

    def fake_acrylic_build(
        self: Any,
        definition: dict[str, Any],
        available_tools: dict[str, Any],
        checkpointer: Any,
        **kwargs: Any,
    ) -> Any:
        return SENTINEL_SUBGRAPH

    monkeypatch.setattr(hybrid_mod, "build_subagent", fake_build_subagent)
    monkeypatch.setattr(hybrid_mod, "build_deep_agent_runnable", fake_build_deep_agent_runnable)
    monkeypatch.setattr(AcrylicTopologyBuilder, "build", fake_acrylic_build)

    HybridTopologyBuilder().build(definition, {}, MemorySaver())

    # First node ("plain") is used as the orchestrator. Mirrors star topology
    # fallback: the node is still compiled as a subagent too.
    by_node = {node_id: (config, subagents) for config, node_id, subagents in deep_agent_calls}
    orchestrator_config, subagents = by_node["plain"]
    assert orchestrator_config.get("name") == "plain-agent"
    assert len(subagents) == 3


def test_factory_selects_hybrid_and_forwards_kwargs(monkeypatch: Any) -> None:
    """build_agent_from_definition routes topology 'hybrid' to the hybrid
    builder and forwards skills/backend/tools_ctx."""
    captured: dict[str, Any] = {}

    def fake_hybrid_build(self: Any, definition: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return SENTINEL_RUNNABLE

    import core.factory as factory_mod

    monkeypatch.setattr(factory_mod.HybridTopologyBuilder, "build", fake_hybrid_build)

    tools_ctx = {"node_tools": {"plain": object()}}
    result = build_agent_from_definition(
        _hybrid_definition(),
        checkpointer=MemorySaver(),
        skills=["/skills/a/"],
        composite_backend=object(),
        tools_ctx=tools_ctx,  # type: ignore[arg-type]
    )

    assert result is SENTINEL_RUNNABLE
    assert captured["skills"] == ["/skills/a/"]
    assert captured["composite_backend"] is not None
    assert captured["tools_ctx"] == tools_ctx
