from __future__ import annotations

import json
import unittest

import _support

from agent_harness.claude_runtime import build_command
from agent_harness.mcp_server import REVIEW_SCHEMA
from agent_harness.review import (
    CRITIC_SYSTEM_PROMPT,
    REVIEW_FIELD_DESCRIPTIONS,
    REVIEW_JSON_SCHEMA,
    build_stage_prompt,
    validate_review,
    validate_review_with_normalization,
)
from agent_harness.util import InputError


class ReviewNormalizationTests(unittest.TestCase):
    def test_cli_and_mcp_review_field_rules_match(self) -> None:
        for schema in (REVIEW_JSON_SCHEMA, REVIEW_SCHEMA):
            for field, description in REVIEW_FIELD_DESCRIPTIONS.items():
                self.assertEqual(
                    description, schema["properties"][field]["description"]
                )

    def test_critic_cli_receives_schema_and_explicit_verdict_rules(self) -> None:
        with _support.TempRepo() as repo:
            command = build_command(
                "/fake/claude", profile="critic", model="claude-opus-5", cwd=repo.path
            )
        schema = json.loads(command[command.index("--json-schema") + 1])
        self.assertEqual(REVIEW_JSON_SCHEMA, schema)
        self.assertIn(REVIEW_FIELD_DESCRIPTIONS["verdict"], CRITIC_SYSTEM_PROMPT)
        self.assertEqual(
            CRITIC_SYSTEM_PROMPT,
            command[command.index("--append-system-prompt") + 1],
        )

    def test_findings_override_pass_and_are_preserved(self) -> None:
        source = _support.finding_review()
        source["verdict"] = "pass"

        review, normalization = validate_review_with_normalization(
            source, origin="claude"
        )

        self.assertEqual("changes_requested", review["verdict"])
        self.assertEqual(source["findings"], review["findings"])
        self.assertEqual(
            {
                "original_verdict": "pass",
                "final_verdict": "changes_requested",
                "reason": "findings_present",
            },
            normalization,
        )

    def test_blocking_question_has_priority_over_findings(self) -> None:
        source = _support.finding_review()
        source["verdict"] = "pass"
        source["blocking_question"] = "Which contract is authoritative?"

        review, normalization = validate_review_with_normalization(
            source, origin="claude"
        )

        self.assertEqual("blocked", review["verdict"])
        self.assertEqual(source["findings"], review["findings"])
        self.assertEqual(
            {
                "original_verdict": "pass",
                "final_verdict": "blocked",
                "reason": "blocking_question_present",
            },
            normalization,
        )

    def test_compatible_pass_is_unchanged(self) -> None:
        review, normalization = validate_review_with_normalization(
            _support.PASS_REVIEW, origin="claude"
        )

        self.assertEqual("pass", review["verdict"])
        self.assertEqual([], review["findings"])
        self.assertIsNone(review["blocking_question"])
        self.assertIsNone(normalization)

    def test_malformed_finding_remains_invalid(self) -> None:
        source = _support.finding_review()
        source["verdict"] = "pass"
        source["findings"][0]["severity"] = "P4"

        with self.assertRaisesRegex(InputError, "finding.severity"):
            validate_review_with_normalization(source, origin="claude")

    def test_non_claude_reviews_keep_strict_verdict_validation(self) -> None:
        source = _support.finding_review()
        source["verdict"] = "pass"

        for origin in ("codex", "codex_fallback"):
            with self.subTest(origin=origin):
                with self.assertRaisesRegex(InputError, "pass review"):
                    validate_review(source, origin=origin)

    def test_non_claude_compatible_verdict_is_not_normalized(self) -> None:
        source = dict(_support.PASS_REVIEW)
        source["blocking_question"] = "Which contract is authoritative?"

        review = validate_review(source, origin="codex")

        self.assertEqual("pass", review["verdict"])
        self.assertEqual(source["blocking_question"], review["blocking_question"])


class ReviewTestEvidenceTests(unittest.TestCase):
    def packet(self, checks: dict, fingerprint: str = "current") -> dict:
        prompt = build_stage_prompt(
            profile="critic",
            contract={
                "risk": "medium",
                "goal": "Сохранить результат обработки сообщения",
                "done_when": ["Повтор сообщения не меняет сохранённый результат"],
            },
            state={
                "diff_fingerprint": fingerprint,
                "check_results": checks,
            },
        )
        payload = prompt.split("<agent_harness_packet>\n", 1)[1]
        return json.loads(payload.split("\n</agent_harness_packet>", 1)[0])

    def test_scenario_limits_reach_critic_without_raw_output(self) -> None:
        for summary in (
            "Локальный обработчик и хранилище проверены; настоящий брокер не проверен",
            "Пройдено 2 сценария; проверка реальной интеграции пропущена",
            "Первый запуск: ошибка фикстуры; после исправления оба сценария пройдены",
        ):
            with self.subTest(summary=summary):
                check = {
                    "status": "passed", "exit_code": 0,
                    "duration_ms": 12, "summary": summary,
                    "stdout": "raw output must not enter the packet",
                }
                packet = self.packet({"current": {"scoped-tests": check}})
                self.assertEqual([
                    {"name": "scoped-tests", "status": "passed", "exit_code": 0,
                     "duration_ms": 12, "summary": summary},
                ], packet["current_evidence"]["checks"])
                self.assertEqual(
                    ["Повтор сообщения не меняет сохранённый результат"],
                    packet["task_contract"]["done_when"],
                )

    def test_previous_fingerprint_is_not_current_scenario_proof(self) -> None:
        packet = self.packet({"old": {"scoped-tests": {
            "status": "passed", "exit_code": 0,
            "duration_ms": 12, "summary": "Все сценарии пройдены",
        }}})
        self.assertEqual([], packet["current_evidence"]["checks"])

    def test_missing_and_failed_evidence_are_not_promoted_to_pass(self) -> None:
        self.assertEqual([], self.packet({})["current_evidence"]["checks"])
        packet = self.packet({"current": {"scoped-tests": {
            "status": "failed", "exit_code": 1,
            "duration_ms": 12, "summary": "Повтор создал вторую запись",
        }}})
        self.assertEqual("failed", packet["current_evidence"]["checks"][0]["status"])
        self.assertEqual(1, packet["current_evidence"]["checks"][0]["exit_code"])


if __name__ == "__main__":
    unittest.main()
