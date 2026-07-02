# ADR-014: Orchestrator GitBackend — Context Injection from Git Repositories

**Date:** 2026-06-29
**Status:** Proposed

## Context

The harness-runtime executes agent DAGs inside K8s sandbox pods. The star topology uses `create_deep_agent()` for the orchestrator and `create_agent()` for specialists (ADR-011). The orchestrator's context is shaped by three components: the model, the harness loop, and injected instructions (skills, memory, rules).

Currently the orchestrator receives a static `system_prompt` string in the definition.json `nodes[].config`. There is no mechanism to inject reusable, version-controlled instruction sets (skills) from external sources. As a result:

- Domain expertise (research methodologies, review rubrics, brand guidelines) must be embedded directly in prompts at design time.
- Workflow designers cannot reference curated resource catalogs (skills, MCP server configs, agent definitions, prompts) maintained in a central repository.
- There is no integration path from AgentRegistry, which stores all resource kinds (MCPServer, Skill, Agent, Prompt) as `Repository`-referenced artifacts (`reference/solo/agentregistry/pkg/api/v1alpha1/common.go`).

The harness-runtime has zero skills middleware wired (ADR-016 in waypoint project, proposed). Specialists must remain lean with isolated context — they receive only the `task()` delegation message (ADR-011:40).

## Decision

### 1. Orchestrator receives git-backed skills via CompositeBackend

The orchestrator is the only node that receives git-backed skills. Specialists are plain langchain agents (per ADR-011) and do not get skills, git access, or `SkillsMiddleware`. This preserves specialist context isolation — specialists receive only the `task()` delegation message and operate on the shared filesystem via `FilesystemMiddleware`.

### 2. Environment variables define the base repository

| Variable | Required | Description |
|---|---|---|
| `AGENTREGISTRY_GIT_OWNER` | Yes | GitHub organization or user (e.g. `acme`) |
| `AGENTREGISTRY_GIT_REPO` | Yes | GitHub repository name (e.g. `agent-resources`) |
| `AGENTREGISTRY_GITHUB_TOKEN` | No | Token for private repository access (standard GitHub convention) |

The harness-runtime constructs the clone URL as:

```
https://github.com/{AGENTREGISTRY_GIT_OWNER}/{AGENTREGISTRY_GIT_REPO}.git
```

When `AGENTREGISTRY_GITHUB_TOKEN` is set, the URL is adjusted to include token auth:

```
https://{AGENTREGISTRY_GITHUB_TOKEN}@github.com/{AGENTREGISTRY_GIT_OWNER}/{AGENTREGISTRY_GIT_REPO}.git
```

### 3. `gitRef` field on orchestrator node

The `agent-dag-schema.json` adds a `gitRef` field to `nodes[].config`, required on orchestrator nodes:

- Type: `string`
- Description: Subfolder within the git repository containing resources (skills, configs, prompts) for this agent.
- Example: `"workflows/content-orchestrator"`
- Semantics: Maps to AgentRegistry's `Repository.Subfolder` (`reference/solo/agentregistry/pkg/api/v1alpha1/common.go:10-14`). The full `Repository` struct can be reconstructed as: `{URL: clone_url, Subfolder: gitRef, Branch: default}`.

The default branch is `main`. Definitions without `gitRef` work unchanged — no `GitBackend` is created and no `SkillsMiddleware` is wired.

### 4. GitBackend — pure cloner, no BackendProtocol

`GitBackend` is a new class in `harness-runtime/backends/git_backend.py`. It does **not** implement `BackendProtocol`. Its sole responsibility is cloning:

1. **At construction**: Shallow clones the repo (`git clone --depth 1 --branch main`) to a temp directory, navigates to `gitRef` subfolder, exposes the local path via `self.path`.
2. **No other methods**: Read/write/list/grep/glob operations are handled by deepagents built-in `FilesystemBackend` and `SkillsMiddleware` on the local filesystem. `GitBackend.cleanup()` removes the temp directory.

### 5. CompositeBackend wiring

The star topology builder creates a `CompositeBackend` for the orchestrator:

