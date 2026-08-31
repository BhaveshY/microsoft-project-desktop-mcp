# Microsoft Project Desktop MCP

Use Codex or another stdio MCP client to create, schedule, update, analyze, and
export Microsoft Project desktop plans on the same Windows PC.

## Quick start

Requirements:

- Windows 11 and Python 3.10 or newer.
- The classic Microsoft Project desktop application with the
  `MSProject.Application` COM class registered. Project for the web, Planner,
  and macOS are not supported.
- Codex desktop or another MCP client that supports local stdio servers.

Prepare the isolated Python environment without launching Project:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-ms-project-mcp.ps1 -SetupOnly
```

The root `.mcp.json` already registers the server as `microsoft-project`.
Restart or reload the MCP client after pulling these files or changing trust.
Then call `msp_capabilities` first. A ready desktop reports `backend=live`,
`available=true`, and `activates_desktop=false`; the capability probe itself
never launches Project.

A good caller workflow is:

1. Create, open, or attach with `msp_project`.
2. Read the compact project state with `msp_query` and retain its `state` token.
3. Submit related typed operations in one `msp_apply` batch using that token and
   a new idempotency key.
4. For destructive work, request `mode=plan`, inspect the impact, then repeat
   the identical request as `mode=commit` with the returned confirmation token.
5. Use `msp_schedule`, `msp_status`, and `msp_analyze` through execution.
6. Save/export explicitly and close server-owned files. Detach user-owned files.

## The eight tools

| Tool | Purpose | Safety boundary |
|---|---|---|
| `msp_capabilities` | Report installation, backend, operations, and verification status | Read-only; never activates Project |
| `msp_project` | Create, open, attach, save, detach, or close | Explicit ownership and dirty-file disposition |
| `msp_query` | Read projects, tasks, dependencies, resources, assignments, calendars, baselines, or status | Stable refs, bounded pages, signed snapshot cursors |
| `msp_apply` | Create/update/delete typed project objects in batches | Expected-state check, idempotency, plan/confirm, native reread |
| `msp_schedule` | Calculate, level, clear leveling, or reschedule incomplete work | Allowlisted native Project commands |
| `msp_status` | Set status date, task actuals, and daily assignment actual work | Expected state, grouped Undo, exact reread |
| `msp_analyze` | Read critical path, slack, constraints, overallocation, variance, earned value, or health | Read-only native schedule data |
| `msp_export` | Produce PDF or copy a saved clean MPP | Absolute destination, extension, overwrite, and confirmation checks |

The public surface deliberately stays small. `msp_apply` carries a discriminated
union of typed operations for tasks/WBS, dependencies, resources, assignments,
calendars, project properties, costs, and baselines. This avoids dozens of
fragile single-field tools without exposing raw automation.

## Planning and committing changes

Every mutating request includes:

- `expected_state`: the latest state token returned by a session, query, or
  receipt;
- `idempotency_key`: a caller-generated key, reused only for the exact same
  request;
- `mode`: `plan` or `commit` where the tool supports planning.

Creating, opening, and attaching also require an idempotency key. Their result
is replayed only while the returned desktop session is still valid; an
uncertain or prior-process result is blocked for manual reconciliation rather
than opening a duplicate document.

Example: create a summary, two children, a finish-to-start dependency, a work
resource, and an assignment in one batch. Batch-local `client_ref` values may
only point to creates earlier in the same batch.

```json
{
  "project": {"session_id": "...", "project_key": "..."},
  "batch": {
    "expected_state": {"token": "sha256:..."},
    "idempotency_key": "launch-plan-2026-08-28-001",
    "mode": "commit",
    "operations": [
      {"op":"create_task","client_ref":"summary","name":"Launch","duration_minutes":0},
      {"op":"create_task","client_ref":"design","name":"Design","duration_minutes":2400,
       "parent":{"kind":"task","client_ref":"summary"}},
      {"op":"create_task","client_ref":"build","name":"Build","duration_minutes":4800,
       "parent":{"kind":"task","client_ref":"summary"},
       "after":{"kind":"task","client_ref":"design"}},
      {"op":"add_dependency","predecessor":{"kind":"task","client_ref":"design"},
       "successor":{"kind":"task","client_ref":"build"},"dependency_type":"FS","lag_minutes":0},
      {"op":"create_resource","client_ref":"engineer","name":"Engineer","resource_type":"work",
       "max_units_percent":"100"},
      {"op":"create_assignment","client_ref":"design-owner",
       "task":{"kind":"task","client_ref":"design"},
       "resource":{"kind":"resource","client_ref":"engineer"},"units_percent":"100"}
    ]
  }
}
```

Deletes, baseline changes, leveling/rescheduling, exports, and discard-close
flows require a stored, unexpired plan or explicit confirmation as appropriate.
Confirmation tokens are one-time and bound to the exact payload, project ref,
and expected state.

## Desktop behavior and ownership

The live adapter uses one dedicated COM single-threaded apartment. All Project
objects are created, read, mutated, verified, and released on that thread.
Calls are serialized; timeouts never redispatch the same work. The adapter does
not use active-cell selection, clipboard operations, VBA, macros, or generic
`GetField`/`SetField` access.

Projects created or opened by the MCP are server-owned. They may be saved and
closed only with an explicit dirty-file disposition. A project attached from a
user's already-running Project window is user-owned: the MCP may edit or save it
when explicitly asked, but it never closes that document or quits the user's
Project process. Use `detach` when finished.

Untitled projects require an explicit absolute `.mpp` path before saving or
using `save_and_close`; the server will not allow Project to open a modal Save
As prompt during an unattended MCP call. Save As never overwrites another file
implicitly.

Project/task/resource/assignment identities are not row numbers. Task,
resource, and assignment references use Project's stable `UniqueID` within the
bound document. Session/project handles include a random per-process namespace.
After an MCP restart, open or attach again and obtain new handles; persisted
ledger data prevents unsafe redispatch but does not make a COM session resumable.

## Scheduling and status coverage

The live path supports:

- deterministic task creation at root or under a parent, with sibling placement
  after the referenced task's complete subtree;
- task duration, milestone, fixed cost, and accrual updates;
- FS, SS, FF, and SF dependencies with lag in minutes;
- work and material resource rates, per-use costs, and labels; `standard_rate`
  is per hour for work resources and per material unit for material resources;
  cost resources use explicit assignment `cost` values; work assignments use
  `units_percent`, while material assignments use `material_units`;
- base calendars, weekly work intervals, and calendar exceptions;
- project summary properties, baselines 0 through 10, calculation, leveling,
  leveling clear, and rescheduling incomplete work;
- status date, task progress/actuals, and per-day assignment Actual Work;
- native critical/slack/constraint/overallocation/variance/earned-value reads;
- PDF export and safe copies of saved, clean MPP files.

Existing task row reordering (`move_task`) is rejected. Microsoft documents
object-scoped task indentation, but not an equivalent object-scoped row move;
the usual move/indent UI commands depend on selection. Recursive task deletion
is also rejected, and a non-recursive delete refuses summary tasks because
Project would cascade that deletion to their subtasks. Recreate or restructure
such sections in a reviewed batch. Live variance and earned-value analysis is
currently limited to the primary baseline (`baseline=0`); other baseline
numbers are rejected rather than mislabeled.

## Failure, replay, and recovery

The production ledger lives under
`%LOCALAPPDATA%\OpenAI\MicrosoftProjectMCP` by default, or
`MSP_MCP_STATE_DIR` when set. It uses SQLite WAL mode and a persisted signing
secret. The directory keeps the Windows user-profile ACL inheritance and the
server additionally reinforces full control for the current user when possible.

- A stale expected state fails before mutation.
- Concurrent identical commits have exactly one dispatcher.
- A known pre-dispatch failure releases the key for safe retry.
- A committed result replays without touching Project.
- A timeout, crash, or uncertain rollback becomes `unknown_commit_state` and is
  never automatically replayed. Inspect the project, reacquire a session, and
  use a new state/key after reconciliation.
- Eligible batches are grouped into one Project Undo-list entry, calculated
  once, and reread. This is Undo-atomic, not a database ACID transaction.

## Install and verification

Run the bounded no-Project verifier:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify-ms-project-mcp.ps1
```

