만든이유: 코덱스 토큰 다쓰려면 goal 돌려놓고 자는거 많이해야하는데.. 경제보복 해야하는데.. 이게 너무 꼬임.. 

# cinuxx (mySkill)

`ciduxx` is an explicit-invocation Codex skill, a vendor-neutral visual AI
change log, and a Linux supervisor for deep program-improvement loops:

```text
inspect -> implement -> verify -> independently audit -> repair -> repeat
```

It is designed for long-running, high-reasoning work rather than short answers. It can coordinate several registered Codex sessions on one Linux PC and schedule `shutdown +1` exactly once only after the whole group reaches the configured terminal condition.

> Repository label: `cinuxx (mySkill)`. The invocable skill keeps the original requested spelling, `$ciduxx`.

## Why Ciduxx

- Uses native Codex Goal mode interactively or a `codex exec` supervisor that resumes the exact worker thread between repair iterations.
- Defaults to 24 work/verify/fix iterations, `xhigh` reasoning, and two fresh read-only completion auditors.
- Records material branches as readable `A: ...`, `B: ...` Markdown with evidence and rollback notes.
- Maintains one self-contained `AI_CHANGELOG.html` where human requests and
  verified semantic outcomes appear as a readable conversation.
- Persists supervisor state outside the project under `${XDG_STATE_HOME:-$HOME/.local/state}/ciduxx`.
- Anchors real-power authorization to the OS account home's fixed `.local/state/ciduxx` path, ignoring caller-controlled `HOME` and `XDG_STATE_HOME` values.
- Uses bounded runtime, stagnation detection, atomic state, workspace locks, and exact session UUID resume.
- Coordinates multiple sessions with fixed expected membership or an explicit enrollment seal.
- Keeps shutdown disabled by default and never invokes `sudo`, a shell command string, or force power operations.

Ciduxx spends additional tokens on competing hypotheses, implementation, tests, independent audits, and completion proof. It does not pad responses, and it cannot bypass the model, account, context, rate, or product limits available to Codex.

## Requirements

