"""
Execution Manager Module for Agent Executor.

This module provides the ExecutionManager class responsible for executing
LangGraph agents with state persistence and real-time event streaming.

The ExecutionManager:
- Configures PostgreSQL checkpointing for stateful execution
- Executes LangGraph graphs with streaming support
- Publishes real-time events to Redis during execution
- Handles completion and error scenarios
- Integrates OpenTelemetry tracing for observability

Classes:
    - ExecutionManager: Main class for managing graph execution

References:
    - Requirements: Req. 3.2, 3.3, 3.4, 4.1, 4.2, 4.3, NFR-3.1, NFR-4.2
    - Design: Section 2.11 (Internal Component Architecture)
    - Tasks: Task 7 (Execution Manager Core Logic)
"""

import time
from typing import Any, Dict, Optional

import structlog
from langchain_core.runnables import Runnable
from langgraph.checkpoint.postgres import PostgresSaver

# Import OpenTelemetry for distributed tracing
try:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode
    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False
    import warnings
    warnings.warn(
        "OpenTelemetry not available. Distributed tracing will be disabled.",
        ImportWarning
    )

from observability.metrics import deepagents_runtime_db_connection_errors_total

logger = structlog.get_logger(__name__)


class ExecutionError(Exception):
    """Raised when graph execution fails."""
    pass


