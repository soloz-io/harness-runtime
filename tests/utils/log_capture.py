"""
Log Capture Utilities for Integration Tests.

This module provides comprehensive log capturing functionality to record all logs,
stdout, stderr, and application logs to files in the output directory for debugging
and analysis.

Features:
    - Captures all Python logging (including structlog)
    - Captures stdout/stderr (print statements, etc.)
    - Saves to timestamped files in outputs/ directory
    - Context manager for easy setup/cleanup
    - Preserves original console output

Usage:
    with LogCapture() as log_file:
        # All logs will be captured to log_file
        print("This will be in both console and log file")
        logger.info("This will also be captured")
"""

import logging
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, TextIO

import structlog

from .test_helpers import get_output_dir


class TeeStream:
    """
    Stream that writes to both original stream (console) and log file.
    
    This allows us to capture all stdout/stderr while still showing output
    in the console for real-time monitoring.
    """
    
    def __init__(self, original_stream: TextIO, log_file: TextIO):
        self.original_stream = original_stream
        self.log_file = log_file
        
    def write(self, text: str) -> None:
        """Write text to both original stream and log file."""
        # Write to original stream (console)
        self.original_stream.write(text)
        self.original_stream.flush()
        # Also write to log file
        self.log_file.write(text)
        self.log_file.flush()
        
    def flush(self) -> None:
        """Flush both streams."""
        self.original_stream.flush()
        self.log_file.flush()


@contextmanager
def LogCapture(test_name: str = "test") -> Generator[Path, None, None]:
    """
    Context manager for comprehensive log capture.
    
    Captures all logs, stdout, and stderr to a timestamped file in the outputs
    directory. Automatically restores original logging configuration on exit.
    
    Args:
        test_name: Name prefix for the log file
        
    Yields:
        Path to the log file being written
        
    Example:
        with LogCapture("integration_test") as log_file:
            print("This goes to console and log file")
            logger.info("This also gets captured")
            # log_file contains path to the captured logs
    """
    # Generate unique log filename
    log_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = f"{test_name}_{log_timestamp}.log"
    log_filepath = get_output_dir() / log_filename
    
    # Store original state
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    root_logger = logging.getLogger()
    original_level = root_logger.level
    original_handlers = root_logger.handlers.copy()
    
    # Open log file
    log_file = open(log_filepath, 'w')
    
    try:
        print(f"\n[LOG_CAPTURE] All logs will be saved to: {log_filepath}")
        print("=" * 80)
        
        # ================================================================
        # SETUP PYTHON LOGGING CAPTURE
        # ================================================================
        
        # Create file handler for all logs
        file_handler = logging.FileHandler(log_filepath, mode='a')
        file_handler.setLevel(logging.DEBUG)
        
        # Create detailed formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        file_handler.setFormatter(formatter)
        
        # Configure root logger to capture all library logs
        root_logger.setLevel(logging.DEBUG)
        root_logger.addHandler(file_handler)
        
        # ================================================================
        # SETUP STRUCTLOG CAPTURE (used by the application)
        # ================================================================
        
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.UnicodeDecoder(),
                structlog.processors.JSONRenderer()
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
        
        # ================================================================
        # SETUP STDOUT/STDERR CAPTURE
        # ================================================================
        
        # Redirect stdout and stderr to capture all print statements
        sys.stdout = TeeStream(original_stdout, log_file)
        sys.stderr = TeeStream(original_stderr, log_file)
        
        # Write initial log header
        log_file.write(f"\n{'=' * 80}\n")
        log_file.write(f"LOG CAPTURE STARTED: {datetime.now().isoformat()}\n")
        log_file.write(f"Test: {test_name}\n")
        log_file.write(f"{'=' * 80}\n\n")
        log_file.flush()
        
        # Yield the log file path to the caller
        yield log_filepath
        
    finally:
        # ================================================================
        # CLEANUP AND RESTORE ORIGINAL STATE
        # ================================================================
        
        # Write final log footer
        log_file.write(f"\n{'=' * 80}\n")
        log_file.write(f"LOG CAPTURE ENDED: {datetime.now().isoformat()}\n")
        log_file.write(f"{'=' * 80}\n")
        
        # Restore stdout/stderr
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        
        # Restore root logger
        root_logger.setLevel(original_level)
        root_logger.handlers = original_handlers
        
        # Close log file
        log_file.close()
        
        print(f"[LOG_CAPTURE] Logs saved to: {log_filepath}")


def setup_test_logging(test_name: str) -> tuple[Path, callable]:
    """
    Alternative non-context-manager setup for log capture.
    
    Use this if you need more control over when logging starts/stops.
    Remember to call the cleanup function when done!
    
    Args:
        test_name: Name prefix for the log file
        
    Returns:
        Tuple of (log_file_path, cleanup_function)
        
    Example:
        log_path, cleanup = setup_test_logging("my_test")
        try:
            # Your test code here
            pass
        finally:
            cleanup()
    """
    # This is a simplified version - use LogCapture context manager instead
    # when possible for automatic cleanup
    raise NotImplementedError("Use LogCapture context manager instead")