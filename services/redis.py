"""
Redis Streaming Service

This module provides Redis connection management and event streaming capabilities
for the Agent Executor service. It publishes real-time execution events to Redis
channels using the pub/sub pattern.

Design Reference: design.md Section 2.5 (Redis Streaming Architecture)
Requirements: Req. 4.1, 4.2, 4.3, NFR-4.2
"""

import json
from typing import Any, Dict, Optional

import redis
import structlog
from redis.connection import ConnectionPool

# Import OpenTelemetry for distributed tracing
try:
    from opentelemetry import trace
    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False

from observability.metrics import (
    deepagents_runtime_redis_publish_total,
    deepagents_runtime_redis_publish_errors_total
)

logger = structlog.get_logger(__name__)


class RedisClient:
    """
    Manages Redis connection and streaming event publishing.

    This client handles:
    - Connection pooling for efficient Redis access
    - Publishing stream events during LangGraph execution
    - Publishing final completion events
    - Structured logging for all Redis operations

    Channel Naming Convention: langgraph:stream:{thread_id}
    where thread_id = job_id from JobExecutionEvent
    """

    def __init__(
        self,
        host: str,
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None,
        max_connections: int = 10,
        socket_timeout: int = 5,
        socket_connect_timeout: int = 5,
    ) -> None:
        """
        Initialize Redis client with connection pooling.

        Args:
            host: Redis server hostname (e.g., "redis.bizmatters-dev.svc.cluster.local")
            port: Redis server port (default: 6379)
            db: Redis database number (default: 0)
            password: Optional Redis password for authentication (default: None)
            max_connections: Maximum number of connections in the pool (default: 10)
            socket_timeout: Socket timeout in seconds (default: 5)
            socket_connect_timeout: Socket connection timeout in seconds (default: 5)

        Raises:
            redis.ConnectionError: If initial connection to Redis fails
        """
        self.host = host
        self.port = port

        # Create connection pool for efficient connection management
        # As per design.md: "Redis Client: redis-py library with connection pooling"
        self.pool = ConnectionPool(
            host=host,
            port=port,
            db=db,
            password=password,
            max_connections=max_connections,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_connect_timeout,
            decode_responses=True,  # Auto-decode responses to strings
        )

        # Initialize Redis client with connection pool
        self.client = redis.Redis(connection_pool=self.pool)

        # Test connection on initialization
        try:
            self.client.ping()
            logger.info(
                "redis_connection_established",
                host=host,
                port=port,
                max_connections=max_connections,
            )
        except redis.ConnectionError as e:
            logger.error(
                "redis_connection_failed",
                host=host,
                port=port,
                error=str(e),
            )
            raise

    def publish_stream_event(
        self,
        thread_id: str,
        event_type: str,
        data: Dict[str, Any],
        trace_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> int:
        """
        Publish a streaming event to Redis channel.

        This method publishes real-time execution events (LLM tokens, tool calls, etc.)
        to a Redis pub/sub channel for consumption by client applications.

        Channel Format: langgraph:stream:{thread_id}
        Event Format: {"event_type": str, "data": dict}

        Args:
            thread_id: Thread ID (same as job_id in most cases)
            event_type: Type of event (e.g., "on_llm_stream", "on_tool_start", "on_tool_end")
            data: Event-specific payload from LangGraph stream
            trace_id: Optional distributed tracing ID for correlation
            job_id: Optional job ID for logging correlation

        Returns:
            Number of subscribers that received the message

        Raises:
            redis.RedisError: If publishing fails

        Requirements:
            - Req. 4.1: Redis connection with connection pooling
            - Req. 4.2: Stream event publishing with structured logging
            - NFR-4.2: Distributed tracing with OpenTelemetry
        """
        channel = f"langgraph:stream:{thread_id}"

        # Construct event payload as per design.md Section 4.4
        event_payload = {"event_type": event_type, "data": data}

        # Create OpenTelemetry span for Redis publish operation
        if OTEL_AVAILABLE:
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span("redis_publish_stream") as span:
                span.set_attribute("redis.channel", channel)
                span.set_attribute("event_type", event_type)
                span.set_attribute("thread_id", thread_id)
                if trace_id:
                    span.set_attribute("trace_id", trace_id)
                if job_id:
                    span.set_attribute("job_id", job_id)

                try:
                    # Serialize to JSON
                    message = json.dumps(event_payload)

                    # Publish to Redis channel
                    subscriber_count = self.client.publish(channel, message)

                    # Record metrics for successful publish
                    deepagents_runtime_redis_publish_total.labels(event_type=event_type).inc()

                    # Structured logging with correlation IDs
                    logger.info(
                        "redis_stream_event_published",
                        channel=channel,
                        event_type=event_type,
                        subscriber_count=subscriber_count,
                        trace_id=trace_id,
                        job_id=job_id,
                    )

                    span.set_attribute("subscriber_count", subscriber_count)
                    return subscriber_count

                except json.JSONDecodeError as e:
                    deepagents_runtime_redis_publish_errors_total.inc()
                    logger.error(
                        "redis_event_serialization_failed",
                        channel=channel,
                        event_type=event_type,
                        error=str(e),
                        trace_id=trace_id,
                        job_id=job_id,
                    )
                    span.record_exception(e)
                    raise

                except redis.RedisError as e:
                    deepagents_runtime_redis_publish_errors_total.inc()
                    logger.error(
                        "redis_publish_failed",
                        channel=channel,
                        event_type=event_type,
                        error=str(e),
                        trace_id=trace_id,
                        job_id=job_id,
                    )
                    span.record_exception(e)
                    raise
        else:
            # Fallback: publish without tracing
            try:
                # Serialize to JSON
                message = json.dumps(event_payload)

                # Publish to Redis channel
                subscriber_count = self.client.publish(channel, message)

                # Record metrics for successful publish
                deepagents_runtime_redis_publish_total.labels(event_type=event_type).inc()

                # Structured logging with correlation IDs
                logger.info(
                    "redis_stream_event_published",
                    channel=channel,
                    event_type=event_type,
                    subscriber_count=subscriber_count,
                    trace_id=trace_id,
                    job_id=job_id,
                )

                return subscriber_count

            except json.JSONDecodeError as e:
                deepagents_runtime_redis_publish_errors_total.inc()
                logger.error(
                    "redis_event_serialization_failed",
                    channel=channel,
                    event_type=event_type,
                    error=str(e),
                    trace_id=trace_id,
                    job_id=job_id,
                )
                raise

            except redis.RedisError as e:
                deepagents_runtime_redis_publish_errors_total.inc()
                logger.error(
                    "redis_publish_failed",
                    channel=channel,
                    event_type=event_type,
                    error=str(e),
                    trace_id=trace_id,
                    job_id=job_id,
                )
                raise

    def publish_end_event(
        self,
        thread_id: str,
        trace_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> int:
        """
        Publish final completion event to signal execution end.

        This method publishes an "end" event to signal that LangGraph execution
        has completed (either successfully or with failure). Client applications
        can use this to know when to stop listening for stream events.

        Channel Format: langgraph:stream:{thread_id}
        Event: {"event_type": "end", "data": {}}

        Args:
            thread_id: Thread ID (same as job_id in most cases)
            trace_id: Optional distributed tracing ID for correlation
            job_id: Optional job ID for logging correlation

        Returns:
            Number of subscribers that received the message

        Raises:
            redis.RedisError: If publishing fails

        Requirements:
            - Req. 4.3: End event publishing
        """
        return self.publish_stream_event(
            thread_id=thread_id,
            event_type="end",
            data={},
            trace_id=trace_id,
            job_id=job_id,
        )

    def health_check(self) -> bool:
        """
        Check Redis connection health.

        Returns:
            True if Redis is reachable, False otherwise
        """
        try:
            return self.client.ping()
        except redis.RedisError as e:
            logger.error("redis_health_check_failed", error=str(e))
            return False

    def close(self) -> None:
        """
        Close Redis connection pool.

        This method should be called during application shutdown to ensure
        all connections are properly closed.
        """
        try:
            self.pool.disconnect()
            logger.info("redis_connection_closed", host=self.host, port=self.port)
        except Exception as e:
            logger.error(
                "redis_close_failed",
                host=self.host,
                port=self.port,
                error=str(e),
            )

    def __enter__(self) -> "RedisClient":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit - close connections."""
        self.close()
