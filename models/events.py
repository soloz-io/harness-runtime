"""
CloudEvent data models for Agent Executor Service.

This module defines Pydantic models for the data payloads of CloudEvents
consumed and produced by the Agent Executor service. These models ensure
type safety and validation of event data structures.

Models:
    - JobExecutionEvent: Payload for incoming agent execution requests
    - JobCompletedEvent: Payload for successful job completion notifications
    - JobFailedEvent: Payload for failed job notifications

References:
    - Requirements: Req. 1.2, 1.3, 5.2, 5.4
    - Design: Section 4 (Data Models)
"""

from typing import Any, Dict, Optional
from pydantic import BaseModel, Field, field_validator


class JobExecutionEvent(BaseModel):
    """
    Data payload for the CloudEvent that triggers agent execution.

    This event is consumed from the NATS message queue (subject: agent.execute.*)
    and contains all information needed to execute a LangGraph agent.

    Attributes:
        trace_id: UUID string for distributed tracing across services
        job_id: Unique identifier for this job execution (also used as thread_id)
        agent_definition: Complete LangGraph agent definition including tools and configuration
        input_payload: Input data to be passed to the agent for execution

    Example:
        {
            "trace_id": "uuid-for-distributed-tracing",
            "job_id": "uuid-string-1234",
            "agent_definition": {
                "nodes": [...],
                "edges": [...],
                "tool_definitions": [...]
            },
            "input_payload": {
                "messages": [{"role": "user", "content": "Please analyze this data."}]
            }
        }
    """

    trace_id: str = Field(
        ...,
        description="UUID for distributed tracing",
        min_length=1
    )

    job_id: str = Field(
        ...,
        description="Unique job identifier (used as thread_id for LangGraph)",
        min_length=1
    )

    agent_definition: Dict[str, Any] = Field(
        ...,
        description="LangGraph agent definition with nodes, edges, and tools"
    )

    input_payload: Dict[str, Any] = Field(
        ...,
        description="Input data for agent execution"
    )

    @field_validator('trace_id', 'job_id')
    @classmethod
    def validate_non_empty_string(cls, v: str) -> str:
        """Ensure trace_id and job_id are non-empty strings."""
        if not v or not v.strip():
            raise ValueError("Field cannot be empty or whitespace")
        return v.strip()

    @field_validator('agent_definition')
    @classmethod
    def validate_agent_definition(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure agent_definition is not empty."""
        if not v:
            raise ValueError("agent_definition cannot be empty")
        return v

    @field_validator('input_payload')
    @classmethod
    def validate_input_payload(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure input_payload is not empty."""
        if not v:
            raise ValueError("input_payload cannot be empty")
        return v

    model_config = {
        "json_schema_extra": {
            "example": {
                "trace_id": "uuid-trace-123",
                "job_id": "uuid-job-456",
                "agent_definition": {
                    "nodes": [{"id": "orchestrator", "type": "agent"}],
                    "edges": [{"from": "START", "to": "orchestrator"}],
                    "tool_definitions": []
                },
                "input_payload": {
                    "messages": [{"role": "user", "content": "Execute task"}]
                }
            }
        }
    }


class JobCompletedEvent(BaseModel):
    """
    Data payload for the CloudEvent emitted when a job completes successfully.

    This event is published to NATS (subject: agent.status.*) via Knative NatsSink
    to notify the Agent Lifecycle API of successful job completion.

    Attributes:
        job_id: Unique identifier for the completed job
        result: Final output or state from the LangGraph execution

    Example:
        {
            "job_id": "uuid-string-1234",
            "result": {
                "output": "Task completed successfully",
                "final_state": {...}
            }
        }
    """

    job_id: str = Field(
        ...,
        description="Unique job identifier",
        min_length=1
    )

    result: Dict[str, Any] = Field(
        ...,
        description="Final result from agent execution"
    )

    @field_validator('job_id')
    @classmethod
    def validate_non_empty_string(cls, v: str) -> str:
        """Ensure job_id is a non-empty string."""
        if not v or not v.strip():
            raise ValueError("job_id cannot be empty or whitespace")
        return v.strip()

    @field_validator('result')
    @classmethod
    def validate_result(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure result is not None (empty dict is allowed)."""
        if v is None:
            raise ValueError("result cannot be None")
        return v

    model_config = {
        "json_schema_extra": {
            "example": {
                "job_id": "uuid-job-456",
                "result": {
                    "output": "Analysis complete",
                    "data": {"key": "value"}
                }
            }
        }
    }


class JobFailedEvent(BaseModel):
    """
    Data payload for the CloudEvent emitted when a job execution fails.

    This event is published to NATS (subject: agent.status.*) via Knative NatsSink
    to notify the Agent Lifecycle API of job failure with structured error details.

    Attributes:
        job_id: Unique identifier for the failed job
        error: Structured error information with message and optional stack trace

    Example:
        {
            "job_id": "uuid-string-1234",
            "error": {
                "message": "Tool execution failed: SQL Query Error",
                "type": "ToolExecutionError",
                "stack_trace": "Traceback (most recent call last):\\n..."
            }
        }
    """

    job_id: str = Field(
        ...,
        description="Unique job identifier",
        min_length=1
    )

    error: Dict[str, Any] = Field(
        ...,
        description="Structured error details (message, type, stack_trace)"
    )

    @field_validator('job_id')
    @classmethod
    def validate_non_empty_string(cls, v: str) -> str:
        """Ensure job_id is a non-empty string."""
        if not v or not v.strip():
            raise ValueError("job_id cannot be empty or whitespace")
        return v.strip()

    @field_validator('error')
    @classmethod
    def validate_error(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        """
        Ensure error dict contains at minimum a 'message' field.

        Validates that the error structure includes the essential 'message' field
        while allowing additional fields like 'type' and 'stack_trace'.
        """
        if not v:
            raise ValueError("error cannot be empty")
        if 'message' not in v:
            raise ValueError("error must contain a 'message' field")
        if not v['message'] or not str(v['message']).strip():
            raise ValueError("error message cannot be empty")
        return v

    model_config = {
        "json_schema_extra": {
            "example": {
                "job_id": "uuid-job-456",
                "error": {
                    "message": "Tool execution failed: Database connection timeout",
                    "type": "DatabaseConnectionError",
                    "stack_trace": "Traceback (most recent call last):\\n  File..."
                }
            }
        }
    }


# New models for HTTP API endpoints

class JobRequest(BaseModel):
    """
    Request model for POST /deepagents-runtime/invoke endpoint.
    
    This model represents the HTTP request payload for initiating
    agent execution via the new HTTP API.
    
    Attributes:
        trace_id: UUID string for distributed tracing
        job_id: Unique identifier for this job execution (also used as thread_id)
        agent_definition: Complete LangGraph agent definition
        input_payload: Input data to be passed to the agent
    """
    
    trace_id: str = Field(
        ...,
        description="UUID for distributed tracing",
        min_length=1
    )
    
    job_id: str = Field(
        ...,
        description="Unique job identifier (used as thread_id for LangGraph)",
        min_length=1
    )
    
    agent_definition: Dict[str, Any] = Field(
        ...,
        description="LangGraph agent definition with nodes, edges, and tools"
    )
    
    input_payload: Dict[str, Any] = Field(
        ...,
        description="Input data for agent execution"
    )
    
    @field_validator('trace_id', 'job_id')
    @classmethod
    def validate_non_empty_string(cls, v: str) -> str:
        """Ensure trace_id and job_id are non-empty strings."""
        if not v or not v.strip():
            raise ValueError("Field cannot be empty or whitespace")
        return v.strip()
    
    @field_validator('agent_definition')
    @classmethod
    def validate_agent_definition(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure agent_definition is not empty."""
        if not v:
            raise ValueError("agent_definition cannot be empty")
        return v
    
    @field_validator('input_payload')
    @classmethod
    def validate_input_payload(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure input_payload is not empty."""
        if not v:
            raise ValueError("input_payload cannot be empty")
        return v


class JobResponse(BaseModel):
    """
    Response model for POST /deepagents-runtime/invoke endpoint.
    
    Attributes:
        thread_id: Unique identifier for the execution thread
        status: Current status of the job
    """
    
    thread_id: str = Field(
        ...,
        description="Unique identifier for the execution thread"
    )
    
    status: str = Field(
        ...,
        description="Current status of the job",
        pattern="^(started|processing)$"
    )


class ExecutionState(BaseModel):
    """
    Response model for GET /deepagents-runtime/state/{thread_id} endpoint.
    
    Attributes:
        thread_id: Unique identifier for the execution thread
        status: Current status of the execution
        result: Final result if completed
        generated_files: Generated files if available
        error: Error details if failed
    """
    
    thread_id: str = Field(
        ...,
        description="Unique identifier for the execution thread"
    )
    
    status: str = Field(
        ...,
        description="Current status of the execution",
        pattern="^(running|completed|failed)$"
    )
    
    result: Optional[Dict[str, Any]] = Field(
        None,
        description="Final result from agent execution (if completed)"
    )
    
    generated_files: Optional[Dict[str, Any]] = Field(
        None,
        description="Generated files from execution (if available)"
    )
    
    error: Optional[Dict[str, Any]] = Field(
        None,
        description="Error details (if failed)"
    )


class StreamEvent(BaseModel):
    """
    Model for WebSocket streaming events.
    
    Attributes:
        event_type: Type of the event (on_state_update, on_llm_stream, end)
        data: Event data payload
    """
    
    event_type: str = Field(
        ...,
        description="Type of the event"
    )
    
    data: Dict[str, Any] = Field(
        ...,
        description="Event data payload"
    )
