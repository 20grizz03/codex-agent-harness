from __future__ import annotations

import unittest
from unittest.mock import patch

import _support

from agent_harness.contract import DEFAULT_EXECUTION
from agent_harness.mcp_server import TOOLS
from agent_harness.service import HarnessService
from agent_harness.util import InputError
from test_campaign import campaign_arguments, task


class ModelDefaultsTests(unittest.TestCase):
    def test_new_run_and_mcp_schemas_use_sol_6_high(self) -> None:
        expected = {
            "native_model": "gpt-6-sol",
            "reasoning_effort": "high",
            "escalation_model": None,
        }
        with _support.TempRepo() as repo:
            run = HarnessService({}).create_run({
                "workspace": str(repo.path),
                "goal": "Update documentation",
                "done_when": ["Documentation is current"],
            })
            self.assertEqual(expected, run["contract"]["execution"])
        tools = {tool["name"]: tool["inputSchema"]["properties"] for tool in TOOLS}
        for schema in (
            tools["create_run"]["execution"],
            tools["create_campaign"]["tasks"]["items"]["properties"]["execution"],
        ):
            self.assertEqual(expected, {
                key: value["default"] for key, value in schema["properties"].items()
            })

    def test_stored_run_keeps_previous_default_after_upgrade(self) -> None:
        with _support.TempRepo() as repo:
            with patch.dict(DEFAULT_EXECUTION, {"native_model": "gpt-5.6-sol"}):
                contract = HarnessService({}).create_run({
                    "workspace": str(repo.path),
                    "goal": "Existing task",
                    "done_when": ["Task is complete"],
                })["contract"]
            restored = HarnessService({}).get_run({
                "workspace": str(repo.path), "run_id": contract["run_id"],
            })
            self.assertEqual(contract, restored["contract"])
            self.assertEqual("gpt-5.6-sol", restored["contract"]["execution"]["native_model"])

    def test_old_campaign_execution_is_inherited_and_cannot_be_replaced(self) -> None:
        with _support.TempRepo() as repo:
            definition = task("T-1", kind="implementation")
            definition["execution"] = {
                "native_model": "gpt-5.6-sol",
                "reasoning_effort": "xhigh",
                "escalation_model": "gpt-6-astra",
            }
            contract = HarnessService({}).create_campaign(
                campaign_arguments(repo, tasks=[definition])
            )["contract"]
            reference = {
                "workspace": str(repo.path),
                "campaign_id": contract["campaign_id"], "task_id": "T-1",
            }
            service = HarnessService({})
            service.record_campaign_task({**reference, "status": "in_progress"})
            run = service.create_run({"workspace": str(repo.path), "campaign": reference})
            self.assertEqual(definition["execution"], run["contract"]["execution"])
            restored = service.get_campaign({
                "workspace": str(repo.path), "campaign_id": contract["campaign_id"],
            })
            self.assertEqual(contract, restored["contract"])
            with self.assertRaisesRegex(InputError, "execution conflicts"):
                service.create_run({
                    "workspace": str(repo.path), "campaign": reference,
                    "execution": dict(DEFAULT_EXECUTION),
                })
