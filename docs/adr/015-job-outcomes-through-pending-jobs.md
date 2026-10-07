# ADR-015: Job Outcomes Reach the Agent Through Its Own Pending Jobs

**Date:** 2026-10-07
**Status:** Accepted

```
  AGENT (main or subagent)                    JOB (workflow run)
  ────────────────────────                    ──────────────────
  tool: job CLI ──── runs/start ────────────► workflow_runs row
       │             ◄── {status: QUEUED,           status: running
       │                  run_id}                    │
       ▼                                             │
  job-tracking (wrap_tool_call)                      │
    pending_jobs += run_id                           │
    (subagent: returned to main                      │
     agent with its state)                           │
       │                                             ▼
       │                                   record-outcome step (own node)
       │                                     write outcome on own run
       │                                     (message, url, pass, ...)
       │                                             │
       │                                   notify step
       │                                     send-message ──────► harness
       │                                                          │
       │                                   run status: completed  │
       │                                                          ▼
       │                                         writes chat row (UI only)
       │                                         wake-up check (turn lock):
       │                                           no run in progress
       │                                           AND no pending interrupt
       │                                           AND a pending job finished
       │                                            │ yes            │ no
       │                                            ▼                ▼
       │                                   wake-up run      nothing to do
       │                                   (no messages,    (reported at
       │                                    external_event)  next model call)
       │                                            │
       ▼                                            ▼
  MAIN AGENT, before every model call ◄─────────────┘
  job-reporting (before_model)
    pending_jobs empty? ── yes ──► model call
       │ no
       ▼
    look up those run ids in workflow_runs
    finished (outcome recorded, failed or cancelled)?
       │ yes                                   │ no
       ▼                                       ▼
    append "[System Notification] ..."      stays pending
    pending_jobs -= run_id
    (one checkpoint)
       │
       ▼
    model call

  Paused on ask_user: nothing runs. User answers ──► Command(resume=answer)
  ──► tool returns answer ──► before_model reports finished jobs ──► model call

  Wake-up check also runs: when a run ends, and when a session is first built
  in a harness process.
```

## Context

Agents start background jobs: media generation, renders, processing. A job CLI
starts one through the SDK's `runs/start`, gets back the run's id, and prints it
in its result. The job then runs as a workflow run (`workflow_runs`). It reports
itself to the chat with notices it sends when it starts, completes and fails
(ADR-013, "Background jobs"). The agent has to act on a job's outcome: present a
result, retry, or move to the next step.

Three ways of getting the outcome to the agent have been tried, and each one
broke:

1. **Each notice ran as a user turn in the harness.** A notice that arrived
   while `ask_user` was pending was taken as the user's answer
   (`Command(resume=...)`), or it started a fresh run, and deepagents'
   `PatchToolCallsMiddleware` then reported the open question as cancelled.
2. **The harness held notices and the chat replayed them.** Rows were duplicated
   on every page load, and a replayed notice hid the open approval card.
3. **The chat delivered notices (ADR-013, "System notices are outside the graph").**
   This is correct for interrupts, but the agent hears about a job only while a
   browser tab is open, which rules out autonomous runs. A notice that arrives
   during a question is delivered one turn after the answer, two tabs can both
   deliver it, and a tab with a stale view decides when the agent is told.

All three push an event at the agent. Each push has to be timed against the
graph's state, and has to survive a crash while it's in transit.

Facts the decision rests on:

- **Resuming is only for the user's answer.** A paused graph continues only
  through `Command(resume=...)`, and the resume value becomes the tool's
  result. Any other new input either answers the question or abandons it,
  whatever its role.
- **The agent already knows which jobs it started.** Each job CLI's result
  carries the `run_id` that `runs/start` returned.
- **Job status is already stored.** `workflow_runs` holds every run's status
  (running, completed, failed, cancelled) and error. It does not store the
  outcome message, url or `pass`: the job's final step computes those and sends
  them only as a notice.
- **A run's final status is written after its last step.** The run is marked
  completed or failed only after the notify step has sent its notice
  (`finalizeWorkflowRun`).
- **Graph state is in the database.** The harness uses LangGraph's Postgres
  checkpointer, so every state field is saved with each step's checkpoint and
  loaded at the start of each run.
- **LangChain middleware has the hooks needed.** `before_model` is its own graph
  node, run before every model call, and its update commits as that node's
  checkpoint. `wrap_tool_call` runs around every tool call and can return a
  state update along with the tool's message. A middleware can declare its own
  state fields.
- **deepagents passes state to subagents and back.** A subagent starts with the
  main agent's state, apart from `messages`, `todos` and `structured_response`.
  When it finishes, its state, apart from those same keys, is returned to the
  main agent as an update. Job CLIs are run by subagents.
- **History is not a record of what the agent knows.** The harness runs
  deepagents' `SummarizationMiddleware`, so on a long run old messages are
  summarised away.

## Decision

