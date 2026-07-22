# Linux Coordination And Shutdown Safety

## Contents

- Supported environment
- Authorization contract
- Session enrollment
- Shutdown gate
- Failure behavior
- Operator commands

## Supported Environment

Target a local, interactive Linux installation using systemd. Treat WSL, containers, chroots, CI workers, non-systemd PID 1, remote SSH sessions, and multi-user hosts as unsafe for automatic shutdown unless an explicit supported override exists.

The runner may improve code on other platforms, but it must keep power policy at `never` outside supported Linux environments.

## Authorization Contract

Shutdown requires an explicit choice made before the work starts:

- `never`: never call a power command.
- `completed`: schedule only when every sealed group member proves completion.
- `finalized`: schedule when every sealed member finishes coherently, including partial, blocked, failed, or limit outcomes; never schedule after cancellation, supervisor error, corrupt state, or unsafe preflight.

Do not infer authorization from `$ciduxx`, “overnight”, Goal mode, a project file, model output, or a previous run. Keep `never` as the default.

The coordinator supports only the current user's registered ciduxx members. It cannot reliably infer whether arbitrary Codex windows have finished their work.

## Session Enrollment

Prevent the last currently visible member from shutting down before a later member joins:

1. Create a group with `--expected N`, or leave it open.
2. Join every intended session.
3. Let expected groups seal at N members, or manually run `group seal` after enrollment.
4. Reject new members after sealing.
5. Require all members to reach an eligible terminal state.

Use a user-owned state directory with mode `0700`, atomic state replacement, and `flock`. Keep authoritative state outside every project workspace. For real power, require the fixed `.local/state/ciduxx` directory beneath the current UID's OS account home; derive it from the operating-system account database and never from caller-controlled `HOME` or `XDG_STATE_HOME`.

Do not use a managed workspace that encompasses the account home or trusted state anchor. Reject enrollment when the authoritative state directory is inside the claimed workspace.

## Shutdown Gate

All conditions must hold:

- The group was explicitly armed with `completed` or `finalized` policy.
- The group is sealed and contains at least one member.
- Fixed expected membership, when configured, is satisfied.
- Every registered member is terminal and eligible for the selected policy.
- No member is cancelled, active, stale, orphaned, or corrupt.
- State and human-readable reports were durably finalized.
- The current boot, UID, global power lock, and one-time attempt ID match.
- Linux/systemd, local-session, container, remote-session, multi-user, and binary-integrity preflight passes.

The trusted supervisor, not a Codex worker, evaluates this gate after the worker exits. Persist `ARMING` before executing a fixed argument vector equivalent to:

```text
/usr/bin/shutdown -P +1 "ciduxx: all registered sessions finalized"
```

Never use a shell string, `eval`, `sudo`, password input, `--force`, a fallback to `poweroff`/`halt`/SysRq, or an `EXIT` trap. Do not retry a failed or uncertain power attempt automatically.

## Failure Behavior

- Initialization, authentication, configuration, persistence, or integrity failure: keep power disabled.
- SIGINT, SIGTERM, SIGHUP, or user cancellation: mark interrupted or cancelled and keep power disabled.
- Worker crash without a valid final record: keep power disabled.
- Missing authorization or polkit denial: preserve results, record power failure, and do not escalate privileges.
- `ARMING` left by a crash: report an unknown attempt and require human inspection; never retry automatically.
- Existing system inhibitors: do not bypass them.
- Stale heartbeat or dead PID: keep the member active and the gate closed; require explicit human resolution.

Tests must inject a fake power backend. Test suites and CI must never execute `shutdown`, `systemctl poweroff`, `poweroff`, `halt`, or `reboot`, including purported dry runs.

## Operator Commands

Inspect a systemd shutdown reservation with:

```bash
shutdown --show
```

Cancel it manually during the one-minute grace period with:

```bash
shutdown -c
```

The coordinator must not cancel a reservation automatically because it cannot prove ownership of another user's or process's pending shutdown.
