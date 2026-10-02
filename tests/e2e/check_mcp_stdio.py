"""The MCP server over stdio, the way an assistant runs it: `python -m clearcote_jet.mcp_server` in a subprocess with
real Clearcote (headless), driven by an MCP client on the local loan form. Lists the tools, then snapshot with a
screenshot, act by index and close_tab. No model calls. Anything the server printed on stdout would break the
protocol here. The profile dir is deleted afterwards.
"""

import asyncio
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).parent
FIXTURE = (HERE / "fixture.html").resolve().as_uri()
failures = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


async def main():
    profile = Path(tempfile.mkdtemp(prefix="ccagent-stdio-", dir=HERE))
    env = dict(os.environ, CLEARCOTE_JET_PROFILE=str(profile), CLEARCOTE_JET_HEADLESS="1")
    params = StdioServerParameters(command=sys.executable, args=["-m", "clearcote_jet.mcp_server"], env=env)
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                names = {t.name for t in (await client.list_tools()).tools}
                check({"browse", "snapshot", "act", "close_tab"} <= names, f"the server offers its tools {sorted(names)}")

                res = await client.call_tool("snapshot", {"url": FIXTURE, "screenshot": True})
                kinds = [c.type for c in res.content]
                mime = getattr(res.content[-1], "mime_type", None) or getattr(res.content[-1], "mimeType", None)
                check(kinds == ["text", "image"] and mime == "image/png", f"snapshot answers with its view and a PNG ({kinds})")
                check(base64.b64decode(res.content[1].data)[:8] == b"\x89PNG\r\n\x1a\n", "the image is a PNG")
                view = res.content[0].text
                reader = re.search(r'\[(\d+)\] textbox "Reader number"', view)
                check(bool(reader) and "tab_id: t1" in view, "the view names the reader-number field")

                res = await client.call_tool("act", {"tab_id": "t1", "op": "TYPE_TEXT", "target": reader.group(1),
                                                     "text": "A-4417"})
                out = res.content[0].text
                check("status: done" in out and 'value="A-4417"' in out, "act typed by index; the new view shows it")
                res = await client.call_tool("act", {"tab_id": "t1", "op": "CLICK", "target": 999})
                check("CLICK needs a target, one of:" in res.content[0].text, "a wrong index (an int) gets the valid ones")
                res = await client.call_tool("act", {"tab_id": "t9", "op": "WAIT"})
                check(res.content[0].text == "status: error (tab 't9' is gone)\nopen tabs: t1", "an unknown tab is named")
                res = await client.call_tool("close_tab", {"tab_id": "t1"})
                check(res.content[0].text == "closed t1", "close_tab answers over the protocol")
    finally:
        # The server's browser outlives the client by a moment and would write into a removed profile: wait for it.
        for _ in range(40):
            if not shutil.which("pgrep") or subprocess.run(["pgrep", "-f", str(profile)],
                                                            capture_output=True).returncode != 0:
                break
            await asyncio.sleep(0.25)
        for _ in range(20):
            shutil.rmtree(profile, ignore_errors=True)
            if not profile.exists():
                break
            await asyncio.sleep(0.5)
        check(not profile.exists(), "profile dir removed")
    print("RESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}", flush=True)
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
