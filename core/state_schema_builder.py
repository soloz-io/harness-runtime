"""
State Schema Builder for Dynamic AgentState Creation.

This module provides functionality to dynamically create AgentState subclasses
from schema configuration dictionaries.
"""

import structlog
from typing import Any, Dict, List
from langchain.agents.middleware.types import AgentState
from langgraph.graph.message import add_messages
from typing import Annotated

logger = structlog.get_logger(__name__)


def create_state_schema_from_config(schema_config: Dict[str, Any]) -> type:
    """
    Dynamically create an AgentState subclass from schema configuration.
    
    Args:
        schema_config: Dictionary defining state fields and their types
            Example:
            {
                "proposed_changes": {
                    "type": "list",
                    "item_type": "dict",
                    "reducer": "add_messages"
                },
                "definition": {
                    "type": "dict"
                }
            }
    
    Returns:
        Dynamically created AgentState subclass with proper type annotations
    
    Example:
        >>> schema = {
        ...     "proposed_changes": {
        ...         "type": "list",
        ...         "item_type": "dict",
        ...         "reducer": "add_messages"
        ...     }
        ... }
        >>> StateClass = create_state_schema_from_config(schema)
        >>> # StateClass now has: proposed_changes: Annotated[List[Dict[str, Any]], add_messages]
    """
    logger.info(
        "creating_state_schema",
        field_count=len(schema_config),
        fields=list(schema_config.keys())
    )
    
    # Build annotations dictionary
    annotations = {}
    
    for field_name, field_config in schema_config.items():
        field_type = field_config.get("type", "any")
        item_type = field_config.get("item_type")
        reducer = field_config.get("reducer")
        
        # Determine the Python type annotation
        if field_type == "list":
            if item_type == "dict":
                base_type = List[Dict[str, Any]]
            elif item_type == "str":
                base_type = List[str]
            elif item_type == "int":
                base_type = List[int]
            else:
                base_type = List[Any]
            
            # Apply reducer if specified
            if reducer == "add_messages":
                annotations[field_name] = Annotated[base_type, add_messages]
            else:
                annotations[field_name] = base_type
                
        elif field_type == "dict":
            annotations[field_name] = Dict[str, Any]
        elif field_type == "str":
            annotations[field_name] = str
        elif field_type == "int":
            annotations[field_name] = int
        else:
            annotations[field_name] = Any
        
        logger.debug(
            "field_annotation_created",
            field_name=field_name,
            field_type=field_type,
            has_reducer=bool(reducer)
        )
    
    # Create dynamic class inheriting from AgentState
    DynamicState = type(
        "DynamicAgentState",
        (AgentState,),
        {"__annotations__": annotations}
    )
    
    logger.info(
        "state_schema_created",
        class_name="DynamicAgentState",
        field_count=len(annotations)
    )
    
    return DynamicState