### The agent pulls the outcomes of jobs it started

The agent's graph state has a field, `pending_jobs`: the run ids of the jobs it
has started and not yet been told the outcome of. The checkpointer saves it with
every step, so it survives harness restarts and sandbox loss.

**Nothing is pushed to the agent.** Before each model call, the main agent
checks its own pending jobs and is told about the ones that have finished. A job
outcome is never user input, never a resume value, and never starts a run on a
paused graph.

### Who adds an id: job tracking on every agent

A **job-tracking middleware** runs on the main agent and on every subagent. It
declares `pending_jobs` and wraps every tool call. When a tool's result reports
a started job, it returns the tool's message together with an update that adds
that run id.

**The platform contract:** a tool result reports a started job when it is a JSON
object with `status: "QUEUED"` and a non-empty `run_id`, the id `runs/start`
returned. Job CLIs already print this shape.

**An id added in a subagent reaches the main agent through deepagents' own state
passing.** The subagent returns its state when it finishes, and its
`pending_jobs` comes back with it.

**The field merges rather than being overwritten,** because subagents running in
parallel each return their own list in the same step:

- A list (from a tool result, or from a subagent returning) is unioned with the
  current ids. A subagent that echoes back the ids it was given changes nothing.
- A removal takes ids out.

### Who removes an id: job reporting on the main agent only

A **job-reporting middleware** runs on the main agent only. Its `before_model`
step:

1. reads `pending_jobs` from state, and does nothing if it is empty;
2. looks those runs up in `workflow_runs` by id;
3. for each **finished** run, returns both a notification message with the
   outcome and a removal of that run's id.

Both are committed in that node's checkpoint. Either the agent was told and the
id is gone, or neither happened and the id is still pending.

**A run counts as finished** when its outcome is recorded (below), or its status
is failed or cancelled. Finished does not depend on status alone, because the
completed status is written after the notify step, and a wake-up caused by that
step can reach the agent first.

**What the agent is told:**

- a recorded outcome: the outcome's message, url, media type and `pass`;
- a failed run with no outcome: the run's error;
- a cancelled run: that it was cancelled;
- a run id that no longer exists: that the job can't be found. The id is still
  removed.

A job still running stays pending.

**A removed id cannot come back.** `before_model` runs only between steps, never
while a subagent runs inside a tool step, so a subagent can never return a copy
of the list that still holds an id removed in the meantime.

**Subagents never report or remove jobs.** A job a subagent started is reported
to the main agent, which decides who acts on it (the `pass` field).

### The run records its own outcome

`workflow_runs` gets an **outcome** field. Each job workflow has a
**record-outcome step** (`system/record-job-outcome`), a node of its own placed
directly before the step that sends the completed or failed notice. It writes
the outcome onto its own run: message, url, media type, `pass`, and for a
failure the error and title. It is a separate step rather than part of the
notice step, so the outcome is on the run before anything can wake the agent to
read it. The run's own row is the only system of record for a job's outcome.

### Reporting happens before the next model call

| Graph state when a job finishes | When the agent is told |
|---|---|
| **Running** | At its next model call in that run. |
| **Paused on an interrupt** | After the user answers. `Command(resume=...)` continues the run, the tool returns the answer, and the next model call reports the job. The agent gets the answer and the outcome in the same step. |
| **Idle** (no run, no pending interrupt) | The harness starts a wake-up run (below). |

### Waking an idle agent

The job's completed or failed notice still goes to the harness through
`send-message`. The harness writes the chat's display row, as today, and then
runs a **wake-up check** under the session's turn lock:

- if no run is in progress,
- and no interrupt is pending,
- and a run in `pending_jobs` is finished,

it starts a wake-up run.

The same check runs at two other points:

- **when a run ends,** for a job that finished after that run's last model call;
- **when the session is first built in a harness process,** for a job that
  finished while no harness was running. The first request to a new sandbox
  builds the session.

**A wake-up run is an internal invocation, not a message.**

- Its input has no messages: no synthetic user message, no notice text and no
  resume value.
- Its run reason is `external_event`, recorded with the run's configuration and
  in the harness's logs, so traces and evals can tell it apart from a user turn.
- It starts at the agent's entry node, the first `before_model` hook, so the
  job-reporting middleware reports before the first model call.
- The check only starts a run when there is something to report, so a wake-up
  run never calls the model with nothing new.

**The notice wakes the agent, but delivers nothing.** If it is lost (the sandbox
is down, or a request fails), the job is still in `pending_jobs` and its outcome
is still on its run. The agent is told at its next model call, whatever starts
it: a user message, an answer, another job's notice, or a new sandbox for the
session. No retry, hook or acknowledgement is needed, because nothing is in
transit.

### Delivered outcomes are part of the transcript

A reported outcome is appended to the agent's messages as a notification
message, tagged with its run id. Its text has the form
`[System Notification] <message>`, followed by its fields, which is the form the
agents' instructions already describe. The model API needs it in the user
position, but no harness path treats it as human input. The chat's history
writer does not display it, because the chat already shows the job's notice row.

