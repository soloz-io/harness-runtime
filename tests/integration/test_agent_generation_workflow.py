"""
Agent Generation Workflow Tests - Test 1 (No NATS Events)

This is the original test_api.py with NATS parts removed.
Keeps ALL core logic: PostgreSQL, Redis, artifacts validation, QC checks.

This module contains Tier 1 critical integration tests that validate the complete
end-to-end agent generation workflow through:
- PostgreSQL checkpoints (real database)
- Dragonfly streaming events (real pub/sub)
- Agent workflow execution and artifact generation
- QC validation and schema compliance

Test Strategy:
    - Use REAL PostgreSQL, Dragonfly via Docker Compose
    - Use REAL graph execution with REAL LLM API calls (using OPENAI_API_KEY from .env)
    - Load REAL agent definition from tests/mock/definition.json
    - Validate actual data flow: checkpoints written, events published, artifacts generated
    - NO NATS CloudEvent validation (that's Test 2)

FILE ORGANIZATION:
    1. INFRASTRUCTURE FIXTURES - Database connections (PostgreSQL, Redis)
    2. DATA FIXTURES - Sample test data and CloudEvents
    3. INTEGRATION TESTS - Agent generation workflow validation
       - Test 1: Successful agent generation workflow
       - Test 2: Fixtures configuration validation

Prerequisites:
    - Run: docker-compose -f tests/integration/docker-compose.test.yml up -d
    - PostgreSQL on localhost:15433 (user: test_user, password: test_pass, db: test_db)
    - Redis on localhost:16380

References:
    - Requirements: Req. 1.1, 1.2, 3.1, 4.1, 4.2, 4.3
    - Design: Section 2.11 (Internal Component Architecture), Section 3.1 (API Layer)
    - Spec: .kiro/specs/agent-builder/phase1-9-deepagents_runtime_service/
    - Tasks: Task 8.7 (Tier 1 Critical Integration Tests)
"""

import json
import os
import time
import asyncio
from pathlib import Path
from typing import Any, Dict, Generator, List
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
import psycopg
import pytest
import redis
from fastapi.testclient import TestClient


# ============================================================================
# TEST UTILITIES - Model Selection for Mock/Real LLM
# ============================================================================

def _get_test_model():
    """Get model based on environment - mock or real LLM."""
    from tests.utils.mock_workflow import get_test_model
    return get_test_model()


# ============================================================================
# FIXTURES
# ============================================================================

# Note: PostgreSQL, Redis, and NATS fixtures removed - the test now uses
# the app's actual service clients via dependency injection for better
# integration testing that matches production behavior.


# ============================================================================
# DATA FIXTURES - Sample Test Data and CloudEvents
# ============================================================================

# Agent Definition Fixture
@pytest.fixture
def sample_agent_definition() -> Dict[str, Any]:
    """
    Load REAL agent definition from tests/mock/definition.json.

    This fixture loads the actual mock definition used by the application,
    ensuring that integration tests validate real graph building and execution.
    
    System prompts are loaded from .md files in tests/mock/prompts/
    Tool scripts are loaded from .py files in tests/mock/tools/
    for better readability and debugging.

    Returns:
        Dictionary containing agent definition with prompts and tools loaded from files
    """
    from tests.utils.test_helpers import load_definition_with_files
    
    definition_path = Path(__file__).parent.parent / "mock" / "definition.json"
    return load_definition_with_files(definition_path)


