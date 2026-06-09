"""
Pytest configuration for integration tests.

This module sets up environment variables for integration tests using the
centralized in-cluster configuration system.
"""

# Import centralized configuration FIRST (before any other imports)
from tests.integration.in_cluster_conftest import setup_in_cluster_environment

# Set up the environment automatically
# setup_in_cluster_environment()  # Commented out to allow Kubernetes secrets to take precedence

# Now import other modules that depend on environment variables
import asyncio
import subprocess
import time
import signal
import pytest
from typing import Generator


@pytest.fixture(scope="session")
def nats_consumer_service() -> Generator[subprocess.Popen, None, None]:
    """
    Start the deepagents-runtime service with NATS consumer for integration testing.
    
    This fixture starts the FastAPI application as a background process, which includes
    the NATS consumer that listens to the AGENT_EXECUTION stream. This allows the
    test_nats_consumer_processing test to be self-sustainable.
    
    The service runs on port 8081 to avoid conflicts with any development instances
    running on port 8080.
    
    Yields:
        subprocess.Popen: The running service process
        
    Cleanup:
        Terminates the service process after tests complete
    """
    import sys
    import os
    from pathlib import Path
    
    # Get the project root directory
    project_root = Path(__file__).parent.parent.parent
    
    print(f"\n[FIXTURE] Starting deepagents-runtime service for NATS consumer testing...")
    print(f"[FIXTURE] Project root: {project_root}")
    print(f"[FIXTURE] Python executable: {sys.executable}")
    
    # Start the service as a background process
    # Use port 8081 to avoid conflicts with development instances
    process = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", 
            "api.main:app", 
            "--host", "0.0.0.0",
            "--port", "8081",
            "--log-level", "info"
        ],
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # Line buffered
        universal_newlines=True
    )
    
    print(f"[FIXTURE] Service started with PID: {process.pid}")
    
    # Wait for service to start up
    # Check for startup indicators in the logs
    startup_timeout = 30  # 30 seconds
    startup_indicators = [
        "nats_consumer_started",
        "deepagents_runtime_service_started",
        "Application startup complete"
    ]
    
    print(f"[FIXTURE] Waiting up to {startup_timeout}s for service startup...")
    start_time = time.time()
    startup_complete = False
    
    while time.time() - start_time < startup_timeout:
        if process.poll() is not None:
            # Process has terminated
            stdout, stderr = process.communicate()
            print(f"[FIXTURE] ERROR: Service process terminated during startup")
            print(f"[FIXTURE] Exit code: {process.returncode}")
            print(f"[FIXTURE] Output: {stdout}")
            raise RuntimeError(f"Service failed to start (exit code: {process.returncode})")
        
        # Read available output without blocking
        try:
            # Use select on Unix systems to check if data is available
            import select
            if select.select([process.stdout], [], [], 0.1)[0]:
                line = process.stdout.readline()
                if line:
                    print(f"[FIXTURE] Service: {line.strip()}")
                    # Check for startup indicators
                    if any(indicator in line for indicator in startup_indicators):
                        startup_complete = True
                        break
        except (ImportError, OSError):
            # Fallback for Windows or if select is not available
            time.sleep(0.5)
        
        time.sleep(0.1)
    
    if not startup_complete:
        print(f"[FIXTURE] WARNING: Startup indicators not detected within {startup_timeout}s")
        print(f"[FIXTURE] Proceeding anyway - service may still be functional")
    else:
        print(f"[FIXTURE] ✓ Service startup completed successfully")
    
    # Additional wait to ensure NATS consumer is fully ready
    print(f"[FIXTURE] Waiting additional 2s for NATS consumer initialization...")
    time.sleep(2)
    
    print(f"[FIXTURE] ✓ deepagents-runtime service ready for testing")
    
    try:
        yield process
    finally:
        # Cleanup: Terminate the service process
        print(f"\n[FIXTURE] Shutting down deepagents-runtime service (PID: {process.pid})")
        
        try:
            # Try graceful shutdown first
            process.terminate()
            
            # Wait up to 10 seconds for graceful shutdown
            try:
                process.wait(timeout=10)
                print(f"[FIXTURE] ✓ Service shut down gracefully")
            except subprocess.TimeoutExpired:
                # Force kill if graceful shutdown fails
                print(f"[FIXTURE] Graceful shutdown timed out, force killing...")
                process.kill()
                process.wait()
                print(f"[FIXTURE] ✓ Service force killed")
                
        except Exception as e:
            print(f"[FIXTURE] Error during service shutdown: {e}")
            try:
                process.kill()
            except:
                pass
