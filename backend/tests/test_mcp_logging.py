import asyncio
import logging
import re

import pytest

from app.mcp.server import mcp


def test_tool_call_is_logged_without_excerpt_text(caplog):
    caplog.set_level(logging.INFO, logger="keystone.mcp")
    with pytest.raises(Exception):  # unknown tool: fails after logging, no DB access
        asyncio.run(mcp.call_tool("log_context_x", {
            "client": "claude", "project": "recipe-app",
            "messages": [{"role": "user", "content": "secret excerpt text"}]}))
    line = caplog.records[-1].getMessage()
    assert line.startswith("log_context_x ")
    assert '"project": "recipe-app"' in line and "<1 messages>" in line
    assert "secret excerpt text" not in line
    assert re.search(r" \d+ms ERROR ", line)  # duration logged even when the call fails