It compiles the package, runs focused tests, parses configuration, builds and
inspects a wheel, exercises stdio tool discovery/capabilities in mock and auto
modes, and confirms the desktop smoke refuses to run without consent.

The real desktop smoke is deliberately separate because it launches Project
and writes a disposable MPP fixture:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-ms-project-desktop.ps1 -AllowWriteFixture
```

The command prints JSON and leaves the unique fixture path for inspection. A
passing run verifies only the local Project installation that executed it. The
live smoke has passed on Microsoft Project Standard 2019, version
16.0.20326.20112, on 32-bit Office. Passing mocks and fake-COM tests are not
reported as desktop proof.

## Official object-model grounding

The adapter follows Microsoft's documented [Application object and external
automation model](https://learn.microsoft.com/en-us/office/vba/api/project.application),
[read-only Project.Name semantics](https://learn.microsoft.com/en-us/office/vba/api/project.project.name),
[Tasks.Add placement](https://learn.microsoft.com/en-us/office/vba/api/project.tasks.add),
[task-object OutlineIndent](https://learn.microsoft.com/en-us/office/vba/api/project.task.outlineindent),
[Undo transactions](https://learn.microsoft.com/en-us/office/vba/api/project.application.openundotransaction),
[option-sensitive Assignments.Add units](https://learn.microsoft.com/en-us/office/vba/api/project.assignments.add),
[assignment units](https://learn.microsoft.com/en-us/office/vba/api/project.assignment.units),
[resource overtime-rate limits](https://learn.microsoft.com/en-us/office/vba/api/project.resource.overtimerate),
[material-label limits](https://learn.microsoft.com/en-us/office/vba/api/project.resource.materiallabel),
[timephased assignment data](https://learn.microsoft.com/en-us/office/vba/api/project.assignment.timescaledata),
[TimeScaleValue.Value minutes](https://learn.microsoft.com/en-us/office/vba/api/project.timescalevalue.value),
[leveling](https://learn.microsoft.com/en-us/office/vba/api/project.application.levelnow),
[status rescheduling](https://learn.microsoft.com/en-us/office/vba/api/project.application.updateproject),
[baseline NA semantics](https://support.microsoft.com/en-us/project/baseline1-10-start-fields),
and [fixed-format export](https://learn.microsoft.com/en-us/office/vba/api/project.project.exportasfixedformat).
