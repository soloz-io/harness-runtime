"""Test utilities for harness-runtime."""

# No mock LLM utilities — LiteLLM parity:
# Protocol tests use fake_server.py (pure stdlib echo server).
# DB integration tests use real LLM with real API keys.