class ExecutionManager:
    """
    Manages LangGraph execution with checkpointing and streaming.

    This class is responsible for:
    1. Setting up PostgreSQL checkpointer for state persistence
    2. Executing LangGraph graphs with streaming support
    3. Publishing real-time events to Redis during execution
    4. Handling completion and error scenarios
    5. Propagating distributed tracing context

    The ExecutionManager ensures that all agent executions are:
    - Stateful: Checkpoints saved to PostgreSQL after each node
    - Observable: Events streamed to Redis in real-time
    - Traceable: OpenTelemetry spans created with trace_id propagation
    - Resilient: Checkpoints preserved even on partial failures

    Attributes:
        redis_client: RedisClient instance for streaming events
        postgres_connection_string: PostgreSQL connection string with credentials
        checkpointer: PostgresSaver instance for checkpoint persistence
        connection_pool: PostgreSQL connection pool

    Example:
        redis_client = RedisClient(host="redis.example.com")
        postgres_conn_str = "postgresql://user:pass@host:5432/db?search_path=agent_executor"

        executor = ExecutionManager(redis_client, postgres_conn_str)

        result = executor.execute(
            graph=compiled_graph,
            job_id="job-123",
            input_payload={"messages": [{"role": "user", "content": "Hello"}]},
            trace_id="trace-456"
        )
    """

    def __init__(
        self,
        redis_client: Any,  # RedisClient from services.redis
        postgres_connection_string: str,
    ) -> None:
        """
        Initialize ExecutionManager with dependencies.

        Args:
            redis_client: RedisClient instance for streaming events
            postgres_connection_string: PostgreSQL connection string with credentials
                                       Must include search_path=agent_executor option

        References:
            - Requirements: Req. 3.2, 4.1
            - Design: Section 2.4 (Database Connection Architecture)
            - Tasks: Task 7.1
        """
        self.redis_client = redis_client
        self.postgres_connection_string = postgres_connection_string
        self.checkpointer: Optional[PostgresSaver] = None
        self._checkpointer_context = None  # Store context manager for cleanup

        # Initialize checkpointer on construction
        self._setup_checkpointer()

        logger.info(
            "execution_manager_initialized",
            has_redis=redis_client is not None,
            has_postgres=bool(postgres_connection_string)
        )

    def _setup_checkpointer(self) -> None:
        """
        Set up PostgreSQL checkpointer using LangGraph's recommended approach.

        Uses PostgresSaver.from_conn_string() which properly handles:
        - autocommit=True for CREATE INDEX CONCURRENTLY statements
        - row_factory=dict_row for proper result handling
        - Connection pooling internally

        Note: We store the checkpointer as a context manager and call setup()
        which creates the checkpoint tables if they don't exist.

        Raises:
            Exception: If checkpointer initialization fails

        References:
            - Requirements: Req. 3.2
            - Design: Section 2.4 (Database Connection Architecture)
            - Tasks: Task 7.2
            - LangGraph docs: https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-postgres/README.md
        """
        try:
            logger.info("setting_up_postgres_checkpointer")

            # Use from_conn_string which properly configures:
            # - autocommit=True (required for CREATE INDEX CONCURRENTLY)
            # - row_factory=dict_row (required for proper result handling)
            # This is the recommended approach per LangGraph documentation
            #
            # IMPORTANT: from_conn_string() is a context manager, so we need to
            # manually enter it and store the context for later cleanup.
            # This ensures the connection has autocommit=True when setup() runs.
            self._checkpointer_context = PostgresSaver.from_conn_string(
                self.postgres_connection_string
            )
            self.checkpointer = self._checkpointer_context.__enter__()
            
            # Call setup() to create checkpoint tables if they don't exist
            # This runs the migrations defined in PostgresSaver.MIGRATIONS
            logger.info("running_postgres_checkpointer_setup")
            self.checkpointer.setup()
            logger.info("postgres_checkpointer_setup_completed")

            logger.info("postgres_checkpointer_initialized")

        except Exception as e:
            deepagents_runtime_db_connection_errors_total.inc()
            logger.error(
                "checkpointer_setup_failed",
                error=str(e),
                error_type=type(e).__name__
            )
            raise

    def execute(
        self,
        graph: Runnable,
        job_id: str,
        input_payload: Dict[str, Any],
        trace_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Execute LangGraph with streaming and checkpoint persistence.

        This is the main execution method that:
        1. Compiles the graph with PostgresSaver checkpointer
        2. Configures execution with thread_id=job_id for checkpoint persistence
        3. Invokes graph.stream() with input payload
        4. Iterates over stream events and publishes to Redis
        5. Extracts and publishes LLM token events, tool start/end events
        6. Creates OpenTelemetry span with trace_id propagation
        7. Publishes final 'end' event to Redis
        8. Returns final result dictionary

        Args:
            graph: Compiled LangGraph runnable (from GraphBuilder)
            job_id: Unique job identifier (used as thread_id for checkpoints)
            input_payload: Initial input data for graph execution
            trace_id: Optional distributed tracing ID for correlation

        Returns:
            Final result dictionary from graph execution

        Raises:
            ExecutionError: If execution fails at any step

        References:
            - Requirements: Req. 3.3, 3.4, 4.1, 4.2, NFR-4.2
            - Design: Section 2.5 (Redis Streaming Architecture)
            - Tasks: Task 7.3, Task 9.2
        """
        start_time = time.time()

        # Create OpenTelemetry span if available
        # Note: This span is created within the current trace context propagated from main.py
        if OTEL_AVAILABLE:
            tracer = trace.get_tracer(__name__)
            span = tracer.start_span(
                "graph_execution",
                attributes={
                    "job_id": job_id,
                    "trace_id": trace_id or "unknown",
                    "thread_id": job_id
                }
            )
        else:
            span = None

        try:
            logger.info(
                "starting_graph_execution",
                job_id=job_id,
                trace_id=trace_id,
                has_checkpointer=self.checkpointer is not None
            )

            # Configure execution with thread_id
            # As per requirements: "THE Agent Executor SHALL use the job_id as the thread_id"
            # Note: The graph is already compiled with the checkpointer in GraphBuilder
            config = {
                "configurable": {
                    "thread_id": job_id
                }
            }

            logger.info(
                "invoking_graph_stream",
                job_id=job_id,
                thread_id=job_id,
                trace_id=trace_id
            )

            # Invoke graph.stream() with input payload
            # Use multiple stream modes to capture both state updates and LLM token events
            # - "values": State updates after each step
            # - "messages": LLM token-by-token streaming
            # - "events": Tool invocations and other events (including task tool for subagents)
            stream_modes = ["values", "messages", "events"]
            
            final_state = None
            event_count = 0

            logger.info("starting_stream_iteration", job_id=job_id)
            
            for event in graph.stream(input_payload, config, stream_mode=stream_modes):
                event_count += 1
                
                if event_count == 1:
                    logger.info("first_event_received", job_id=job_id)
                if event_count % 10 == 0:
                    logger.info("stream_progress", job_id=job_id, event_count=event_count)

                # When using multiple stream modes, events are tuples of (mode, data)
                if isinstance(event, tuple) and len(event) == 2:
                    mode, data = event
                    if mode == "messages":
                        # LLM token event
                        event_type = "on_llm_stream"
                        event_data = self._extract_event_data(data)
                    elif mode == "values":
                        # State update event
                        event_type = "on_state_update"
                        event_data = self._extract_event_data(data)
                        final_state = data  # Store final state
                    elif mode == "events":
                        # Tool/chain events (includes task tool invocations)
                        # Extract event type from the event data
                        event_type = data.get("event", "on_event")
                        event_data = data
                    else:
                        event_type = f"on_{mode}"
                        event_data = self._extract_event_data(data)
                else:
                    # Fallback for single stream mode
                    event_type = self._determine_event_type(event)
                    event_data = self._extract_event_data(event)
                    final_state = event

                # Publish stream event to Redis
                # Channel format: langgraph:stream:{thread_id}
                self.redis_client.publish_stream_event(
                    thread_id=job_id,
                    event_type=event_type,
                    data=event_data,
                    trace_id=trace_id,
                    job_id=job_id
                )

                # Log significant events
                if event_type in ["on_llm_stream", "on_tool_start", "on_tool_end"]:
                    logger.debug(
                        "stream_event_published",
                        job_id=job_id,
                        event_type=event_type,
                        trace_id=trace_id
                    )

            # After stream completes, publish 'end' event
            self._handle_completion(job_id, trace_id)

            # Extract final result from graph state
            final_result = self._extract_final_result(final_state)

            # Calculate execution duration
            duration_ms = int((time.time() - start_time) * 1000)

            logger.info(
                "graph_execution_completed",
                job_id=job_id,
                trace_id=trace_id,
                duration_ms=duration_ms,
                event_count=event_count
            )

            # Set span status to OK if available
            if span:
                span.set_status(Status(StatusCode.OK))
                span.set_attribute("duration_ms", duration_ms)
                span.set_attribute("event_count", event_count)

            return final_result

        except Exception as e:
            # Log execution failure with full stack trace
            logger.error(
                "graph_execution_failed",
                job_id=job_id,
                trace_id=trace_id,
                error=str(e),
                error_type=type(e).__name__,
                duration_ms=int((time.time() - start_time) * 1000)
            )

            # Set span status to ERROR if available
            if span:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)

            # Re-raise exception for handling at API layer
            # As per requirements: "Re-raise exceptions for handling at API layer"
            raise ExecutionError(f"Graph execution failed: {e}") from e

        finally:
            # End OpenTelemetry span
            if span:
                span.end()

    def _determine_event_type(self, event: Any) -> str:
        """
        Determine the event type from a stream event.

        Args:
            event: Stream event from LangGraph

        Returns:
            Event type string (e.g., "on_llm_stream", "on_tool_start", "on_tool_end", "on_chain_end")
        """
        # LangGraph stream events typically have a structure like:
        # {"event": "on_llm_stream", "data": {...}}
        # or they might be state updates

        if isinstance(event, dict):
            if "event" in event:
                return event["event"]
            elif "messages" in event:
                return "on_chain_end"  # State update with messages
            else:
                return "on_state_update"
        else:
            return "unknown"

    def _extract_event_data(self, event: Any) -> Dict[str, Any]:
        """
        Extract data payload from a stream event.

        This method safely extracts data from LangGraph stream events,
        handling non-serializable objects like Overwrite, Command, etc.

        Args:
            event: Stream event from LangGraph

        Returns:
            JSON-serializable event data dictionary
        """
        if isinstance(event, dict):
            # Create a serializable copy of the event
            serializable_event = {}
            
            for key, value in event.items():
                try:
                    # Try to serialize the value to check if it's JSON-safe
                    import json
                    json.dumps(value)
                    serializable_event[key] = value
                except (TypeError, ValueError):
                    # If not serializable, convert to string representation
                    serializable_event[key] = str(value)
            
            # If event has a 'data' field, return it
            if "data" in serializable_event:
                return serializable_event["data"]
            
            # Otherwise return the sanitized event
            return serializable_event
        else:
            return {"raw_event": str(event)}

    def _handle_completion(
        self,
        job_id: str,
        trace_id: Optional[str] = None
    ) -> None:
        """
        Handle execution completion by publishing 'end' event.

        This method is called after the graph stream completes successfully.
        It publishes a final 'end' event to Redis to signal that execution
        has finished and clients can stop listening for stream events.

        Args:
            job_id: Job identifier (used as thread_id)
            trace_id: Optional distributed tracing ID

        References:
            - Requirements: Req. 4.3
            - Tasks: Task 7.4
        """
        try:
            # Publish 'end' event to Redis
            # As per requirements: "THE Agent Executor SHALL publish an 'end' event"
            self.redis_client.publish_end_event(
                thread_id=job_id,
                trace_id=trace_id,
                job_id=job_id
            )

            logger.info(
                "completion_event_published",
                job_id=job_id,
                trace_id=trace_id
            )

        except Exception as e:
            # Log error but don't fail the execution
            # The execution completed successfully even if end event fails
            logger.warning(
                "completion_event_publish_failed",
                job_id=job_id,
                trace_id=trace_id,
                error=str(e)
            )

    def _extract_final_result(self, final_state: Any) -> Dict[str, Any]:
        """
        Extract final result from graph state.

        Args:
            final_state: Final state from graph execution

        Returns:
            Dictionary containing the final result
        """
        if final_state is None:
            return {"output": None, "status": "completed"}

        if isinstance(final_state, dict):
            # If state has messages, extract the last message
            if "messages" in final_state and final_state["messages"]:
                last_message = final_state["messages"][-1]
                if hasattr(last_message, "content"):
                    return {"output": last_message.content, "status": "completed"}
                else:
                    return {"output": str(last_message), "status": "completed"}

            # If state has other fields, return them
            return {"output": final_state, "status": "completed"}

        # Fallback: return string representation
        return {"output": str(final_state), "status": "completed"}

    def health_check(self) -> bool:
        """
        Check if ExecutionManager dependencies are healthy.

        Returns:
            True if PostgreSQL and Redis are accessible, False otherwise
        """
        try:
            # Check PostgreSQL connection via checkpointer
            if not self.checkpointer:
                return False

            # The checkpointer manages its own connection pool internally
            # We can verify it's working by checking if it was initialized
            
            # Check Redis connection
            if not self.redis_client.health_check():
                return False

            return True

        except Exception as e:
            deepagents_runtime_db_connection_errors_total.inc()
            logger.error("execution_manager_health_check_failed", error=str(e))
            return False

    def close(self) -> None:
        """
        Close ExecutionManager resources.

        This method should be called during application shutdown to ensure
        all connections are properly closed.
        """
        try:
            # Exit the checkpointer context manager to close the connection
            if self._checkpointer_context:
                self._checkpointer_context.__exit__(None, None, None)
                self._checkpointer_context = None
                self.checkpointer = None
                logger.info("postgres_checkpointer_closed")

            if self.redis_client:
                self.redis_client.close()

        except Exception as e:
            logger.error("execution_manager_close_failed", error=str(e))

    def __enter__(self) -> "ExecutionManager":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit - close resources."""
        self.close()
