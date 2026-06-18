# ADR-002: Test Plan Convention

**Date:** 2026-06-18
**Status:** Proposed

Every feature test plan follows this template:

```markdown
# <Feature> — Test Plan

## Business Journeys

| ID | Journey | Assertion | Status |
|----|---------|-----------|--------|
| A1 | ... | ... | ✅ |

## Setup

- ...

## Known Issues

- ...
```

**Rules:**
- One ADR per feature, journeys map to `test_*` functions
- Status: `✅ Passes` / `⚠️ Flaky` / `❌ Failing` / `🟡 Not implemented`
- Tests follow ADR-001 (business-first, fail-hard, same paths as production)
