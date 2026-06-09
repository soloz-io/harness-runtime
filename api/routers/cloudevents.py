"""
CloudEvent processing endpoints for NATS JetStream integration.

This module handles incoming CloudEvents from NATS JetStream,
orchestrates agent execution, and emits result CloudEvents.
"""

import time
import traceback
from typing import Any, Dict

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import ValidationError

from core.builder import GraphBuilder
from core.executor import ExecutionManager
from models.events import JobExecutionEvent
from services.cloudevents import CloudEventEmitter
from api.dependencies import get_graph_builder, get_execution_manager, get_cloudevent_emitter
from observability.metrics import (
    deepagents_runtime_jobs_total,
    deepagents_runtime_job_duration_seconds,
)

# Import OpenTelemetry if available
try:
    from opentelemetry import trace
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    OTEL_AVAILABLE = True
    tracer = trace.get_tracer(__name__)
except ImportError:
    OTEL_AVAILABLE = False
    tracer = None

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="", tags=["cloudevents"])


@router.post("/", status_code=status.HTTP_200_OK)
async def process_cloudevent(
    request: Request,
    graph_builder: GraphBuilder = Depends(get_graph_builder),
    execution_manager: ExecutionManager = Depends(get_execution_manager),
    cloudevent_emitter: CloudEventEmitter = Depends(get_cloudevent_emitter)
) -> Response:
    """
    Main endpoint for processing CloudEvents from NATS JetStream.

    This endpoint:
    1. Receives CloudEvent from NATS consumer (HTTP POST with CloudEvent headers)
    2. Parses JobExecutionEvent from CloudEvent data field
    3. Builds LangGraph agent from agent_definition
    4. Executes agent with streaming to Dragonfly/Redis
    5. Emits result CloudEvent (completed or failed) to NATS
    6. Returns HTTP 200 OK to acknowledge processing

    Request:
        CloudEvent with type: dev.my-platform.agent.execute
        Data payload: JobExecutionEvent (trace_id, job_id, agent_definition, input_payload)

    Response:
        200 OK: Job processed and result CloudEvent emitted
        400 Bad Request: Malformed CloudEvent or JobExecutionEvent
        503 Service Unavailable: Service dependencies not available

    Error Handling:
        - Malformed events: Return 400 (no retry)
        - Execution failures: Emit job.failed CloudEvent, return 200
        - Infrastructure failures: Return 503 (NATS will retry)

    References:
        - Requirements: Req. 1.1, 1.2, 1.3, 1.4, 3.1, 5.1, 5.3, 5.5
        - Design: Section 3.1 (API Layer), Section 5 (Error Handling)
        - Tasks: Task 8.4, 8.5, 8.6
    """
    try:
        # Extract trace context from CloudEvent headers for distributed tracing
        # W3C Trace Context propagation via traceparent/tracestate headers
        if tracer and OTEL_AVAILABLE:
            carrier = dict(request.headers)
            ctx = TraceContextTextMapPropagator().extract(carrier=carrier)
        else:
            ctx = None

        # Parse CloudEvent from request
        # CloudEvent headers: ce-type, ce-source, ce-id, ce-specversion
        # CloudEvent data: JSON body
        request_body = await request.json()

        logger.info(
            "cloudevent_received",
            ce_type=request.headers.get("ce-type"),
            ce_source=request.headers.get("ce-source"),
            ce_id=request.headers.get("ce-id")
        )

        # Extract JobExecutionEvent from CloudEvent data field
        # CloudEvent structure: {"data": {...}, "specversion": "1.0", ...}
        if "data" in request_body:
            event_data = request_body["data"]
        else:
            # If no 'data' field, assume the body itself is the event data
            event_data = request_body

        # Validate and parse JobExecutionEvent using Pydantic
        try:
            job_event = JobExecutionEvent(**event_data)
        except ValidationError as e:
            logger.error(
                "malformed_job_execution_event",
                validation_errors=e.errors(),
                event_data=event_data
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Malformed JobExecutionEvent: {e.errors()}"
            )

        # Extract fields from JobExecutionEvent
        trace_id = job_event.trace_id
        job_id = job_event.job_id
        agent_definition = job_event.agent_definition
        input_payload = job_event.input_payload

        # Add trace_id and job_id to logging context
        structlog.contextvars.bind_contextvars(
            trace_id=trace_id,
            job_id=job_id
        )

        logger.info(
            "processing_job_execution_event",
            trace_id=trace_id,
            job_id=job_id,
            has_agent_definition=bool(agent_definition),
            has_input_payload=bool(input_payload)
        )

        # Track job execution start time for metrics
        job_start_time = time.time()

        # Orchestration logic: Build → Execute → Emit Result
        try:
            # Step 1: Build LangGraph agent from definition
            if tracer:
                with tracer.start_as_current_span("build_agent_graph", context=ctx) as span:
                    span.set_attribute("job_id", job_id)
                    span.set_attribute("trace_id", trace_id)
                    span.set_attribute("agent.definition.id", agent_definition.get("id", "unknown"))
                    logger.info("building_agent_from_definition", job_id=job_id, trace_id=trace_id)
                    compiled_graph = graph_builder.build_from_definition(agent_definition)
                    logger.info("agent_built_successfully", job_id=job_id, trace_id=trace_id)
            else:
                logger.info("building_agent_from_definition", job_id=job_id, trace_id=trace_id)
                compiled_graph = graph_builder.build_from_definition(agent_definition)
                logger.info("agent_built_successfully", job_id=job_id, trace_id=trace_id)

            # Step 2: Execute agent with streaming
            # Use execution strategy pattern for clean separation of concerns
            from core.model_factory import ExecutionFactory
            
            execution_strategy = ExecutionFactory.create_strategy(execution_manager=execution_manager)
            
            def execute_with_strategy():
                return execution_strategy.execute_workflow(
                    graph_builder, agent_definition, job_id, trace_id
                )
            
            if tracer:
                with tracer.start_as_current_span("execute_agent", context=ctx) as span:
                    span.set_attribute("job_id", job_id)
                    span.set_attribute("trace_id", trace_id)
                    span.set_attribute("thread_id", job_id)
                    logger.info("executing_agent", job_id=job_id, trace_id=trace_id)
                    result = execute_with_strategy()
                    logger.info("agent_execution_completed", job_id=job_id, trace_id=trace_id, has_result=bool(result))
            else:
                logger.info("executing_agent", job_id=job_id, trace_id=trace_id)
                result = execute_with_strategy()
                logger.info("agent_execution_completed", job_id=job_id, trace_id=trace_id, has_result=bool(result))

            # Step 3: Emit job.completed CloudEvent
            logger.info("emitting_completed_event", job_id=job_id, trace_id=trace_id)
            await cloudevent_emitter.emit_completed(
                job_id=job_id,
                result=result,
                trace_id=trace_id
            )
            logger.info("completed_event_emitted", job_id=job_id, trace_id=trace_id)

            # Record metrics for successful job completion
            job_duration = time.time() - job_start_time
            deepagents_runtime_jobs_total.labels(status='completed').inc()
            deepagents_runtime_job_duration_seconds.observe(job_duration)

            logger.info(
                "job_metrics_recorded",
                job_id=job_id,
                trace_id=trace_id,
                status="completed",
                duration_seconds=job_duration
            )

            # Return HTTP 200 OK to acknowledge successful processing
            return Response(status_code=status.HTTP_200_OK)

        except Exception as e:
            # Execution failure: Emit job.failed CloudEvent
            logger.error(
                "agent_execution_failed",
                job_id=job_id,
                trace_id=trace_id,
                error=str(e),
                error_type=type(e).__name__,
                stack_trace=traceback.format_exc()
            )

            # Construct structured error payload
            error_payload = {
                "message": str(e),
                "type": type(e).__name__,
                "stack_trace": traceback.format_exc()
            }

            # Emit job.failed CloudEvent
            logger.info("emitting_failed_event", job_id=job_id, trace_id=trace_id)
            await cloudevent_emitter.emit_failed(
                job_id=job_id,
                error=error_payload,
                trace_id=trace_id
            )
            logger.info("failed_event_emitted", job_id=job_id, trace_id=trace_id)

            # Record metrics for failed job
            job_duration = time.time() - job_start_time
            deepagents_runtime_jobs_total.labels(status='failed').inc()
            deepagents_runtime_job_duration_seconds.observe(job_duration)

            logger.info(
                "job_metrics_recorded",
                job_id=job_id,
                trace_id=trace_id,
                status="failed",
                duration_seconds=job_duration
            )

            # Return HTTP 200 OK (failure was handled by emitting failed event)
            # This prevents NATS from retrying the job
            return Response(status_code=status.HTTP_200_OK)

    except HTTPException:
        # Re-raise HTTPException (400 Bad Request for malformed events)
        raise

    except Exception as e:
        # Unexpected error: Log and return 503 for NATS retry
        logger.error(
            "unexpected_error_processing_cloudevent",
            error=str(e),
            error_type=type(e).__name__,
            stack_trace=traceback.format_exc()
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Unexpected error: {str(e)}"
        )

    finally:
        # Clear logging context
        structlog.contextvars.clear_contextvars()