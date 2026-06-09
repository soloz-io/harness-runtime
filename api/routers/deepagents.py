"""
DeepAgents Runtime HTTP and WebSocket endpoints for IDE Orchestrator integration.

This module provides:
- POST /deepagents-runtime/invoke: Initiate agent execution
- GET /deepagents-runtime/state/{thread_id}: Get execution state
- WebSocket /deepagents-runtime/stream/{thread_id}: Stream execution events
"""

import asyncio
import time
import traceback
from typing import Any, Dict, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status

from core.builder import GraphBuilder
from core.executor import ExecutionManager
from models.events import JobRequest, JobResponse, ExecutionState
from api.dependencies import get_execution_manager, get_graph_builder
from observability.metrics import (
    deepagents_runtime_http_requests_total,
    deepagents_runtime_http_request_duration_seconds,
    deepagents_runtime_websocket_connections_total,
    deepagents_runtime_websocket_connections_active,
    deepagents_runtime_websocket_messages_sent_total,
    deepagents_runtime_websocket_duration_seconds,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/deepagents-runtime", tags=["deepagents"])


async def _execute_agent_async(
    trace_id: str,
    job_id: str,
    agent_definition: Dict[str, Any],
    input_payload: Dict[str, Any],
    graph_builder: GraphBuilder,
    execution_manager: ExecutionManager
) -> None:
    """
    Execute agent asynchronously in the background.
    
    This function runs the agent execution and handles streaming
    events to Redis for WebSocket consumption.
    
    Args:
        trace_id: Trace ID for distributed tracing
        job_id: Job ID (used as thread_id)
        agent_definition: Agent definition
        input_payload: Input payload for execution
        graph_builder: GraphBuilder instance
        execution_manager: ExecutionManager instance
    """
    try:
        # Add trace_id and job_id to logging context
        structlog.contextvars.bind_contextvars(
            trace_id=trace_id,
            job_id=job_id
        )
        
        logger.info("async_execution_started", job_id=job_id, trace_id=trace_id)
        
        # Build agent graph
        compiled_graph = graph_builder.build_from_definition(agent_definition)
        logger.info("agent_built_for_async_execution", job_id=job_id, trace_id=trace_id)
        
        # Execute agent with streaming
        from core.model_factory import ExecutionFactory
        execution_strategy = ExecutionFactory.create_strategy(execution_manager=execution_manager)
        
        result = execution_strategy.execute_workflow(
            graph_builder, agent_definition, job_id, trace_id
        )
        
        logger.info("async_execution_completed", job_id=job_id, trace_id=trace_id, has_result=bool(result))
        
    except Exception as e:
        logger.error(
            "async_execution_failed",
            job_id=job_id,
            trace_id=trace_id,
            error=str(e),
            error_type=type(e).__name__,
            stack_trace=traceback.format_exc()
        )
    finally:
        # Clear logging context
        structlog.contextvars.clear_contextvars()


async def _get_thread_state(execution_manager: ExecutionManager, thread_id: str) -> Optional[ExecutionState]:
    """
    Get the current state of an execution thread.
    
    Args:
        execution_manager: ExecutionManager instance
        thread_id: Thread ID to check
        
    Returns:
        ExecutionState if found, None otherwise
    """
    logger.info("get_thread_state_start", thread_id=thread_id)
    
    try:
        # Check if thread exists in checkpointer
        checkpointer = execution_manager.checkpointer if hasattr(execution_manager, 'checkpointer') else None
        logger.info("checkpointer_available", thread_id=thread_id, has_checkpointer=bool(checkpointer))
        
        if checkpointer:
            # Try to get the latest checkpoint for this thread
            try:
                logger.info("attempting_checkpointer_get", thread_id=thread_id)
                # Get the latest checkpoint
                config = {"configurable": {"thread_id": thread_id}}
                checkpoint = checkpointer.get(config)
                logger.info("checkpointer_get_result", thread_id=thread_id, has_checkpoint=bool(checkpoint))
                
                if checkpoint:
                    # Thread exists, determine status based on checkpoint
                    # If we have a checkpoint, the execution has started
                    # For now, return a simple "completed" status
                    # TODO: Implement proper status determination from checkpoint data
                    logger.info("returning_completed_state", thread_id=thread_id)
                    return ExecutionState(
                        thread_id=thread_id,
                        status="completed",
                        result={"message": "Execution completed"},
                        generated_files={}
                    )
                else:
                    logger.info("no_checkpoint_found", thread_id=thread_id)
            except Exception as e:
                logger.warning("checkpointer_get_failed", thread_id=thread_id, error=str(e), error_type=type(e).__name__)
        else:
            logger.warning("no_checkpointer_available", thread_id=thread_id)
        
        # If no checkpoint found, thread doesn't exist
        logger.info("thread_not_found", thread_id=thread_id)
        return None
        
    except Exception as e:
        logger.error(
            "get_thread_state_failed",
            thread_id=thread_id,
            error=str(e),
            error_type=type(e).__name__
        )
        return None


async def _stream_events_for_thread(
    websocket: WebSocket,
    thread_id: str,
    execution_manager: ExecutionManager
) -> None:
    """
    Stream execution events for a specific thread.
    
    This function monitors Redis for events related to the thread_id
    and streams them to the WebSocket client.
    
    Args:
        websocket: WebSocket connection
        thread_id: Thread ID to monitor
        execution_manager: ExecutionManager instance
    """
    logger.info("websocket_streaming_start", thread_id=thread_id)
    
    try:
        # Get Redis client for event streaming
        redis_client = execution_manager.redis_client if hasattr(execution_manager, 'redis_client') else None
        logger.info("redis_client_check", thread_id=thread_id, has_redis_client=bool(redis_client))
        
        if not redis_client:
            logger.warning("redis_client_unavailable", thread_id=thread_id)
            await websocket.send_json({
                "event_type": "error",
                "data": {
                    "error": "Redis client not available for streaming",
                    "context": "event_streaming"
                }
            })
            return
        
        # Subscribe to Redis events for this thread
        # This is a simplified implementation - in practice, we'd use Redis Streams
        # or pub/sub to get real-time events from the execution
        
        # For now, simulate streaming by checking for completion
        execution_completed = False
        check_interval = 1.0  # Check every second
        max_wait_time = 300  # 5 minutes timeout
        elapsed_time = 0
        
        logger.info("websocket_streaming_loop_start", thread_id=thread_id, max_wait_time=max_wait_time)
        
        while not execution_completed and elapsed_time < max_wait_time:
            try:
                logger.debug("checking_thread_state", thread_id=thread_id, elapsed_time=elapsed_time)
                # Check if execution is complete
                state = await _get_thread_state(execution_manager, thread_id)
                logger.debug("thread_state_result", thread_id=thread_id, has_state=bool(state), status=state.status if state else None)
                
                if state and state.status == "completed":
                    # Send final state update with files
                    if state.result or state.generated_files:
                        state_update_event = {
                            "event_type": "on_state_update",
                            "data": {
                                "messages": "Execution completed",
                                "files": state.generated_files or {},
                                "result": state.result
                            }
                        }
                        await websocket.send_json(state_update_event)
                        deepagents_runtime_websocket_messages_sent_total.labels(event_type="on_state_update").inc()
                    
                    # Send end event
                    end_event = {
                        "event_type": "end",
                        "data": {}
                    }
                    await websocket.send_json(end_event)
                    deepagents_runtime_websocket_messages_sent_total.labels(event_type="end").inc()
                    
                    execution_completed = True
                    logger.info("websocket_streaming_completed", thread_id=thread_id)
                    
                elif state and state.status == "failed":
                    # Send error event
                    error_event = {
                        "event_type": "error",
                        "data": {
                            "error": state.error or {"message": "Execution failed"},
                            "context": "execution_failed"
                        }
                    }
                    await websocket.send_json(error_event)
                    deepagents_runtime_websocket_messages_sent_total.labels(event_type="error").inc()
                    
                    # Send end event
                    end_event = {
                        "event_type": "end",
                        "data": {}
                    }
                    await websocket.send_json(end_event)
                    deepagents_runtime_websocket_messages_sent_total.labels(event_type="end").inc()
                    
                    execution_completed = True
                    logger.info("websocket_streaming_failed", thread_id=thread_id)
                    
                else:
                    # Send periodic update
                    progress_event = {
                        "event_type": "on_state_update",
                        "data": {
                            "messages": f"Execution in progress... ({elapsed_time}s)",
                            "status": state.status if state else "running"
                        }
                    }
                    await websocket.send_json(progress_event)
                    deepagents_runtime_websocket_messages_sent_total.labels(event_type="on_state_update").inc()
                
                # Wait before next check
                await asyncio.sleep(check_interval)
                elapsed_time += check_interval
                
            except WebSocketDisconnect:
                logger.info("websocket_client_disconnected", thread_id=thread_id)
                break
            except Exception as e:
                logger.error(
                    "websocket_streaming_error",
                    thread_id=thread_id,
                    error=str(e),
                    error_type=type(e).__name__
                )
                await websocket.send_json({
                    "event_type": "error",
                    "data": {
                        "error": str(e),
                        "context": "streaming_loop"
                    }
                })
                break
        
        if elapsed_time >= max_wait_time:
            logger.warning("websocket_streaming_timeout", thread_id=thread_id)
            await websocket.send_json({
                "event_type": "error",
                "data": {
                    "error": "Execution timeout",
                    "context": "streaming_timeout"
                }
            })
            await websocket.send_json({
                "event_type": "end",
                "data": {}
            })
            
    except Exception as e:
        logger.error(
            "stream_events_failed",
            thread_id=thread_id,
            error=str(e),
            error_type=type(e).__name__,
            stack_trace=traceback.format_exc()
        )


@router.post("/invoke", response_model=JobResponse, status_code=status.HTTP_200_OK)
async def invoke_agent(
    job_request: JobRequest,
    graph_builder: GraphBuilder = Depends(get_graph_builder),
    execution_manager: ExecutionManager = Depends(get_execution_manager)
) -> JobResponse:
    """
    HTTP endpoint to initiate agent execution.
    
    This endpoint accepts a JobRequest and initiates agent execution,
    returning a thread_id immediately for WebSocket streaming.
    
    Args:
        job_request: JobRequest containing trace_id, job_id, agent_definition, input_payload
        graph_builder: GraphBuilder dependency for building agent graphs
        execution_manager: ExecutionManager dependency for execution
        
    Returns:
        JobResponse with thread_id and status
        
    Raises:
        HTTPException: 400 for invalid request, 503 for service unavailable
        
    References:
        - Requirements: 1.1, 1.2, 1.4
    """
    # Track request start time for metrics
    request_start_time = time.time()
    
    try:
        # Extract fields from request
        trace_id = job_request.trace_id
        job_id = job_request.job_id
        agent_definition = job_request.agent_definition
        input_payload = job_request.input_payload
        
        # Add trace_id and job_id to logging context
        structlog.contextvars.bind_contextvars(
            trace_id=trace_id,
            job_id=job_id
        )
        
        logger.info(
            "http_invoke_request_received",
            trace_id=trace_id,
            job_id=job_id,
            has_agent_definition=bool(agent_definition),
            has_input_payload=bool(input_payload)
        )
        
        # Start execution asynchronously (non-blocking)
        # The execution will run in the background and stream events
        asyncio.create_task(
            _execute_agent_async(
                trace_id=trace_id,
                job_id=job_id,
                agent_definition=agent_definition,
                input_payload=input_payload,
                graph_builder=graph_builder,
                execution_manager=execution_manager
            )
        )
        
        logger.info(
            "http_invoke_started",
            trace_id=trace_id,
            job_id=job_id,
            thread_id=job_id
        )
        
        # Record successful request metrics
        request_duration = time.time() - request_start_time
        deepagents_runtime_http_requests_total.labels(
            method="POST", endpoint="invoke", status="200"
        ).inc()
        deepagents_runtime_http_request_duration_seconds.labels(
            method="POST", endpoint="invoke"
        ).observe(request_duration)
        
        # Return thread_id immediately
        return JobResponse(
            thread_id=job_id,
            status="started"
        )
        
    except Exception as e:
        # Record failed request metrics
        request_duration = time.time() - request_start_time
        deepagents_runtime_http_requests_total.labels(
            method="POST", endpoint="invoke", status="500"
        ).inc()
        deepagents_runtime_http_request_duration_seconds.labels(
            method="POST", endpoint="invoke"
        ).observe(request_duration)
        
        logger.error(
            "http_invoke_failed",
            error=str(e),
            error_type=type(e).__name__,
            stack_trace=traceback.format_exc()
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to initiate execution: {str(e)}"
        )
    finally:
        # Clear logging context
        structlog.contextvars.clear_contextvars()


@router.get("/state/{thread_id}", response_model=ExecutionState, status_code=status.HTTP_200_OK)
async def get_execution_state(
    thread_id: str,
    execution_manager: ExecutionManager = Depends(get_execution_manager)
) -> ExecutionState:
    """
    HTTP endpoint to retrieve final execution state.
    
    This endpoint returns the current state of an execution thread,
    including final results if completed.
    
    Args:
        thread_id: Unique identifier for the execution thread
        execution_manager: ExecutionManager dependency
        
    Returns:
        ExecutionState with current status and results
        
    Raises:
        HTTPException: 404 for thread not found, 503 for service unavailable
        
    References:
        - Requirements: 1.2, 1.4
    """
    # Track request start time for metrics
    request_start_time = time.time()
    
    try:
        logger.info("http_state_request_received", thread_id=thread_id)
        
        # Get execution state from ExecutionManager
        # This will check the checkpointer for the thread state
        state = await _get_thread_state(execution_manager, thread_id)
        
        if state is None:
            # Record 404 metrics
            request_duration = time.time() - request_start_time
            deepagents_runtime_http_requests_total.labels(
                method="GET", endpoint="state", status="404"
            ).inc()
            deepagents_runtime_http_request_duration_seconds.labels(
                method="GET", endpoint="state"
            ).observe(request_duration)
            
            logger.warning("thread_not_found", thread_id=thread_id)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Thread {thread_id} not found"
            )
        
        # Record successful request metrics
        request_duration = time.time() - request_start_time
        deepagents_runtime_http_requests_total.labels(
            method="GET", endpoint="state", status="200"
        ).inc()
        deepagents_runtime_http_request_duration_seconds.labels(
            method="GET", endpoint="state"
        ).observe(request_duration)
        
        logger.info("http_state_retrieved", thread_id=thread_id, status=state.status)
        return state
        
    except HTTPException:
        raise
    except Exception as e:
        # Record 500 error metrics
        request_duration = time.time() - request_start_time
        deepagents_runtime_http_requests_total.labels(
            method="GET", endpoint="state", status="500"
        ).inc()
        deepagents_runtime_http_request_duration_seconds.labels(
            method="GET", endpoint="state"
        ).observe(request_duration)
        
        logger.error(
            "http_state_failed",
            thread_id=thread_id,
            error=str(e),
            error_type=type(e).__name__,
            stack_trace=traceback.format_exc()
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve state: {str(e)}"
        )


