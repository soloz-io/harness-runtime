# ADR-014: Orchestrator GitBackend — Context Injection from Git Repositories

**Date:** 2026-06-29
**Status:** Proposed

## Context

The harness-runtime executes agent DAGs inside K8s sandbox pods. The star topology uses `create_deep_agent()` for the orchestrator and `create_agent()` for specialists (ADR-011). The orchestrator's context is shaped by three components: the model, the harness loop, and injected instructions (skills, memory, rules).

Currently the orchestrator receives a static `system_prompt` string in the definition.json `nodes[].config`. There is no mechanism to inject reusable, version-controlled instruction sets (skills) from external sources. As a result:

- Domain expertise (research methodologies, review rubrics, brand guidelines) must be embedded directly in prompts at design time.
- Workflow designers cannot reference curated resource catalogs (skills, MCP server configs, agent definitions, prompts) maintained in a central repository.
- Agents cannot read or update their own skill content during execution — edits are lost across runs.
- There is no integration path from AgentRegistry, which stores all resource kinds (MCPServer, Skill, Agent, Prompt) as `Repository`-referenced artifacts (`reference/solo/agentregistry/pkg/api/v1alpha1/common.go`).

The harness-runtime has zero skills middleware wired (ADR-016 in waypoint project, proposed). Specialists must remain lean with isolated context — they receive only the `task()` delegation message (ADR-011:40).

## Decision

### 1. Orchestrator receives a GitBackend via CompositeBackend

The orchestrator is the only node that receives a `GitBackend`. Specialists are plain langchain agents (per ADR-011) and do not get skills, git access, or `SkillsMiddleware`. This preserves specialist context isolation — specialists receive only the `task()` delegation message and operate on the shared filesystem via `FilesystemMiddleware`.

### 2. Environment variables define the base repository

| Variable | Required | Description |
|---|---|---|
| `HARNESS_GIT_OWNER` | Yes | GitHub organization or user (e.g. `acme`) |
| `HARNESS_GIT_REPO` | Yes | GitHub repository name (e.g. `agent-resources`) |
| `GITHUB_TOKEN` | No | Token for private repository access (standard GitHub convention) |

The harness-runtime constructs the clone URL as:
```
https://github.com/{HARNESS_GIT_OWNER}/{HARNESS_GIT_REPO}.git
```

When `GITHUB_TOKEN` is set, the URL is adjusted to include token auth:
```
https://{GITHUB_TOKEN}@github.com/{HARNESS_GIT_OWNER}/{HARNESS_GIT_REPO}.git
```

### 3. `gitRef` field on orchestrator node

The `agent-dag-schema.json` adds a `gitRef` field to `nodes[].config`, required on orchestrator nodes:

- Type: `string`
- Description: Subfolder within the git repository containing resources (skills, configs, prompts) for this agent.
- Example: `"workflows/content-orchestrator"`
- Semantics: Maps to AgentRegistry's `Repository.Subfolder` (`reference/solo/agentregistry/pkg/api/v1alpha1/common.go:10-14`). The full `Repository` struct can be reconstructed as: `{URL: clone_url, Subfolder: gitRef, Branch: default}`.

The default branch is `main`. Definitions without `gitRef` work unchanged — no `GitBackend` is created and no `SkillsMiddleware` is wired.

### 4. GitBackend implements BackendProtocol

`GitBackend` is a new class in `harness-runtime/backends/git_backend.py` implementing `deepagents.backends.protocol.BackendProtocol`. It:

1. **At construction**: Shallow clones the repo (`git clone --depth 1 --branch main`) to a temp directory, navigates to `gitRef` subfolder.
2. **On read** (`read_file`, `ls`, `glob`, `grep`): Reads directly from the cloned working tree.
3. **On write** (`write_file`, `edit_file`): Modifies files in the working tree and records a timeline journal entry at `.timeline/{file}.yaml` with `{timestamp, agent_id, run_id, decision_context}`.
4. **On sync** (`sync()`): Calls `git add`, `git commit`, `git push` for all changes since last sync. On push conflict, reads the timeline journal and may surface decisions back to the orchestrator (not part of this ADR — deferred to future HITL integration).

### 5. Sync points

`GitBackend.sync()` is called at exactly two points in the orchestrator's execution lifecycle:

1. **Before summarization**: When `SummarizationMiddleware.wrap_model_call()` offloads conversation history to the backend. This ensures skill edits are persisted before messages are summarized and context is trimmed.
2. **Before result emission**: When the runtime is about to call `publisher.publish_result()` (`core/executor.py:492-536`) — both for HITL interrupts (`__interrupt__` detected) and final graph completion. This ensures the user sees the latest skill content before any pause or termination.

