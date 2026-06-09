"""
NATS Consumer Service for Agent Executor.

This module provides the NATSConsumer class responsible for consuming CloudEvents
from NATS JetStream, executing agents, and publishing result CloudEvents back to NATS.

Components:
    - NATSConsumer: Consumes messages from NATS and processes agent execution requests

Architecture:
    - Connects to NATS JetStream
    - Creates durable consumer for AGENT_EXECUTION stream
    - Processes CloudEvents containing JobExecutionEvent payloads
    - Executes agents using ExecutionManager
    - Publishes result CloudEvents to NATS subjects

References:
    - Requirements: Req. 1.2, 8.1, 8.2, 8.5, 13.2, 13.3
    - Design: Section 2.2.3 (Message Processing Flow)
    - Tasks: Task 1.2
"""

import asyncio
import json
import traceback
import uuid
from typing import Any, Dict, Optional

import nats
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig
import structlog
from pydantic import ValidationError

from core.builder import GraphBuilder
from core.executor import ExecutionManager
from models.events import JobExecutionEvent
from services.cloudevents import CloudEventEmitter
from observability.metrics import (
    deepagents_runtime_nats_messages_processed_total,
    deepagents_runtime_nats_messages_failed_total
)

logger = structlog.get_logger(__name__)


class NATSConsumer:
    """
    Consumes CloudEvents from NATS JetStream and processes agent execution requests.

    This service connects to NATS JetStream, subscribes to the AGENT_EXECUTION stream,
    and processes incoming messages by executing agents and publishing results.

    Attributes:
        nats_url: NATS server URL
        stream_name: JetStream stream name (AGENT_EXECUTION)
        consumer_group: Durable consumer name for load balancing
        execution_manager: ExecutionManager instance for agent execution
        cloudevent_emitter: CloudEventEmitter instance for publishing results
        nc: NATS connection
        js: JetStream context
        running: Flag indicating if consumer is running

    Example:
        >>> consumer = NATSConsumer(
        ...     nats_url="nats://nats.nats.svc:4222",
        ...     stream_name="AGENT_EXECUTION",
        ...     consumer_group="agent-executor-workers",
        ...     execution_manager=execution_manager,
        ...     cloudevent_emitter=cloudevent_emitter
        ... )
        >>> await consumer.start()
    """

    def __init__(
        self,
        nats_url: str,
        stream_name: str,
        consumer_group: str,
        execution_manager: ExecutionManager,
        cloudevent_emitter: CloudEventEmitter
    ) -> None:
        """
        Initialize NATSConsumer with configuration.

        Args:
            nats_url: NATS server URL (e.g., "nats://nats.nats.svc:4222")
            stream_name: JetStream stream name (e.g., "AGENT_EXECUTION")
            consumer_group: Durable consumer name (e.g., "agent-executor-workers")
            execution_manager: ExecutionManager instance for executing agents
            cloudevent_emitter: CloudEventEmitter instance for publishing results

        References:
            - Requirements: Req. 1.2, 13.2
            - Tasks: Task 1.2
        """
        self.nats_url = nats_url
        self.stream_name = stream_name
        self.consumer_group = consumer_group
        self.execution_manager = execution_manager
        self.cloudevent_emitter = cloudevent_emitter
        
        self.nc: Optional[nats.NATS] = None
        self.js: Optional[JetStreamContext] = None
        self.running = False

        logger.info(
            "nats_consumer_initialized",
            nats_url=self.nats_url,
            stream_name=self.stream_name,
            consumer_group=self.consumer_group
        )

    async def start(self) -> None:
        """
        Start the NATS consumer and begin processing messages.

        This method:
        1. Connects to NATS server
        2. Creates JetStream context
        3. Creates or retrieves durable consumer
        4. Continuously fetches and processes messages
        5. Handles errors and reconnection

        This method runs indefinitely until stop() is called.

        Raises:
            Exception: If NATS connection fails or consumer creation fails

        References:
            - Requirements: Req. 1.2, 8.1, 13.2
            - Tasks: Task 1.2
        """
        try:
            logger.info("connecting_to_nats", nats_url=self.nats_url)
            
            # Connect to NATS with timeout to prevent hanging
            self.nc = await nats.connect(self.nats_url, connect_timeout=10)
            self.js = self.nc.jetstream()
            
            logger.info("nats_connected", nats_url=self.nats_url)

            # Create or retrieve durable consumer
            try:
                consumer_config = ConsumerConfig(
                    durable_name=self.consumer_group,
                    ack_policy="explicit",
                    max_deliver=3,  # Retry up to 3 times
                    ack_wait=300,  # 5 minutes to process message
                )
                
                # Create pull-based consumer
                consumer = await self.js.pull_subscribe(
                    subject="agent.execute.*",
                    durable=self.consumer_group,
                    stream=self.stream_name,
                    config=consumer_config
                )
                
                logger.info(
                    "nats_consumer_created",
                    stream=self.stream_name,
                    consumer=self.consumer_group,
                    subject="agent.execute.*"
                )
            except Exception as e:
                logger.error(
                    "nats_consumer_creation_failed",
                    error=str(e),
                    stream=self.stream_name,
                    consumer=self.consumer_group
                )
                raise

            # Start consuming messages
            self.running = True
            logger.info("nats_consumer_started", message="Starting message consumption loop")

            while self.running:
                try:
                    # Fetch messages in batches
                    msgs = await consumer.fetch(batch=1, timeout=5)
                    
                    for msg in msgs:
                        try:
                            await self.process_message(msg)
                            await msg.ack()
                            logger.info(
                                "message_acknowledged",
                                subject=msg.subject,
                                sequence=msg.metadata.sequence.stream
                            )
                        except Exception as e:
                            logger.error(
                                "message_processing_failed",
                                error=str(e),
                                subject=msg.subject,
                                stack_trace=traceback.format_exc()
                            )
                            # Negative acknowledgment - message will be redelivered
                            await msg.nak()
                
                except asyncio.TimeoutError:
                    # No messages available, continue polling
                    continue
                except Exception as e:
                    logger.error(
                        "nats_fetch_error",
                        error=str(e),
                        stack_trace=traceback.format_exc()
                    )
                    # Wait before retrying
                    await asyncio.sleep(5)

        except Exception as e:
            logger.error(
                "nats_consumer_failed",
                error=str(e),
                stack_trace=traceback.format_exc()
            )
            raise
        finally:
            if self.nc and not self.nc.is_closed:
                await self.nc.close()
                logger.info("nats_connection_closed")

    async def stop(self) -> None:
        """
        Stop the NATS consumer gracefully.

        This method sets the running flag to False, which will cause the
        message consumption loop to exit.

        References:
            - Requirements: Req. 1.2
            - Tasks: Task 1.3
        """
        logger.info("stopping_nats_consumer")
        self.running = False
        
        if self.nc and not self.nc.is_closed:
            await self.nc.close()
            logger.info("nats_connection_closed")

    async def process_message(self, msg: Any) -> None:
        """
        Process a NATS message containing a CloudEvent.

        This method:
        1. Parses the message data as a CloudEvent
        2. Extracts JobExecutionEvent from CloudEvent data
        3. Builds LangGraph agent from agent_definition
        4. Executes agent using ExecutionManager
        5. Publishes result CloudEvent to NATS

        Args:
            msg: NATS message object

        Raises:
            ValidationError: If CloudEvent or JobExecutionEvent is malformed
            Exception: If agent execution fails

        References:
            - Requirements: Req. 8.1, 8.2, 8.5, 13.3
            - Tasks: Task 1.2
        """
        try:
            # Parse message data as JSON
            message_data = json.loads(msg.data.decode())
            
            logger.info(
                "processing_nats_message",
                subject=msg.subject,
                sequence=msg.metadata.sequence.stream if msg.metadata else None
            )

            # Extract JobExecutionEvent from CloudEvent data
            # CloudEvent structure: {"data": {...}, "type": "...", ...}
            if "data" in message_data:
                event_data = message_data["data"]
            else:
                # If no 'data' field, assume the body itself is the event data
                event_data = message_data

            # Validate and parse JobExecutionEvent using Pydantic
            try:
                job_event = JobExecutionEvent(**event_data)
            except ValidationError as e:
                logger.error(
                    "malformed_job_execution_event",
                    validation_errors=e.errors(),
                    event_data=event_data
                )
                raise

            # Extract fields from JobExecutionEvent
            trace_id = job_event.trace_id
            job_id = job_event.job_id
            agent_definition = job_event.agent_definition
            input_payload = job_event.input_payload

            logger.info(
                "executing_agent_from_nats",
                trace_id=trace_id,
                job_id=job_id,
                has_agent_definition=bool(agent_definition),
                has_input_payload=bool(input_payload)
            )

            # Build LangGraph agent from definition
            graph_builder = GraphBuilder(checkpointer=self.execution_manager.checkpointer)
            compiled_graph = graph_builder.build_from_definition(agent_definition)
            
            logger.info("agent_built_from_nats", job_id=job_id, trace_id=trace_id)

            # Execute agent with streaming
            result = self.execution_manager.execute(
                graph=compiled_graph,
                job_id=job_id,
                input_payload=input_payload,
                trace_id=trace_id
            )
            
            logger.info(
                "agent_execution_completed_from_nats",
                job_id=job_id,
                trace_id=trace_id,
                has_result=bool(result)
            )

            # Publish result CloudEvent to NATS
            await self.publish_result(
                job_id=job_id,
                result=result,
                trace_id=trace_id,
                status="completed"
            )

            # Record success metric
            deepagents_runtime_nats_messages_processed_total.inc()

        except Exception as e:
            # Execution failure: Publish failed CloudEvent
            logger.error(
                "agent_execution_failed_from_nats",
                error=str(e),
                error_type=type(e).__name__,
                stack_trace=traceback.format_exc()
            )

            # Try to extract job_id and trace_id for error reporting
            try:
                job_id = job_event.job_id if 'job_event' in locals() else "unknown"
                trace_id = job_event.trace_id if 'job_event' in locals() else str(uuid.uuid4())
            except:
                job_id = "unknown"
                trace_id = str(uuid.uuid4())

            # Construct structured error payload
            error_payload = {
                "message": str(e),
                "type": type(e).__name__,
                "stack_trace": traceback.format_exc()
            }

            # Publish failed CloudEvent to NATS
            await self.publish_result(
                job_id=job_id,
                result=error_payload,
                trace_id=trace_id,
                status="failed"
            )

            # Record failure metric
            deepagents_runtime_nats_messages_failed_total.inc()

    async def publish_result(
        self,
        job_id: str,
        result: Dict[str, Any],
        trace_id: str,
        status: str
    ) -> None:
        """
        Publish result CloudEvent to NATS.

        This method publishes a CloudEvent to the appropriate NATS subject
        based on the execution status (completed or failed).

        Args:
            job_id: Job identifier
            result: Execution result or error payload
            trace_id: Trace identifier for distributed tracing
            status: Execution status ("completed" or "failed")

        Raises:
            Exception: If NATS publish fails

        References:
            - Requirements: Req. 8.3, 8.4, 13.4
            - Tasks: Task 1.2, 1.4
        """
        try:
            # Determine subject based on status
            if status == "completed":
                subject = "agent.status.completed"
            else:
                subject = "agent.status.failed"

            # Construct CloudEvent payload
            cloudevent_data = {
                "specversion": "1.0",
                "type": f"dev.my-platform.agent.{status}",
                "source": "agent-executor-service",
                "subject": job_id,
                "id": str(uuid.uuid4()),
                "traceparent": self._build_traceparent(trace_id),
                "data": {
                    "job_id": job_id,
                    "result" if status == "completed" else "error": result
                }
            }

            # Publish to NATS
            await self.js.publish(
                subject=subject,
                payload=json.dumps(cloudevent_data).encode()
            )

            logger.info(
                "result_published_to_nats",
                job_id=job_id,
                trace_id=trace_id,
                subject=subject,
                status=status
            )

        except Exception as e:
            logger.error(
                "nats_publish_failed",
                job_id=job_id,
                trace_id=trace_id,
                subject=subject,
                error=str(e),
                stack_trace=traceback.format_exc()
            )
            raise

    @staticmethod
    def _build_traceparent(trace_id: str) -> str:
        """
        Build W3C Trace Context traceparent header value.

        Args:
            trace_id: 32-character hex trace identifier

        Returns:
            W3C traceparent header value

        References:
            - W3C Trace Context: https://www.w3.org/TR/trace-context/
        """
        # Ensure trace_id is 32 characters (pad or truncate if needed)
        normalized_trace_id = trace_id.replace("-", "").lower()[:32].zfill(32)

        # Generate a random parent_id (span_id)
        parent_id = uuid.uuid4().hex[:16]

        # trace_flags: "01" means sampled
        return f"00-{normalized_trace_id}-{parent_id}-01"

    async def wait_for_connection(self, timeout: float = 10.0) -> bool:
        """
        Wait for NATS connection to be established.

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            True if connection is established, False if timeout

        References:
            - Requirements: Req. 17.3
            - Tasks: Task 1.6
        """
        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < timeout:
            if self.nc is not None and not self.nc.is_closed:
                return True
            await asyncio.sleep(0.1)
        return False

    def health_check(self) -> bool:
        """
        Check if NATS consumer is healthy.

        Returns:
            True if connected and running, False otherwise

        References:
            - Requirements: Req. 17.3
            - Tasks: Task 1.6
        """
        return self.nc is not None and not self.nc.is_closed and self.running