```python
cloner = GitBackend(git_ref)
skills_backend = FilesystemBackend(
    root_dir=str(cloner.path), virtual_mode=True,
)
composite_backend = CompositeBackend(
    default=StateBackend(),
    routes={"/skills/": skills_backend},
)

create_deep_agent(
    ...,
    backend=composite_backend,
    skills=["/skills/"],
)
```

`SkillsMiddleware` (auto-wired by `create_deep_agent` when both `backend` and `skills` are provided) discovers skills via `backend.ls("/skills/")` and lazy-loads them via `backend.read()` (progressive disclosure per deepagents/middleware/skills.py). The cloned repo's subfolder may contain any resource type (skills, configs, prompts) — the `CompositeBackend` presents a unified filesystem view with `/skills/` routed to the cloned directory and all other paths served by `StateBackend`.

All file operations use deepagents built-in tools (`FilesystemMiddleware`'s `read_file`, `ls_file`, etc.) operating on the `FilesystemBackend`. `GitBackend` itself has no runtime role after construction.

## Ownership

This ADR defines architectural constraints and does not own platform resources. For resource ownership, see ADR-039.

| Resource Class | System of Record | Lifecycle Owner | Reconciler | Consumer | Phase |
|---|---|---|---|---|---|
| Orchestrator skills content | Git repository (`AGENTREGISTRY_GIT_OWNER`/`AGENTREGISTRY_GIT_REPO`) | Workflow designer | GitBackend (clone at init only) | SkillsMiddleware → orchestrator LLM context | Day-0 |
| `gitRef` definition | `agent-dag-schema.json` nodes[].config | Workflow designer | Schema validation | Harness-runtime topology builder | Day-0 |
| Git auth token | Environment (`AGENTREGISTRY_GITHUB_TOKEN`) | Platform operator | Runtime env injection | GitBackend clone auth | Day-1+ |

## Consequences

### Positive

- **Reusable resource catalogs**: Workflows reference curated repositories instead of embedding domain knowledge in prompts.
- **Context isolation preserved**: Specialists remain lean — only the orchestrator has git-backed skills.
- **Read-only, zero overhead**: No sync, no write-back, no conflict resolution. GitBacked skills are injected at startup and served via existing deepagents built-ins.
- **AgentRegistry compatible**: `gitRef` maps directly to AgentRegistry's `Repository.Subfolder` pattern, enabling future registry integration.
- **Backwards compatible**: Omitted `gitRef` means no GitBackend; existing definitions work unchanged.
- **Single repo per org**: Multiple workflows share one org-level repo with different subfolders, reducing repository sprawl.

### Negative

- **Availability dependency**: The git repo must be reachable at clone time. Offline environments need a local mirror.
- **Auth surface**: `GITHUB_TOKEN` in environment variables is a secret management concern shared with the platform operator.
- **Clone latency**: `git clone --depth 1` adds startup latency proportional to repo size, even when only a subfolder is needed.
- **No sync** in v1: Skill edits during a run are not persisted back to git (no `write_file` to `/skills/`). Future work may add HITL-based conflict resolution.

## Impact

This ADR does not amend any existing ADR. It extends the star topology builder defined in ADR-011 and the middleware stack composition defined in ADR-012. The `GitBackend` is a new utility class consumed at graph-build time by `star_topology.py`.

The `agent-dag-schema.json` (`packages/wpt-engine/schemas/agent-dag-schema.json`) must be updated to add `gitRef` as a field on `nodes[].config.properties`.

## References

- ADR-011: Topology Builder Split — Star (create_deep_agent) vs Acrylic (create_agent)
- ADR-012: Middleware Stack Composition — middleware ordering and injection
- ADR-016 (waypoint project): Agent Context Layer — Memory and Skills Management (proposed)
- `backends/git_backend.py`: `GitBackend` — pure cloner
- `core/star_topology.py`: CompositeBackend + FilesystemBackend wiring for git-backed skills
- `deepagents/backends/composite.py`: CompositeBackend routing
- `deepagents/backends/filesystem.py`: FilesystemBackend with virtual_mode
- `deepagents/middleware/skills.py`: SkillsMiddleware discovery and lazy-loading
- `reference/solo/agentregistry/pkg/api/v1alpha1/common.go` — Repository struct (URL, Branch, Commit, Subfolder)