- Linux; local systemd is required only for automatic shutdown.
- Python 3.10 or newer, using only the standard library.
- A current authenticated Codex CLI. The managed runner uses `codex exec --json`, `--output-schema`, and `exec resume`.
- Git for the default managed-run safety checks.
- Optional: `uv` for the skill-validation command under [Verify](#verify). Ciduxx itself has no third-party Python dependencies.

Automatic shutdown intentionally refuses WSL, containers, CI, non-systemd hosts, root-run ciduxx, remote SSH sessions, and multiple logged-in users unless the relevant explicit override is supplied.

## Install

Clone the repository and run the Linux installer:

```bash
git clone https://github.com/studyreadbook4ever/cinuxx-myskill.git
cd cinuxx-myskill
./install.sh
```

The installer symlinks the skill into the first configured location it finds (`$CODEX_SKILLS_DIR`, `$CODEX_HOME/skills`, `~/.agents/skills`, or an existing `~/.codex/skills`) and installs the `ciduxx` launcher in `~/.local/bin`.

Restart Codex if the skill does not appear immediately. You can also ask `$skill-installer` to install the `skills/ciduxx` path from this repository.

## Native Goal Mode

For an interactive session, combine Codex persistence with the ciduxx completion protocol:

```text
/goal Use $ciduxx to improve this program until the full test suite passes and the requested behavior is independently verified. Record material decisions.
```

`$ciduxx` alone does not authorize host shutdown. State power intent explicitly and use a registered group when shutdown is desired.

## Visual AI Change Log

`AI_CHANGELOG.html` is a self-contained semantic exhibit for AI-assisted work.
Each actionable human modification request appears on the left, and one or more
verified outcome summaries appear on the right.

It is not release notes, a code diff, a tool log, or a full chat transcript. The
exhibit stores only display-safe request text and behaviorally meaningful
outcomes. Its canonical JSON, static no-JavaScript presentation, and CSS skin
live in one commit-friendly HTML file. It opens directly from `file://` and
needs no server, CDN, build step, or sidecar.

Create the project-root opt-in file:

```bash
ciduxx exhibit init --title "Project AI Change Log"
```

Generate portable project instructions for Codex or Claude Code:

```bash
ciduxx exhibit instructions --agent codex
ciduxx exhibit instructions --agent claude
```

Place the generated instructions in the project guidance read by the chosen
agent, such as `AGENTS.md` or `CLAUDE.md`. These are one-time setup actions.
During normal work the coding agent owns exhibit updates: after implementing
and verifying a human request, it appends the safe request and all verified
semantic outcomes as one grouped turn. A human does not transcribe work while
it happens.

The cross-client structured command is:

```bash
ciduxx exhibit record --file AI_CHANGELOG.html --payload -
```

It accepts one UTF-8 JSON object on standard input, so long or multilingual
content does not depend on shell quoting. For a deliberately visible long task,
an agent can use `exhibit begin` followed by `exhibit answer`; normal work
should prefer atomic `exhibit record` so failed or unchanged work does not
leave a pending entry.

Validate a finished exhibit with:

```bash
ciduxx exhibit validate --require-answered
```

Treat the exhibit as public repository content. Never include credentials,
private paths, hidden prompts, chain-of-thought, raw logs, patch hunks, plans,
guesses, or failed attempts. `redacted: true` and `--redacted` only label a
safe paraphrase already supplied by the agent; they do not sanitize text.

### CSS skins

Content and presentation are independent inside the same HTML file. Apply a
UTF-8 CSS skin without changing conversation turns:

```bash
ciduxx exhibit skin --css-file theme.css --name theme-name
ciduxx exhibit skin --builtin default
```

The embedded skin block is preserved across future records. External network
assets are blocked by the page security policy; use local styling or data URLs.
See [the semantic exhibit protocol](skills/ciduxx/references/exhibit-protocol.md)
for the JSON contract, stable selectors and variables, format markers, and
Claude/Codex workflow.

## Headless Deep Loop

Inspect prerequisites first:

```bash
ciduxx doctor
ciduxx doctor --power
```

Run one supervised loop without shutdown:

```bash
ciduxx run \
  --workspace /absolute/path/to/repo \
  --objective "Eliminate the flaky tests and prove the full suite is stable"
```

The default runner is deliberately deep but finite: 24 iterations, 8 hours, 90 minutes per turn, `xhigh` reasoning, two fresh auditors, and a three-turn stagnation limit. Supported models may use a more intensive setting explicitly:

```bash
ciduxx run \
  --workspace /absolute/path/to/repo \
  --objective-file /absolute/path/to/goal.md \
  --reasoning-effort ultra \
  --max-hours 12
```

Dirty worktrees are refused by default. Review existing changes and add `--allow-dirty` only when in-place work is intentional. Ciduxx never resets, cleans, stashes, commits, pushes, or merges worker changes by itself.

### Managed exhibit recording

If `AI_CHANGELOG.html` already exists at the workspace root, `ciduxx run`
detects it automatically. Use `--exhibit-file` to opt in explicitly and create
a missing exhibit after verified work, or `--no-exhibit` to disable recording:

```bash
ciduxx run \
  --workspace /absolute/path/to/repo \
  --objective "Improve the import workflow" \
  --exhibit-file AI_CHANGELOG.html
```

The worker returns a display-safe request and the complete final list of
verified outcomes; the supervisor performs one idempotent write after coherent
finalization. Completed work is `answered`; durable incomplete work is
`partial`. Generated resume commands carry a logical task key, so a later run
updates that partial turn instead of duplicating the same human request.
Failed, cancelled, and unchanged work is not presented as a completed change.

## Several Codex Windows, One Shutdown

Create a group before starting the participating sessions. A fixed count is the simplest race-free enrollment barrier:

```bash
ciduxx group create \
  --name tonight \
  --expected 3 \
  --shutdown-on completed \
  --shutdown-delay 1
```

Copy the returned `group_id`, then start one runner in each terminal, ideally in separate repositories or Git worktrees:

```bash
ciduxx run --group GROUP_ID --member-name api \
  --workspace /work/api --objective-file /work/api-goal.md

ciduxx run --group GROUP_ID --member-name web \
  --workspace /work/web --objective-file /work/web-goal.md

ciduxx run --group GROUP_ID --member-name docs \
  --workspace /work/docs --objective-file /work/docs-goal.md
```

The third member seals a group created with `--expected 3`. The last finishing member takes the group lock. Shutdown is attempted only when all three results are `completed`. A failed, blocked, partial, cancelled, missing, or unregistered session prevents shutdown under this policy.

For an unknown number of windows, omit `--expected`, join every member, then close enrollment explicitly:

```bash
ciduxx group seal GROUP_ID
```

For interactive Codex windows, put the same group ID in each Goal prompt:

```text
/goal Use $ciduxx in group GROUP_ID for this task. Register this session before work, heartbeat it, and finalize it only after the completion audit.
```

Only registered ciduxx sessions are counted. Process scanning cannot determine whether an arbitrary Codex window has actually completed its task, so ciduxx never guesses.

Inspect coordination state at any time:

```bash
ciduxx group status GROUP_ID
ciduxx group list
```

`--shutdown-on finalized` is an explicit alternative for overnight runs where cleanly recorded `partial`, `blocked`, `failed`, or `limit` outcomes should also permit shutdown. Cancellation, signal interruption, corrupt state, supervisor failure, unsafe host checks, or an unsealed group never permit it.

## Shutdown Safety

The supervisor owns power policy outside the worker's workspace. Model JSON, decision Markdown, repository instructions, project files, and caller-selected environment state paths are never trusted to arm shutdown.

Non-power runs may follow `XDG_STATE_HOME`, but automatic power is eligible only when authoritative group and ledger state live at `.local/state/ciduxx` beneath the current UID's OS account home (as reported by the system account database). `ciduxx doctor` prints both the selected and trusted paths. If XDG points elsewhere, use that state only with policy `never`, or pass the printed trusted path through the global `--state-root` option before creating the shutdown group. This prevents a nested worker from redirecting state into its writable repository and manufacturing power authorization.

Keep each managed workspace narrower than the account home and never grant it write access to the trusted state anchor. The runner rejects a registered workspace that contains its authoritative state directory.

When the gate passes, ciduxx executes a fixed argument vector equivalent to:

```bash
/usr/bin/shutdown -P +1 "ciduxx: all registered sessions finalized"
```

It does not call `sudo`, bypass systemd inhibitors, use `--force`, fall back to another power mechanism, or automatically retry an uncertain attempt. If polkit or the local login session does not permit shutdown, results remain saved and the group records `power_failed`.

If ciduxx is interrupted after durably entering `arming` but before it can record the result, treat the attempt as `arming_unknown`: power may already be scheduled. The gate stays closed, ciduxx does not retry or cancel automatically, and an operator must inspect the group state and the system reservation before taking further action.

Inspect or cancel the system reservation during the grace period:

```bash
shutdown --show
shutdown -c
```

## Output

The optional tracked visual artifact is:

```text
AI_CHANGELOG.html
```

It is intentionally separate from ignored supervisor diagnostics. Human-readable
managed-run reports are written to:

```text
.ciduxx/runs/<run-id>/
├── decisions.md
├── objective.md
├── summary.md
├── state.json
└── iterations/
    └── 001/
        ├── result.json
        ├── events.jsonl
        ├── stderr.log
        ├── auditor-1.json
        └── auditor-2.json
```

Each numbered directory records one worker iteration. Auditor files appear for completion-candidate iterations and reflect the configured verifier count.

Authoritative locks, nonces, and raw events remain under the selected user state directory, outside the project sandbox. Any group authorized for real power must instead use the fixed OS-account state anchor described above.

## Verify

The test suite injects fake Codex and fake power backends; it never executes a real shutdown program:

```bash
python3 -m unittest discover -s tests -v
SKILL_VALIDATOR=/path/to/skill-creator/scripts/quick_validate.py
uv run --with pyyaml python "$SKILL_VALIDATOR" skills/ciduxx
```

See [the semantic exhibit protocol](skills/ciduxx/references/exhibit-protocol.md),
[loop protocol](skills/ciduxx/references/protocol.md), and
[Linux shutdown safety model](skills/ciduxx/references/linux-safety.md) for the
full data, state, and trust contracts.
