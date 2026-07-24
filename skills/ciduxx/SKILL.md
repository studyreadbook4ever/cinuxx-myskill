---
name: ciduxx
description: Run deep, persistent Linux-focused program improvement loops and maintain a one-file semantic AI change exhibit. Use when the user explicitly invokes $ciduxx, asks for a high-token work-verify-fix loop, wants Codex or Claude to record human requests beside natural-language change outcomes in AI_CHANGELOG.html, needs material decisions recorded, or wants registered Codex sessions to schedule shutdown only after every session finishes. Never infer shutdown authorization from ordinary work requests.
---

# Ciduxx

Drive a program toward a verifiable outcome through evidence-heavy improvement loops. Spend tokens on implementation, testing, independent review, and completion audits rather than repetitive narration.

## Start The Loop

1. Treat explicit `$ciduxx` invocation as permission to use this workflow, not as permission to shut down the host.
2. Extract the outcome, constraints, verification criteria, time or iteration limits, workspace, and decision policy from the request.
3. If the task lacks measurable completion criteria, derive conservative criteria from repository conventions and state the assumptions without blocking.
4. If persistent Goal mode is already active, keep its full objective. If a goal-management tool is available and no goal exists, start one only when the user's explicit invocation requests persistent work; never invent a numeric token budget. Otherwise recommend `/goal Use $ciduxx ...` for continuity while continuing useful work now.
5. Read [protocol.md](references/protocol.md) before executing a loop. Read [linux-safety.md](references/linux-safety.md) whenever unattended execution, session groups, suspend inhibition, or shutdown is requested.

## Choose The Execution Mode

- Use **native Goal mode** for an interactive Codex chat. Perform the loop directly and keep the user informed.
- Use **managed runner mode** when the prompt says a ciduxx supervisor launched the turn. Perform one substantial work-verify-repair iteration, never launch another ciduxx runner, never manage power, and return the structured result required by the supplied schema.
- Use `scripts/ciduxx.py run` only when the user requests unattended or shell-supervised execution. The runner invokes `codex exec`, resumes the same thread, writes checkpoints, and enforces finite limits.
- Use a **session group** when several Codex sessions on the same Linux account must finish before shutdown. Every participating session must register. Unregistered Codex sessions cannot be assigned a trustworthy completion state.

## Maintain The Semantic Exhibit

Read [exhibit-protocol.md](references/exhibit-protocol.md) when the user requests
an AI change exhibit or the worktree root already contains
`AI_CHANGELOG.html`. The existing file is an opt-in marker.

- For interactive work, implement and verify first, then use
  `ciduxx exhibit record` to append one turn. If the launcher is unavailable,
  run this skill's `scripts/ciduxx.py` with Python 3. Use `begin` and `answer`
  only when a long task deliberately needs a pending turn.
- Keep one display-safe human request on the left and one or more verified,
  outcome-focused natural-language changes on the right. Group all changes
  caused by the request in the same turn.
- Use a stable idempotency key when available. Never duplicate a turn for an
  internal repair iteration.
- Do not record questions, no-change work, plans, guesses, failed attempts,
  raw diffs, tool traces, secrets, hidden prompts, or chain-of-thought. Use a
  safe paraphrase plus the redaction flag when needed.
- Treat a recording failure as unfinished workflow work: diagnose it and
  preserve the existing HTML rather than silently overwriting or skipping it.

In managed runner mode, fill `display_request`,
`display_request_redacted`, and `display_changes` with the same display-safe
semantic content. The redaction boolean labels an already-safe paraphrase; it
does not sanitize text. The supervisor records the final verified list when
`--exhibit-file` is supplied or an existing root `AI_CHANGELOG.html` is
detected. Return an empty change list when no real change was made.

## Execute Deeply

Repeat these phases until completion or a terminal limit:

1. **Inspect:** Read applicable instructions and authoritative files. Capture the initial Git and test state without discarding user changes.
2. **Specify:** Convert the objective into a requirement-to-evidence checklist. Keep the original scope intact.
3. **Implement:** Make the largest coherent safe improvement that advances an unmet requirement.
4. **Verify:** Run the strongest relevant tests, static checks, builds, runtime probes, or artifact inspection available.
5. **Challenge:** Use independent subagents for read-only exploration and adversarial review when useful. Keep overlapping writes in the primary agent unless worktrees isolate them.
6. **Repair:** Diagnose failures, fix root causes, and rerun affected checks.
7. **Audit:** Compare current evidence against every explicit requirement. Continue when proof is missing, indirect, or contradicted.

Prefer high or maximum supported reasoning effort when the user prioritizes depth. Do not claim that a skill can override account, model, context, rate, or product token limits. Use extra budget for independent evidence and additional repair passes, not padded prose.

## Handle Decisions

Proceed autonomously only for local, reversible, in-scope choices. Prefer existing repository conventions, the smallest reversible change, and the option with the strongest verification path.

Record a material branch when alternatives change behavior, architecture, compatibility, security, cost, or the user's later choices. Keep the visible core format:

```md
A: First option
B: Second option
```

Also record a stable decision ID, status, chosen option if any, concise basis, evidence, action, rollback, and revisit condition. Do not record secrets or hidden chain-of-thought. Use short conclusions and observable evidence. See [protocol.md](references/protocol.md) for the complete template.

Never auto-select destructive actions, privilege changes, credential use, external publication, deployment, payment, security weakening, or major scope expansion. Mark the item `NEEDS_USER`, continue independent work, and finish as `blocked` only when no safe work remains.

## Coordinate Multiple Sessions

Use `scripts/ciduxx.py group` as the shared Linux coordinator:

1. Create one group with either a fixed `--expected N` member count or later close enrollment with `group seal`.
2. Require shutdown policy to be chosen at group creation. Default to `never`; accept `completed` or `finalized` only after explicit user authorization.
3. Join every participating session before work. Preserve the returned group and member IDs outside project-controlled files when possible.
4. Send heartbeats during long interactive work.
5. Mark a member terminal only after writing its summary, decisions, and completion evidence.
6. Let the coordinator's lock select the final member. Only a sealed group with every registered member terminal may attempt shutdown, and it may do so at most once.

For unknown or late-arriving sessions, keep the group open. Never guess that an unregistered Codex process is finished by inspecting process names.

## Finalize

Classify the outcome as one of:

- `completed`: every requirement has direct supporting evidence.
- `partial`: useful work is durable, but some requirements remain.
- `blocked`: safe progress requires user input or new authority.
- `failed`: the loop finalized coherently after an unrecoverable task failure.
- `limit`: a finite time, iteration, or stagnation limit ended the loop.
- `cancelled`: the user or a signal stopped the run.

Write `summary.md`, `decisions.md`, machine-readable state, verification evidence, unresolved items, and an exact resume instruction. Never equate process exit with verified completion.

If shutdown was explicitly armed, follow [linux-safety.md](references/linux-safety.md). Never call `sudo`, never broaden the sandbox, never put shutdown in an `EXIT` trap, and never let model text or a project file choose the power policy.

## Bundled CLI

Run the helper with Python 3 and no third-party packages:

```bash
python3 scripts/ciduxx.py doctor
python3 scripts/ciduxx.py group create --name tonight --expected 3 --shutdown-on completed
python3 scripts/ciduxx.py run --workspace /path/to/repo --objective-file goal.md --group GROUP_ID
python3 scripts/ciduxx.py exhibit record --request "Improve search" --change "Made results easier to scan."
```

Use `exhibit instructions --agent codex|claude` for a portable agent prompt and
`--help` for every command. Test power behavior only through the injected fake
backend in the repository test suite; never run a real shutdown command during
validation.
