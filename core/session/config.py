from dataclasses import dataclass, field
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AgentConfig:
    model_name: str
    system_prompt: Optional[str] = None
    per_agent_prompts: dict[str, str] = field(default_factory=dict)


def extract_agent_config(agent_definition: dict[str, Any]) -> AgentConfig:
    """Extract model_name and system_prompt from agent definition.

    ``model_name`` is read from ``nodes[0].config.model`` (the orchestrator node).
    ``system_prompt`` is built from ALL nodes' ``config.system_prompt``,
    concatenated with per-agent labels.
    """
    nodes = agent_definition.get("nodes", [])
    if not nodes:
        raise ValueError("No nodes found in agent definition")

    model_name: str | None = None
    per_agent_prompts: dict[str, str] = {}

    for node in nodes:
        nid = node.get("id", "unknown")
        cfg = node.get("config", {}) or {}
        prompt = cfg.get("system_prompt", "")
        per_agent_prompts[nid] = prompt

        if model_name is None:
            model_cfg = cfg.get("model", {}) or {}
            model_name = model_cfg.get("model_name") or model_cfg.get("model")

    if not model_name:
        raise ValueError(
            "No model name found in agent definition. "
            "Set config.model.model_name in the first node, "
            "or set LLM_MODEL_NAME env var"
        )

    system_prompt = _format_multi_agent_prompt(per_agent_prompts, nodes)
    return AgentConfig(
        model_name=str(model_name),
        system_prompt=system_prompt or None,
        per_agent_prompts=per_agent_prompts,
    )


def _format_multi_agent_prompt(
    prompts: dict[str, str],
    nodes: list[dict[str, Any]],
) -> str:
    """Format per-agent system prompts into a single labelled document."""
    info: dict[str, tuple[str, str]] = {}
    for n in nodes:
        nid = n.get("id", "unknown")
        ntype = n.get("type", "")
        ndesc = (n.get("config") or {}).get("description", "")
        info[nid] = (ntype, ndesc)

    parts: list[str] = []
    for nid, prompt in prompts.items():
        if not prompt:
            continue
        ntype, ndesc = info.get(nid, ("", ""))
        label = f"### {nid}" + (f" ({ntype})" if ntype else "")
        body = prompt
        if ndesc:
            body = f"> {ndesc}\n\n{prompt}"
        parts.append(f"{label}\n{body}")
    return "\n\n---\n\n".join(parts)


def persist_system_prompt(
    session_id: str,
    config: AgentConfig,
    pool: Any,
) -> None:
    """Persist system prompt to chat_sessions if provided.

    Uses ``WHERE system_prompt IS NULL`` to prevent accidental overwrites.
    """
    if pool is None or not config.system_prompt:
        return
    try:
        with pool.connection() as conn:
            conn.execute(
                "UPDATE chat_sessions SET system_prompt = %s WHERE id = %s AND system_prompt IS NULL",
                (config.system_prompt, session_id),
            )
    except Exception:
        logger.warning("failed_to_store_system_prompt", session_id=session_id)
