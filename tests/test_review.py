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


if __name__ == "__main__":
    unittest.main()
