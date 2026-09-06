from __future__ import annotations

import json
import subprocess
import sys
import unittest

import _support

from agent_harness.mcp_server import McpServer, TOOLS
from agent_harness.service import HarnessService


EXPECTED_TOOLS = {
    "check_runtime",
    "create_campaign",
    "get_campaign",
    "list_campaigns",
    "record_campaign_task",
    "record_campaign_intervention",
    "seal_campaign_candidate",
    "record_campaign_comparison",
    "finish_campaign",
    "create_run",
    "get_run",
    "list_runs",
    "measure_diff",
    "plan_checks",
    "record_check",
    "start_stage",
    "poll_stage",
    "cancel_stage",
    "record_review_resolution",
    "finish_run",
}


class McpContractTests(unittest.TestCase):
    def test_public_tool_surface_is_exact(self) -> None:
        self.assertEqual(EXPECTED_TOOLS, {tool["name"] for tool in TOOLS})
        for tool in TOOLS:
            self.assertFalse(tool["inputSchema"].get("additionalProperties", True))
            self.assertFalse(tool["annotations"]["destructiveHint"])

    def test_create_campaign_exposes_local_openspec_default(self) -> None:
        create_campaign = next(
            tool for tool in TOOLS if tool["name"] == "create_campaign"
        )
        schema = create_campaign["inputSchema"]["properties"]["spec"]
        self.assertEqual(["openspec"], schema["properties"]["kind"]["enum"])
        self.assertEqual(
            "^[a-z0-9][a-z0-9-]*$",
            schema["properties"]["change_id"]["pattern"],
        )
        storage = schema["properties"]["storage"]
        self.assertEqual(["local", "repository"], storage["enum"])
        self.assertEqual("local", storage["default"])

    def test_review_budget_is_exposed_for_campaign_tasks_and_runs(self) -> None:
        create_campaign = next(
            tool for tool in TOOLS if tool["name"] == "create_campaign"
        )
        task = create_campaign["inputSchema"]["properties"]["tasks"]["items"]
        create_run = next(tool for tool in TOOLS if tool["name"] == "create_run")
        for schema in (
            task["properties"]["review_budget"],
            create_run["inputSchema"]["properties"]["review_budget"],
        ):
            self.assertEqual(
                700,
                schema["properties"]["max_production_lines"]["maximum"],
            )
            self.assertFalse(schema["additionalProperties"])

    def test_measure_diff_is_read_only(self) -> None:
        measure = next(tool for tool in TOOLS if tool["name"] == "measure_diff")
        self.assertTrue(measure["annotations"]["readOnlyHint"])
        self.assertTrue(measure["annotations"]["idempotentHint"])

    def test_mcp_allowlist_matches_public_tool_surface(self) -> None:
        configuration = json.loads(
            (_support.PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8")
        )
        enabled = configuration["mcpServers"]["agent-harness"]["enabled_tools"]
        self.assertEqual(EXPECTED_TOOLS, set(enabled))
        self.assertEqual(len(EXPECTED_TOOLS), len(enabled))
        server = configuration["mcpServers"]["agent-harness"]
        self.assertEqual(["-X", "utf8", "./scripts/mcp_server.py"], server["args"])
        inherited = server["env_vars"]
        self.assertTrue({"PATH", "HOME", "USER", "SystemRoot"}.issubset(inherited))

    def test_initialize_and_tool_listing(self) -> None:
        server = McpServer(HarnessService({}))
        initialized = server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assertEqual("agent-harness", initialized["result"]["serverInfo"]["name"])
        listed = server.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        self.assertEqual(EXPECTED_TOOLS, {tool["name"] for tool in listed["result"]["tools"]})

    def test_tool_errors_are_structured(self) -> None:
        server = McpServer(HarnessService({}))
        result = server.call_tool("missing", {})
        self.assertTrue(result["isError"])
        self.assertIn("unknown tool", result["structuredContent"]["error"])

    def test_create_and_get_run_through_mcp(self) -> None:
        with _support.TempRepo() as repo:
            server = McpServer(HarnessService({}))
            created = server.call_tool(
                "create_run",
                {
                    "workspace": str(repo.path),
                    "goal": "Update docs",
                    "done_when": ["Docs are current"],
                },
            )
            self.assertFalse(created["isError"])
            run_id = created["structuredContent"]["contract"]["run_id"]
            fetched = server.call_tool(
                "get_run", {"workspace": str(repo.path), "run_id": run_id}
            )
            self.assertEqual(run_id, fetched["structuredContent"]["state"]["run_id"])

    def test_create_and_get_campaign_through_mcp(self) -> None:
        with _support.TempRepo() as repo:
            server = McpServer(HarnessService({}))
            created = server.call_tool(
                "create_campaign",
                {
                    "workspace": str(repo.path),
                    "title": "Epic",
                    "goal": "Deliver the epic",
                    "done_when": ["The epic is complete"],
                    "risk": "medium",
                    "source": {"kind": "jira", "ref": "DEMO-1"},
                    "tasks": [
                        {
                            "id": "T-1",
                            "title": "Task",
                            "goal": "Complete the task",
                            "done_when": ["The task is complete"],
                            "kind": "analysis",
                        }
                    ],
                },
            )
            self.assertFalse(created["isError"])
            campaign_id = created["structuredContent"]["contract"]["campaign_id"]
            fetched = server.call_tool(
                "get_campaign",
                {"workspace": str(repo.path), "campaign_id": campaign_id},
            )
            self.assertEqual(
                campaign_id,
                fetched["structuredContent"]["state"]["campaign_id"],
            )

    def test_stdio_entrypoint_speaks_json_rpc(self) -> None:
        requests = "\n".join(
            [
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
                ),
                json.dumps(
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
                ),
            ]
        )
        completed = subprocess.run(
            [sys.executable, str(_support.PLUGIN_ROOT / "scripts" / "mcp_server.py")],
            input=requests + "\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([1, 2], [response["id"] for response in responses])
        self.assertEqual(
            EXPECTED_TOOLS,
            {tool["name"] for tool in responses[1]["result"]["tools"]},
        )


if __name__ == "__main__":
    unittest.main()
