"""
Prometheus metrics endpoint.

This module provides the /metrics endpoint for Prometheus scraping.
"""

from fastapi import APIRouter, Response

from observability.metrics import get_metrics

router = APIRouter(prefix="", tags=["metrics"])


@router.get("/metrics")
async def metrics() -> Response:
    """
    Prometheus metrics endpoint.

    Exposes metrics in Prometheus text format for scraping by Prometheus server.
    Metrics include job execution counts, durations, and infrastructure health indicators.

    Returns:
        Response with metrics in Prometheus text format

    Metrics Exposed:
        - deepagents_runtime_jobs_total{status="completed|failed"}: Total job count
        - deepagents_runtime_job_duration_seconds: Histogram of job durations
        - deepagents_runtime_http_requests_total: HTTP request counts
        - deepagents_runtime_websocket_connections_total: WebSocket connection counts
        - deepagents_runtime_health_checks_total: Health check counts

    References:
        - Tasks: Task 1.6, 9.3 (Prometheus metrics endpoint)
        - Requirements: 17.4, 17.5, Observable pillar
        - Design: Section 2.8 (Observability Design)
    """
    metrics_data, content_type = get_metrics()
    return Response(content=metrics_data, media_type=content_type)