@router.websocket("/stream/{thread_id}")
async def stream_execution(
    websocket: WebSocket,
    thread_id: str,
    execution_manager: ExecutionManager = Depends(get_execution_manager)
):
    """
    WebSocket endpoint for streaming execution events.
    
    This endpoint provides real-time streaming of LangGraph events
    for a specific execution thread.
    
    Args:
        websocket: WebSocket connection
        thread_id: Unique identifier for the execution thread
        execution_manager: ExecutionManager dependency
        
    Event Format:
        {
            "event_type": "on_state_update|on_llm_stream|end",
            "data": {
                "messages": "...",
                "files": {
                    "/THE_SPEC/constitution.md": {
                        "content": ["line1", "line2"],
                        "created_at": "2025-01-01T00:00:00Z",
                        "modified_at": "2025-01-01T00:00:00Z"
                    }
                }
            }
        }
        
    References:
        - Requirements: 1.3, 1.5
    """
    await websocket.accept()
    
    # Track WebSocket connection metrics
    connection_start_time = time.time()
    deepagents_runtime_websocket_connections_total.inc()
    deepagents_runtime_websocket_connections_active.inc()
    
    try:
        logger.info("websocket_connection_established", thread_id=thread_id)
        
        # Start streaming events for this thread_id
        await _stream_events_for_thread(websocket, thread_id, execution_manager)
        
    except WebSocketDisconnect:
        logger.info("websocket_disconnected", thread_id=thread_id)
    except Exception as e:
        logger.error(
            "websocket_error",
            thread_id=thread_id,
            error=str(e),
            error_type=type(e).__name__,
            stack_trace=traceback.format_exc()
        )
        try:
            error_event = {
                "event_type": "error",
                "data": {
                    "error": str(e),
                    "context": "websocket_streaming"
                }
            }
            await websocket.send_json(error_event)
            deepagents_runtime_websocket_messages_sent_total.labels(event_type="error").inc()
        except:
            pass  # Connection might be closed
    finally:
        # Record WebSocket connection duration and decrement active connections
        connection_duration = time.time() - connection_start_time
        deepagents_runtime_websocket_duration_seconds.observe(connection_duration)
        deepagents_runtime_websocket_connections_active.dec()
        
        try:
            await websocket.close()
        except:
            pass