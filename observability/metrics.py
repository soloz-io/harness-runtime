"""
Prometheus metrics for DeepAgents Runtime Service.

This module defines and exports all Prometheus metrics used by the service.
Metrics are exposed via the /metrics endpoint for Prometheus scraping.

Metrics:
    - deepagents_runtime_jobs_total: Counter for total jobs processed (labels: status)
    - deepagents_runtime_job_duration_seconds: Histogram for job execution duration
    - deepagents_runtime_db_connection_errors_total: Counter for database connection errors
    - deepagents_runtime_redis_publish_total: Counter for Redis stream events published
    - deepagents_runtime_redis_publish_errors_total: Counter for Redis publish errors
    - deepagents_runtime_nats_messages_processed_total: Counter for NATS messages processed
    - deepagents_runtime_nats_messages_failed_total: Counter for NATS messages failed

References:
    - Tasks: Task 1.6, 9.3 (Add Prometheus metrics)
    - Requirements: 17.5, Observable pillar
    - Design: Section 2.8 (Observability Design)
"""

from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST, CollectorRegistry, REGISTRY

# Use a separate registry for tests to avoid conflicts
import os
if os.getenv('PYTEST_CURRENT_TEST'):
    # Create a separate registry for tests
    test_registry = CollectorRegistry()
    registry = test_registry
else:
    # Use the default registry for production
    registry = REGISTRY

# Job execution metrics
deepagents_runtime_jobs_total = Counter(
    'deepagents_runtime_jobs_total',
    'Total number of agent execution jobs processed',
    ['status'],  # status=completed|failed
    registry=registry
)

deepagents_runtime_job_duration_seconds = Histogram(
    'deepagents_runtime_job_duration_seconds',
    'Duration of agent execution jobs in seconds',
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
    registry=registry
)

# Infrastructure metrics
deepagents_runtime_db_connection_errors_total = Counter(
    'deepagents_runtime_db_connection_errors_total',
    'Total number of database connection errors',
    registry=registry
)

# Redis metrics (optional but useful for monitoring)
deepagents_runtime_redis_publish_total = Counter(
    'deepagents_runtime_redis_publish_total',
    'Total number of Redis stream events published',
    ['event_type'],  # event_type=on_llm_stream|on_tool_start|on_tool_end|end|unknown
    registry=registry
)

deepagents_runtime_redis_publish_errors_total = Counter(
    'deepagents_runtime_redis_publish_errors_total',
    'Total number of Redis publish errors',
    registry=registry
)

# NATS metrics
deepagents_runtime_nats_messages_processed_total = Counter(
    'deepagents_runtime_nats_messages_processed_total',
    'Total number of NATS messages processed successfully',
    registry=registry
)

deepagents_runtime_nats_messages_failed_total = Counter(
    'deepagents_runtime_nats_messages_failed_total',
    'Total number of NATS messages that failed processing',
    registry=registry
)

# HTTP API metrics
deepagents_runtime_http_requests_total = Counter(
    'deepagents_runtime_http_requests_total',
    'Total number of HTTP API requests',
    ['method', 'endpoint', 'status'],  # method=GET|POST, endpoint=invoke|state, status=200|400|500
    registry=registry
)

deepagents_runtime_http_request_duration_seconds = Histogram(
    'deepagents_runtime_http_request_duration_seconds',
    'Duration of HTTP API requests in seconds',
    ['method', 'endpoint'],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    registry=registry
)

# WebSocket metrics
deepagents_runtime_websocket_connections_total = Counter(
    'deepagents_runtime_websocket_connections_total',
    'Total number of WebSocket connections established',
    registry=registry
)

deepagents_runtime_websocket_connections_active = Counter(
    'deepagents_runtime_websocket_connections_active',
    'Number of currently active WebSocket connections',
    registry=registry
)

deepagents_runtime_websocket_messages_sent_total = Counter(
    'deepagents_runtime_websocket_messages_sent_total',
    'Total number of WebSocket messages sent',
    ['event_type'],  # event_type=on_state_update|on_llm_stream|end|error
    registry=registry
)

deepagents_runtime_websocket_duration_seconds = Histogram(
    'deepagents_runtime_websocket_duration_seconds',
    'Duration of WebSocket connections in seconds',
    buckets=[1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0],
    registry=registry
)

# Health check metrics
deepagents_runtime_health_checks_total = Counter(
    'deepagents_runtime_health_checks_total',
    'Total number of health check requests',
    ['type', 'status'],  # type=liveness|readiness, status=healthy|unhealthy
    registry=registry
)


def get_metrics() -> tuple[bytes, str]:
    """
    Generate Prometheus metrics in text format.

    This function collects all registered metrics and formats them according
    to the Prometheus text exposition format for scraping by Prometheus server.

    Returns:
        Tuple of (metrics_bytes, content_type) where:
        - metrics_bytes: Prometheus metrics in text format (bytes)
        - content_type: MIME type for Prometheus metrics format

    Example:
        >>> metrics_data, content_type = get_metrics()
        >>> print(content_type)
        text/plain; version=0.0.4; charset=utf-8

    References:
        - Tasks: Task 9.3
        - Requirements: Observable pillar
    """
    return generate_latest(registry), CONTENT_TYPE_LATEST