`pending_jobs`, not the messages, records what the agent has been told, so
summarising history cannot make a job look unreported.

### Delivery guarantee

**At-least-once processing.**

- **The report step committed:** the outcome is in the agent's state and the id
  is gone, so the job is never reported again.
- **The report step not committed:** the id is still pending, so the job is
  reported at the next model call.
- **A model step or tool step crashes before committing:** it runs again, so the
  model can act on an outcome more than once.

Actions an outcome triggers must therefore be idempotent, keyed by the run id
where the action has a durable effect, such as re-queuing a job.

### Started notices stay display-only

A `job/started` notice is written to the chat for its task list and does
nothing else. The tool that started the job has already told the agent, and its
result added the run id to `pending_jobs`.

### Alternatives considered

- **A session event inbox, written by the harness when it receives a notice.**
  Rejected because a notice in transit can be lost, and covering that needed a
  per-session sequence with a row lock, compaction, a workflow hook and retries.
- **Querying `workflow_runs` by session.** Rejected because it returns every
  past run of the session. The agent needs only the jobs it started and hasn't
  been told about.
- **The chat delivers notices (ADR-013, 2026-10-07).** Superseded for the
  reasons in Context, design 3.
- **Pushing outcomes into state with `update_state` while paused.** Rejected
  because it forks the checkpoint, can drop the pending interrupt, and conflicts
  with a running graph.
- **Outcomes as `ToolMessage`s.** Rejected because the only open tool call is
  the question, so the outcome would become its answer.

## Ownership

This ADR defines an execution protocol and does not own platform resources
outside the session's harness and a job's own run. For resource ownership, see
ADR-039.

| Resource Class | System of Record | Lifecycle Owner | Reconciler | Consumer | Phase |
|---|---|---|---|---|---|
| A job's status and outcome | `workflow_runs` (status, error, outcome) | The job's workflow run | None | Job-reporting middleware | Day-1+ |
| The agent's pending jobs | Graph checkpoint (`pending_jobs`) | Job-tracking (add) and job-reporting (remove) middleware | Wake-up check (when a notice arrives, when a run ends, when a session is built) | Main agent | Day-1+ |
| A job's chat row | `chat_messages` | Harness, on the job's notice | None | Chat UI | Day-1+ |

## Consequences

### Positive

- The agent hears about its own jobs with no browser open.
- Interrupts are safe by construction: an outcome has no path to
  `Command(resume=...)` or to the human-input path.
- An answer and an outcome that arrived during the question reach the agent in
  the same step.
- Nothing is in transit, so nothing can be lost in transit. No new table, no
  sequence, no compaction, no hooks, no retries.
- The agent is told only about jobs it started, each one once per committed
  checkpoint.
- The UI only displays.

### Negative

- If the wake-up notice is lost while the agent is idle, the agent is told at
  the next activity on the session, not immediately.
- Processing is at-least-once, so actions an outcome triggers must be
  idempotent.
- The `status: "QUEUED"` and `run_id` result shape becomes a platform contract
  for job CLIs. A CLI that reports a started job differently is not tracked.
- A wake-up run has no human message, so instructions and evals must cover an
  agent acting on a job outcome alone.
- An agent paused on a question cannot act on outcomes until the user answers.
  This is intended, since the question blocks the agent.
- Outcomes wait while a subagent runs, and are reported to the main agent's next
  model call after the subagent returns.

## Impact

- **ADR-013:**
  - "System notices are outside the graph (2026-10-07)" is superseded by this ADR.
  - "Background jobs": a completed or failed notice no longer becomes a
    `[System Notification]` turn. It writes the chat row and wakes an idle agent.
- **SDK:**
  - an outcome field on `workflow_runs`;
  - the `system/record-job-outcome` step, a separate node before each job
    workflow's completed and failed notices, writes the outcome to its own run.
- **Harness:**
  - the job-tracking middleware on every agent (the `pending_jobs` field, its
    merge rule, and `wrap_tool_call`);
  - the job-reporting middleware on the main agent (`before_model`);
  - the wake-up check and the internal wake-up run (run reason `external_event`,
    no input messages);
  - the chat history writer skips notification messages.
- **Chat UI:** the notice delivery effect and its `[System Notification]`
  delivery records are removed. The chat only displays notices.
- **Job workflows:** a record-outcome node and a record-failure node, each with
  its own state, ahead of the completed and failed notices.
- **CLIs:** unchanged. Job CLIs already print `status` and `run_id`.

## References

- ADR-011: Topology builder split (main agent vs subagents)
- ADR-012: Middleware stack composition
- ADR-013: HITL / interrupt protocol
- ADR-039: Platform Ownership Model
- LangGraph interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- LangChain middleware: https://docs.langchain.com/oss/python/langchain/middleware
