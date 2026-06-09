# LLM Mocking for deepagents-runtime Tests

This directory contains utilities for mocking LLM calls in tests using event replay from captured real workflow executions.

## Overview

The mock system replays actual events from successful workflow runs, ensuring 100% fidelity to real execution while being fast and deterministic.

## Usage

### Environment Variables

Set `USE_MOCK_LLM=true` to enable mock mode:

```bash
# Run tests with mock LLM (fast, no API costs)
USE_MOCK_LLM=true pytest tests/integration/test_agent_generation_workflow.py

# Run tests with real LLM (comprehensive, requires API keys)
USE_MOCK_LLM=false pytest tests/integration/test_agent_generation_workflow.py
```

### Configuration Options

| Variable | Default | Description |
|----------|---------|-------------|
| `USE_MOCK_LLM` | `false` | Enable mock LLM mode |
| `MOCK_TIMEOUT` | `30` | Timeout for mock tests (seconds) |
| `REAL_TIMEOUT` | `480` | Timeout for real LLM tests (seconds) |
| `MOCK_EVENT_DELAY` | `5` | Delay between events (milliseconds) |
| `MOCK_EVENTS_FILE` | `run_20251218_115227/all_events.json` | Events file to replay |

## How It Works

1. **Event Capture**: Real workflow runs capture all events to JSON files
2. **Event Replay**: Mock mode replays these events to Redis channels
3. **Same Validation**: Identical test logic validates both mock and real runs
4. **Fast Execution**: Mock tests complete in ~30 seconds vs 8 minutes for real

## Files

- `mock_workflow.py` - Main mock implementation with event replay
- `test_config.py` - Configuration management utilities
- `__init__.py` - Package exports

## Benefits

- ✅ **100% Real Events** - Uses actual captured workflow events
- ✅ **Same Test Code** - No duplication, just environment switch
- ✅ **Fast PR Feedback** - Mock tests run in seconds
- ✅ **Zero API Costs** - No LLM API calls in mock mode
- ✅ **Deterministic** - Same results every time
- ✅ **Easy Debugging** - Real event structure for troubleshooting

## GitHub Workflows

- **Pull Requests**: Use mock LLM for fast feedback
- **Main Branch**: Use real LLM for comprehensive validation
- **Manual Trigger**: Choose mock or real mode

## Updating Mock Events

To update mock events with new workflow captures:

1. Run a successful test with real LLM
2. Copy the generated events file from `tests/integration/outputs/run_*/all_events.json`
3. Update `MOCK_EVENTS_FILE` environment variable to point to new file

## Troubleshooting

### Mock Tests Failing

1. Check if events file exists: `tests/integration/outputs/run_20251218_115227/all_events.json`
2. Verify Redis is running and accessible
3. Check timeout settings for mock mode

### Real Tests Failing

1. Verify API keys are set: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`
2. Check network connectivity to LLM providers
3. Increase timeout if needed: `REAL_TIMEOUT=600`

### Event Replay Issues

1. Check Redis pub/sub is working
2. Verify job_id replacement in events
3. Monitor Redis channels: `redis-cli MONITOR`