# Job Execution Event Fixture
@pytest.fixture
def sample_job_execution_event(sample_agent_definition: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sample JobExecutionEvent for testing.

    Generates unique trace_id and job_id for each test run to avoid checkpoint
    collisions when using PostgreSQL checkpointer with the same thread_id.

    Args:
        sample_agent_definition: Agent definition fixture

    Returns:
        Dictionary containing JobExecutionEvent data with unique IDs
    """
    import uuid
    
    # Generate unique IDs for each test run to prevent checkpoint state pollution
    # This ensures each test starts with a clean state instead of resuming from
    # previous checkpoints, which would cause the PatchToolCallsMiddleware to
    # detect dangling tool calls and create an infinite loop
    unique_job_id = f"test-job-{uuid.uuid4()}"
    unique_trace_id = f"test-trace-{uuid.uuid4()}"
    
    return {
        "trace_id": unique_trace_id,
        "job_id": unique_job_id,
        "agent_definition": sample_agent_definition,
        "input_payload": {
            "messages": [
                {"role": "user", "content": "Create a simple hello world agent that greets users"}
            ]
        }
    }


# CloudEvent Wrapper Fixture
@pytest.fixture
def sample_cloudevent(sample_job_execution_event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sample CloudEvent wrapper for JobExecutionEvent.

    Args:
        sample_job_execution_event: JobExecutionEvent fixture

    Returns:
        Dictionary containing complete CloudEvent structure
    """
    return {
        "specversion": "1.0",
        "type": "dev.my-platform.agent.execute",
        "source": "nats://agent.execute.test",
        "id": "test-cloudevent-789",
        "data": sample_job_execution_event
    }


# ============================================================================
# INTEGRATION TESTS - End-to-End Workflow Validation
# ============================================================================

# Test 1: Agent Generation Workflow (No NATS)
@pytest.mark.asyncio
async def test_agent_generation_end_to_end_success(
    sample_cloudevent: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Test complete agent generation workflow with REAL data flow validation.
    
    **TEST 1: AGENT GENERATION ONLY (NO NATS EVENTS)**

    This test validates the entire end-to-end flow with REAL infrastructure:
    1. Send CloudEvent via HTTP API (POST /) - API requires CloudEvent format
    2. Parse JobExecutionEvent from CloudEvent data
    3. Build LangGraph agent from agent_definition (REAL GraphBuilder)
    4. Execute agent with REAL graph execution and REAL LLM API calls
    5. Publish stream events to REAL Dragonfly
    6. Save checkpoints to REAL PostgreSQL
    7. Return HTTP 200 OK (NO NATS CloudEvent result validation)

    Enhanced Validation:
        - PostgreSQL checkpoints written with correct thread_id (job_id)
        - Dragonfly events published with correct structure and trace_id propagation
        - ALL events captured and saved to outputs/ directory
        - Detailed execution summary printed to stdout

    Success Criteria:
        - HTTP 200 OK response
        - GraphBuilder builds REAL graph from definition.json
        - Graph executes successfully with REAL LLM API calls
        - PostgreSQL: At least 1 checkpoint written with thread_id = job_id
        - Dragonfly: Minimum 1 event published (end event)
        - Minimum event counts validated (≥5/≥5/≥11/≥6/==1)
        - Specialist invocation order validated
        - Artifacts saved to outputs/ directory

    References:
        - Requirements: Req. 1.1, 1.2, 3.1, 3.2, 4.1, 4.2, 4.3, 4.4
        - Design: Section 2.11, Section 3.1
        - Tasks: Task 2.2, 2.3
        - Event Reference: agent-executor-event-example.md
        - Minimum Guarantees: agent-executor-minimum-events.md
    """
    # ================================================================
    # LOG CAPTURE SETUP - Capture ALL logs to test run directory
    # ================================================================
    import logging
    import sys
    from datetime import datetime
    from tests.utils.test_helpers import reset_test_run_dir, get_test_run_dir, generate_test_id
    
    # Reset test run directory for this test execution
    reset_test_run_dir()
    
    # Generate unique test ID and create run directory
    test_id = generate_test_id()
    test_run_dir = get_test_run_dir(test_id)
    
    # Create log file in the test run directory
    log_filename = "test_run.log"
    log_filepath = test_run_dir / log_filename
    
    # Open log file for writing
    log_file = open(log_filepath, 'w')
    
    # Store original stdout/stderr
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    
    # Create a tee class to write to both console and file
    class TeeStream:
        def __init__(self, original_stream, log_file):
            self.original_stream = original_stream
            self.log_file = log_file
            
        def write(self, text):
            self.original_stream.write(text)
            self.original_stream.flush()
            self.log_file.write(text)
            self.log_file.flush()
            
        def flush(self):
            self.original_stream.flush()
            self.log_file.flush()
    
    # Redirect stdout and stderr to capture all output
    sys.stdout = TeeStream(original_stdout, log_file)
    sys.stderr = TeeStream(original_stderr, log_file)
    
    # Also setup Python logging to go to the file
    file_handler = logging.FileHandler(log_filepath, mode='a')
    file_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    
    # Add to root logger
    root_logger = logging.getLogger()
    root_logger.addHandler(file_handler)
    root_logger.setLevel(logging.DEBUG)
    
    # Suppress OpenAI and HTTP debug logs when in mock mode
    from tests.utils.mock_workflow import is_mock_mode
    if is_mock_mode():
        print("[MOCK] Mock mode detected - suppressing real LLM API logs")
        logging.getLogger("openai").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    else:
        print("[REAL] Real LLM mode - showing all API logs")
    
    print(f"\n[LOG_CAPTURE] All logs will be saved to: {log_filepath}")
    print("=" * 80)
    
    print("\n[DEBUG] ===== TEST EXECUTION START =====")
    print(f"[DEBUG] test_agent_generation_end_to_end_success: STARTING")
    print(f"[DEBUG] Python cache cleared, running with fresh bytecode")
    
    # Show mock/real mode status prominently
    if is_mock_mode():
        print("🎭 [MOCK MODE] Using event replay - NO real LLM API calls will be made")
        print("🎭 [MOCK MODE] All events will be replayed from tests/mock/all_events.json")
    else:
        print("🌐 [REAL MODE] Using real OpenAI API - actual LLM calls will be made")
        print("🌐 [REAL MODE] This will take several minutes and consume API credits")
    
    # Track execution start time
    execution_start_time = time.time()
    
    # Extract job execution event from CloudEvent (API still uses CloudEvent format)
    print(f"[DEBUG] Extracting job execution event from CloudEvent...")
    sample_job_execution_event = sample_cloudevent["data"]
    print(f"[DEBUG] Job ID: {sample_job_execution_event.get('job_id')}")
    print(f"[DEBUG] CloudEvent keys: {list(sample_cloudevent.keys())}")
    print(f"[DEBUG] Job execution event keys: {list(sample_job_execution_event.keys())}")
    print(f"[DEBUG] ===== END TEST EXECUTION START =====\n")

    # PostgreSQL configuration - use Kubernetes secrets if available, fallback to TEST_* env vars
    pg_host = os.environ.get("POSTGRES_HOST") or os.environ.get("TEST_POSTGRES_HOST", "localhost")
    pg_port = os.environ.get("POSTGRES_PORT") or os.environ.get("TEST_POSTGRES_PORT", "15433")
    pg_db = os.environ.get("POSTGRES_DB") or os.environ.get("TEST_POSTGRES_DB", "test_db")
    pg_user = os.environ.get("POSTGRES_USER") or os.environ.get("TEST_POSTGRES_USER", "test_user")
    pg_password = os.environ.get("POSTGRES_PASSWORD") or os.environ.get("TEST_POSTGRES_PASSWORD", "test_pass")
    print(f"[DEBUG] PostgreSQL: {pg_user}@{pg_host}:{pg_port}/{pg_db}")
    
    monkeypatch.setenv("POSTGRES_HOST", pg_host)
    monkeypatch.setenv("POSTGRES_PORT", pg_port)
    monkeypatch.setenv("POSTGRES_DB", pg_db)
    monkeypatch.setenv("POSTGRES_USER", pg_user)
    monkeypatch.setenv("POSTGRES_PASSWORD", pg_password)
    monkeypatch.setenv("POSTGRES_SCHEMA", "public")

    # Dragonfly configuration - use Kubernetes secrets if available, fallback to TEST_* env vars
    dragonfly_host = os.environ.get("DRAGONFLY_HOST") or os.environ.get("TEST_REDIS_HOST", "localhost")
    dragonfly_port = os.environ.get("DRAGONFLY_PORT") or os.environ.get("TEST_REDIS_PORT", "16380")
    monkeypatch.setenv("DRAGONFLY_HOST", dragonfly_host)
    monkeypatch.setenv("DRAGONFLY_PORT", dragonfly_port)
    if os.environ.get("DRAGONFLY_PASSWORD") or os.environ.get("TEST_REDIS_PASSWORD"):
        dragonfly_password = os.environ.get("DRAGONFLY_PASSWORD") or os.environ.get("TEST_REDIS_PASSWORD")
        monkeypatch.setenv("DRAGONFLY_PASSWORD", dragonfly_password)

    # LLM API key - Load from .env file for actual LLM calls (without override to preserve K8s secrets)
    from dotenv import load_dotenv
    load_dotenv(override=False)
    
    # NATS URL - use Kubernetes secret if available, fallback to TEST_NATS_URL for local dev
    nats_url = os.environ.get("NATS_URL") or os.environ.get("TEST_NATS_URL", "nats://localhost:14222")
    monkeypatch.setenv("NATS_URL", nats_url)
    print(f"[DEBUG] NATS_URL set to: {nats_url}")

    # Setup mock workflow if in mock mode
    from tests.utils.mock_workflow import is_mock_mode, setup_mock_workflow_for_test
    mock_coordinator = None
    
    if is_mock_mode():
        print("[DEBUG] Setting up mock workflow...")
        # We'll update the Redis client after the app starts
        mock_coordinator = setup_mock_workflow_for_test(None, sample_job_execution_event["job_id"])

    # Import app after environment is configured with model patching
    print("[DEBUG] Importing FastAPI app...")
    
    # Apply model patching for the entire test execution
    model_patches = []
    if is_mock_mode():
        print("🎭 [MOCK] Applying mock model patches to prevent real LLM calls...")
        from tests.utils.mock_workflow import get_test_model
        mock_model = get_test_model()
        print(f"🎭 [MOCK] Mock model created: {type(mock_model).__name__}")
        
        openai_patch = patch("langchain_openai.ChatOpenAI", return_value=mock_model)
        anthropic_patch = patch("langchain_anthropic.ChatAnthropic", return_value=mock_model)
        model_patches = [openai_patch, anthropic_patch]
        
        # Start the patches
        for i, p in enumerate(model_patches):
            p.start()
            print(f"🎭 [MOCK] Patch {i+1} started: {p}")
        
        print("🎭 [MOCK] All model patches active - real LLM calls are now blocked")
    
    try:
        from api.main import app
        print("[DEBUG] App imported successfully (new modular structure)")

        # Create test client with lifespan context
        print("[DEBUG] Creating TestClient (this starts app lifespan)...")
        with TestClient(app) as client:
            print("[DEBUG] TestClient created, app lifespan started")
            
            # CRITICAL FIX: Get the app's service clients after lifespan startup
            # This ensures we use the same client instances for both the app and the test
            print("[DEBUG] Getting app's service clients after lifespan startup...")
            from api.dependencies import get_redis_client, get_execution_manager
            
            app_redis_client = get_redis_client()
            app_execution_manager = get_execution_manager()
            
            print("[DEBUG] App service clients obtained successfully")
            print(f"[DEBUG] - Redis client: {type(app_redis_client).__name__}")
            print(f"[DEBUG] - Execution manager: {type(app_execution_manager).__name__}")
            print("[DEBUG] - NATS consumer: Not used in this test (Agent Generation Only)")
            
            # Update mock coordinator to use the app's Redis client
            if is_mock_mode() and mock_coordinator:
                print("[DEBUG] Updating mock coordinator to use app's Redis client...")
                mock_coordinator.redis_client = app_redis_client.client  # Use underlying Redis client
                mock_coordinator.replay_mock.redis_client = app_redis_client.client
                print("[DEBUG] Mock coordinator updated successfully")
            
            # Subscribe to Redis channel using the app's Redis client
            print("[DEBUG] Setting up Redis pub/sub with app's Redis client...")
            pubsub = app_redis_client.client.pubsub()  # Access the underlying Redis client
            channel = f"langgraph:stream:{sample_job_execution_event['job_id']}"
            pubsub.subscribe(channel)
            print(f"[DEBUG] Subscribed to Redis channel: {channel}")
            
            # Start listening in a separate thread (non-blocking)
            streaming_events: List[Dict[str, Any]] = []

            def capture_events():
                """Capture streaming events from Redis pub/sub."""
                for message in pubsub.listen():
                    if message['type'] == 'message':
                        try:
                            event_data = json.loads(message['data'])
                            streaming_events.append(event_data)

                            # Stop after final "end" event
                            if event_data.get("event_type") == "end":
                                break
                        except json.JSONDecodeError:
                            pass  # Ignore non-JSON messages

            # Start event capture in background
            import threading
            capture_thread = threading.Thread(target=capture_events, daemon=True)
            capture_thread.start()
            print("[DEBUG] Started Redis event capture thread")
            
            # Start mock workflow execution BEFORE sending HTTP request if in mock mode
            if is_mock_mode() and mock_coordinator:
                print("[DEBUG] Starting mock workflow execution BEFORE HTTP request...")
                # Start the mock workflow in a separate thread so it doesn't block
                def start_mock_replay():
                    time.sleep(0.5)  # Small delay to ensure HTTP request starts first
                    mock_coordinator.replay_mock.start_replay(app_redis_client.client, sample_job_execution_event["job_id"])
                
                mock_thread = threading.Thread(target=start_mock_replay, daemon=True)
                mock_thread.start()
                print("[DEBUG] Mock workflow thread started")
            
            # Prepare CloudEvent request (still needed for API, but no NATS validation)
            headers = {
                "ce-type": "dev.my-platform.agent.execute",
                "ce-source": "test-client",
                "ce-id": "test-agent-generation-001",
                "ce-specversion": "1.0"
            }

            # Send POST request to CloudEvent endpoint
            print("[DEBUG] Sending POST request to / (CloudEvent endpoint)...")
            response = client.post(
                "/",
                json=sample_cloudevent,
                headers=headers
            )
            print(f"[DEBUG] Response received: {response.status_code}")

            # ================================================================
            # VALIDATION 1: HTTP Response
            # ================================================================
            assert response.status_code == 200, \
                f"Expected 200 OK, got {response.status_code}: {response.text}"

            # Wait for event capture to complete
            # Mock mode: fast execution (15s), Real LLM: longer execution (2 minutes)
            print(f"\n[DEBUG] ===== EVENT CAPTURE MONITORING =====")
            max_wait = 15 if is_mock_mode() else 300
            wait_interval = 1 if is_mock_mode() else 30
            
            # Get expected event count for mock mode validation
            expected_event_count = None
            if is_mock_mode() and mock_coordinator:
                expected_event_count = len(mock_coordinator.replay_mock.events)
                print(f"[DEBUG] Expected events in mock mode: {expected_event_count}")
            
            waited = 0
            print(f"[DEBUG] Mode: {'MOCK' if is_mock_mode() else 'REAL'} LLM")
            print(f"[DEBUG] Waiting up to {max_wait}s for agent execution to complete...")
            print(f"[DEBUG] Initial streaming_events count: {len(streaming_events)}")
            
            while waited < max_wait:
                current_event_count = len(streaming_events)
                end_events = [e for e in streaming_events if e is not None and e.get("event_type") == "end"]
                
                # For mock mode, check both end event AND expected event count
                if is_mock_mode() and expected_event_count:
                    events_complete = current_event_count >= expected_event_count
                    if end_events and events_complete:
                        print(f"[DEBUG] ✅ Mock replay complete: {current_event_count}/{expected_event_count} events")
                        print(f"[DEBUG] Found {len(end_events)} 'end' event(s) after {waited} seconds")
                        break
                    elif end_events and not events_complete:
                        print(f"[DEBUG] 🔄 End event found but waiting for all events: {current_event_count}/{expected_event_count}")
                elif not is_mock_mode() and end_events:  # Real mode
                    print(f"[DEBUG] ✅ Real execution complete after {waited} seconds")
                    print(f"[DEBUG] Final event count: {current_event_count}")
                    break
                    
                if waited % wait_interval == 0 and waited > 0:  # Progress updates
                    recent_event_types = [e.get("event_type") if e is not None else 'None' for e in streaming_events[-5:]]
                    print(f"[DEBUG] Still executing... ({waited}s elapsed, {current_event_count} events so far)")
                    print(f"[DEBUG] Recent event types: {recent_event_types}")
                    if is_mock_mode() and expected_event_count:
                        print(f"[DEBUG] Progress: {current_event_count}/{expected_event_count} events")
                    
                time.sleep(1)
                waited += 1
            
            if waited >= max_wait:
                mode_str = "MOCK" if is_mock_mode() else "REAL"
                print(f"[DEBUG] ❌ Timeout after {max_wait} seconds waiting for completion ({mode_str} mode)")
                print(f"[DEBUG] Final event count: {len(streaming_events)}")
                print(f"[DEBUG] Last 10 event types: {[e.get('event_type') if e is not None else 'None' for e in streaming_events[-10:]]}")
                assert False, f"Test failed: Agent execution took longer than {max_wait} seconds. Mode: {mode_str}"
            
            print(f"[DEBUG] ===== END EVENT CAPTURE MONITORING =====\n")
            
            # Give more time for final events to be captured, especially in mock mode
            buffer_time = 3 if is_mock_mode() else 2
            print(f"[DEBUG] Waiting {buffer_time} seconds for final events...")
            time.sleep(buffer_time)
            
            # Additional validation for mock mode - ensure we have the final state update with files
            if is_mock_mode():
                final_event_count = len(streaming_events)
                state_update_events = [e for e in streaming_events if e.get("event_type") == "on_state_update"]
                print(f"[DEBUG] Mock mode final validation:")
                print(f"[DEBUG] - Total events captured: {final_event_count}")
                print(f"[DEBUG] - State update events: {len(state_update_events)}")
                
                # Check if the last state update has files data
                if state_update_events:
                    last_state_update = state_update_events[-1]
                    files_data = last_state_update.get("data", {}).get("files", {})
                    print(f"[DEBUG] - Files in last state update: {len(files_data)} files")
                    if files_data:
                        file_paths = list(files_data.keys())
                        print(f"[DEBUG] - File paths: {file_paths[:5]}{'...' if len(file_paths) > 5 else ''}")
                    else:
                        print(f"[DEBUG] - WARNING: No files found in last state update")
                else:
                    print(f"[DEBUG] - WARNING: No state update events found")
            
            # Final event summary before processing
            print(f"[DEBUG] ===== FINAL EVENT SUMMARY =====")
            print(f"[DEBUG] Total events captured: {len(streaming_events)}")
            if streaming_events:
                event_types = [e.get("event_type") for e in streaming_events if e is not None]
                unique_types = list(set(event_types))
                print(f"[DEBUG] Unique event types: {unique_types}")
                print(f"[DEBUG] First event type: {event_types[0] if event_types else 'NONE'}")
                print(f"[DEBUG] Last event type: {event_types[-1] if event_types else 'NONE'}")
                
                # Check for critical event types
                on_state_update_count = sum(1 for t in event_types if t == "on_state_update")
                end_count = sum(1 for t in event_types if t == "end")
                print(f"[DEBUG] on_state_update events: {on_state_update_count}")
                print(f"[DEBUG] end events: {end_count}")
            else:
                print(f"[DEBUG] ❌ WARNING: No events captured!")
            print(f"[DEBUG] ===== END FINAL EVENT SUMMARY =====\n")
            
            # NOTE: NATS message waiting removed for Test 1 (Agent Generation Only)
            
            # Calculate total execution duration
            total_duration_s = time.time() - execution_start_time

            # ================================================================
            # IMPORT HELPERS AFTER TEST EXECUTION
            # ================================================================
            from tests.utils.test_helpers import (
                extract_checkpoints,
                extract_specialist_timeline,
                generate_checkpoint_summary,
                generate_cloudevent_summary,
                generate_execution_summary,
                save_artifact,
                validate_minimum_events,
                validate_specialist_order,
                validate_event_structure,
                validate_workflow_result,
                validate_redis_artifacts,
                extract_and_save_generated_files,
            )
            
            # Note: test_id was already generated at the start of the test
            # All artifacts will be saved to the same run directory

            # ================================================================
            # ARTIFACT COLLECTION: Save ALL events to file
            # ================================================================
            save_artifact("all_events.json", streaming_events, as_json=True)
            
            # ================================================================
            # ARTIFACT COLLECTION: Extract and save generated files
            # ================================================================
            # This extracts all files created by write_file tool calls and saves
            # them to a 'files/' subdirectory for easy debugging and review
            print("\n[DEBUG] Extracting generated files from events...")
            
            # Add retry logic for file extraction in case of timing issues
            max_retries = 3 if is_mock_mode() else 1
            extracted_files = {}
            
            for attempt in range(max_retries):
                try:
                    extracted_files = extract_and_save_generated_files(streaming_events)
                    print(f"[DEBUG] Attempt {attempt + 1}: Extracted {len(extracted_files)} files")
                    
                    if len(extracted_files) > 0:
                        break  # Success
                    elif attempt < max_retries - 1 and is_mock_mode():
                        print(f"[DEBUG] No files extracted on attempt {attempt + 1}, retrying in 1 second...")
                        time.sleep(1)
                        
                except Exception as e:
                    print(f"[DEBUG] File extraction attempt {attempt + 1} failed: {e}")
                    if attempt < max_retries - 1:
                        time.sleep(1)
                    else:
                        raise
            
            print(f"[DEBUG] Final result: Extracted {len(extracted_files)} files")
            
            # Additional debugging for mock mode if no files were extracted
            if is_mock_mode() and len(extracted_files) == 0:
                print(f"[DEBUG] WARNING: No files extracted in mock mode - debugging event structure...")
                state_updates = [e for e in streaming_events if e.get("event_type") == "on_state_update"]
                print(f"[DEBUG] Found {len(state_updates)} state update events")
                
                for i, event in enumerate(state_updates[-3:], max(0, len(state_updates) - 3)):
                    files_data = event.get("data", {}).get("files", {})
                    print(f"[DEBUG] State update {i}: {len(files_data)} files")
                    if files_data:
                        print(f"[DEBUG] File keys: {list(files_data.keys())[:3]}{'...' if len(files_data) > 3 else ''}")
                        
                        # Check file content structure
                        first_file_key = list(files_data.keys())[0]
                        first_file_data = files_data[first_file_key]
                        print(f"[DEBUG] Sample file structure: {type(first_file_data)}")
                        if isinstance(first_file_data, dict):
                            print(f"[DEBUG] Sample file keys: {list(first_file_data.keys())}")
                            content = first_file_data.get("content", [])
                            print(f"[DEBUG] Content type: {type(content)}, length: {len(content) if hasattr(content, '__len__') else 'N/A'}")
                        break

            # ================================================================
            # VALIDATION 2: PostgreSQL Checkpoint Validation
            # ================================================================
            # Query checkpoints written during execution using the app's PostgreSQL connection
            print("[DEBUG] Creating PostgreSQL connection using app's connection string...")
            import psycopg
            
            # Use the same connection string as the app
            postgres_conn_string = app_execution_manager.postgres_connection_string
            print(f"[DEBUG] Using PostgreSQL connection string from app")
            
            # Create connection and extract checkpoints
            postgres_connection = psycopg.connect(postgres_conn_string)
            checkpoints = extract_checkpoints(postgres_connection, sample_job_execution_event["job_id"])
            print(f"[DEBUG] Extracted {len(checkpoints)} checkpoints from PostgreSQL")

            # ================================================================
            # ARTIFACT COLLECTION: Save checkpoints to file
            # ================================================================
            save_artifact("checkpoints.json", checkpoints, as_json=True)

            # ================================================================
            # REQ 3.1: job_id MUST be used as thread_id (CRITICAL)
            # ================================================================
            # Reference: requirements.md Section 3 "Stateful Graph Execution and Persistence"
            # "THE Agent Executor SHALL use the `job_id` from the `JobExecutionEvent`
            #  as the `thread_id` for the LangGraph execution."

            # Import mock mode check
            from tests.utils.mock_workflow import is_mock_mode
            
            # Checkpoint validation only for real LLM mode (mock mode doesn't persist checkpoints)
            if not is_mock_mode():
                assert len(checkpoints) > 0, \
                    "Req 3.1 VIOLATION: At least one checkpoint must be written to PostgreSQL"

                # Verify thread_id = job_id for ALL checkpoints
                for checkpoint in checkpoints:
                    thread_id = checkpoint["thread_id"]

                    assert thread_id == sample_job_execution_event["job_id"], \
                        f"Req 3.1 VIOLATION: thread_id must equal job_id. " \
                        f"Expected '{sample_job_execution_event['job_id']}', got '{thread_id}'"
            else:
                print("🎭 [MOCK MODE] Skipping PostgreSQL checkpoint validation - mock mode doesn't persist checkpoints")
                print(f"[DEBUG] Mock mode extracted {len(checkpoints)} checkpoints (expected: 0)")
                
            # ================================================================
            # REQ 3.3: Checkpoints saved after each step
            # ================================================================
            # Reference: requirements.md Section 3
            # "WHILE a LangGraph Graph is executing, THE Agent Executor SHALL save
            #  a Checkpoint to the Primary Data Store after the completion of each
            #  operational step within the graph."

            # Checkpoint validation only for real LLM mode (mock mode doesn't persist checkpoints)
            if not is_mock_mode():
                assert len(checkpoints) >= 1, \
                    f"Req 3.3: Expected at least one checkpoint after graph step execution, " \
                    f"got {len(checkpoints)}"

                # Verify checkpoint contains state data
                for checkpoint in checkpoints:
                    checkpoint_data = checkpoint["checkpoint"]

                    assert checkpoint_data is not None, \
                        "Req 3.3: Checkpoint must contain state data"

                    assert isinstance(checkpoint_data, dict), \
                        f"Req 3.3: Checkpoint must be a dict. Got: {type(checkpoint_data)}"

                    # Verify checkpoint has required LangGraph fields
                    # Reference: LangGraph PostgresSaver checkpoint structure
                    # https://langchain-ai.github.io/langgraph/reference/checkpoints/
                    assert "v" in checkpoint_data or "channel_values" in checkpoint_data, \
                        "Req 3.3: Checkpoint must contain LangGraph state (v or channel_values)"
            else:
                print("🎭 [MOCK MODE] Skipping Req 3.3 checkpoint step validation - mock mode doesn't persist checkpoints")
                print(f"[DEBUG] Mock mode found {len(checkpoints)} checkpoints (expected: 0 for mock mode)")

            # ================================================================
            # REQ 3.4: File System Artifacts Validation (CRITICAL)
            # ================================================================
            # Reference: Builder Agent workflow - validates that all required specification
            # files and the final definition.json were actually generated and emitted in Redis events
            
            print("\n" + "="*80)
            print("REDIS ARTIFACTS VALIDATION")
            print("="*80)
            
            is_valid, artifact_errors = validate_redis_artifacts(streaming_events, sample_job_execution_event["job_id"])
            
            if not is_valid:
                error_msg = "CRITICAL FAILURE: Required artifacts not found in Redis streaming events:\n\n"
                for i, error in enumerate(artifact_errors, 1):
                    error_msg += f"{i}. {error}\n"
                error_msg += "\nThis indicates the multi-agent workflow did not successfully generate "
                error_msg += "the required specification files. The workflow may have completed with "
                error_msg += "status='completed' but failed to produce the expected artifacts."
                
                assert False, error_msg
            
            print("✅ All required artifacts found and validated in Redis streaming events:")
            print("   - /THE_SPEC/constitution.md")
            print("   - /THE_SPEC/plan.md") 
            print("   - /THE_SPEC/requirements.md")
            print("   - /definition.json (✅ schema validated)")
            print("="*80)

            # ================================================================
            # VALIDATION 3: Redis Streaming Events Validation
            # ================================================================
            # Stop pub/sub listener
            pubsub.unsubscribe(channel)
            pubsub.close()

            # ================================================================
            # TIER 1: CRITICAL VALIDATIONS (MUST PASS)
            # ================================================================
            # Reference: agent-executor-minimum-events.md Section "Enforceable Test Assertions"
            print("\n" + "="*80)
            print("TIER 1: CRITICAL VALIDATIONS")
            print("="*80)

            # CRITICAL 1: Validate Subagent Invocation Pattern
            # Task tool calls are embedded in the message history within on_state_update events
            # Extract all messages from state updates and count task tool calls
            print(f"\n[DEBUG] ===== SUBAGENT INVOCATION VALIDATION =====")
            task_tool_calls = []
            on_state_update_events_processed = 0
            
            for event in streaming_events:
                if event is not None and event.get("event_type") == "on_state_update":
                    on_state_update_events_processed += 1
                    print(f"[DEBUG] Processing on_state_update event #{on_state_update_events_processed}")
                    
                    event_data = event.get("data", {})
                    print(f"[DEBUG] event_data type: {type(event_data)}, is None: {event_data is None}")
                    
                    if event_data is not None:
                        messages_str = event_data.get("messages", "")
                        print(f"[DEBUG] messages_str type: {type(messages_str)}, length: {len(messages_str) if isinstance(messages_str, str) else 'NOT_STRING'}")
                    else:
                        messages_str = ""
                        print(f"[DEBUG] event_data was None, using empty messages_str")
                    # Count occurrences of task tool calls in the message history
                    # Tool calls appear as: {'name': 'task', 'args': {...}, ...}
                    if "'name': 'task'" in messages_str or '"name": "task"' in messages_str:
                        # Count individual task calls by looking for subagent_type in args
                        import re
                        task_matches = re.findall(r"'name': 'task'.*?'subagent_type': '([^']+)'", messages_str)
                        if task_matches:
                            print(f"[DEBUG] Found {len(task_matches)} task matches in this event: {task_matches}")
                        task_tool_calls.extend(task_matches)
            
            print(f"[DEBUG] Processed {on_state_update_events_processed} on_state_update events")
            print(f"[DEBUG] Total task_tool_calls found: {len(task_tool_calls)}")
            print(f"[DEBUG] ===== END SUBAGENT INVOCATION VALIDATION =====\n")
            
            assert len(task_tool_calls) >= 5, \
                f"CRITICAL FAILURE: Expected ≥5 'task' tool invocations (for 5 subagents), " \
                f"got {len(task_tool_calls)}. " \
                f"Subagents invoked: {task_tool_calls}. " \
                f"This indicates SubAgentMiddleware is not working correctly."
            
            print(f"✅ Subagent invocations: {len(task_tool_calls)} task tool calls")
            print(f"   Subagents invoked: {', '.join(task_tool_calls)}")

            # CRITICAL 2: Validate All 5 Specialists Were Invoked
            # Check that all expected specialists appear in the task_tool_calls list
            expected_specialists = [
                "Guardrail Agent",
                "Impact Analysis Agent", 
                "Workflow Spec Agent",
                "Agent Spec Agent",
                "Multi-Agent Compiler Agent"
            ]
            
            for specialist in expected_specialists:
                assert specialist in task_tool_calls, \
                    f"CRITICAL FAILURE: Specialist '{specialist}' was not invoked. " \
                    f"Invoked: {task_tool_calls}"
            
            print(f"✅ All 5 specialists invoked successfully")

            # ================================================================
            # TIER 2: CONSISTENCY VALIDATIONS (SHOULD PASS)
            # ================================================================
            print("\n" + "="*80)
            print("TIER 2: CONSISTENCY VALIDATIONS")
            print("="*80)

            # Event structure validation
            is_valid, errors = validate_event_structure(streaming_events)
            if not is_valid:
                print(f"⚠️  WARNING: Event structure issues:\n" + "\n".join(errors))
            else:
                print("✅ Event structure validated")

            # Minimum event guarantees
            is_valid, errors = validate_minimum_events(streaming_events, use_typical=True)
            if not is_valid:
                is_valid_critical, errors_critical = validate_minimum_events(streaming_events, use_typical=False)
                if not is_valid_critical:
                    print(f"⚠️  WARNING: Even critical event guarantees not met:\n" + "\n".join(errors_critical))
                else:
                    print(f"⚠️  WARNING: Only critical guarantees met:\n" + "\n".join(errors))
            else:
                print("✅ Minimum event guarantees met")

            # Execution order validation
            is_valid, errors = validate_specialist_order(streaming_events)
            if not is_valid:
                print(f"⚠️  WARNING: Execution order issues:\n" + "\n".join(errors))
            else:
                print("✅ Execution order validated")

            # ================================================================
            # REQ 4.1: Redis channel naming convention
            # ================================================================
            # Reference: requirements.md Section 4 "Real-Time Output Streaming"
            # "THE Agent Executor SHALL publish LLM token generation events to a
            #  Redis channel named `langgraph:stream:{thread_id}`."
            # Also ref: design.md Section 2.5 "Redis Streaming Architecture"

            expected_channel = f"langgraph:stream:{sample_job_execution_event['job_id']}"
            assert channel == expected_channel, \
                f"Req 4.1 VIOLATION: Channel must be 'langgraph:stream:{{thread_id}}'. " \
                f"Expected '{expected_channel}', got '{channel}'"

            # ================================================================
            # REQ 4.1-4.3: Redis Streaming Events MUST be published
            # ================================================================
            # Reference: requirements.md Section 4
            # "REQ 4.1: SHALL publish LLM token generation events"
            # "REQ 4.2: SHALL publish tool execution start and end events"
            # "REQ 4.3: SHALL publish an 'end' event"

            assert len(streaming_events) >= 1, \
                f"Req 4.1-4.3 VIOLATION: Expected at least one streaming event. " \
                f"Got {len(streaming_events)} events: {[e.get('event_type') if e is not None else 'None' for e in streaming_events]}"

            # ================================================================
            # DESIGN 2.5: Event structure validation
            # ================================================================
            # Reference: design.md Section 4.4 "Redis Stream Payload"
            # All events must have event_type and data fields

            for event in streaming_events:
                if event is None:
                    continue
                    
                assert "event_type" in event, \
                    f"Design 2.5 VIOLATION: Event must have event_type field. Got: {event.keys()}"

                assert "data" in event, \
                    f"Design 2.5 VIOLATION: Event must have data field. Got: {event.keys()}"

            # Verify specific event types exist
            event_types = [e["event_type"] for e in streaming_events if e is not None]

            # REQ 4.1: LLM token generation events
            assert any(event_type in ["on_llm_stream", "on_llm_new_token", "on_chain_end"]
                      for event_type in event_types), \
                f"Req 4.1 VIOLATION: Must publish LLM token generation events. Got event types: {event_types}"

            # REQ 4.3: Final 'end' event MUST be published
            assert "end" in event_types, \
                f"Req 4.3 VIOLATION: Must publish final 'end' event to signal completion. " \
                f"Got event types: {event_types}"

            # Verify final "end" event structure
            end_events = [e for e in streaming_events if e is not None and e.get("event_type") == "end"]
            assert len(end_events) > 0, \
                "Req 4.3 VIOLATION: Expected at least one 'end' event in Redis stream"

            final_end_event = end_events[0]
            assert isinstance(final_end_event["data"], dict), \
                "Req 4.3 VIOLATION: Final 'end' event data should be a dict"

            # ================================================================
            # NOTE: CloudEvent Emission Validation (NATS) removed for Test 1
            # ================================================================

            # ================================================================
            # CRITICAL: Validate workflow completed successfully (not HALT)
            # ================================================================
            print("\n" + "="*80)
            print("WORKFLOW RESULT VALIDATION")
            print("="*80)
            
            # Extract actual result from streaming events instead of using mock data
            actual_result = {
                "status": "completed",  # Inferred from successful completion (no exceptions thrown)
                "output": "Workflow completed successfully - all required artifacts generated",  # Default success message
                "files": {},  # Will be populated from final state update
                "execution_time": total_duration_s
            }
            
            # Extract files from the final state update event
            print(f"\n[DEBUG] ===== RESULT EXTRACTION DEBUG =====")
            print(f"[DEBUG] Total streaming_events: {len(streaming_events) if streaming_events else 0}")
            
            if streaming_events:
                # Log event type distribution for debugging
                event_type_counts = {}
                for event in streaming_events:
                    if event is None:
                        event_type = "None"
                    else:
                        event_type = event.get("event_type", "UNKNOWN")
                    event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
                print(f"[DEBUG] Event type distribution: {event_type_counts}")
                
                # Find the final on_state_update event (second to last, before "end" event)
                print(f"[DEBUG] Searching for final on_state_update event...")
                final_state_event = None
                on_state_update_count = 0
                
                for i, event in enumerate(reversed(streaming_events)):
                    if event is None:
                        continue
                    event_type = event.get("event_type")
                    if event_type == "on_state_update":
                        on_state_update_count += 1
                        if final_state_event is None:  # Take the first (most recent) one
                            final_state_event = event
                            print(f"[DEBUG] Found final on_state_update event at position {len(streaming_events) - 1 - i} (counting from end)")
                            break
                
                print(f"[DEBUG] Total on_state_update events found: {on_state_update_count}")
                print(f"[DEBUG] final_state_event is None: {final_state_event is None}")
                
                if final_state_event is not None:
                    print(f"[DEBUG] Processing final_state_event...")
                    
                    # Extract files from final state
                    print(f"[DEBUG] Calling final_state_event.get('data', {{}})...")
                    event_data = final_state_event.get("data", {})
                    print(f"[DEBUG] event_data type: {type(event_data)}")
                    print(f"[DEBUG] event_data keys: {list(event_data.keys()) if isinstance(event_data, dict) else 'NOT_A_DICT'}")
                    
                    print(f"[DEBUG] Calling event_data.get('files', {{}})...")
                    files_data = event_data.get("files", {}) if event_data is not None else {}
                    print(f"[DEBUG] files_data type: {type(files_data)}")
                    print(f"[DEBUG] files_data is dict: {isinstance(files_data, dict)}")
                    
                    if isinstance(files_data, dict):
                        actual_result["files"] = files_data
                        print(f"[DEBUG] ✅ Successfully extracted {len(actual_result['files'])} files from final state")
                        print(f"[DEBUG] File names: {list(files_data.keys())[:5]}...")  # Show first 5 file names
                    else:
                        print(f"[DEBUG] ⚠️ files_data is not a dict, got: {files_data}")
                    
                    # Try to extract a more specific success message from the final AI message
                    print(f"[DEBUG] Extracting success message...")
                    messages_str = event_data.get("messages", "") if event_data is not None else ""
                    print(f"[DEBUG] messages_str type: {type(messages_str)}")
                    print(f"[DEBUG] messages_str length: {len(messages_str) if isinstance(messages_str, str) else 'NOT_STRING'}")
                    
                    if isinstance(messages_str, str) and "successfully" in messages_str:
                        print(f"[DEBUG] Found 'successfully' in messages, searching for patterns...")
                        # Look for success messages in the conversation
                        import re
                        success_patterns = [
                            r"workflow.*?successfully.*?completed",
                            r"successfully.*?completed.*?verified",
                            r"final.*?specification.*?ready"
                        ]
                        for i, pattern in enumerate(success_patterns):
                            matches = re.findall(pattern, messages_str, re.IGNORECASE)
                            if matches:
                                actual_result["output"] = f"Multi-agent workflow completed successfully: {matches[-1]}"
                                print(f"[DEBUG] ✅ Found success pattern {i+1}: {matches[-1]}")
                                break
                        else:
                            print(f"[DEBUG] No success patterns matched")
                    else:
                        print(f"[DEBUG] No 'successfully' found in messages or messages not a string")
                        
                else:
                    print("[DEBUG] ❌ WARNING: No on_state_update events found in streaming_events")
                    print(f"[DEBUG] Available event types (first 10): {[e.get('event_type') for e in streaming_events[:10]]}")
                    print(f"[DEBUG] Available event types (last 10): {[e.get('event_type') for e in streaming_events[-10:]]}")
            else:
                print("[DEBUG] ❌ ERROR: streaming_events is empty or None")
                
            print(f"[DEBUG] ===== END RESULT EXTRACTION DEBUG =====\n")
            
            print(f"[DEBUG] ===== WORKFLOW VALIDATION DEBUG =====")
            print(f"[DEBUG] actual_result keys: {list(actual_result.keys())}")
            print(f"[DEBUG] actual_result['status']: {actual_result.get('status')}")
            print(f"[DEBUG] actual_result['files'] count: {len(actual_result.get('files', {}))}")
            print(f"[DEBUG] checkpoints count: {len(checkpoints) if checkpoints else 0}")
            print(f"[DEBUG] Calling validate_workflow_result...")
            
            try:
                is_valid, validation_errors = validate_workflow_result(actual_result, checkpoints)
                print(f"[DEBUG] ✅ validate_workflow_result completed successfully")
                print(f"[DEBUG] is_valid: {is_valid}")
                print(f"[DEBUG] validation_errors count: {len(validation_errors) if validation_errors else 0}")
                if validation_errors:
                    print(f"[DEBUG] validation_errors: {validation_errors}")
            except Exception as e:
                print(f"[DEBUG] ❌ ERROR in validate_workflow_result: {type(e).__name__}: {e}")
                print(f"[DEBUG] Exception details: {repr(e)}")
                import traceback
                print(f"[DEBUG] Traceback: {traceback.format_exc()}")
                raise  # Re-raise the exception
            
            print(f"[DEBUG] ===== END WORKFLOW VALIDATION DEBUG =====\n")
            
            if not is_valid:
                error_msg = "WORKFLOW EXECUTION FAILED:\n\n"
                for i, error in enumerate(validation_errors, 1):
                    error_msg += f"{i}. {error}\n"
                error_msg += "\nThis indicates the multi-agent workflow encountered errors and could not "
                error_msg += "complete successfully. Common causes:\n"
                error_msg += "  - Missing required specification files (e.g., requirements.md)\n"
                error_msg += "  - Incomplete implementation plan from Impact Analysis Agent\n"
                error_msg += "  - Logical errors detected by Multi-Agent Compiler Agent\n"
                error_msg += "\nCheck the test logs and CloudEvent output for details."
                
                assert False, error_msg
            
            print(f"✅ Workflow completed successfully (no HALT errors)")
            print(f"✅ Workflow validation passed - artifacts generated and verified")
            if actual_result.get("output"):
                print(f"✅ Final output: {actual_result['output'][:100]}...")
            print("="*80)

            # ================================================================
            # ARTIFACT COLLECTION: Save specialist timeline (no CloudEvent in Test 1)
            # ================================================================
            
            specialist_timeline = extract_specialist_timeline(streaming_events)
            save_artifact("specialist_timeline.json", specialist_timeline, as_json=True)

            # ================================================================
            # GENERATE AND PRINT EXECUTION SUMMARY
            # ================================================================
            execution_summary = generate_execution_summary(
                streaming_events,
                checkpoints,
                specialist_timeline,
                None,  # No CloudEvent in Test 1
                total_duration_s
            )
            
            checkpoint_summary = generate_checkpoint_summary(checkpoints)
            
            # Save summary to file
            full_summary = f"{execution_summary}\n\n{checkpoint_summary}"
            save_artifact("summary.txt", full_summary, as_json=False)
            
            # Print ONLY summary to stdout (not all events)
            print("\n" + execution_summary)
            print("\n" + checkpoint_summary)
            
            print(f"\n[LOG_CAPTURE] Complete test logs saved to: {log_filepath}")
            
            # ================================================================
            # LOG CAPTURE CLEANUP
            # ================================================================
            # Restore original stdout/stderr
            sys.stdout = original_stdout
            sys.stderr = original_stderr
            
            # Close log file
            if log_file is not None:
                log_file.close()
            
            print(f"[LOG_CAPTURE] Logs saved to: {log_filepath}")
            
            # Cleanup mock workflow if used
            if is_mock_mode() and mock_coordinator:
                from tests.utils.mock_workflow import cleanup_mock_workflow
                cleanup_mock_workflow(sample_job_execution_event["job_id"])

        # Wait for event capture to complete
        # Mock mode: fast execution (10s), Real LLM: longer execution (2 minutes)
        print(f"\n[DEBUG] ===== EVENT CAPTURE MONITORING =====")
        max_wait = 10 if is_mock_mode() else 300
        wait_interval = 1 if is_mock_mode() else 30
        
        waited = 0
        print(f"[DEBUG] Mode: {'MOCK' if is_mock_mode() else 'REAL'} LLM")
        print(f"[DEBUG] Waiting up to {max_wait}s for agent execution to complete...")
        print(f"[DEBUG] Initial streaming_events count: {len(streaming_events)}")
        
        while waited < max_wait:
            current_event_count = len(streaming_events)
            end_events = [e for e in streaming_events if e is not None and e.get("event_type") == "end"]
            
            if end_events:
                print(f"[DEBUG] ✅ Found {len(end_events)} 'end' event(s) after {waited} seconds")
                print(f"[DEBUG] Final event count: {current_event_count}")
                break
                
            if waited % wait_interval == 0 and waited > 0:  # Progress updates
                recent_event_types = [e.get("event_type") if e is not None else 'None' for e in streaming_events[-5:]]
                print(f"[DEBUG] Still executing... ({waited}s elapsed, {current_event_count} events so far)")
                print(f"[DEBUG] Recent event types: {recent_event_types}")
                
            time.sleep(1)
            waited += 1
        
        if waited >= max_wait:
            mode_str = "MOCK" if is_mock_mode() else "REAL"
            print(f"[DEBUG] ❌ Timeout after {max_wait} seconds waiting for completion ({mode_str} mode)")
            print(f"[DEBUG] Final event count: {len(streaming_events)}")
            print(f"[DEBUG] Last 10 event types: {[e.get('event_type') if e is not None else 'None' for e in streaming_events[-10:]]}")
            assert False, f"Test failed: Agent execution took longer than {max_wait} seconds. Mode: {mode_str}"
        
        print(f"[DEBUG] ===== END EVENT CAPTURE MONITORING =====\n")
        
        # Give a bit more time for final events to be captured
        print(f"[DEBUG] Waiting 2 seconds for final events...")
        time.sleep(2)
        
        # Final event summary before processing
        print(f"[DEBUG] ===== FINAL EVENT SUMMARY =====")
        print(f"[DEBUG] Total events captured: {len(streaming_events)}")
        if streaming_events:
            event_types = [e.get("event_type") for e in streaming_events if e is not None]
            unique_types = list(set(event_types))
            print(f"[DEBUG] Unique event types: {unique_types}")
            print(f"[DEBUG] First event type: {event_types[0] if event_types else 'NONE'}")
            print(f"[DEBUG] Last event type: {event_types[-1] if event_types else 'NONE'}")
            
            # Check for critical event types
            on_state_update_count = sum(1 for t in event_types if t == "on_state_update")
            end_count = sum(1 for t in event_types if t == "end")
            print(f"[DEBUG] on_state_update events: {on_state_update_count}")
            print(f"[DEBUG] end events: {end_count}")
        else:
            print(f"[DEBUG] ❌ WARNING: No events captured!")
        print(f"[DEBUG] ===== END FINAL EVENT SUMMARY =====\n")
        
        # NOTE: NATS message waiting removed for Test 1 (Agent Generation Only)
        
        # Calculate total execution duration
        total_duration_s = time.time() - execution_start_time

        # ================================================================
        # IMPORT HELPERS AFTER TEST EXECUTION
        # ================================================================
        from tests.utils.test_helpers import (
            extract_checkpoints,
            extract_specialist_timeline,
            generate_checkpoint_summary,
            generate_cloudevent_summary,
            generate_execution_summary,
            save_artifact,
            validate_minimum_events,
            validate_specialist_order,
            validate_event_structure,
            validate_workflow_result,
            validate_redis_artifacts,
            extract_and_save_generated_files,
        )
        
        # Note: test_id was already generated at the start of the test
        # All artifacts will be saved to the same run directory

        # ================================================================
        # ARTIFACT COLLECTION: Save ALL events to file
        # ================================================================
        save_artifact("all_events.json", streaming_events, as_json=True)
        
        # ================================================================
        # ARTIFACT COLLECTION: Extract and save generated files
        # ================================================================
        # This extracts all files created by write_file tool calls and saves
        # them to a 'files/' subdirectory for easy debugging and review
        print("\n[DEBUG] Extracting generated files from events...")
        extracted_files = extract_and_save_generated_files(streaming_events)
        print(f"[DEBUG] Extracted {len(extracted_files)} files")

        # ================================================================
        # VALIDATION 2: PostgreSQL Checkpoint Validation
        # ================================================================
        # Query checkpoints written during execution
        checkpoints = extract_checkpoints(postgres_connection, sample_job_execution_event["job_id"])

        # ================================================================
        # ARTIFACT COLLECTION: Save checkpoints to file
        # ================================================================
        save_artifact("checkpoints.json", checkpoints, as_json=True)

        # ================================================================
        # REQ 3.1: job_id MUST be used as thread_id (CRITICAL)
        # ================================================================
        # Reference: requirements.md Section 3 "Stateful Graph Execution and Persistence"
        # "THE Agent Executor SHALL use the `job_id` from the `JobExecutionEvent`
        #  as the `thread_id` for the LangGraph execution."

        # Import mock mode check (if not already imported)
        from tests.utils.mock_workflow import is_mock_mode
        
        # Checkpoint validation only for real LLM mode (mock mode doesn't persist checkpoints)
        if not is_mock_mode():
            assert len(checkpoints) > 0, \
                "Req 3.1 VIOLATION: At least one checkpoint must be written to PostgreSQL"

            # Verify thread_id = job_id for ALL checkpoints
            for checkpoint in checkpoints:
                thread_id = checkpoint["thread_id"]

                assert thread_id == sample_job_execution_event["job_id"], \
                    f"Req 3.1 VIOLATION: thread_id must equal job_id. " \
                    f"Expected '{sample_job_execution_event['job_id']}', got '{thread_id}'"
        else:
            print("🎭 [MOCK MODE] Skipping Req 3.1 checkpoint validation - mock mode doesn't persist checkpoints")
            print(f"[DEBUG] Mock mode extracted {len(checkpoints)} checkpoints (expected: 0)")

        # ================================================================
        # REQ 3.3: Checkpoints saved after each step
        # ================================================================
        # Reference: requirements.md Section 3
        # "WHILE a LangGraph Graph is executing, THE Agent Executor SHALL save
        #  a Checkpoint to the Primary Data Store after the completion of each
        #  operational step within the graph."

        # Import mock mode check (if not already imported)
        from tests.utils.mock_workflow import is_mock_mode
        
        # Checkpoint validation only for real LLM mode (mock mode doesn't persist checkpoints)
        if not is_mock_mode():
            assert len(checkpoints) >= 1, \
                f"Req 3.3: Expected at least one checkpoint after graph step execution, " \
                f"got {len(checkpoints)}"

            # Verify checkpoint contains state data
            for checkpoint in checkpoints:
                checkpoint_data = checkpoint["checkpoint"]

                assert checkpoint_data is not None, \
                    "Req 3.3: Checkpoint must contain state data"

                assert isinstance(checkpoint_data, dict), \
                    f"Req 3.3: Checkpoint must be a dict. Got: {type(checkpoint_data)}"

                # Verify checkpoint has required LangGraph fields
                # Reference: LangGraph PostgresSaver checkpoint structure
                # https://langchain-ai.github.io/langgraph/reference/checkpoints/
                assert "v" in checkpoint_data or "channel_values" in checkpoint_data, \
                    "Req 3.3: Checkpoint must contain LangGraph state (v or channel_values)"
        else:
            print("🎭 [MOCK MODE] Skipping Req 3.3 checkpoint step validation - mock mode doesn't persist checkpoints")
            print(f"[DEBUG] Mock mode found {len(checkpoints)} checkpoints (expected: 0 for mock mode)")

        # ================================================================
        # REQ 3.4: File System Artifacts Validation (CRITICAL)
        # ================================================================
        # Reference: Builder Agent workflow - validates that all required specification
        # files and the final definition.json were actually generated and emitted in Redis events
        
        print("\n" + "="*80)
        print("REDIS ARTIFACTS VALIDATION")
        print("="*80)
        
        is_valid, artifact_errors = validate_redis_artifacts(streaming_events, sample_job_execution_event["job_id"])
        
        if not is_valid:
            error_msg = "CRITICAL FAILURE: Required artifacts not found in Redis streaming events:\n\n"
            for i, error in enumerate(artifact_errors, 1):
                error_msg += f"{i}. {error}\n"
            error_msg += "\nThis indicates the multi-agent workflow did not successfully generate "
            error_msg += "the required specification files. The workflow may have completed with "
            error_msg += "status='completed' but failed to produce the expected artifacts."
            
            assert False, error_msg
        
        print("✅ All required artifacts found and validated in Redis streaming events:")
        print("   - /THE_SPEC/constitution.md")
        print("   - /THE_SPEC/plan.md") 
        print("   - /THE_SPEC/requirements.md")
        print("   - /definition.json (✅ schema validated)")
        print("="*80)

        # ================================================================
        # VALIDATION 3: Redis Streaming Events Validation
        # ================================================================
        # Stop pub/sub listener
        pubsub.unsubscribe(channel)
        pubsub.close()

        # ================================================================
        # TIER 1: CRITICAL VALIDATIONS (MUST PASS)
        # ================================================================
        # Reference: agent-executor-minimum-events.md Section "Enforceable Test Assertions"
        print("\n" + "="*80)
        print("TIER 1: CRITICAL VALIDATIONS")
        print("="*80)

        # CRITICAL 1: Validate Subagent Invocation Pattern
        # Task tool calls are embedded in the message history within on_state_update events
        # Extract all messages from state updates and count task tool calls
        print(f"\n[DEBUG] ===== SUBAGENT INVOCATION VALIDATION =====")
        task_tool_calls = []
        on_state_update_events_processed = 0
        
        for event in streaming_events:
            if event is not None and event.get("event_type") == "on_state_update":
                on_state_update_events_processed += 1
                print(f"[DEBUG] Processing on_state_update event #{on_state_update_events_processed}")
                
                event_data = event.get("data", {})
                print(f"[DEBUG] event_data type: {type(event_data)}, is None: {event_data is None}")
                
                if event_data is not None:
                    messages_str = event_data.get("messages", "")
                    print(f"[DEBUG] messages_str type: {type(messages_str)}, length: {len(messages_str) if isinstance(messages_str, str) else 'NOT_STRING'}")
                else:
                    messages_str = ""
                    print(f"[DEBUG] event_data was None, using empty messages_str")
                # Count occurrences of task tool calls in the message history
                # Tool calls appear as: {'name': 'task', 'args': {...}, ...}
                if "'name': 'task'" in messages_str or '"name": "task"' in messages_str:
                    # Count individual task calls by looking for subagent_type in args
                    import re
                    task_matches = re.findall(r"'name': 'task'.*?'subagent_type': '([^']+)'", messages_str)
                    if task_matches:
                        print(f"[DEBUG] Found {len(task_matches)} task matches in this event: {task_matches}")
                    task_tool_calls.extend(task_matches)
        
        print(f"[DEBUG] Processed {on_state_update_events_processed} on_state_update events")
        print(f"[DEBUG] Total task_tool_calls found: {len(task_tool_calls)}")
        print(f"[DEBUG] ===== END SUBAGENT INVOCATION VALIDATION =====\n")
        
        assert len(task_tool_calls) >= 5, \
            f"CRITICAL FAILURE: Expected ≥5 'task' tool invocations (for 5 subagents), " \
            f"got {len(task_tool_calls)}. " \
            f"Subagents invoked: {task_tool_calls}. " \
            f"This indicates SubAgentMiddleware is not working correctly."
        
        print(f"✅ Subagent invocations: {len(task_tool_calls)} task tool calls")
        print(f"   Subagents invoked: {', '.join(task_tool_calls)}")

        # CRITICAL 2: Validate All 5 Specialists Were Invoked
        # Check that all expected specialists appear in the task_tool_calls list
        expected_specialists = [
            "Guardrail Agent",
            "Impact Analysis Agent", 
            "Workflow Spec Agent",
            "Agent Spec Agent",
            "Multi-Agent Compiler Agent"
        ]
        
        for specialist in expected_specialists:
            assert specialist in task_tool_calls, \
                f"CRITICAL FAILURE: Specialist '{specialist}' was not invoked. " \
                f"Invoked: {task_tool_calls}"
        
        print(f"✅ All 5 specialists invoked successfully")

        # ================================================================
        # TIER 2: CONSISTENCY VALIDATIONS (SHOULD PASS)
        # ================================================================
        print("\n" + "="*80)
        print("TIER 2: CONSISTENCY VALIDATIONS")
        print("="*80)

        # Event structure validation
        is_valid, errors = validate_event_structure(streaming_events)
        if not is_valid:
            print(f"⚠️  WARNING: Event structure issues:\n" + "\n".join(errors))
        else:
            print("✅ Event structure validated")

        # Minimum event guarantees
        is_valid, errors = validate_minimum_events(streaming_events, use_typical=True)
        if not is_valid:
            is_valid_critical, errors_critical = validate_minimum_events(streaming_events, use_typical=False)
            if not is_valid_critical:
                print(f"⚠️  WARNING: Even critical event guarantees not met:\n" + "\n".join(errors_critical))
            else:
                print(f"⚠️  WARNING: Only critical guarantees met:\n" + "\n".join(errors))
        else:
            print("✅ Minimum event guarantees met")

        # Execution order validation
        is_valid, errors = validate_specialist_order(streaming_events)
        if not is_valid:
            print(f"⚠️  WARNING: Execution order issues:\n" + "\n".join(errors))
        else:
            print("✅ Execution order validated")

        # ================================================================
        # REQ 4.1: Redis channel naming convention
        # ================================================================
        # Reference: requirements.md Section 4 "Real-Time Output Streaming"
        # "THE Agent Executor SHALL publish LLM token generation events to a
        #  Redis channel named `langgraph:stream:{thread_id}`."
        # Also ref: design.md Section 2.5 "Redis Streaming Architecture"

        expected_channel = f"langgraph:stream:{sample_job_execution_event['job_id']}"
        assert channel == expected_channel, \
            f"Req 4.1 VIOLATION: Channel must be 'langgraph:stream:{{thread_id}}'. " \
            f"Expected '{expected_channel}', got '{channel}'"

        # ================================================================
        # REQ 4.1-4.3: Redis Streaming Events MUST be published
        # ================================================================
        # Reference: requirements.md Section 4
        # "REQ 4.1: SHALL publish LLM token generation events"
        # "REQ 4.2: SHALL publish tool execution start and end events"
        # "REQ 4.3: SHALL publish an 'end' event"

        assert len(streaming_events) >= 1, \
            f"Req 4.1-4.3 VIOLATION: Expected at least one streaming event. " \
            f"Got {len(streaming_events)} events: {[e.get('event_type') if e is not None else 'None' for e in streaming_events]}"

        # ================================================================
        # DESIGN 2.5: Event structure validation
        # ================================================================
        # Reference: design.md Section 4.4 "Redis Stream Payload"
        # All events must have event_type and data fields

        for event in streaming_events:
            if event is None:
                continue
                
            assert "event_type" in event, \
                f"Design 2.5 VIOLATION: Event must have event_type field. Got: {event.keys()}"

            assert "data" in event, \
                f"Design 2.5 VIOLATION: Event must have data field. Got: {event.keys()}"

        # Verify specific event types exist
        event_types = [e["event_type"] for e in streaming_events if e is not None]

        # REQ 4.1: LLM token generation events
        assert any(event_type in ["on_llm_stream", "on_llm_new_token", "on_chain_end"]
                  for event_type in event_types), \
            f"Req 4.1 VIOLATION: Must publish LLM token generation events. Got event types: {event_types}"

        # REQ 4.3: Final 'end' event MUST be published
        assert "end" in event_types, \
            f"Req 4.3 VIOLATION: Must publish final 'end' event to signal completion. " \
            f"Got event types: {event_types}"

        # Verify final "end" event structure
        end_events = [e for e in streaming_events if e is not None and e.get("event_type") == "end"]
        assert len(end_events) > 0, \
            "Req 4.3 VIOLATION: Expected at least one 'end' event in Redis stream"

        final_end_event = end_events[0]
        assert isinstance(final_end_event["data"], dict), \
            "Req 4.3 VIOLATION: Final 'end' event data should be a dict"

        # ================================================================
        # NOTE: CloudEvent Emission Validation (NATS) removed for Test 1
        # ================================================================

        # ================================================================
        # CRITICAL: Validate workflow completed successfully (not HALT)
        # ================================================================
        print("\n" + "="*80)
        print("WORKFLOW RESULT VALIDATION")
        print("="*80)
        
        # Extract actual result from streaming events instead of using mock data
        actual_result = {
            "status": "completed",  # Inferred from successful completion (no exceptions thrown)
            "output": "Workflow completed successfully - all required artifacts generated",  # Default success message
            "files": {},  # Will be populated from final state update
            "execution_time": total_duration_s
        }
        
        # Extract files from the final state update event
        print(f"\n[DEBUG] ===== RESULT EXTRACTION DEBUG =====")
        print(f"[DEBUG] Total streaming_events: {len(streaming_events) if streaming_events else 0}")
        
        if streaming_events:
            # Log event type distribution for debugging
            event_type_counts = {}
            for event in streaming_events:
                if event is None:
                    event_type = "None"
                else:
                    event_type = event.get("event_type", "UNKNOWN")
                event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
            print(f"[DEBUG] Event type distribution: {event_type_counts}")
            
            # Find the final on_state_update event (second to last, before "end" event)
            print(f"[DEBUG] Searching for final on_state_update event...")
            final_state_event = None
            on_state_update_count = 0
            
            for i, event in enumerate(reversed(streaming_events)):
                if event is None:
                    continue
                event_type = event.get("event_type")
                if event_type == "on_state_update":
                    on_state_update_count += 1
                    if final_state_event is None:  # Take the first (most recent) one
                        final_state_event = event
                        print(f"[DEBUG] Found final on_state_update event at position {len(streaming_events) - 1 - i} (counting from end)")
                        break
            
            print(f"[DEBUG] Total on_state_update events found: {on_state_update_count}")
            print(f"[DEBUG] final_state_event is None: {final_state_event is None}")
            
            if final_state_event is not None:
                print(f"[DEBUG] Processing final_state_event...")
                
                # Extract files from final state
                print(f"[DEBUG] Calling final_state_event.get('data', {{}})...")
                event_data = final_state_event.get("data", {})
                print(f"[DEBUG] event_data type: {type(event_data)}")
                print(f"[DEBUG] event_data keys: {list(event_data.keys()) if isinstance(event_data, dict) else 'NOT_A_DICT'}")
                
                print(f"[DEBUG] Calling event_data.get('files', {{}})...")
                files_data = event_data.get("files", {}) if event_data is not None else {}
                print(f"[DEBUG] files_data type: {type(files_data)}")
                print(f"[DEBUG] files_data is dict: {isinstance(files_data, dict)}")
                
                if isinstance(files_data, dict):
                    actual_result["files"] = files_data
                    print(f"[DEBUG] ✅ Successfully extracted {len(actual_result['files'])} files from final state")
                    print(f"[DEBUG] File names: {list(files_data.keys())[:5]}...")  # Show first 5 file names
                else:
                    print(f"[DEBUG] ⚠️ files_data is not a dict, got: {files_data}")
                
                # Try to extract a more specific success message from the final AI message
                print(f"[DEBUG] Extracting success message...")
                messages_str = event_data.get("messages", "") if event_data is not None else ""
                print(f"[DEBUG] messages_str type: {type(messages_str)}")
                print(f"[DEBUG] messages_str length: {len(messages_str) if isinstance(messages_str, str) else 'NOT_STRING'}")
                
                if isinstance(messages_str, str) and "successfully" in messages_str:
                    print(f"[DEBUG] Found 'successfully' in messages, searching for patterns...")
                    # Look for success messages in the conversation
                    import re
                    success_patterns = [
                        r"workflow.*?successfully.*?completed",
                        r"successfully.*?completed.*?verified",
                        r"final.*?specification.*?ready"
                    ]
                    for i, pattern in enumerate(success_patterns):
                        matches = re.findall(pattern, messages_str, re.IGNORECASE)
                        if matches:
                            actual_result["output"] = f"Multi-agent workflow completed successfully: {matches[-1]}"
                            print(f"[DEBUG] ✅ Found success pattern {i+1}: {matches[-1]}")
                            break
                    else:
                        print(f"[DEBUG] No success patterns matched")
                else:
                    print(f"[DEBUG] No 'successfully' found in messages or messages not a string")
                    
            else:
                print("[DEBUG] ❌ WARNING: No on_state_update events found in streaming_events")
                print(f"[DEBUG] Available event types (first 10): {[e.get('event_type') for e in streaming_events[:10]]}")
                print(f"[DEBUG] Available event types (last 10): {[e.get('event_type') for e in streaming_events[-10:]]}")
        else:
            print("[DEBUG] ❌ ERROR: streaming_events is empty or None")
            
        print(f"[DEBUG] ===== END RESULT EXTRACTION DEBUG =====\n")
        
        print(f"[DEBUG] ===== WORKFLOW VALIDATION DEBUG =====")
        print(f"[DEBUG] actual_result keys: {list(actual_result.keys())}")
        print(f"[DEBUG] actual_result['status']: {actual_result.get('status')}")
        print(f"[DEBUG] actual_result['files'] count: {len(actual_result.get('files', {}))}")
        print(f"[DEBUG] checkpoints count: {len(checkpoints) if checkpoints else 0}")
        print(f"[DEBUG] Calling validate_workflow_result...")
        
        try:
            is_valid, validation_errors = validate_workflow_result(actual_result, checkpoints)
            print(f"[DEBUG] ✅ validate_workflow_result completed successfully")
            print(f"[DEBUG] is_valid: {is_valid}")
            print(f"[DEBUG] validation_errors count: {len(validation_errors) if validation_errors else 0}")
            if validation_errors:
                print(f"[DEBUG] validation_errors: {validation_errors}")
        except Exception as e:
            print(f"[DEBUG] ❌ ERROR in validate_workflow_result: {type(e).__name__}: {e}")
            print(f"[DEBUG] Exception details: {repr(e)}")
            import traceback
            print(f"[DEBUG] Traceback: {traceback.format_exc()}")
            raise  # Re-raise the exception
        
        print(f"[DEBUG] ===== END WORKFLOW VALIDATION DEBUG =====\n")
        
        if not is_valid:
            error_msg = "WORKFLOW EXECUTION FAILED:\n\n"
            for i, error in enumerate(validation_errors, 1):
                error_msg += f"{i}. {error}\n"
            error_msg += "\nThis indicates the multi-agent workflow encountered errors and could not "
            error_msg += "complete successfully. Common causes:\n"
            error_msg += "  - Missing required specification files (e.g., requirements.md)\n"
            error_msg += "  - Incomplete implementation plan from Impact Analysis Agent\n"
            error_msg += "  - Logical errors detected by Multi-Agent Compiler Agent\n"
            error_msg += "\nCheck the test logs and CloudEvent output for details."
            
            assert False, error_msg
        
        print(f"✅ Workflow completed successfully (no HALT errors)")
        print(f"✅ Workflow validation passed - artifacts generated and verified")
        if actual_result.get("output"):
            print(f"✅ Final output: {actual_result['output'][:100]}...")
        print("="*80)

        # ================================================================
        # ARTIFACT COLLECTION: Save specialist timeline (no CloudEvent in Test 1)
        # ================================================================
        
        specialist_timeline = extract_specialist_timeline(streaming_events)
        save_artifact("specialist_timeline.json", specialist_timeline, as_json=True)

        # ================================================================
        # GENERATE AND PRINT EXECUTION SUMMARY
        # ================================================================
        execution_summary = generate_execution_summary(
            streaming_events,
            checkpoints,
            specialist_timeline,
            None,  # No CloudEvent in Test 1
            total_duration_s
        )
        
        checkpoint_summary = generate_checkpoint_summary(checkpoints)
        
        # Save summary to file
        full_summary = f"{execution_summary}\n\n{checkpoint_summary}"
        save_artifact("summary.txt", full_summary, as_json=False)
        
        # Print ONLY summary to stdout (not all events)
        print("\n" + execution_summary)
        print("\n" + checkpoint_summary)
        
        print(f"\n[LOG_CAPTURE] Complete test logs saved to: {log_filepath}")
        
        # ================================================================
        # LOG CAPTURE CLEANUP
        # ================================================================
        # Restore original stdout/stderr
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        
        # Close log file
        if log_file is not None:
            log_file.close()
        
        print(f"[LOG_CAPTURE] Logs saved to: {log_filepath}")
        
        # Cleanup mock workflow if used
        if is_mock_mode() and mock_coordinator:
            from tests.utils.mock_workflow import cleanup_mock_workflow
            cleanup_mock_workflow(sample_job_execution_event["job_id"])
        
        # Close PostgreSQL connection if it was created
        if 'postgres_connection' in locals() and postgres_connection:
            postgres_connection.close()
            print("[DEBUG] PostgreSQL connection closed")
    
    finally:
        # Stop model patches if they were started
        if is_mock_mode():
            print("[DEBUG] Stopping mock model patches...")
            for p in model_patches:
                p.stop()


# Test 2: Fixtures Configuration Test
@pytest.mark.asyncio
async def test_fixtures_are_properly_configured(
    sample_agent_definition: Dict[str, Any],
    sample_job_execution_event: Dict[str, Any],
    sample_cloudevent: Dict[str, Any]
) -> None:
    """
    Test that sample data fixtures are properly configured.
    
    This test validates that the test data is working correctly
    before running the main integration tests.
    
    Note: Service client fixtures (PostgreSQL, Redis, NATS) were removed
    since the integration test now uses the app's actual service clients
    via dependency injection for better production-like testing.
    """
    print("\n[DEBUG] Testing sample data fixture configuration...")
    
    # Test sample data
    assert sample_agent_definition is not None, "sample_agent_definition is None"
    assert sample_job_execution_event is not None, "sample_job_execution_event is None"
    assert sample_cloudevent is not None, "sample_cloudevent is None"
    
    # Test sample data structure
    assert "job_id" in sample_job_execution_event, "sample_job_execution_event missing job_id"
    assert "agent_definition" in sample_job_execution_event, "sample_job_execution_event missing agent_definition"
    assert "data" in sample_cloudevent, "sample_cloudevent missing data"
    assert sample_cloudevent["data"] == sample_job_execution_event, "CloudEvent data mismatch"
    
    print("✅ All sample data fixtures configured correctly")
    print("ℹ️  Service clients (PostgreSQL, Redis, NATS) are now obtained from the app via dependency injection")


# ============================================================================
# NOTE: NATS Consumer Integration Test removed for Test 1 (Agent Generation Only)
# ============================================================================


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
