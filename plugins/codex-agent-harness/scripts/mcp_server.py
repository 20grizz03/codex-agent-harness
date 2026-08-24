#!/usr/bin/env python3
"""Stable stdio entrypoint for the Agent Harness MCP server."""

from __future__ import annotations

import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "src"))

from agent_harness.mcp_server import serve  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(serve())