### 6. CompositeBackend wiring

The star topology builder creates a `CompositeBackend` for the orchestrator:

```python
GitBackend(
    composite=CompositeBackend(
        default=StateBackend(),
        routes={"/skills/": GitBackend(git_ref=orchestrator.config.gitRef)}
    ),
    sources=["/skills/"],
)
```

`SkillsMiddleware` discovers skills via `backend.ls("/skills/")` and lazy-loads them via `read_file` (progressive disclosure per deepagents/middleware/skills.py). The cloned repo's subfolder may contain any resource type (skills, configs, prompts) — the `CompositeBackend` presents a unified filesystem view.

## Ownership

This ADR defines architectural constraints and does not own platform resources. For resource ownership, see ADR-039.

| Resource Class | System of Record | Lifecycle Owner | Reconciler | Consumer | Phase |
|---|---|---|---|---|---|
| Orchestrator skills content | Git repository (`HARNESS_GIT_OWNER`/`HARNESS_GIT_REPO`) | Workflow designer | GitBackend (clone at init, push at sync points) | SkillsMiddleware → orchestrator LLM context | Day-0 |
| `gitRef` definition | `agent-dag-schema.json` nodes[].config | Workflow designer | Schema validation | Harness-runtime topology builder | Day-0 |
| Git auth token | Environment (`GITHUB_TOKEN`) | Platform operator | Runtime env injection | GitBackend clone auth | Day-1+ |

## Consequences

### Positive

- **Reusable resource catalogs**: Workflows reference curated repositories instead of embedding domain knowledge in prompts.
- **Context isolation preserved**: Specialists remain lean — only the orchestrator has git-backed skills.
- **Agent-editable content**: The orchestrator can update skills during execution via `write_file`/`edit_file`, with changes persisted back to git.
- **Crash safety via sync points**: Two explicit sync points (summarization and result emission) bound the window of lost edits.
- **AgentRegistry compatible**: `gitRef` maps directly to AgentRegistry's `Repository.Subfolder` pattern, enabling future registry integration.
- **Backwards compatible**: Omitted `gitRef` means no GitBackend; existing definitions work unchanged.
- **Single repo per org**: Multiple workflows share one org-level repo with different subfolders, reducing repository sprawl.

### Negative

- **Availability dependency**: The git repo must be reachable at clone time and at every sync point. Offline environments need a local mirror.
- **Auth surface**: `GITHUB_TOKEN` in environment variables is a secret management concern shared with the platform operator.
- **No push conflict resolution in v1**: On push conflict, the sync fails silently (edits stay local). HITL-based conflict resolution is deferred.
- **Clone latency**: `git clone --depth 1` adds startup latency proportional to repo size, even when only a subfolder is needed.

## Impact

This ADR does not amend any existing ADR. It extends the star topology builder defined in ADR-011 and the middleware stack composition defined in ADR-012. The `GitBackend` is a new middleware-provided tool pathway following the pattern established in ADR-010.

The `agent-dag-schema.json` (`packages/wpt-engine/schemas/agent-dag-schema.json`) must be updated to add `gitRef` as a field on `nodes[].config.properties`.

## References

- ADR-011: Topology Builder Split — Star (create_deep_agent) vs Acrylic (create_agent)
- ADR-012: Middleware Stack Composition — middleware ordering and injection
- ADR-010: Builtin Tool Architecture — middleware-provided tool pattern
- ADR-013: HITL / interrupt_on Protocol — result frame emission lifecycle
- ADR-016 (waypoint project): Agent Context Layer — Memory and Skills Management (proposed)
- `reference/solo/agentregistry/pkg/api/v1alpha1/common.go` — Repository struct (URL, Branch, Commit, Subfolder)
- `reference/solo/agentregistry/internal/cli/common/gitutil/gitutil.go` — ParseGitHubURL, CloneAndCopy
- `reference/langchain/deepagents/libs/deepagents/deepagents/backends/composite.py` — CompositeBackend routing
- `reference/langchain/deepagents/libs/deepagents/deepagents/middleware/skills.py` — SkillsMiddleware
- `reference/langchain/deepagents/libs/deepagents/deepagents/middleware/summarization.py` — SummarizationMiddleware sync point
- `core/executor.py` — `publish_result()` at lines 492, 525, 536 (result emission sync point)
