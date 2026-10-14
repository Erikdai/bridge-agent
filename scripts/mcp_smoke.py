"""Drive the BridgeAgent MCP server from a plain MCP client, no LLM involved.

    python scripts/mcp_smoke.py

Spawns `bridgeagent mcp` over stdio, lists the tools, then performs the
calculator task by hand: observe the page, launch calc, type the expression,
read the display, close, return to the browser.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

PAGE = (Path(__file__).resolve().parents[1] / "tasks" / "pages" / "numbers.html").as_uri()


def text_of(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


async def main() -> int:
    exe = (
        Path(sys.executable).with_name("bridgeagent.exe")
        if sys.platform == "win32"
        else Path(sys.executable).with_name("bridgeagent")
    )
    params = StdioServerParameters(command=str(exe), args=["mcp"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        print("tools:", [t.name for t in tools.tools])

        obs = text_of(await session.call_tool("observe", {"start_url": PAGE}))
        nums = re.findall(r"(?:First|Second): (\d+)", obs)
        print("page numbers:", nums)
        assert len(nums) == 2, obs[:300]

        out = text_of(await session.call_tool("launch", {"command": "calc"}))
        print(out.splitlines()[0])
        assert "SURFACE: desktop" in out and "Calculator" in out, out[:300]

        out = text_of(await session.call_tool("type_text", {"text": f"{nums[0]}+{nums[1]}="}))
        display = re.search(r'text "Display is ([\d,]+)"', out)
        print(out.splitlines()[0], "| display:", display.group(1) if display else None)
        if not display:
            print("--- observation after typing (first 30 lines) ---")
            print("\n".join(out.splitlines()[:30]))
        assert display and display.group(1).replace(",", "") == str(int(nums[0]) + int(nums[1]))

        out = text_of(await session.call_tool("press", {"keys": "Alt+F4"}))
        print(out.splitlines()[0])
        out = text_of(await session.call_tool("focus_browser", {}))
        print(out.splitlines()[0], "| surface:", re.search(r"SURFACE: (\w+)", out).group(1))

        skills = text_of(await session.call_tool("list_skills", {}))
        print("skills:", skills.splitlines()[:3])
    print("MCP smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
