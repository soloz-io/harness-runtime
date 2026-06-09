"""
Health check endpoints for Kubernetes probes.

This module provides liveness and readiness check endpoints with
OpenTelemetry tracing and Prometheus metrics.
"""

import time
from typing import Any, Dict

import structlog
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from core.executor import ExecutionManager
from services.redis import RedisClient
from observability.metrics import deepagents_runtime_health_checks_total
from api.dependencies import get_redis_client, get_execution_manager, get_nats_consumer

# Import OpenTelemetry if available
try:
    from opentelemetry import trace
    OTEL_AVAILABLE = True
    tracer = trace.get_tracer(__name__)
except ImportError:
    OTEL_AVAILABLE = False
    tracer = None

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="", tags=["health"])


async def _check_service_dependencies(
    redis_client: RedisClient,
    execution_manager: ExecutionManager,
    nats_consumer,
    span=None
) -> Dict[str, bool]:
    """
    Check health of all service dependencies.
    
    Args:
        redis_client: RedisClient instance
        execution_manager: ExecutionManager instance
        nats_consumer: NATSConsumer instance
        span: Optional OpenTelemetry span for tracing
        
    Returns:
        Dictionary with service health status
    """
    services_health = {
        "dragonfly": False,
        "postgres": False,
        "nats": False
    }

    # Check Dragonfly
    try:
        if span and tracer:
            with tracer.start_as_current_span("health_check_dragonfly") as dragonfly_span:
                services_health["dragonfly"] = redis_client.health_check()
                dragonfly_span.set_attribute("health.dragonfly.status", services_health["dragonfly"])
        else:
            services_health["dragonfly"] = redis_client.health_check()
    except Exception as e:
        logger.error("dragonfly_health_check_failed", error=str(e))
        services_health["dragonfly"] = False
        if span:
            span.record_exception(e)

    # Check PostgreSQL (via ExecutionManager)
    try:
        if span and tracer:
            with tracer.start_as_current_span("health_check_postgres") as postgres_span:
                services_health["postgres"] = execution_manager.health_check()
                postgres_span.set_attribute("health.postgres.status", services_health["postgres"])
        else:
            services_health["postgres"] = execution_manager.health_check()
    except Exception as e:
        logger.error("postgres_health_check_failed", error=str(e))
        services_health["postgres"] = False
        if span:
            span.record_exception(e)

    # Check NATS
    try:
        if span and tracer:
            with tracer.start_as_current_span("health_check_nats") as nats_span:
                services_health["nats"] = nats_consumer.health_check()
                nats_span.set_attribute("health.nats.status", services_health["nats"])
        else:
            services_health["nats"] = nats_consumer.health_check()
    except Exception as e:
        logger.error("nats_health_check_failed", error=str(e))
        services_health["nats"] = False
        if span:
            span.record_exception(e)

    return services_health


@router.get("/health", status_code=status.HTTP_200_OK)
async def health_check() -> Dict[str, Any]:
    """
    Health check endpoint for Kubernetes liveness probes.

    Simple liveness check that returns 200 OK if the service is running.
    Does not check external dependencies. Enhanced with OpenTelemetry tracing.

    Returns:
        200 OK: Service is alive

    Response format:
        {
            "status": "healthy",
            "timestamp": "2025-01-01T00:00:00Z"
        }

    References:
        - Requirements: 8.4
        - Design: Section 2.8 (Observability Design)
        - Tasks: Task 1.3
    """
    if tracer and OTEL_AVAILABLE:
        with tracer.start_as_current_span("health_check") as span:
            span.set_attribute("health.check_type", "liveness")
            span.set_attribute("health.status", "healthy")
            
            response = {
                "status": "healthy",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            }
            
            # Record health check metrics
            deepagents_runtime_health_checks_total.labels(type="liveness", status="healthy").inc()
            
            logger.info("health_check_completed", status="healthy")
            return response
    else:
        response = {
            "status": "healthy",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        
        # Record health check metrics
        deepagents_runtime_health_checks_total.labels(type="liveness", status="healthy").inc()
        
        logger.info("health_check_completed", status="healthy")
        return response


@router.get("/ready", status_code=status.HTTP_200_OK)
async def readiness_check(
    redis_client: RedisClient = Depends(get_redis_client),
    execution_manager: ExecutionManager = Depends(get_execution_manager),
    nats_consumer = Depends(get_nats_consumer)
) -> Dict[str, Any]:
    """
    Readiness check endpoint for Kubernetes readiness probes.

    Checks connectivity to all external dependencies with OpenTelemetry tracing:
    - Dragonfly (Redis-compatible cache)
    - PostgreSQL (via ExecutionManager)
    - NATS (via NATSConsumer)

    Returns:
        200 OK: All services are ready
        503 Service Unavailable: One or more services are unreachable

    Response format:
        {
            "status": "ready" | "not_ready",
            "services": {
                "dragonfly": true | false,
                "postgres": true | false,
                "nats": true | false
            },
            "timestamp": "2025-01-01T00:00:00Z"
        }

    References:
        - Requirements: 8.4
        - Design: Section 2.8 (Observability Design)
        - Tasks: Task 1.3
    """
    if tracer and OTEL_AVAILABLE:
        with tracer.start_as_current_span("readiness_check") as span:
            span.set_attribute("health.check_type", "readiness")
            
            services_health = await _check_service_dependencies(
                redis_client, execution_manager, nats_consumer, span
            )
            
            # Determine overall readiness status
            all_ready = all(services_health.values())
            span.set_attribute("health.all_ready", all_ready)
            
            response_data = {
                "status": "ready" if all_ready else "not_ready",
                "services": services_health,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            }
            
            if all_ready:
                # Record successful readiness check
                deepagents_runtime_health_checks_total.labels(type="readiness", status="healthy").inc()
                logger.info("readiness_check_passed", services=services_health)
                return response_data
            else:
                # Record failed readiness check
                deepagents_runtime_health_checks_total.labels(type="readiness", status="unhealthy").inc()
                logger.warning("readiness_check_failed", services=services_health)
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content=response_data
                )
    else:
        services_health = await _check_service_dependencies(
            redis_client, execution_manager, nats_consumer
        )
        
        # Determine overall readiness status
        all_ready = all(services_health.values())
        
        response_data = {
            "status": "ready" if all_ready else "not_ready",
            "services": services_health,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        
        if all_ready:
            # Record successful readiness check
            deepagents_runtime_health_checks_total.labels(type="readiness", status="healthy").inc()
            logger.info("readiness_check_passed", services=services_health)
            return response_data
        else:
            # Record failed readiness check
            deepagents_runtime_health_checks_total.labels(type="readiness", status="unhealthy").inc()
            logger.warning("readiness_check_failed", services=services_health)
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=response_data
            )