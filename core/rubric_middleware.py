"""
Static Rubric Middleware.

Provides a custom middleware to inject a static rubric into the agent's state,
and a helper to instantiate the full rubric stack.
"""

from typing import Any, List, Optional
import structlog
from langchain_core.language_models import BaseChatModel

logger = structlog.get_logger(__name__)

try:
    from deepagents.middleware import AgentMiddleware
    from deepagents.middleware.rubric import RubricMiddleware
    DEEPAGENTS_AVAILABLE = True
except ImportError:
    DEEPAGENTS_AVAILABLE = False
    AgentMiddleware = object  # dummy for typing


class StaticRubricMiddleware(AgentMiddleware):
    """
    Middleware that injects a static rubric string into the invocation state.
    This ensures that the downstream RubricMiddleware activates.
    """
    def __init__(self, rubric: str):
        self.rubric = rubric

    def before_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        """Inject the rubric into the state if missing or different."""
        if "rubric" not in state or state["rubric"] != self.rubric:
            return {"rubric": self.rubric}
        return None


def build_rubric_middlewares(rubric: Optional[str], model: Any) -> List[Any]:
    """
    Helper to conditionally build the rubric middleware stack.
    
    Args:
        rubric: The static rubric string from the configuration.
        model: The model identifier or BaseChatModel instance.
        
    Returns:
        List of middlewares to append to the stack.
    """
    if not rubric or not DEEPAGENTS_AVAILABLE:
        return []
        
    logger.info("configuring_rubric_middleware", rubric_length=len(rubric))
    
    return [
        StaticRubricMiddleware(rubric),
        RubricMiddleware(model=model)
    ]
