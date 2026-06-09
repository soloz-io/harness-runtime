"""
CloudEvent Emitter Service for Agent Executor.

This module provides the CloudEventEmitter class responsible for constructing
and publishing CloudEvents for job completion and failure notifications. It
publishes events to NATS JetStream for event-driven architecture.

Components:
    - CloudEventEmitter: Emits job.completed and job.failed CloudEvents

Architecture:
    - Connects to NATS JetStream
    - Constructs CloudEvents using the cloudevents library
    - Publishes events to NATS subjects (agent.status.completed, agent.status.failed)
    - Propagates trace_id for distributed tracing (NFR-4.2)

CloudEvent Types:
    - dev.my-platform.agent.completed: Job completed successfully
    - dev.my-platform.agent.failed: Job execution failed

References:
    - Requirements: Req. 8.3, 8.4, 13.4
    - Design: Section 2.2.3 (Message Processing Flow)
    - Tasks: Task 1.4
"""

import json
import os
import traceback
import uuid
from typing import Any, Dict, Optional

import nats
from nats.js import JetStreamContext
import structlog
from cloudevents.http import CloudEvent

from models.events import JobCompletedEvent, JobFailedEvent

logger = structlog.get_logger(__name__)


class CloudEventEmitter:
    """
    Emits CloudEvents for job completion and failure notifications.

    This service constructs CloudEvents compliant with the CNCF CloudEvents specification
    and publishes them to NATS JetStream subjects for event-driven architecture.

    Attributes:
        nats_url: NATS server URL (from NATS_URL environment variable)
        nc: NATS connection (lazy-initialized)
        js: JetStream context (lazy-initialized)

    Example:
        >>> emitter = CloudEventEmitter()
        >>> await emitter.emit_completed(
        ...     job_id="uuid-job-123",
        ...     result={"output": "Task complete"},
        ...     trace_id="uuid-trace-456"
        ... )
    """

    def __init__(self) -> None:
        """
        Initialize CloudEventEmitter with NATS configuration.

        NATS_URL is read from environment variable and defaults to the standard
        Kubernetes service URL for NATS.

        References:
            - Requirements: Req. 8.3, 8.4, 13.4
            - Tasks: Task 1.4
        """
        # Don't read NATS_URL here - read it lazily in _ensure_connected
        # This allows tests to override the env var before connection
        self.nc: Optional[nats.NATS] = None
        self.js: Optional[JetStreamContext] = None

        logger.info(
            "cloudevent_emitter_initialized",
            message="CloudEventEmitter ready to emit events to NATS",
        )

    async def _ensure_connected(self) -> None:
        """
        Ensure NATS connection is established.

        This method lazily initializes the NATS connection and JetStream context
        if they haven't been created yet. Reads NATS_URL from environment at connection
        time to allow tests to override it.

        Raises:
            Exception: If NATS connection fails
        """
        if self.nc is None or self.nc.is_closed:
            # Read NATS_URL lazily to allow test overrides
            nats_url = os.getenv("NATS_URL", "nats://nats.nats.svc:4222")
            logger.info("connecting_to_nats_for_cloudevents", nats_url=nats_url)
            # Add connection timeout to prevent hanging
            self.nc = await nats.connect(nats_url, connect_timeout=10)
            self.js = self.nc.jetstream()
            logger.info("nats_connected_for_cloudevents")

    async def emit_completed(self, job_id: str, result: Dict[str, Any], trace_id: str) -> None:
        """
        Emit a CloudEvent for successful job completion.

        Constructs a CloudEvent with type 'dev.my-platform.agent.completed' and
        publishes it to NATS subject 'agent.status.completed'. The event includes
        the job result and propagates the trace_id for distributed tracing.

        Args:
            job_id: Unique identifier for the completed job
            result: Final output or state from the LangGraph execution
            trace_id: UUID for distributed tracing across services

        Raises:
            Exception: If NATS publish fails
            ValueError: If job_id or trace_id are empty

        Example:
            >>> await emitter.emit_completed(
            ...     job_id="uuid-job-123",
            ...     result={"output": "Analysis complete", "data": {...}},
            ...     trace_id="uuid-trace-456"
            ... )

        CloudEvent Structure:
            {
                "specversion": "1.0",
                "type": "dev.my-platform.agent.completed",
                "source": "agent-executor-service",
                "subject": "uuid-job-123",
                "id": "uuid-event-789",
                "traceparent": "00-{trace_id}-...",
                "data": {
                    "job_id": "uuid-job-123",
                    "result": {...}
                }
            }

        References:
            - Requirements: Req. 8.3, 8.4, 13.4
            - Tasks: Task 1.4
        """
        # Validate inputs
        if not job_id or not job_id.strip():
            raise ValueError("job_id cannot be empty")
        if not trace_id or not trace_id.strip():
            raise ValueError("trace_id cannot be empty for distributed tracing")

        # Ensure NATS connection
        await self._ensure_connected()

        # Create data payload using Pydantic model for validation
        event_data = JobCompletedEvent(job_id=job_id, result=result)

        # Construct CloudEvent payload
        cloudevent_payload = {
            "specversion": "1.0",
            "type": "dev.my-platform.agent.completed",
            "source": "agent-executor-service",
            "subject": job_id,
            "id": str(uuid.uuid4()),
            "traceparent": self._build_traceparent(trace_id),
            "data": event_data.model_dump()
        }

        logger.info(
            "emitting_completed_cloudevent",
            job_id=job_id,
            trace_id=trace_id,
            event_type="dev.my-platform.agent.completed",
            subject="agent.status.completed",
            message="Publishing job completion CloudEvent to NATS",
        )

        # Publish to NATS
        await self.js.publish(
            subject="agent.status.completed",
            payload=json.dumps(cloudevent_payload).encode()
        )

        logger.info(
            "completed_cloudevent_emitted",
            job_id=job_id,
            trace_id=trace_id,
            event_id=cloudevent_payload["id"],
            message="Job completion CloudEvent successfully emitted to NATS",
        )

    async def emit_failed(self, job_id: str, error: Dict[str, Any], trace_id: str) -> None:
        """
        Emit a CloudEvent for failed job execution.

        Constructs a CloudEvent with type 'dev.my-platform.agent.failed' and
        publishes it to NATS subject 'agent.status.failed'. The event includes
        structured error details with message, error type, and stack trace.

        Args:
            job_id: Unique identifier for the failed job
            error: Structured error details (must contain 'message' field)
            trace_id: UUID for distributed tracing across services

        Raises:
            Exception: If NATS publish fails
            ValueError: If job_id, trace_id are empty, or error lacks 'message'

        Example:
            >>> await emitter.emit_failed(
            ...     job_id="uuid-job-123",
            ...     error={
            ...         "message": "Tool execution failed: Database timeout",
            ...         "type": "ToolExecutionError",
            ...         "stack_trace": "Traceback (most recent call last):\\n..."
            ...     },
            ...     trace_id="uuid-trace-456"
            ... )

        CloudEvent Structure:
            {
                "specversion": "1.0",
                "type": "dev.my-platform.agent.failed",
                "source": "agent-executor-service",
                "subject": "uuid-job-123",
                "id": "uuid-event-789",
                "traceparent": "00-{trace_id}-...",
                "data": {
                    "job_id": "uuid-job-123",
                    "error": {
                        "message": "...",
                        "type": "...",
                        "stack_trace": "..."
                    }
                }
            }

        References:
            - Requirements: Req. 8.3, 8.4, 13.4
            - Tasks: Task 1.4
        """
        # Validate inputs
        if not job_id or not job_id.strip():
            raise ValueError("job_id cannot be empty")
        if not trace_id or not trace_id.strip():
            raise ValueError("trace_id cannot be empty for distributed tracing")

        # Ensure NATS connection
        await self._ensure_connected()

        # Create data payload using Pydantic model for validation
        # This will validate that error contains at minimum a 'message' field
        event_data = JobFailedEvent(job_id=job_id, error=error)

        # Construct CloudEvent payload
        cloudevent_payload = {
            "specversion": "1.0",
            "type": "dev.my-platform.agent.failed",
            "source": "agent-executor-service",
            "subject": job_id,
            "id": str(uuid.uuid4()),
            "traceparent": self._build_traceparent(trace_id),
            "data": event_data.model_dump()
        }

        logger.error(
            "emitting_failed_cloudevent",
            job_id=job_id,
            trace_id=trace_id,
            event_type="dev.my-platform.agent.failed",
            error_message=error.get("message", "Unknown error"),
            error_type=error.get("type", "UnknownError"),
            subject="agent.status.failed",
            message="Publishing job failure CloudEvent to NATS",
        )

        # Publish to NATS
        await self.js.publish(
            subject="agent.status.failed",
            payload=json.dumps(cloudevent_payload).encode()
        )

        logger.error(
            "failed_cloudevent_emitted",
            job_id=job_id,
            trace_id=trace_id,
            event_id=cloudevent_payload["id"],
            error_message=error.get("message"),
            message="Job failure CloudEvent successfully emitted to NATS",
        )

    @staticmethod
    def _build_traceparent(trace_id: str) -> str:
        """
        Build W3C Trace Context traceparent header value.

        The traceparent header propagates distributed tracing context across services.
        Format: version-trace_id-parent_id-trace_flags

        Args:
            trace_id: 32-character hex trace identifier

        Returns:
            W3C traceparent header value (e.g., "00-{trace_id}-{span_id}-01")

        Example:
            >>> CloudEventEmitter._build_traceparent("a1b2c3d4e5f6...")
            "00-a1b2c3d4e5f6...-7890abcdef123456-01"

        Notes:
            - Version: "00" (current W3C Trace Context version)
            - trace_id: Propagated from incoming request
            - parent_id: Generated random 16-char hex (this span's ID)
            - trace_flags: "01" (sampled for distributed tracing)

        References:
            - W3C Trace Context: https://www.w3.org/TR/trace-context/
            - NFR-4.2: Distributed tracing requirement
        """
        # Ensure trace_id is 32 characters (pad or truncate if needed)
        normalized_trace_id = trace_id.replace("-", "").lower()[:32].zfill(32)

        # Generate a random parent_id (span_id) for this CloudEvent emission span
        parent_id = uuid.uuid4().hex[:16]

        # trace_flags: "01" means sampled (include in distributed traces)
        return f"00-{normalized_trace_id}-{parent_id}-01"
