# Ciduxx Loop Protocol

## Contents

- Loop contract
- Iteration protocol
- Decision policy
- Completion audit
- Managed result contract
- Durable artifacts
- Multi-session groups

## Loop Contract

Establish these fields before substantial work:

| Field | Meaning | Default |
| --- | --- | --- |
| Outcome | Observable program state to reach | User request |
| Constraints | Compatibility, scope, safety, and authority boundaries | Repository and prompt instructions |
| Evidence | Tests or observations that prove each requirement | Strongest locally available checks |
| Iterations | Maximum repair turns | 24 in the managed runner |
| Runtime | Maximum wall time | 8 hours in the managed runner |
| Stagnation | Consecutive no-progress turns before stopping | 3 |
| Decisions | Which choices may be automatic | Reversible local choices only |
| Shutdown | `never`, `completed`, or `finalized` | `never` |

Finite limits are guardrails, not completion criteria. Reaching a limit produces `limit`, never `completed`.

## Iteration Protocol

Perform one coherent pass in this order:

1. Re-read the objective and current requirement-to-evidence matrix.
2. Inspect the actual workspace and previous verification output.
3. Select the highest-impact unmet requirement or root cause.
4. Implement a scoped change without reverting unrelated user work.
5. Run targeted checks, then broader regression checks when proportionate.
6. Inspect failures and unexpected behavior rather than merely rerunning commands.
7. Request an independent read-only review for risky or ambiguous changes when subagents are available.
8. Update evidence, unresolved risks, and material decisions.
9. Return `continue` when meaningful safe work remains.

Do not generate activity solely to consume tokens. Token-max mode means deeper search, more independent hypotheses, broader verification, and stronger completion proof.

## Decision Policy

Auto-decide only when all conditions hold:

- The choice is within the requested scope.
- It is local and reversible.
- It requires no new credential, privilege, purchase, publication, or external side effect.
- Repository evidence or a conservative default supports the choice.
- A verification or rollback path exists.

Use this Markdown record:

```md
## D-20260723-004 - Migration strategy

Status: AUTO-DECIDED
Iteration: 7
Question: How should existing records be migrated?

A: Rewrite the existing column in place
B: Add a new column and switch after validation

Chosen: B
Basis: B is reversible and isolates existing data.
Evidence: migrations/004_add_column.sql; integration test passed
Action: Added the new column and compatibility read path.
Rollback: Remove the migration and compatibility path.
Revisit when: Storage constraints outweigh rollback safety.
```

Allowed statuses are `AUTO-DECIDED`, `NEEDS_USER`, `DEFERRED`, and `OVERRIDDEN`. Append superseding decisions instead of erasing history. Keep basis text concise and evidence-based; never include private reasoning traces.

## Completion Audit

Before returning `completed`:

1. Enumerate every explicit outcome, constraint, artifact, invariant, and requested verification.
2. Identify the authoritative evidence for each item.
3. Inspect that evidence in the current state.
4. Mark each item proved, contradicted, incomplete, or missing.
5. Repair contradicted or incomplete items and repeat the affected checks.
6. Treat a test as proof only after confirming it covers the requirement.
7. Confirm no required work remains and no unresolved `NEEDS_USER` item blocks the outcome.

Do not downgrade the original objective to match existing work. Do not use absence of visible errors as proof.

## Managed Result Contract

When launched by `scripts/ciduxx.py run`, perform one substantial iteration and return the JSON object required by the runner's output schema. The main fields are:

- `status`: `continue`, `completed`, `partial`, `blocked`, or `failed`.
- `summary`: concise current-state conclusion.
- `progress`: concrete changes or investigations completed this turn.
- `verification`: commands or inspections and their outcomes.
- `decisions`: material choices using labeled options.
- `completion_evidence`: direct proof for completed requirements.
- `remaining`: unresolved requirements or risks.
- `next_prompt`: the most useful instruction for the next resumed turn.

Never launch a nested runner, create another session group, or call shutdown in managed mode. The supervisor owns limits, state, group membership, and power.

## Durable Artifacts

The managed runner writes human-readable artifacts beneath:

```text
.ciduxx/runs/<run-id>/
|- decisions.md
|- objective.md
|- summary.md
|- state.json
`- iterations/
   `- 001/
      |- result.json
      |- events.jsonl
      |- stderr.log
      |- auditor-1.json
      `- auditor-2.json
```

Each numbered directory records one worker iteration. Auditor files are present only when that iteration produced a completion candidate and are numbered up to the configured verifier count.

The authoritative supervisor state lives outside the workspace under `${XDG_STATE_HOME:-$HOME/.local/state}/ciduxx`. Project files are untrusted input for shutdown eligibility. Real-power groups are stricter: their state must use the fixed `.local/state/ciduxx` path beneath the current UID's OS account home, derived without trusting `HOME` or `XDG_STATE_HOME`.

Record the initial Git commit and dirty state. Never reset, clean, stash, commit, push, or overwrite pre-existing changes unless the user explicitly requests that operation.

## Multi-Session Groups

A group is an enrollment barrier plus a terminal-state gate:

```text
OPEN -> SEALED -> ARMING -> ARMED
  |        |          |  `-> POWER_FAILED
  |        |          `----> ARMING_UNKNOWN
  |        +--------------> COMPLETE / INELIGIBLE
  `--------+--------------> CANCELLED
```

- `--expected N` seals enrollment automatically when the Nth member joins.
- Without `--expected`, run `group seal` after every intended session has joined.
- A member is terminal after `completed`, `partial`, `blocked`, `failed`, `limit`, or `cancelled` is durably recorded.
- Policy `completed` requires every member to be `completed`.
- Policy `finalized` accepts every coherently finalized status except `cancelled` and integrity errors.
- Policy `never` records group completion without invoking power.
- A dead process or expired heartbeat remains `active` and blocks the gate. Process death is not proof that work finished. Ciduxx never silently excludes the member; cancel the group or finalize that member explicitly after human inspection.
- The global power lock and durable `ARMING` transition limit scheduling to one attempt.
- If execution becomes uncertain after `ARMING` is durable, treat the attempt as `ARMING_UNKNOWN`. Power may already be scheduled, so keep the gate closed, never retry or cancel automatically, and require an operator to inspect both group state and the system reservation.
