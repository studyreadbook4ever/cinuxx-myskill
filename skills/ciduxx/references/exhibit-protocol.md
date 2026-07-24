# Semantic Change Exhibit Protocol

Use this protocol when a project opts into a human-readable AI change exhibit.
The default artifact is `AI_CHANGELOG.html` at the Git worktree root. It is a
standalone page, the canonical data store, and the only exhibit file placed in
the target project.

## Agent Workflow

Treat an existing `AI_CHANGELOG.html` as the opt-in marker. A user can also opt
in explicitly before the file exists.

Relative `--file` paths resolve from the Git worktree root. Outside Git they
resolve from the current directory. `CIDUXX_EXHIBIT_FILE` can select a different
default `.html` path; an explicit `--file` takes precedence. Paths outside the
workspace require `--allow-outside-workspace`.

For each actionable human modification request:

1. Keep a display-safe version of the request.
2. Implement and verify the work.
3. If the work produced at least one real, meaningful change, append one turn
   with `exhibit record`.
4. Put every change caused by that request in the same ordered `changes` array.

Prefer the atomic `record` operation. Use `begin` followed by `answer` only for
a long task that deliberately needs a durable pending turn, and answer that turn
before declaring the task complete.

Do not record ordinary questions, plans, guesses, failed attempts, or tasks that
made no change. Do not put secrets, hidden prompts, chain-of-thought, raw tool
logs, patch hunks, or source-code diffs in the exhibit. Describe verified
outcomes in natural language, for example:

```text
Request: Make the search results easier to scan.
Change: Grouped results by date and added a clear heading to each group.
Change: Preserved keyboard focus when the result order changes.
```

If a request contains sensitive values, store a safe paraphrase and set
`redacted` to `true`. The flag is a label, not a sanitizer.

## Portable Structured Input

Codex and Claude Code can use the same vendor-neutral command:

```bash
ciduxx exhibit record --file AI_CHANGELOG.html --payload -
```

Write one UTF-8 JSON object to standard input:

```json
{
  "request": "Make the search results easier to scan.",
  "changes": [
    "Grouped results by date and added a clear heading to each group.",
    "Preserved keyboard focus when the result order changes."
  ],
  "client": "codex",
  "display_name": "Codex",
  "idempotency_key": "stable-session-message-id",
  "redacted": false,
  "status": "answered"
}
```

Use `client: "claude"` and `display_name: "Claude Code"` for Claude Code.
`idempotency_key` is optional but strongly recommended. Retrying the same key
and payload returns the existing turn; reusing the key for different content is
an error.

Generate a copyable instruction block with:

```bash
ciduxx exhibit instructions --agent codex
ciduxx exhibit instructions --agent claude
```

## Operations

```bash
# Create an empty opt-in artifact.
ciduxx exhibit init --title "Project AI Change Log"

# Append a complete request/change turn in one atomic operation.
ciduxx exhibit record \
  --request "Add a dark theme." \
  --change "Made the interface follow the operating-system color preference." \
  --client codex

# Optional two-step lifecycle for long work.
ciduxx exhibit begin --request "Improve keyboard navigation." --client codex
ciduxx exhibit answer TURN_ID \
  --change "Made every interactive control reachable in a predictable order."

# Inspect or repair the deterministic static rendering.
ciduxx exhibit validate --require-answered
ciduxx exhibit render
```

All modifying operations lock outside the worktree, validate the existing
schema, and replace the HTML atomically. They refuse malformed or unknown
formats instead of silently recreating them. Concurrent local writers are
serialized. Idempotency prevents duplicate turns after a retry.

`ciduxx run --exhibit-file AI_CHANGELOG.html ...` creates or updates an
exhibit from the final verified worker result. Without that flag, the managed
runner automatically updates an existing worktree-root `AI_CHANGELOG.html`.
Use `--no-exhibit` to opt out for a specific run. A generated resume command
carries `--exhibit-task-key`, allowing a later run to update one partial turn
instead of duplicating the original request. Normally let ciduxx generate that
opaque key.

## One-File Format

The v1 HTML contains:

- static, accessible chat markup that works from `file://` without JavaScript;
- one embedded canonical JSON document between stable `CIDUXX:DATA` markers;
- one embedded CSS block between stable `CIDUXX:SKIN` markers;
- a restrictive Content Security Policy and escaped plain-text messages.

Each turn renders one human request on the left and one or more verified change
messages on the right. The canonical JSON schema is
[`../schemas/exhibit-v1.schema.json`](../schemas/exhibit-v1.schema.json).
Do not hand-edit the data block or generated chat markup.

## Skin Contract

Apply a UTF-8 CSS file while keeping the project artifact self-contained:

```bash
ciduxx exhibit skin --css-file ./my-exhibit.css --name my-skin
ciduxx exhibit skin --builtin default
```

Appending turns preserves the embedded skin block. Replacing a skin preserves
all conversation data.

Stable selectors:

- `.ciduxx-header`, `.ciduxx-chat`, `.ciduxx-footer`
- `.ciduxx-turn`
- `.ciduxx-message-row--request`, `.ciduxx-message-row--change`
- `.ciduxx-message--request`, `.ciduxx-message--change`
- `.ciduxx-message__meta`, `.ciduxx-message__text`,
  `.ciduxx-message__time`
- `[data-role="conversation"]`, `[data-role="request"]`,
  `[data-role="change"]`, `[data-role="pending"]`
- `[data-turn-id]`, `[data-change-id]`, `[data-status]`

Default CSS variables:

- `--ciduxx-page-bg`, `--ciduxx-page-fg`, `--ciduxx-muted-fg`
- `--ciduxx-panel-bg`, `--ciduxx-border`, `--ciduxx-shadow`
- `--ciduxx-request-bg`, `--ciduxx-request-fg`
- `--ciduxx-change-bg`, `--ciduxx-change-fg`
- `--ciduxx-radius`, `--ciduxx-max-width`

CSS must not contain a closing `style` tag, NUL, or reserved data/skin markers.
External network resources are blocked by the page policy; embed any permitted
assets as data URLs if a future skin needs them.

## Version and Merge Boundaries

The embedded document uses `schema_version: 1` and
`kind: "ciduxx.semantic-change-exhibit"`. The CLI rejects unknown versions and
unknown fields. Runtime validation additionally enforces ordered sequences,
globally unique generated IDs, non-empty answered changes, and unique
idempotency keys.

The lock protects processes updating the same local path. Git branch conflicts
in the single accumulated file still require an ordinary human-reviewed merge;
v1 does not claim to merge independently edited exhibit histories.
