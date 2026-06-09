"""
Dependency injection for FastAPI endpoints.

This module provides dependency injection functions for all service components
used across different routers.
"""

from fastapi import Depends, HTTPException, status

from core.builder import GraphBuilder
from core.executor import ExecutionManager
from services.cloudevents import CloudEventEmitter
from services.redis import RedisClient

# Global service instances (initialized in lifespan)
_redis_client: RedisClient | None = None
_execution_manager: ExecutionManager | None = None
_cloudevent_emitter: CloudEventEmitter | None = None
_nats_consumer = None


def get_service_instances():
    """Get all service instances for debugging."""
    return {
        'redis': _redis_client,
        'execution': _execution_manager,
        'emitter': _cloudevent_emitter,
        'nats': _nats_consumer
    }


def set_redis_client(client: RedisClient) -> None:
    """Set the global RedisClient instance."""
    global _redis_client
    _redis_client = client


def set_execution_manager(manager: ExecutionManager) -> None:
    """Set the global ExecutionManager instance."""
    global _execution_manager
    _execution_manager = manager


def set_cloudevent_emitter(emitter: CloudEventEmitter) -> None:
    """Set the global CloudEventEmitter instance."""
    global _cloudevent_emitter
    _cloudevent_emitter = emitter


def set_nats_consumer(consumer) -> None:
    """Set the global NATSConsumer instance."""
    global _nats_consumer
    _nats_consumer = consumer


def get_redis_client() -> RedisClient:
    """
    Dependency injection for RedisClient.

    Returns:
        Initialized RedisClient instance

    Raises:
        HTTPException: If RedisClient is not initialized

    References:
        - Requirements: Req. 2.3
        - Tasks: Task 8.2
    """
    if _redis_client is None:
        import structlog
        logger = structlog.get_logger(__name__)
        logger.error("redis_client_not_initialized", available_services=get_service_instances())
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RedisClient not initialized"
        )
    return _redis_client


def get_execution_manager() -> ExecutionManager:
    """
    Dependency injection for ExecutionManager.

    Returns:
        Initialized ExecutionManager instance

    Raises:
        HTTPException: If ExecutionManager is not initialized

    References:
        - Requirements: Req. 3.2, 4.1
        - Tasks: Task 8.2
    """
    if _execution_manager is None:
        import structlog
        logger = structlog.get_logger(__name__)
        logger.error("execution_manager_not_initialized", available_services=get_service_instances())
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ExecutionManager not initialized"
        )
    return _execution_manager


def get_graph_builder(
    execution_manager: ExecutionManager = Depends(get_execution_manager)
) -> GraphBuilder:
    """
    Dependency injection for GraphBuilder.

    Args:
        execution_manager: ExecutionManager dependency (for accessing checkpointer)

    Returns:
        New GraphBuilder instance with checkpointer dependency

    References:
        - Requirements: Req. 3.1, 14.2
        - Tasks: Task 1.1
    """
    # Pass the checkpointer from ExecutionManager to GraphBuilder
    # This allows the graph to be compiled with checkpoint persistence
    checkpointer = execution_manager.checkpointer if execution_manager else None
    return GraphBuilder(checkpointer=checkpointer)


def get_cloudevent_emitter() -> CloudEventEmitter:
    """
    Dependency injection for CloudEventEmitter.

    Returns:
        Initialized CloudEventEmitter instance

    Raises:
        HTTPException: If CloudEventEmitter is not initialized

    References:
        - Requirements: Req. 5.1, 5.3
        - Tasks: Task 8.2
    """
    if _cloudevent_emitter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CloudEventEmitter not initialized"
        )
    return _cloudevent_emitter


def get_nats_consumer():
    """
    Dependency injection for NATSConsumer.

    Returns:
        Initialized NATSConsumer instance

    Raises:
        HTTPException: If NATSConsumer is not initialized

    References:
        - Requirements: Req. 8.1
        - Tasks: Task 1.3
    """
    if _nats_consumer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NATSConsumer not initialized"
        )
    return _nats_consumer