"""
Test configuration utilities for deepagents-runtime.

This module provides configuration management for test execution,
including environment-based switching between mock and real LLM modes.
"""

import os
from typing import Dict, Any


class TestConfig:
    """Configuration manager for test execution."""
    
    @staticmethod
    def is_mock_mode() -> bool:
        """Check if tests should run in mock LLM mode."""
        env_value = os.getenv("USE_MOCK_LLM", "false")
        result = env_value.lower() == "true"
        print(f"[TEST_CONFIG] is_mock_mode() called: USE_MOCK_LLM='{env_value}' -> {result}")
        return result
    
    @staticmethod
    def get_mock_timeout() -> int:
        """Get timeout for mock mode tests (seconds)."""
        return int(os.getenv("MOCK_TIMEOUT", "30"))
    
    @staticmethod
    def get_real_timeout() -> int:
        """Get timeout for real LLM tests (seconds)."""
        return int(os.getenv("REAL_TIMEOUT", "480"))
    
    @staticmethod
    def get_mock_event_delay() -> int:
        """Get delay between mock events (milliseconds)."""
        return int(os.getenv("MOCK_EVENT_DELAY", "5"))
    
    @staticmethod
    def get_mock_events_file() -> str:
        """Get path to mock events file."""
        return os.getenv("MOCK_EVENTS_FILE", "run_20251218_115227/all_events.json")
    
    @staticmethod
    def should_cleanup_mock() -> bool:
        """Check if mock resources should be cleaned up after tests."""
        return os.getenv("CLEANUP_MOCK", "true").lower() == "true"
    
    @staticmethod
    def get_test_summary() -> Dict[str, Any]:
        """Get summary of current test configuration."""
        return {
            "mode": "MOCK" if TestConfig.is_mock_mode() else "REAL",
            "mock_timeout": TestConfig.get_mock_timeout(),
            "real_timeout": TestConfig.get_real_timeout(),
            "mock_event_delay": TestConfig.get_mock_event_delay(),
            "mock_events_file": TestConfig.get_mock_events_file(),
            "cleanup_mock": TestConfig.should_cleanup_mock(),
        }


# Environment variable documentation
ENV_VARS = {
    "USE_MOCK_LLM": {
        "description": "Enable mock LLM mode (true/false)",
        "default": "false",
        "example": "USE_MOCK_LLM=true"
    },
    "MOCK_TIMEOUT": {
        "description": "Timeout for mock tests in seconds",
        "default": "30",
        "example": "MOCK_TIMEOUT=60"
    },
    "REAL_TIMEOUT": {
        "description": "Timeout for real LLM tests in seconds", 
        "default": "480",
        "example": "REAL_TIMEOUT=600"
    },
    "MOCK_EVENT_DELAY": {
        "description": "Delay between mock events in milliseconds",
        "default": "5",
        "example": "MOCK_EVENT_DELAY=10"
    },
    "MOCK_EVENTS_FILE": {
        "description": "Path to captured events file for replay",
        "default": "run_20251218_115227/all_events.json",
        "example": "MOCK_EVENTS_FILE=run_20251218_120000/all_events.json"
    },
    "CLEANUP_MOCK": {
        "description": "Cleanup mock resources after tests (true/false)",
        "default": "true", 
        "example": "CLEANUP_MOCK=false"
    }
}


def print_test_config():
    """Print current test configuration."""
    config = TestConfig.get_test_summary()
    
    print("\n" + "="*60)
    print("TEST CONFIGURATION")
    print("="*60)
    
    for key, value in config.items():
        print(f"{key:20s}: {value}")
    
    print("\nEnvironment Variables:")
    for env_var, info in ENV_VARS.items():
        current_value = os.getenv(env_var, info["default"])
        print(f"  {env_var:20s}: {current_value} (default: {info['default']})")
    
    print("="*60)


if __name__ == "__main__":
    print_test_config()