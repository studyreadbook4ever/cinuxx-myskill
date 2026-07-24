from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "skills" / "ciduxx" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import ciduxx_exhibit as exhibit  # noqa: E402


def record_in_process(
    file_name: str,
    state_root: str,
    index: int,
    barrier: multiprocessing.Barrier,
    queue: multiprocessing.Queue,
) -> None:
    os.environ["XDG_STATE_HOME"] = state_root
    try:
        barrier.wait()
        result = exhibit.record_turn(
            Path(file_name),
            request=f"요청 {index}",
            changes=[f"결과 {index}로 바꿨습니다."],
            client="codex",
            display_name="Codex",
            idempotency_key=f"concurrent-{index}",
        )
        queue.put(("ok", result["turn_id"]))
    except Exception as exc:  # pragma: no cover - surfaced through the queue
        queue.put(("error", repr(exc)))


class ExhibitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.path = self.repo / exhibit.DEFAULT_EXHIBIT_NAME
        self.state = self.root / "state"
        self.environment = mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": str(self.state)}, clear=False
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def test_record_creates_one_file_and_accumulates_grouped_turns(self) -> None:
        first = exhibit.record_turn(
            self.path,
            request="로그인 문구를 명확하게 바꿔줘.",
            changes=[
                "버튼 문구를 ‘계속’에서 ‘로그인’으로 바꿨습니다.",
                "스크린 리더용 접근성 이름을 추가했습니다.",
            ],
            client="codex",
            display_name="Codex",
        )
        second = exhibit.record_turn(
            self.path,
            request="오류 메시지를 이해하기 쉽게 해줘.",
            changes=["오류 원인과 해결 방법을 함께 표시하도록 바꿨습니다."],
            client="claude",
            display_name="Claude",
        )

        self.assertTrue(first["created"])
        self.assertTrue(second["created"])
        self.assertEqual(
            [item.name for item in self.repo.iterdir() if item.name != ".git"],
            [exhibit.DEFAULT_EXHIBIT_NAME],
        )
        data = exhibit.read_exhibit(self.path, require_answered=True)
        self.assertEqual(len(data["turns"]), 2)
        self.assertEqual(len(data["turns"][0]["changes"]), 2)
        self.assertEqual(len(data["turns"][1]["changes"]), 1)
        page = self.path.read_text(encoding="utf-8")
        self.assertEqual(page.count('data-role="request"'), 2)
        self.assertEqual(page.count('data-role="change"'), 3)
        self.assertIn("ciduxx-message-row--request", page)
        self.assertIn("ciduxx-message-row--change", page)

    def test_unicode_newlines_and_markup_are_rendered_only_as_text(self) -> None:
        attack = (
            '안녕 👋\n</script><script>window.PWNED=1</script>\n'
            '<img src=x onerror="window.PWNED=2">&'
        )
        exhibit.record_turn(
            self.path,
            request=attack,
            changes=[attack],
            client="codex",
        )
        data = exhibit.read_exhibit(self.path)
        self.assertEqual(data["turns"][0]["request"]["text"], attack)
        self.assertEqual(data["turns"][0]["changes"][0]["text"], attack)
        page = self.path.read_text(encoding="utf-8")
        self.assertEqual(page.count("<script"), 1)
        self.assertNotIn("<script>window.PWNED", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;img src=x", page)
        self.assertIn("\\u003c/script\\u003e", page)
        self.assertIn(
            "default-src 'none'; style-src 'unsafe-inline';", page
        )

    def test_empty_change_is_rejected_without_modifying_existing_file(self) -> None:
        exhibit.record_turn(
            self.path,
            request="기존 요청",
            changes=["기존 변경"],
            client="codex",
        )
        before = self.path.read_bytes()
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.record_turn(
                self.path,
                request="빈 변경",
                changes=[],
                client="codex",
            )
        self.assertEqual(self.path.read_bytes(), before)

    def test_idempotency_retry_does_not_duplicate_turn(self) -> None:
        values = {
            "request": "중복 없이 기록해줘.",
            "changes": ["동일한 재시도를 한 번만 저장하도록 바꿨습니다."],
            "client": "codex",
            "idempotency_key": "message-42",
        }
        first = exhibit.record_turn(self.path, **values)
        second = exhibit.record_turn(self.path, **values)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["turn_id"], second["turn_id"])
        self.assertEqual(len(exhibit.read_exhibit(self.path)["turns"]), 1)
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.record_turn(
                self.path,
                request="다른 내용",
                changes=values["changes"],
                client="codex",
                idempotency_key="message-42",
            )

    def test_begin_answer_and_require_answered_validation(self) -> None:
        pending = exhibit.begin_turn(
            self.path,
            request="긴 작업을 시작해줘.",
            client="claude",
            display_name="Claude",
            idempotency_key="long-task",
        )
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.read_exhibit(self.path, require_answered=True)
        answered = exhibit.answer_turn(
            self.path,
            turn_id=pending["turn_id"],
            changes=["긴 작업의 결과를 의미 단위로 기록했습니다."],
        )
        self.assertEqual(answered["status"], "answered")
        data = exhibit.read_exhibit(self.path, require_answered=True)
        self.assertEqual(data["turns"][0]["status"], "answered")
        self.assertEqual(len(data["turns"][0]["changes"]), 1)
        retry = exhibit.begin_turn(
            self.path,
            request="긴 작업을 시작해줘.",
            client="claude",
            display_name="Claude",
            idempotency_key="long-task",
        )
        self.assertFalse(retry["created"])
        self.assertEqual(retry["status"], "answered")

    def test_partial_task_can_be_updated_without_duplicating_its_turn(self) -> None:
        first = exhibit.record_turn(
            self.path,
            request="단계적으로 검색을 개선해줘.",
            changes=["검색 결과의 빈 상태를 명확하게 표시했습니다."],
            client="codex",
            idempotency_key="ciduxx-task:resume-1",
            status_value="partial",
            update_partial=True,
        )
        second = exhibit.record_turn(
            self.path,
            request="단계적으로 검색을 개선해줘.",
            changes=["키보드로 결과 사이를 이동할 수 있게 했습니다."],
            client="codex",
            idempotency_key="ciduxx-task:resume-1",
            status_value="partial",
            update_partial=True,
        )
        partial_data = exhibit.read_exhibit(self.path)
        self.assertEqual(len(partial_data["turns"][0]["changes"]), 2)
        final = exhibit.record_turn(
            self.path,
            request="단계적으로 검색을 개선해줘.",
            changes=[
                "검색 결과의 빈 상태를 명확하게 표시했습니다.",
                "키보드로 결과 사이를 이동할 수 있게 했습니다.",
            ],
            client="codex",
            idempotency_key="ciduxx-task:resume-1",
            status_value="answered",
            update_partial=True,
        )
        data = exhibit.read_exhibit(self.path, require_answered=True)
        self.assertEqual(first["turn_id"], second["turn_id"])
        self.assertEqual(first["turn_id"], final["turn_id"])
        self.assertTrue(second["updated"])
        self.assertTrue(final["updated"])
        self.assertEqual(len(data["turns"]), 1)
        self.assertEqual(data["turns"][0]["status"], "answered")
        self.assertEqual(len(data["turns"][0]["changes"]), 2)

    def test_custom_skin_survives_append_and_conversation_survives_reskin(self) -> None:
        exhibit.record_turn(
            self.path,
            request="스킨 전 기록",
            changes=["첫 기록을 만들었습니다."],
            client="codex",
        )
        before_turns = exhibit.read_exhibit(self.path)["turns"]
        custom = "/* human-skin-marker */\n:root { --ciduxx-change-bg: #ff00aa; }\n"
        exhibit.apply_skin(self.path, css=custom, name="human")
        after_skin_turns = exhibit.read_exhibit(self.path)["turns"]
        self.assertEqual(before_turns, after_skin_turns)

        exhibit.record_turn(
            self.path,
            request="스킨 후 기록",
            changes=["스킨을 유지하며 두 번째 기록을 추가했습니다."],
            client="claude",
        )
        page = self.path.read_text(encoding="utf-8")
        self.assertIn("/* human-skin-marker */", page)
        self.assertEqual(len(exhibit.read_exhibit(self.path)["turns"]), 2)

        turns_before_reskin = exhibit.read_exhibit(self.path)["turns"]
        default_css = (
            REPO_ROOT
            / "skills"
            / "ciduxx"
            / "assets"
            / "exhibit-skins"
            / "default.css"
        ).read_text(encoding="utf-8")
        exhibit.apply_skin(self.path, css=default_css, name="default")
        self.assertEqual(turns_before_reskin, exhibit.read_exhibit(self.path)["turns"])

    def test_malformed_existing_file_is_not_overwritten(self) -> None:
        broken = b"<!doctype html><p>not a ciduxx exhibit</p>\n"
        self.path.write_bytes(broken)
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.record_turn(
                self.path,
                request="덮어쓰지 마",
                changes=["손상 파일을 보존했습니다."],
                client="codex",
            )
        self.assertEqual(self.path.read_bytes(), broken)

    def test_skin_and_generated_dom_tampering_are_detected(self) -> None:
        exhibit.record_turn(
            self.path,
            request="무결성을 검사해줘.",
            changes=["정적 표시와 원본 데이터가 일치하게 했습니다."],
            client="codex",
        )
        original = self.path.read_text(encoding="utf-8")
        tampered_dom = original.replace(
            "정적 표시와 원본 데이터가 일치하게 했습니다.",
            "FAKE VISIBLE CLAIM",
            1,
        )
        self.path.write_text(tampered_dom, encoding="utf-8")
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.validate_exhibit(self.path)
        repaired = exhibit.rerender(self.path)
        self.assertTrue(repaired["changed"])
        exhibit.validate_exhibit(self.path)

        unsafe_skin = self.path.read_text(encoding="utf-8").replace(
            "</style>",
            "</style><div>FAKE EVIDENCE</div><style>",
            1,
        )
        self.path.write_text(unsafe_skin, encoding="utf-8")
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.validate_exhibit(self.path)
        before = self.path.read_bytes()
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.record_turn(
                self.path,
                request="위조를 보존하지 마.",
                changes=["위조된 표시를 거부했습니다."],
                client="codex",
            )
        self.assertEqual(self.path.read_bytes(), before)

    def test_unsafe_direct_skin_and_reserved_markers_are_rejected(self) -> None:
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.init_exhibit(
                self.path,
                title="Unsafe",
                skin_css="</style><meta http-equiv=refresh content=0><style>",
            )
        self.assertFalse(self.path.exists())
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.init_exhibit(
                self.path,
                title="Markers",
                skin_css=f"/* {exhibit.DATA_BEGIN} */",
            )
        self.assertFalse(self.path.exists())

    def test_render_size_limit_rejects_write_without_corrupting_file(self) -> None:
        exhibit.record_turn(
            self.path,
            request="기존 기록",
            changes=["기존 변경"],
            client="codex",
        )
        before = self.path.read_bytes()
        with mock.patch.object(exhibit, "MAX_DOCUMENT_BYTES", len(before) + 10):
            with self.assertRaises(exhibit.ExhibitError):
                exhibit.record_turn(
                    self.path,
                    request="크기 제한",
                    changes=["x" * 1000],
                    client="codex",
                )
        self.assertEqual(self.path.read_bytes(), before)

    def test_unpaired_surrogate_and_non_boolean_redaction_are_rejected(self) -> None:
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.record_turn(
                self.path,
                request="bad \ud800",
                changes=["안전한 텍스트"],
                client="codex",
            )
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.record_turn(
                self.path,
                request="redaction type",
                changes=["타입을 검사했습니다."],
                client="codex",
                redacted="false",  # type: ignore[arg-type]
            )
        self.assertFalse(self.path.exists())

    def test_atomic_write_failure_preserves_previous_bytes(self) -> None:
        exhibit.record_turn(
            self.path,
            request="원본",
            changes=["원본을 만들었습니다."],
            client="codex",
        )
        before = self.path.read_bytes()
        with mock.patch.object(
            exhibit, "atomic_write_text", side_effect=OSError("injected")
        ):
            with self.assertRaises(OSError):
                exhibit.record_turn(
                    self.path,
                    request="실패",
                    changes=["저장에 실패했습니다."],
                    client="codex",
                )
        self.assertEqual(self.path.read_bytes(), before)

    def test_symlink_target_and_outside_path_are_rejected(self) -> None:
        outside = self.root / "outside.html"
        outside.write_text("outside", encoding="utf-8")
        self.path.symlink_to(outside)
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.resolve_exhibit_path(
                self.path, workspace=self.repo, allow_outside_workspace=False
            )
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.resolve_exhibit_path(
                outside, workspace=self.repo, allow_outside_workspace=False
            )

    def test_rerender_is_byte_stable(self) -> None:
        exhibit.record_turn(
            self.path,
            request="결정적으로 렌더해줘.",
            changes=["동일한 데이터와 스킨에서 같은 HTML을 만들게 했습니다."],
            client="codex",
        )
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        result = exhibit.rerender(self.path)
        after = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertFalse(result["changed"])
        self.assertEqual(before, after)

    def test_concurrent_writers_do_not_lose_turns(self) -> None:
        count = 8
        barrier = multiprocessing.Barrier(count)
        queue: multiprocessing.Queue = multiprocessing.Queue()
        processes = [
            multiprocessing.Process(
                target=record_in_process,
                args=(str(self.path), str(self.state), index, barrier, queue),
            )
            for index in range(count)
        ]
        for process in processes:
            process.start()
        results = [queue.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)
            self.assertEqual(process.exitcode, 0)
        self.assertTrue(all(status == "ok" for status, _ in results), results)
        data = exhibit.read_exhibit(self.path, require_answered=True)
        self.assertEqual(len(data["turns"]), count)
        self.assertEqual(
            {turn["idempotency_key"] for turn in data["turns"]},
            {f"concurrent-{index}" for index in range(count)},
        )
        self.assertFalse(any(self.repo.glob("*.lock")))

    def test_cli_accepts_claude_json_stdin_and_defaults_to_git_root(self) -> None:
        nested = self.repo / "nested"
        nested.mkdir()
        payload = {
            "request": "Claude에서도 같은 포맷을 써줘.",
            "changes": ["벤더 중립 JSON 입력으로 기록하도록 만들었습니다."],
            "client": "claude",
            "display_name": "Claude",
            "idempotency_key": "claude-1",
        }
        environment = os.environ.copy()
        environment["XDG_STATE_HOME"] = str(self.state)
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "ciduxx.py"),
                "exhibit",
                "record",
                "--payload",
                "-",
            ],
            cwd=nested,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            env=environment,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(Path(response["file"]), self.path)
        data = exhibit.read_exhibit(self.path)
        self.assertEqual(data["turns"][0]["agent"]["client"], "claude")

    def test_cli_rejects_mixed_payload_and_flag_inputs(self) -> None:
        payload = {
            "request": "한 입력 방식만 써줘.",
            "changes": ["혼합 입력을 거부하도록 했습니다."],
        }
        environment = os.environ.copy()
        environment["XDG_STATE_HOME"] = str(self.state)
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "ciduxx.py"),
                "exhibit",
                "record",
                "--payload",
                "-",
                "--client",
                "claude",
            ],
            cwd=self.repo,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            env=environment,
            timeout=20,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--payload cannot be combined", result.stderr)
        self.assertFalse(self.path.exists())

    def test_validate_rejects_unknown_schema_without_rewriting(self) -> None:
        exhibit.init_exhibit(self.path, title="Schema Test")
        page = self.path.read_text(encoding="utf-8")
        page = page.replace('"schema_version": 1', '"schema_version": 99', 1)
        self.path.write_text(page, encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.validate_exhibit(self.path)
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.record_turn(
                self.path,
                request="스키마를 무시하지 마.",
                changes=["지원하지 않는 버전을 거부했습니다."],
                client="codex",
            )
        self.assertEqual(self.path.read_bytes(), before)

    def test_validate_rejects_boolean_numbers_and_noncanonical_client(self) -> None:
        exhibit.record_turn(
            self.path,
            request="엄격한 스키마",
            changes=["스키마 타입을 엄격하게 검사했습니다."],
            client="codex",
        )
        data = exhibit.read_exhibit(self.path)
        data["schema_version"] = True
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.validate_document(data)
        data = exhibit.read_exhibit(self.path)
        data["turns"][0]["sequence"] = True
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.validate_document(data)
        data = exhibit.read_exhibit(self.path)
        data["turns"][0]["agent"]["client"] = "CODEX"
        with self.assertRaises(exhibit.ExhibitError):
            exhibit.validate_document(data)

    def test_instruction_path_is_shell_quoted(self) -> None:
        output = exhibit._instructions("claude", "logs/My Exhibit.html")
        self.assertIn("--file 'logs/My Exhibit.html' --payload -", output)


if __name__ == "__main__":
    unittest.main()
