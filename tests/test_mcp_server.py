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
    "create_run",
    "get_run",
    "list_runs",
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
