"""
Centralized In-Cluster Test Configuration

This module provides standardized configuration for all integration tests
running in Kubernetes clusters. It automatically detects the environment
and configures services to use in-cluster DNS names.

Usage:
    # In any test conftest.py, import this before other imports:
    from tests.integration.in_cluster_conftest import setup_in_cluster_environment
    
    # Call setup at module level (before pytest fixtures)
    setup_in_cluster_environment()

Features:
- Automatic in-cluster service discovery
- Standardized environment variable naming
- Fallback to localhost for local development
- Debug logging for troubleshooting
"""

import os
from typing import Dict, Optional

# ==============================================================================
# IN-CLUSTER SERVICE CONFIGURATION
# ==============================================================================

# Standard service DNS names in Kubernetes
IN_CLUSTER_SERVICES = {
    "postgres": {
        "host": "deepagents-runtime-db-rw",
        "port": "5432",
        "fallback_host": "localhost",
        "fallback_port": "15433"
    },
    "redis": {
        "host": "deepagents-runtime-cache", 
        "port": "6379",
        "fallback_host": "localhost",
        "fallback_port": "16380"
    },
    "nats": {
        "url": "nats://nats.nats.svc:4222",
        "fallback_url": "nats://localhost:14222"
    }
}

# Environment variable mappings
ENV_VAR_MAPPINGS = {
    # Test-specific variables (used by conftest.py files)
    "TEST_POSTGRES_HOST": ("postgres", "host"),
    "TEST_POSTGRES_PORT": ("postgres", "port"),
    "TEST_REDIS_HOST": ("redis", "host"),
    "TEST_REDIS_PORT": ("redis", "port"),
    "TEST_NATS_URL": ("nats", "url"),
    
    # Application variables (used by the app itself)
    "POSTGRES_HOST": ("postgres", "host"),
    "POSTGRES_PORT": ("postgres", "port"),
    "DRAGONFLY_HOST": ("redis", "host"),
    "DRAGONFLY_PORT": ("redis", "port"),
    "NATS_URL": ("nats", "url"),
}

def is_running_in_cluster() -> bool:
    """
    Detect if we're running inside a Kubernetes cluster.
    
    Returns:
        bool: True if running in cluster, False otherwise
    """
    # Check for Kubernetes service account token
    if os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token"):
        return True
    
    # Check for in-cluster environment variables
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return True
        
    # Check if we're using in-cluster DNS names
    postgres_host = os.environ.get("POSTGRES_HOST", "")
    if "deepagents-runtime-db" in postgres_host:
        return True
        
    return False

def get_service_config(service: str, key: str, use_fallback: bool = False) -> str:
    """
    Get configuration for a service.
    
    Args:
        service: Service name (postgres, redis, nats)
        key: Configuration key (host, port, url)
        use_fallback: Whether to use fallback (localhost) values
        
    Returns:
        str: Configuration value
    """
    service_config = IN_CLUSTER_SERVICES.get(service, {})
    
    if use_fallback:
        fallback_key = f"fallback_{key}"
        return service_config.get(fallback_key, service_config.get(key, ""))
    else:
        return service_config.get(key, "")

