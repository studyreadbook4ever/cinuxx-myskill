from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "skills" / "ciduxx" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import ciduxx as ciduxx_cli  # noqa: E402
import ciduxx_exhibit as exhibit  # noqa: E402

from ciduxx_core import (  # noqa: E402
    GroupStore,
    PowerError,
    PowerResult,
    StateError,
    SystemShutdownBackend,
    execute_power_intent,
    secure_artifact_dir,
    trusted_power_state_root,
)


class FakePower:
    def __init__(self, success: bool = True) -> None:
        self.success = success
        self.calls: list[tuple[int, str]] = []

    def schedule(self, delay_minutes: int, reason: str) -> PowerResult:
        self.calls.append((delay_minutes, reason))
        return PowerResult(
            self.success, "fake power result", ("fake-shutdown", f"+{delay_minutes}")
        )


class FalseyPower(FakePower):
    def __bool__(self) -> bool:
        return False


class UncertainPower(FakePower):
    def schedule(self, delay_minutes: int, reason: str) -> PowerResult:
        self.calls.append((delay_minutes, reason))
        return PowerResult(
            False,
            "fake scheduling outcome is unknown",
            ("fake-shutdown", f"+{delay_minutes}"),
            uncertain=True,
        )


def finish_in_process(
    state_root: str,
    group_id: str,
    member_id: str,
    barrier: multiprocessing.Barrier,
    queue: multiprocessing.Queue,
) -> None:
    store = GroupStore(Path(state_root))
    barrier.wait()
    _state, intent = store.finish(
        group_id,
        member_id,
        status_value="completed",
        summary="done",
        evidence=["test passed"],
    )
    queue.put(bool(intent))


class GroupGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "state"
        self.workspace_a = Path(self.temporary.name) / "workspace-a"
        self.workspace_b = Path(self.temporary.name) / "workspace-b"
        self.workspace_a.mkdir()
        self.workspace_b.mkdir()
        self.store = GroupStore(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_group(self, policy: str = "completed", expected: int | None = 2):
        return self.store.create(
            name="tonight",
            expected=expected,
            shutdown_on=policy,
            delay_minutes=1,
        )

    def test_last_completed_member_arms_exactly_once(self) -> None:
        group = self.make_group()
        group_id = group["group_id"]
        _, first = self.store.join(group_id, name="one", workspace=self.workspace_a)
        joined, second = self.store.join(
            group_id, name="two", workspace=self.workspace_b
        )
        self.assertTrue(joined["sealed"])

        state, first_intent = self.store.finish(
            group_id, first, status_value="completed", summary="one done"
        )
        self.assertIsNone(first_intent)
        self.assertEqual(state["status"], "sealed")

        state, last_intent = self.store.finish(
            group_id, second, status_value="completed", summary="two done"
        )
        self.assertIsNotNone(last_intent)
        self.assertEqual(state["status"], "arming")
        fake = FakePower()
        final = execute_power_intent(self.store, last_intent, fake)
        self.assertEqual(final["status"], "armed")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][0], 1)

        repeated, repeated_intent = self.store.finish(
            group_id, second, status_value="completed", summary="two done"
        )
        self.assertEqual(repeated["status"], "armed")
        self.assertIsNone(repeated_intent)
        self.assertEqual(len(fake.calls), 1)

    def test_completed_policy_rejects_partial_member(self) -> None:
        group_id = self.make_group()["group_id"]
        _, first = self.store.join(group_id, name="one", workspace=self.workspace_a)
        _, second = self.store.join(group_id, name="two", workspace=self.workspace_b)
        self.store.finish(group_id, first, status_value="completed", summary="done")
        state, intent = self.store.finish(
            group_id, second, status_value="partial", summary="partial"
        )
        self.assertEqual(state["status"], "ineligible")
        self.assertIsNone(intent)

    def test_finalized_policy_accepts_partial_but_not_cancelled(self) -> None:
        group_id = self.make_group(policy="finalized")["group_id"]
        _, first = self.store.join(group_id, name="one", workspace=self.workspace_a)
        _, second = self.store.join(group_id, name="two", workspace=self.workspace_b)
        self.store.finish(group_id, first, status_value="completed", summary="done")
        state, intent = self.store.finish(
            group_id, second, status_value="partial", summary="partial"
        )
        self.assertEqual(state["status"], "arming")
        self.assertIsNotNone(intent)

        other = self.make_group(policy="finalized")
        _, first = self.store.join(
            other["group_id"], name="one", workspace=self.workspace_a
        )
        _, second = self.store.join(
            other["group_id"], name="two", workspace=self.workspace_b
        )
        self.store.finish(
            other["group_id"], first, status_value="completed", summary="done"
        )
        state, intent = self.store.finish(
            other["group_id"], second, status_value="cancelled", summary="cancelled"
        )
        self.assertEqual(state["status"], "ineligible")
        self.assertIsNone(intent)

    def test_open_group_requires_explicit_seal(self) -> None:
        group_id = self.make_group(expected=None)["group_id"]
        _, member = self.store.join(group_id, name="one", workspace=self.workspace_a)
        state, intent = self.store.finish(
            group_id, member, status_value="completed", summary="done"
        )
        self.assertEqual(state["status"], "open")
        self.assertIsNone(intent)
        state, intent = self.store.seal(group_id)
        self.assertEqual(state["status"], "arming")
        self.assertIsNotNone(intent)

    def test_cancelled_group_cannot_be_rearmed_by_remaining_member(self) -> None:
        group_id = self.make_group()["group_id"]
        _, first = self.store.join(group_id, name="one", workspace=self.workspace_a)
        _, second = self.store.join(group_id, name="two", workspace=self.workspace_b)
        self.store.finish(group_id, first, status_value="completed", summary="done")
        cancelled = self.store.cancel(group_id, "operator cancelled")
        self.assertEqual(cancelled["status"], "cancelled")
        with self.assertRaises(StateError):
            self.store.finish(
                group_id, second, status_value="completed", summary="too late"
            )
        with self.assertRaises(StateError):
            self.store.heartbeat(group_id, second)
        self.assertEqual(self.store.read(group_id)["status"], "cancelled")

    def test_concurrent_finalizers_prepare_one_power_attempt(self) -> None:
        group_id = self.make_group()["group_id"]
        _, first = self.store.join(group_id, name="one", workspace=self.workspace_a)
        _, second = self.store.join(group_id, name="two", workspace=self.workspace_b)
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        queue = context.Queue()
        processes = [
            context.Process(
                target=finish_in_process,
                args=(str(self.root), group_id, member, barrier, queue),
            )
            for member in (first, second)
        ]
        for process in processes:
            process.start()
        results = [queue.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(results.count(True), 1)
        self.assertEqual(self.store.read(group_id)["status"], "arming")

    def test_global_power_ledger_blocks_a_second_group(self) -> None:
        intents = []
        for index, workspace in enumerate(
            (self.workspace_a, self.workspace_b), start=1
        ):
            group = self.store.create(
                name=f"group-{index}",
                expected=1,
                shutdown_on="completed",
                delay_minutes=1,
            )
            _, member = self.store.join(
                group["group_id"], name=f"member-{index}", workspace=workspace
            )
            _, intent = self.store.finish(
                group["group_id"], member, status_value="completed", summary="done"
            )
            self.assertIsNotNone(intent)
            intents.append(intent)

        fake = FakePower()
        first = execute_power_intent(self.store, intents[0], fake)
        second = execute_power_intent(self.store, intents[1], fake)
        self.assertEqual(first["status"], "armed")
        self.assertEqual(second["status"], "power_failed")
        self.assertEqual(len(fake.calls), 1)

    def test_real_backend_requires_an_explicit_capability(self) -> None:
        group = self.store.create(
            name="single",
            expected=1,
            shutdown_on="completed",
            delay_minutes=1,
        )
        _, member = self.store.join(
            group["group_id"], name="one", workspace=self.workspace_a
        )
        _, intent = self.store.finish(
            group["group_id"], member, status_value="completed", summary="done"
        )
        self.assertIsNotNone(intent)
        with mock.patch("ciduxx_core.SystemShutdownBackend") as backend_type:
            state = execute_power_intent(self.store, intent)
        self.assertEqual(state["status"], "power_failed")
        self.assertIn("allow_real_power", state["shutdown"]["detail"])
        backend_type.assert_not_called()
        self.assertFalse((self.root / "power.json").exists())

    def test_falsey_injected_backend_is_still_used(self) -> None:
        group = self.store.create(
            name="single",
            expected=1,
            shutdown_on="completed",
            delay_minutes=1,
        )
        _, member = self.store.join(
            group["group_id"], name="one", workspace=self.workspace_a
        )
        _, intent = self.store.finish(
            group["group_id"], member, status_value="completed", summary="done"
        )
        fake = FalseyPower()
        state = execute_power_intent(self.store, intent, fake)
        self.assertEqual(state["status"], "armed")
        self.assertEqual(len(fake.calls), 1)

    def test_uncertain_power_result_is_never_retried_or_cancelled(self) -> None:
        group = self.store.create(
            name="single",
            expected=1,
            shutdown_on="completed",
            delay_minutes=1,
        )
        _, member = self.store.join(
            group["group_id"], name="one", workspace=self.workspace_a
        )
        _, intent = self.store.finish(
            group["group_id"], member, status_value="completed", summary="done"
        )
        fake = UncertainPower()
        state = execute_power_intent(self.store, intent, fake)
        self.assertEqual(state["status"], "arming_unknown")
        ledger = json.loads((self.root / "power.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["status"], "arming_unknown")
        with self.assertRaises(StateError):
            self.store.cancel(group["group_id"], "too late")
        self.assertEqual(len(fake.calls), 1)

    def test_malformed_power_ledger_blocks_the_adapter(self) -> None:
        group = self.store.create(
            name="single",
            expected=1,
            shutdown_on="completed",
            delay_minutes=1,
        )
        _, member = self.store.join(
            group["group_id"], name="one", workspace=self.workspace_a
        )
        _, intent = self.store.finish(
            group["group_id"], member, status_value="completed", summary="done"
        )
        (self.root / "power.json").write_text("{broken", encoding="utf-8")
        fake = FakePower()
        state = execute_power_intent(self.store, intent, fake)
        self.assertEqual(state["status"], "power_failed")
        self.assertIn("ledger is invalid", state["shutdown"]["detail"])
        self.assertEqual(fake.calls, [])

    def test_changed_persisted_intent_blocks_the_adapter(self) -> None:
        group = self.store.create(
            name="single",
            expected=1,
            shutdown_on="completed",
            delay_minutes=1,
        )
        _, member = self.store.join(
            group["group_id"], name="one", workspace=self.workspace_a
        )
        _, intent = self.store.finish(
            group["group_id"], member, status_value="completed", summary="done"
        )
        state_path = self.root / "groups" / group["group_id"] / "group.json"
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        persisted["shutdown"]["delay_minutes"] = 2
        state_path.write_text(json.dumps(persisted), encoding="utf-8")
        fake = FakePower()
        with self.assertRaises(StateError):
            execute_power_intent(self.store, intent, fake)
        self.assertEqual(fake.calls, [])


class PathAndPowerTests(unittest.TestCase):
    def test_nested_cli_cannot_redirect_xdg_into_workspace_for_power(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            untrusted_repo = root / "untrusted-repo"
            claimed_workspace = root / "claimed-workspace"
            untrusted_repo.mkdir()
            claimed_workspace.mkdir()
            environment = os.environ.copy()
            environment["XDG_STATE_HOME"] = str(untrusted_repo / "xdg-state")
            environment["CI"] = "ciduxx-regression-safety-net"
            base = [sys.executable, str(SCRIPT_DIR / "ciduxx.py")]

            created = subprocess.run(
                [
                    *base,
                    "group",
                    "create",
                    "--name",
                    "redirected",
                    "--expected",
                    "1",
                    "--shutdown-on",
                    "completed",
                ],
                cwd=untrusted_repo,
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            group_id = json.loads(created.stdout)["group_id"]
            joined = subprocess.run(
                [
                    *base,
                    "group",
                    "join",
                    group_id,
                    "--name",
                    "nested-worker",
                    "--workspace",
                    str(claimed_workspace),
                ],
                cwd=untrusted_repo,
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(joined.returncode, 0, joined.stderr)
            member_id = json.loads(joined.stdout)["member_id"]
            finished = subprocess.run(
                [
                    *base,
                    "group",
                    "finish",
                    group_id,
                    member_id,
                    "--status",
                    "completed",
                    "--summary",
                    "claimed done",
                ],
                cwd=untrusted_repo,
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(finished.returncode, ciduxx_cli.EXIT_POWER)
            state = json.loads(finished.stdout)
            self.assertEqual(state["status"], "power_failed")
            self.assertIn("fixed OS-account state root", state["shutdown"]["detail"])
            redirected_root = Path(environment["XDG_STATE_HOME"]) / "ciduxx"
            self.assertFalse((redirected_root / "power.json").exists())

    def test_xdg_redirection_cannot_grant_cli_power_capability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            untrusted_repo = root / "untrusted-repo"
            claimed_workspace = root / "claimed-workspace"
            untrusted_repo.mkdir()
            claimed_workspace.mkdir()
            xdg = untrusted_repo / "xdg-state"
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(xdg)}):
                store = GroupStore()
                self.assertNotEqual(store.root, trusted_power_state_root())
                group = store.create(
                    name="redirected",
                    expected=1,
                    shutdown_on="completed",
                    delay_minutes=1,
                )
                _, member = store.join(
                    group["group_id"],
                    name="nested-worker",
                    workspace=claimed_workspace,
                )
                _, intent = store.finish(
                    group["group_id"],
                    member,
                    status_value="completed",
                    summary="claimed done",
                )
                with mock.patch.object(ciduxx_cli, "execute_power_intent") as execute:
                    state = ciduxx_cli._handle_power_intent(store, intent)
            self.assertEqual(state["status"], "power_failed")
            self.assertIn("fixed OS-account state root", state["shutdown"]["detail"])
            execute.assert_not_called()

    def test_xdg_redirection_cannot_bypass_core_power_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claimed_workspace = root / "claimed-workspace"
            claimed_workspace.mkdir()
            with mock.patch.dict(
                os.environ, {"XDG_STATE_HOME": str(root / "attacker-state")}
            ):
                store = GroupStore()
                group = store.create(
                    name="redirected",
                    expected=1,
                    shutdown_on="completed",
                    delay_minutes=1,
                )
                _, member = store.join(
                    group["group_id"], name="nested", workspace=claimed_workspace
                )
                _, intent = store.finish(
                    group["group_id"],
                    member,
                    status_value="completed",
                    summary="claimed done",
                )
                with mock.patch("ciduxx_core.SystemShutdownBackend") as backend_type:
                    state = execute_power_intent(store, intent, allow_real_power=True)
            self.assertEqual(state["status"], "power_failed")
            self.assertIn("fixed trusted state root", state["shutdown"]["detail"])
            backend_type.assert_not_called()
            self.assertFalse((store.root / "power.json").exists())

    def test_artifact_path_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            outside = Path(temporary) / "outside"
            workspace.mkdir()
            outside.mkdir()
            (workspace / ".ciduxx").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(StateError):
                secure_artifact_dir(workspace, "r-20260723T000000Z-1234abcd")

    def test_power_delay_rejects_shell_payload_before_preflight(self) -> None:
        backend = SystemShutdownBackend()
        with self.assertRaises(PowerError):
            backend.schedule("1; reboot", "test")  # type: ignore[arg-type]

    def test_shutdown_timeout_is_an_uncertain_result(self) -> None:
        backend = SystemShutdownBackend()
        with (
            mock.patch.object(
                backend, "preflight", return_value=Path("/usr/bin/shutdown")
            ),
            mock.patch.object(backend, "_existing_reservation", return_value=None),
            mock.patch(
                "ciduxx_core.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["shutdown"], 15),
            ),
        ):
            result = backend.schedule(1, "ciduxx test")
        self.assertFalse(result.success)
        self.assertTrue(result.uncertain)

    def test_container_environment_marker_is_rejected_without_probing(self) -> None:
        backend = SystemShutdownBackend()
        with mock.patch.dict(os.environ, {"container": "ciduxx-test"}):
            self.assertTrue(backend._inside_container())

    def test_source_never_uses_shell_true_or_os_system(self) -> None:
        source = (SCRIPT_DIR / "ciduxx.py").read_text(encoding="utf-8")
        core = (SCRIPT_DIR / "ciduxx_core.py").read_text(encoding="utf-8")
        combined = source + core
        self.assertNotIn("shell=True", combined)
        self.assertNotIn("os.system(", combined)
        self.assertNotIn("subprocess.call(", combined)


class RunnerIntegrationTests(unittest.TestCase):
    def test_bare_codex_name_cannot_be_shadowed_by_workspace_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            shadow = repo / "codex"
            marker = root / "shadow-ran"
            shadow.write_text(f"#!/bin/sh\ntouch {marker}\nexit 99\n", encoding="utf-8")
            shadow.chmod(0o755)
            command = [
                sys.executable,
                str(SCRIPT_DIR / "ciduxx.py"),
                "--state-root",
                str(root / "state"),
                "run",
                "--workspace",
                str(repo),
                "--objective",
                "Do nothing.",
                "--codex-bin",
                "codex",
                "--allow-non-git",
            ]
            environment = os.environ.copy()
            environment["PATH"] = "."
            result = subprocess.run(
                command,
                cwd=repo,
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Codex executable is unavailable", result.stderr)
            self.assertFalse(marker.exists())

    def test_fake_codex_completes_with_two_fresh_auditors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo with spaces"
            repo.mkdir()
            subprocess.run(
                ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "config", "user.email", "ciduxx@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Ciduxx Test"], cwd=repo, check=True
            )
            (repo / "program.txt").write_text("stable\n", encoding="utf-8")
            subprocess.run(["git", "add", "program.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "fixture"],
                cwd=repo,
                check=True,
                capture_output=True,
            )

            fake = root / "fake codex"
            fake.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import pathlib
                    import sys

                    args = sys.argv[1:]
                    output = pathlib.Path(args[args.index("-o") + 1])
                    schema = pathlib.Path(args[args.index("--output-schema") + 1]).name
                    if schema.startswith("auditor"):
                        payload = {
                            "schema_version": 1,
                            "verdict": "pass",
                            "summary": "independent audit passed",
                            "findings": [],
                            "evidence": ["fixture inspected"],
                        }
                    else:
                        pathlib.Path("program.txt").write_text(
                            "stable\\nstatus: ready\\n", encoding="utf-8"
                        )
                        payload = {
                            "schema_version": 1,
                            "status": "completed",
                            "summary": "status line added and verified",
                            "progress": ["added the requested status line"],
                            "verification": [{"command": "inspect", "outcome": "passed"}],
                            "decisions": [{
                                "status": "AUTO-DECIDED",
                                "question": "Which fixture strategy should be used?",
                                "options": [
                                    {"label": "A", "text": "Keep the stable fixture"},
                                    {"label": "B", "text": "Replace the fixture"},
                                ],
                                "chosen": "A",
                                "basis": "A concise status line satisfies the objective.",
                                "evidence": ["program.txt contains status: ready"],
                                "action": "Add the status line.",
                                "rollback": "Restore the prior fixture if needed.",
                                "revisit_when": "The fixture becomes unstable.",
                            }],
                            "completion_evidence": ["program.txt contains status: ready"],
                            "remaining": [],
                            "next_prompt": "",
                            "display_request": "Add a clear readiness status to the fixture program.",
                            "display_request_redacted": False,
                            "display_changes": [
                                "Added a clear ready status to the fixture output."
                            ],
                        }
                    output.write_text(json.dumps(payload), encoding="utf-8")
                    print(json.dumps({"type": "thread.started", "thread_id": "00000000-0000-0000-0000-000000000001"}))
                    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20}}))
                    """
                ),
                encoding="utf-8",
            )
            fake.chmod(0o755)
            command = [
                sys.executable,
                str(SCRIPT_DIR / "ciduxx.py"),
                "--state-root",
                str(root / "state"),
                "run",
                "--workspace",
                str(repo),
                "--objective",
                "Add a clear readiness status to the fixture program.",
                "--codex-bin",
                "./fake codex",
                "--max-iterations",
                "2",
                "--max-hours",
                "1",
                "--turn-timeout-minutes",
                "1",
                "--verifiers",
                "2",
                "--exhibit-file",
                "AI_CHANGELOG.html",
            ]
            result = subprocess.run(
                command, cwd=root, text=True, capture_output=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            response = json.loads(result.stdout)
            self.assertEqual(response["outcome"], "completed")
            self.assertEqual(response["usage"]["input_tokens"], 300)
            artifact = Path(response["artifact_dir"])
            self.assertTrue((artifact / "summary.md").is_file())
            self.assertTrue((artifact / "decisions.md").is_file())
            self.assertTrue((artifact / "objective.md").is_file())
            decisions = (artifact / "decisions.md").read_text(encoding="utf-8")
            self.assertIn("A: Keep the stable fixture", decisions)
            self.assertIn("B: Replace the fixture", decisions)
            summary = (artifact / "summary.md").read_text(encoding="utf-8")
            self.assertIn("## Resume", summary)
            self.assertIn(f"--state-root {root / 'state'}", summary)
            self.assertIn(f"--codex-bin '{fake}'", summary)
            self.assertIn("--exhibit-file AI_CHANGELOG.html", summary)
            self.assertIn("--exhibit-task-key", summary)
            self.assertEqual(response["group"]["status"], "complete")
            self.assertEqual(
                (repo / "program.txt").read_text(encoding="utf-8"),
                "stable\nstatus: ready\n",
            )
            exhibit = repo / "AI_CHANGELOG.html"
            self.assertTrue(exhibit.is_file())
            exhibit_text = exhibit.read_text(encoding="utf-8")
            self.assertIn(
                "Add a clear readiness status to the fixture program.", exhibit_text
            )
            self.assertIn(
                "Added a clear ready status to the fixture output.", exhibit_text
            )
            self.assertEqual(response["exhibit"]["status"], "answered")

    def test_managed_runner_auto_detects_exhibit_and_honors_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root / "fake-codex"
            fake.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import pathlib
                    import sys

                    args = sys.argv[1:]
                    output = pathlib.Path(args[args.index("-o") + 1])
                    pathlib.Path("program.txt").write_text(
                        "before\\nafter\\n", encoding="utf-8"
                    )
                    payload = {
                        "schema_version": 1,
                        "status": "completed",
                        "summary": "program updated",
                        "progress": ["added the requested output"],
                        "verification": [{"command": "inspect", "outcome": "passed"}],
                        "decisions": [],
                        "completion_evidence": ["program.txt contains after"],
                        "remaining": [],
                        "next_prompt": "",
                        "display_request": "Add the after line.",
                        "display_request_redacted": False,
                        "display_changes": ["Added the requested after line."],
                    }
                    output.write_text(json.dumps(payload), encoding="utf-8")
                    print(json.dumps({
                        "type": "thread.started",
                        "thread_id": "00000000-0000-0000-0000-000000000002"
                    }))
                    print(json.dumps({
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 1,
                            "cached_input_tokens": 0,
                            "output_tokens": 1
                        }
                    }))
                    """
                ),
                encoding="utf-8",
            )
            fake.chmod(0o755)

            def make_repo(name: str) -> Path:
                repo = root / name
                repo.mkdir()
                subprocess.run(
                    ["git", "init", "-b", "main"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "config", "user.email", "ciduxx@example.invalid"],
                    cwd=repo,
                    check=True,
                )
                subprocess.run(
                    ["git", "config", "user.name", "Ciduxx Test"],
                    cwd=repo,
                    check=True,
                )
                (repo / "program.txt").write_text("before\n", encoding="utf-8")
                with mock.patch.dict(
                    os.environ, {"XDG_STATE_HOME": str(root / "init-state")}
                ):
                    exhibit.init_exhibit(
                        repo / exhibit.DEFAULT_EXHIBIT_NAME,
                        title=f"{name} AI Change Log",
                    )
                subprocess.run(["git", "add", "."], cwd=repo, check=True)
                subprocess.run(
                    ["git", "commit", "-m", "fixture"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                )
                return repo

            auto_repo = make_repo("auto")
            auto_result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "ciduxx.py"),
                    "--state-root",
                    str(root / "auto-state"),
                    "run",
                    "--workspace",
                    str(auto_repo),
                    "--objective",
                    "Add the after line.",
                    "--codex-bin",
                    str(fake),
                    "--max-iterations",
                    "1",
                    "--max-hours",
                    "1",
                    "--turn-timeout-minutes",
                    "1",
                    "--verifiers",
                    "0",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(
                auto_result.returncode, 0, auto_result.stderr + auto_result.stdout
            )
            auto_data = exhibit.read_exhibit(
                auto_repo / exhibit.DEFAULT_EXHIBIT_NAME
            )
            self.assertEqual(len(auto_data["turns"]), 1)

            opted_out_repo = make_repo("opted-out")
            opted_out_file = opted_out_repo / exhibit.DEFAULT_EXHIBIT_NAME
            before = opted_out_file.read_bytes()
            opted_out_result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "ciduxx.py"),
                    "--state-root",
                    str(root / "opted-out-state"),
                    "run",
                    "--workspace",
                    str(opted_out_repo),
                    "--objective",
                    "Add the after line.",
                    "--codex-bin",
                    str(fake),
                    "--max-iterations",
                    "1",
                    "--max-hours",
                    "1",
                    "--turn-timeout-minutes",
                    "1",
                    "--verifiers",
                    "0",
                    "--no-exhibit",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(
                opted_out_result.returncode,
                0,
                opted_out_result.stderr + opted_out_result.stdout,
            )
            self.assertEqual(opted_out_file.read_bytes(), before)
            self.assertIsNone(json.loads(opted_out_result.stdout)["exhibit"])


if __name__ == "__main__":
    unittest.main()
