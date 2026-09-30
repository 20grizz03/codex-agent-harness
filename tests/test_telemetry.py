from __future__ import annotations

import unittest

import _support

from agent_harness.service import HarnessService
from agent_harness.util import InputError, StateError


class CodexTelemetryTests(unittest.TestCase):
    def _run(self, repo: _support.TempRepo) -> tuple[HarnessService, dict]:
        service = HarnessService({})
        run_id = service.create_run({
            "workspace": str(repo.path), "goal": "Check usage", "done_when": ["Done"],
        })["contract"]["run_id"]
        return service, {"workspace": str(repo.path), "target_type": "run", "target_id": run_id}

    @staticmethod
    def _observation(**overrides: object) -> dict:
        value = {
            "observation_id": "native-1", "role": "executor", "attempt_id": "attempt-1",
            "path": "primary", "scope_id": "thread-1", "availability": "available",
            "baseline": {"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 20},
            "final": {"input_tokens": 150, "output_tokens": 25, "cached_input_tokens": 35},
        }
        value.update(overrides)
        return value

    def test_run_ingests_cumulative_delta_and_deduplicates_after_restart(self) -> None:
        with _support.TempRepo() as repo:
            service, target = self._run(repo)
            request = {**target, **self._observation()}
            first = service.record_codex_telemetry(request)
            self.assertEqual(50, first["observation"]["delta"]["input_tokens"])
            self.assertEqual(15, first["observation"]["delta"]["output_tokens"])
            self.assertIsNone(first["summary"]["cost"])
            duplicate = HarnessService({}).record_codex_telemetry(request)
            self.assertTrue(duplicate["deduplicated"])
            self.assertEqual(1, len(service.get_run({"workspace": target["workspace"], "run_id": target["target_id"]})["state"]["codex_telemetry"]["observations"]))
            self.assertEqual("available", duplicate["summary"]["coverage"])
            with self.assertRaisesRegex(StateError, "different telemetry"):
                service.record_codex_telemetry({**request, "final": {"input_tokens": 151, "output_tokens": 25, "cached_input_tokens": 35}})

    def test_partial_interrupted_and_unavailable_are_distinct_from_zero(self) -> None:
        with _support.TempRepo() as repo:
            service, target = self._run(repo)
            no_data = service.record_codex_telemetry({
                **target, **self._observation(observation_id="missing", attempt_id="attempt-1",
                                             availability="unavailable", reason="not_exposed",
                                             baseline=None, final=None),
            })
            self.assertEqual("unavailable", no_data["summary"]["coverage"])
            self.assertIsNone(no_data["observation"]["delta"])
            self.assertEqual({}, no_data["summary"]["by_role"]["executor"]["observed_token_deltas"])
            partial = service.record_codex_telemetry({
                **target, **self._observation(observation_id="interrupted", attempt_id="attempt-2",
                                             path="retry", availability="partial",
                                             reason="interrupted", final=None),
            })
            self.assertEqual("partial", partial["summary"]["coverage"])
            self.assertEqual({}, partial["observation"]["delta"])
            zero = service.record_codex_telemetry({
                **target, **self._observation(observation_id="zero", attempt_id="attempt-3",
                                             path="fallback", scope_id="thread-2",
                                             baseline={"input_tokens": 0, "output_tokens": 0},
                                             final={"input_tokens": 0, "output_tokens": 0}),
            })
            self.assertEqual(0, zero["observation"]["delta"]["input_tokens"])
            self.assertEqual(1, zero["summary"]["by_role"]["executor"]["available"])
            self.assertEqual(1, zero["summary"]["by_role"]["executor"]["partial"])
            self.assertEqual(1, zero["summary"]["by_role"]["executor"]["unavailable"])

    def test_roles_retries_and_fallback_remain_separate_and_overlap_is_rejected(self) -> None:
        with _support.TempRepo() as repo:
            service, target = self._run(repo)
            service.record_codex_telemetry({**target, **self._observation()})
            with self.assertRaisesRegex(StateError, "role, attempt, and path"):
                service.record_codex_telemetry({**target, **self._observation(observation_id="again")})
            with self.assertRaisesRegex(StateError, "overlap"):
                service.record_codex_telemetry({**target, **self._observation(
                    observation_id="overlap", role="reviewer", attempt_id="critic-1",
                    path="fallback", baseline={"input_tokens": 140, "output_tokens": 20},
                    final={"input_tokens": 180, "output_tokens": 40})})
            result = service.record_codex_telemetry({**target, **self._observation(
                observation_id="critic", role="reviewer", attempt_id="critic-1",
                path="fallback", scope_id="thread-critic",
                baseline={"input_tokens": 0, "output_tokens": 0},
                final={"input_tokens": 8, "output_tokens": 4})})
            self.assertEqual(50, result["summary"]["by_role"]["executor"]["observed_token_deltas"]["input_tokens"])
            self.assertEqual(8, result["summary"]["by_role"]["reviewer"]["observed_token_deltas"]["input_tokens"])

    def test_rejects_raw_payload_negative_and_inconsistent_availability(self) -> None:
        with _support.TempRepo() as repo:
            service, target = self._run(repo)
            for observation in (
                self._observation(raw_log="secret"),
                self._observation(final={"input_tokens": -1, "output_tokens": 25}),
                self._observation(final={"input_tokens": 90, "output_tokens": 25}),
                self._observation(availability="unavailable", baseline=None, final=None),
                self._observation(availability="partial", reason="interrupted"),
                self._observation(baseline={"input_tokens": 0}, final={"input_tokens": 1}),
                self._observation(scope_id="sk-abcdefghijklmnop"),
                self._observation(role={"invalid": "object"}),
                self._observation(availability="unavailable", reason={"invalid": "object"},
                                  baseline=None, final=None),
            ):
                with self.subTest(observation=observation), self.assertRaises(InputError):
                    service.record_codex_telemetry({**target, **observation})

    def test_campaign_ingestion_is_durable_and_independent(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService({})
            campaign = service.create_campaign({
                "workspace": str(repo.path), "title": "Usage", "goal": "Track usage",
                "done_when": ["Done"], "source": {"kind": "local", "ref": "usage"},
                "risk": "medium", "mode": "delivery",
                "tasks": [{"id": "T-1", "title": "Track", "goal": "Record", "done_when": ["Done"],
                           "kind": "analysis", "dependencies": []}],
            })
            campaign_id = campaign["contract"]["campaign_id"]
            service.record_codex_telemetry({
                "workspace": str(repo.path), "target_type": "campaign", "target_id": campaign_id,
                **self._observation(role="lead"),
            })
            loaded = HarnessService({}).get_campaign({"workspace": str(repo.path), "campaign_id": campaign_id})
            self.assertEqual(50, loaded["state"]["codex_telemetry_summary"]["by_role"]["lead"]["observed_token_deltas"]["input_tokens"])
            self.assertEqual(1, len(loaded["state"]["codex_telemetry"]["observations"]))


if __name__ == "__main__":
    unittest.main()