def setup_in_cluster_environment(force_in_cluster: Optional[bool] = None) -> Dict[str, str]:
    """
    Set up environment variables for in-cluster or local testing.
    
    Args:
        force_in_cluster: Force in-cluster mode (None = auto-detect)
        
    Returns:
        Dict[str, str]: Environment variables that were set
    """
    # Determine if we should use in-cluster configuration
    if force_in_cluster is not None:
        use_in_cluster = force_in_cluster
    else:
        use_in_cluster = is_running_in_cluster()
    
    # Set environment variables
    env_vars_set = {}
    
    for env_var, (service, key) in ENV_VAR_MAPPINGS.items():
        # Skip if already set (don't override existing values)
        if os.environ.get(env_var):
            continue
            
        # Get the appropriate value
        value = get_service_config(service, key, use_fallback=not use_in_cluster)
        
        if value:
            os.environ[env_var] = value
            env_vars_set[env_var] = value
    
    # Set additional standard variables
    additional_vars = {
        "POSTGRES_SCHEMA": "public",
        "USE_MOCK_LLM": "true",
        "MOCK_TIMEOUT": "60"
    }
    
    for var, default_value in additional_vars.items():
        if not os.environ.get(var):
            os.environ[var] = default_value
            env_vars_set[var] = default_value
    
    # Print configuration for debugging
    print("\n" + "=" * 80)
    print("IN-CLUSTER TEST ENVIRONMENT CONFIGURATION")
    print("=" * 80)
    print(f"Environment: {'In-Cluster' if use_in_cluster else 'Local Development'}")
    print(f"Auto-detected: {is_running_in_cluster()}")
    
    # Group by service for better readability
    services_config = {}
    for env_var, value in env_vars_set.items():
        if "POSTGRES" in env_var or "DB" in env_var:
            services_config.setdefault("PostgreSQL", {})[env_var] = value
        elif "REDIS" in env_var or "DRAGONFLY" in env_var:
            services_config.setdefault("Redis/Dragonfly", {})[env_var] = value
        elif "NATS" in env_var:
            services_config.setdefault("NATS", {})[env_var] = value
        else:
            services_config.setdefault("Other", {})[env_var] = value
    
    for service_name, config in services_config.items():
        print(f"\n{service_name}:")
        for var, value in config.items():
            # Mask passwords
            display_value = "*" * len(value) if "PASSWORD" in var and value else value
            print(f"  {var}: {display_value}")
    
    print("=" * 80 + "\n")
    
    return env_vars_set

def get_database_url() -> str:
    """Get the database URL for the current environment."""
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "test_user")
    password = os.environ.get("POSTGRES_PASSWORD", "test_pass")
    database = os.environ.get("POSTGRES_DB", "test_db")
    
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"

def get_redis_url() -> str:
    """Get the Redis URL for the current environment."""
    host = os.environ.get("DRAGONFLY_HOST", "localhost")
    port = os.environ.get("DRAGONFLY_PORT", "6379")
    password = os.environ.get("DRAGONFLY_PASSWORD", "")
    
    if password:
        return f"redis://:{password}@{host}:{port}/0"
    else:
        return f"redis://{host}:{port}/0"

def get_nats_url() -> str:
    """Get the NATS URL for the current environment."""
    return os.environ.get("NATS_URL", "nats://localhost:4222")

# ==============================================================================
# CONVENIENCE FUNCTIONS FOR TESTS
# ==============================================================================

def require_service(service_name: str) -> None:
    """
    Ensure a service is available, skip test if not.
    
    Args:
        service_name: Name of the service (postgres, redis, nats)
    """
    import pytest
    
    if service_name == "postgres":
        try:
            import psycopg
            conn = psycopg.connect(get_database_url(), connect_timeout=5)
            conn.close()
        except Exception as e:
            pytest.skip(f"PostgreSQL not available: {e}")
    
    elif service_name == "redis":
        try:
            import redis
            client = redis.from_url(get_redis_url(), socket_connect_timeout=5)
            client.ping()
        except Exception as e:
            pytest.skip(f"Redis not available: {e}")
    
    elif service_name == "nats":
        try:
            import asyncio
            import nats
            
            async def check_nats():
                nc = await nats.connect(get_nats_url(), connect_timeout=5)
                await nc.close()
            
            asyncio.run(check_nats())
        except Exception as e:
            pytest.skip(f"NATS not available: {e}")

# ==============================================================================
# AUTO-SETUP (called when module is imported)
# ==============================================================================

# Note: Auto-setup disabled to allow Kubernetes secret environment variables
# to take precedence. Tests should call setup_in_cluster_environment() explicitly
# if needed for local development.
# setup_in_cluster_environment()