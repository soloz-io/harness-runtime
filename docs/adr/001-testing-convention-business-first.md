# ADR-001: Business-First Integration Testing Convention

**Date:** 2026-06-10
**Status:** Proposed

---

## Context

No testing convention has been codified as an ADR. As the Waypoint platform grows across multiple packages (waypoint-sdk, bff, frontend, wpt-engine, harness-runtime), inconsistent testing approaches create gaps in coverage and erode confidence in production behavior.

Without a binding convention, tests drift toward one of two failure modes:
- **Unit tests** that validate implementation details rather than business outcomes — they pass after refactoring but don't guarantee the system works.
- **Integration tests with fallback patterns** that catch errors silently — they appear to pass while the assertion effectively tests nothing.

A formal convention is needed to ensure every test validates real production behavior through real code paths, and every assertion maps to a business outcome a user or operator cares about.

---

## Decision

### 1. Same Code Paths as Production (Mandatory)

Tests must use the exact same service classes, dependency injection containers, database clients, HTTP servers, and business logic as production. No test-only abstractions that re-implement or wrap production logic. This ensures maximum code coverage and validates actual production behavior.

This includes running the HTTP server as a real subprocess. Tests must NOT start the server in-process (e.g., via `createApp()` + `serve()` in the test's `beforeAll`). Instead, the server runs as a separate child process, and tests interact with it via HTTP — the same way the BFF and other consumers do. This guarantees that networking, request routing, middleware, error handling, and worker queue processing (e.g., Graphile Worker) execute identically to production.

### 2. No Unit Tests — Integration Only

All tests must be integration tests that exercise the full production stack. Isolated tests of pure functions are prohibited. The platform's value is in runtime behavior — workflow execution, state persistence, event streaming, integration dispatch — and only integration tests validate these. The builder must reject any contribution that introduces a new unit test.

### 3. Business-Journey Assertions Only (Mandatory)

Every assertion in every test must validate a **business outcome** that a user or operator cares about. No assertions on technical internals.

**Business-journey assertions (allowed):**
- "The workflow reached the expected terminal state"
- "The candidate received an offer notification"
- "The rejection email was sent"
- "The run completed within 30 seconds"
- "The operator can see the run status in the UI"

**Technical assertions (prohibited):**
- "The database query returned exactly 3 rows from the workflow_events table" (tests an implementation detail)
- "The HTTP response status code was 200" (tests transport, not business value)
- "The function was called with specific arguments" (tests internals)
- "The array length was exactly 5" (tests structure, not outcome)

A test that only has technical assertions must be rejected by the builder. If a technical failure prevents a business outcome (e.g., a database query fails), the test must assert the resulting **business impact** (e.g., "the run status is failed") — not the technical cause.

### 4. No Fallbacks — Fail Hard

Tests must not contain `try/catch` blocks or conditional assertion paths. Every assertion must fail the test immediately if unmet. Soft assertions, logging failures and continuing, or conditional test paths are prohibited. Tests must never be skipped.

### 5. No Business Logic in Tests

Test files must contain only:
- Setup (fixture loading, infrastructure initialization, environment configuration)
- Assertions (`expect` calls against business outcomes)
- Orchestration (calling production entry points)

Tests must not re-implement or duplicate any production business logic.

### 6. Code Coverage Enforced

Every test run must produce a coverage report with defined thresholds. Coverage is measured against branches, functions, lines, and statements. The CI pipeline gates on these thresholds.

### 7. External Server Over In-Process (Mandatory)

Tests must connect to a server running as a separate process. The test process itself must never call HTTP server constructors or listeners directly. Rationale:
- **Real networking:** HTTP request/response cycle, connection pooling, and error handling execute identically to production
- **Worker queue processing:** Background workers (e.g., Graphile Worker for workflow jobs) run in the same process as the server, not alongside the test runner
- **Graceful shutdown:** Signal handling (SIGTERM, SIGINT) and cleanup hooks are exercised under real conditions
- **Resource isolation:** A crash or hang in the server process does not abort the test runner

The server must be a child process of the test orchestrator, not the test file. Test files must not import `createApp()`, `serve()`, `getWorld()`, or any server-initialization symbols.

### 8. Trigger Through Production API Paths (Mandatory)

Tests must invoke the same HTTP API endpoints that production consumers (BFF, frontend, webhook callers) use. For workflow execution, this means:
- `POST /internal/runs/start` to create and dispatch a workflow run
- `GET /internal/sdk/runs/{id}/status` (or equivalent bridge endpoint) to poll for completion

Tests must not call internal library functions like `start()` from `@workflow/core/runtime` directly — those bypass middleware, validation, worker queuing, and error handling.

If a production API path does not exist for a required action, the missing endpoint must be built as production infrastructure, not as a test-only bypass.

### 9. Single-Command Lifecycle Orchestration

A single entry point script must manage the full test lifecycle: infrastructure provisioning (database containers), server startup (subprocess spawn + health check), test execution (vitest with optional path argument), and teardown (graceful shutdown + container cleanup).

The script must:
- Accept a test path as an optional argument; default to the full integration test suite
- Kill any process occupying the server port before starting
- Start only required infrastructure services (e.g., PostgreSQL, not agent-vault or Redis if unused)
- Wait for server readiness via a health endpoint before launching tests
- Forward the test exit code
- Clean up all resources in a `finally` block — kill server, stop containers

### 10. Clean Shutdown Responsibility

The orchestrator script owns the server process lifecycle with these guarantees:
- **Spawn:** Server starts as a child process with inherited environment variables
- **Health check:** Poll `/health` endpoint until ready or timeout (max 60s); abort on timeout
- **Graceful shutdown:** Send `SIGTERM` to the child process after tests complete
- **Force kill:** Send `SIGKILL` if the process does not exit within 5 seconds of `SIGTERM`
- **Container cleanup:** Run `docker compose down` after server is stopped
- **Signal forwarding:** Intercept `SIGINT`/`SIGTERM` from the terminal to trigger cleanup before exit

---

## Ownership

This ADR defines testing conventions and does not own platform resources. For resource ownership, see ADR-039.

---

## Consequences

### Positive

- Every deployed code path is exercised under real conditions before release
- No divergence between test behavior and production behavior
- Refactoring confidence — a change that passes integration tests is production-safe
- Coverage thresholds prevent untested code from reaching production
- Business-focused assertions keep tests readable and valuable to product owners

### Negative

- Slower test execution compared to isolated unit tests (mitigated by parallel execution and partitioned test suites)
- Heavier infrastructure requirements (real PostgreSQL, real server subprocess per test suite)
- Debugging integration failures requires understanding the full stack

---

## Impact

Each package must adopt this ADR:
- Existing test suites must be audited for prohibited patterns (unit tests, business logic in tests, fallback blocks, technical assertions, in-process server startup, direct library calls instead of HTTP) and corrected
- Coverage configuration must be added to each package's Vitest config with thresholds matching this ADR's minima
- The frontend package must introduce an integration test framework
- E2E Playwright specs must be written covering the primary user journeys
- Each package must provide a single-command test orchestrator script following Section 9

---

## References

- ADR-001: Waypoint Shared SaaS Platform Topology
- ADR-002: Business-First State-Transition Abstraction — all tests assert business outcomes defined by this ADR's abstraction
- ADR-003: Harness Runtime Architecture — Two Topology Builders
