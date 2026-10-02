"""Headed end-to-end check of the MCP take-over tools on real Clearcote, called in-process as the server calls them.

snapshot opens the local loan form; act fills it in by index (no model) and the request appears on the page. Also:
a password field is typed into while only filled=true/false shows of it, a snapshot that went stale is refused, a busy
tab answers busy, a wrong index is answered with the valid ones, a screenshot leaves the page's DOM untouched, every
input event is trusted and the page never sees the reads, and (with TYPESAFE_API_KEY set) one step by instruction.
The profile dir is deleted afterwards.
"""

import asyncio
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from mcp.server.mcpserver import Image

from clearcote_jet import mcp_server
from clearcote_jet.cli import load_env_file

HERE = Path(__file__).parent
FIXTURE = (HERE / "fixture.html").resolve().as_uri()
PIN = "Zq9-library-pin"  # typed into the password field
failures = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


def index(view, pattern):
    found = re.search(pattern, view)
    return found.group(1) if found else None


def elements(view):
    return [line for line in view.splitlines() if re.match(r"\[\d+\] ", line)]


async def main():
    load_env_file(HERE.parent.parent / ".env")
    profile = Path(tempfile.mkdtemp(prefix="ccagent-mcp-", dir=HERE))
    os.environ["CLEARCOTE_JET_PROFILE"] = str(profile)
    try:
        view = await mcp_server.snapshot(url=FIXTURE)
        print("  elements:", elements(view), flush=True)
        check("tab_id: t1 (still open)" in view, "snapshot opened the form in a new tab")
        reader = index(view, r'\[(\d+)\] textbox "Reader number"')
        airport = index(view, r"(\d+:\d+) Airport kiosk")
        check(bool(reader and airport and index(view, r'\[(\d+)\] button "Send request"')),
              "the element table names the field, the option and the button")
        check(not any("Renew all loans" in line for line in elements(view)), "a control under the curtain is not offered")
        pin = index(view, r'\[(\d+)\] textbox "Library PIN" filled=false ops=TYPE_TEXT')
        check(bool(pin), "the password field is offered to type into, shown only as filled=false")

        out = await mcp_server.act("t1", op="TYPE_TEXT", target=reader, text="A-4417")
        check("status: done" in out and "page_changed: True" in out and "model:" not in out,
              "act typed the reader number by index, without the model")
        out = await mcp_server.act("t1", op="TYPE_TEXT", target=pin, text=PIN)
        view = out[out.index("tab_id:"):]  # the new snapshot; the did: line above it echoes the caller's own text
        check("status: done" in out and re.search(r'\] textbox "Library PIN" filled=true ops=', view)
              and PIN not in view, "act typed the PIN; the new snapshot says filled=true, not the PIN")
        out = await mcp_server.act("t1", op="SELECT", target=airport)
        check("status: done" in out and 'value="Airport kiosk"' in out, "act chose the airport kiosk by index")
        send = index(out, r'\[(\d+)\] button "Send request"')  # from the new snapshot act returned
        out = await mcp_server.act("t1", op="CLICK", target=send)
        check("Requested: A-4417 / airport" in out, "act clicked Send request: the new snapshot shows the request")

        page = mcp_server.browser.tabs["t1"].page
        await page.evaluate("document.getElementById('out').textContent = 'changed by the page'")
        out = await mcp_server.act("t1", op="CLICK", target=send)
        check("status: done" in out and "Requested: A-4417 / airport" in out,
              "a change a click does not depend on (a status line elsewhere) does not block it")
        send = index(out, r'\[(\d+)\] button "Send request"')
        await page.evaluate("document.getElementById('reader').value = 'B-1'")  # the form's values changed
        out = await mcp_server.act("t1", op="CLICK", target=send)
        check(out.startswith("status: stale"), "act after the form changed since its snapshot is refused")
        out = await mcp_server.act("t1", op="CLICK", target=send)
        check("call snapshot first" in out, "after a stale answer, act waits for a new snapshot")

        view = await mcp_server.snapshot(tab_id="t1")
        async with mcp_server.browser.tabs["t1"].lock:  # another call holds the tab
            out = await mcp_server.act("t1", op="CLICK", target=send)
        check("is busy with another call" in out, "a second call on a busy tab answers busy")
        out = await mcp_server.act("t1", op="CLICK", target="999")
        check("CLICK needs a target, one of:" in out, "a wrong index is answered with the valid ones")

        await page.evaluate("""() => { window.muts = 0; new MutationObserver(list => { window.muts += list.length; })
            .observe(document, {subtree: true, attributes: true, childList: true, characterData: true}); }""")
        out = await mcp_server.snapshot(tab_id="t1", screenshot=True)
        check(isinstance(out, list) and isinstance(out[1], Image) and out[1].data[:8] == b"\x89PNG\r\n\x1a\n",
              "snapshot with screenshot returns a PNG of the tab")
        check(await page.evaluate("window.muts") == 0, "the screenshot and the snapshot left the page's DOM untouched")

        if os.environ.get("TYPESAFE_API_KEY"):
            out = await mcp_server.act("t1", instruction="Choose Central library as the branch to collect from")
            print("  " + "\n  ".join(out.splitlines()[:4]), flush=True)
            check("status: done" in out and "p=" in out and "model: 1 requests" in out,
                  "act by instruction took one decision and reported its probability and tokens")
            check(await page.eval_on_selector("#branch", "e => e.value") == "central",
                  "the instruction step chose Central library")
            check(await page.input_value("#reader") == "B-1", "nothing else on the form was touched")
        else:
            print("  SKIP act by instruction (no TYPESAFE_API_KEY)", flush=True)

        rec = await page.evaluate("window.rec")
        check(bool(rec["events"]) and all(e["trusted"] for e in rec["events"]),
              f"every input event was trusted ({len(rec['events'])} events)")
        check(rec["qsa"] == 0 and rec["efp"] == 0 and not rec["cacheSeen"], "the page never saw the agent's reads")
        check(await mcp_server.close_tab("t1") == "closed t1", "close_tab closed the tab")
    finally:
        if mcp_server.browser.session:
            await mcp_server.browser.session.close()
        for _ in range(20):
            shutil.rmtree(profile, ignore_errors=True)
            if not profile.exists():
                break
            await asyncio.sleep(0.5)
        check(not profile.exists(), "profile dir removed")
    print("RESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}", flush=True)
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
