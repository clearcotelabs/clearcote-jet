"""Headed end-to-end check of controls inside iframes, on real Clearcote with scripted decisions (no model calls).

A local server serves a page on 127.0.0.1 with five iframes: a same-process one (srcdoc), a cross-site one from
localhost (a different site, so the browser runs it out of process), one under an overlay, one off screen, and one that
starts empty and loads a cross-site page only after the first look. Proves that reachable frame controls are offered and
hidden ones are not, that clicks and typing land inside both kinds of frame with trusted events, that a frame which
changes process is still read, that a change inside a frame counts as a page change, and that frame pages never see the
reads. The profile dir is deleted afterwards.
"""

import asyncio
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from clearcote_jet.browser import Session

HERE = Path(__file__).parent
failures = []

# Every frame records its own input events and whether the agent's cache leaked into its globals.
RECORDER = """<script>
window.rec = {events: [], cacheSeen: false};
for (const t of ['mousedown', 'mouseup', 'click', 'keydown', 'input', 'change'])
  addEventListener(t, e => rec.events.push({t, trusted: e.isTrusted, id: e.target.id || ''}), true);
setInterval(() => { if ('__ca' in window) rec.cacheSeen = true; }, 50);
</script>"""

INNER = f"""<!doctype html><html><body style="font:16px sans-serif;margin:8px">{RECORDER}
<p>Membership desk</p>
<input id="code" aria-label="Member code">
<button id="confirm" onclick="parent.postMessage('confirmed ' + document.getElementById('code').value, '*')">
Cross-site confirm</button>
</body></html>"""

SAME = ("<body style='font:16px sans-serif;margin:8px'>" + RECORDER.replace('"', "&quot;") +
        "<label><input type=checkbox id=consent> Same-process consent</label>"
        "<button id=ping onclick=\"parent.postMessage('ping', '*')\">Same-process ping</button></body>")

OUTER = """<!doctype html><html><head><title>Frames</title></head><body style="font:16px sans-serif">
<h1>Frame desk</h1>
<p id="log">Log:</p>
<iframe id="same" srcdoc="{same}" style="width:420px;height:90px;border:1px solid #999"></iframe>
<iframe id="cross" src="http://localhost:{port}/inner" style="width:420px;height:110px;border:2px solid #999;margin-left:37px"></iframe>
<div style="position:relative;width:420px;height:90px">
  <iframe id="covered" srcdoc="<button>Hidden action</button>" style="width:420px;height:90px;border:0"></iframe>
  <div style="position:absolute;inset:0;background:#eee">Curtain</div>
</div>
<iframe id="away" srcdoc="<button>Offscreen action</button>" style="position:absolute;top:-9999px"></iframe>
<iframe id="late" style="width:420px;height:60px;border:0"></iframe>
<script>addEventListener('message', e => document.getElementById('log').textContent += ' ' + e.data);</script>
</body></html>"""


# Loaded into the empty #late frame after the first observe: the frame was same-process (about:blank) when first seen.
LATE = """<!doctype html><html><body style="margin:8px">
<button onclick="parent.postMessage('late', '*')">Late cross-site action</button></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        port = self.server.server_address[1]
        body = INNER if self.path == "/inner" else LATE if self.path == "/late" else OUTER.format(
            same=SAME.replace('"', "&quot;"), port=port)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


def pick(state, kind, label):
    for a in state["actions"]:
        if a["kind"] == kind and label in a["label"]:
            return a
    return None


async def main():
    await exercise()
    print("RESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}", flush=True)
    return 1 if failures else 0


async def exercise():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    profile = Path(tempfile.mkdtemp(prefix="ccagent-e2e-", dir=HERE))
    session = None
    try:
        session = await Session.launch(profile=profile, headless=False, humanize=True)
        page = await session.new_tab(url)
        await page.wait_for_load_state("load")  # waits for the iframes too
        cross = next(f for f in page.frames if f.url.startswith("http://localhost"))
        same = [f for f in page.frames if f.url == "about:srcdoc" and await f.query_selector("#consent")][0]

        state = await session.observe(page)
        labels = [f"{a['kind']}:{a['label']}" for a in state["actions"]]
        print("  offered:", labels, flush=True)
        check(any(session for _, session in getattr(page, "_ca_oopif", {}).values()),
              "the cross-site frame is out of process (reached through its own session)")
        consent = pick(state, "click", "Same-process consent")
        check(consent is not None and consent.get("frame") and consent["role"] == "checkbox",
              "same-process frame checkbox offered, tagged with its frame")
        check(pick(state, "fill", "Member code") is not None, "cross-site frame text field offered as fill")
        check(pick(state, "click", "Cross-site confirm") is not None, "cross-site frame button offered")
        check(pick(state, "click", "Hidden action") is None, "control in a frame under an overlay not offered")
        check(pick(state, "click", "Offscreen action") is None, "control in an off-screen frame not offered")
        check("Membership desk" in state["text"], "frame text reaches the page text")
        ids = [a["id"] for a in state["actions"]]
        check(len(ids) == len(set(ids)), "action ids are unique across frames")
        if failures:
            return  # nothing in the frames to act on

        await session.act(page, state, consent)
        new = await session.observe(page, after=consent)
        check(await same.is_checked("#consent"), "click ticked the checkbox inside the same-process frame")
        check(new["marker"] != state["marker"], "a change inside a frame is a page change")
        check(pick(new, "click", "Same-process consent").get("checked") == "true", "the frame's new state is observed")
        state = new

        action = pick(state, "fill", "Member code")
        await session.act(page, state, action, "M-2291")
        state = await session.observe(page, after=action)
        check(await cross.input_value("#code") == "M-2291", "typed into the field inside the cross-site frame")

        action = pick(state, "click", "Cross-site confirm")
        await session.act(page, state, action)
        state = await session.observe(page, after=action)
        check("confirmed M-2291" in state["text"], "click inside the cross-site frame reached its button")

        session.humanize = False  # the plain-click path takes the same frame offset and cover check
        action = pick(state, "click", "Same-process ping")
        await session.act(page, state, action)
        state = await session.observe(page, after=action)
        check("ping" in state["text"], "plain click inside a frame reached its button")
        session.humanize = True

        # A widget iframe starts as a same-process about:blank and only then navigates cross-site (out of process).
        await page.evaluate("port => document.getElementById('late').src = `http://localhost:${port}/late`",
                            server.server_address[1])
        for _ in range(50):
            late = next((f for f in page.frames if f.url.endswith("/late")), None)
            if late and await late.query_selector("button"):
                break
            await asyncio.sleep(0.1)
        state = await session.observe(page)
        action = pick(state, "click", "Late cross-site action")
        check(action is not None, "a frame that turned cross-site after it was first seen is read")
        if action:
            await session.act(page, state, action)
            state = await session.observe(page, after=action)
            check((await page.text_content("#log")).endswith(" late"),
                  "click inside the late cross-site frame reached its button")

        for name, frame in (("same-process", same), ("cross-site", cross)):
            rec = await frame.evaluate("window.rec")
            kinds = {e["t"] for e in rec["events"]}
            check(rec["events"] and all(e["trusted"] for e in rec["events"]),
                  f"{name} frame: every input event is isTrusted ({len(rec['events'])} events, {sorted(kinds)})")
            check(not rec["cacheSeen"], f"{name} frame: the element cache is invisible to the page")
        await asyncio.sleep(1)
    finally:
        if session:
            await session.close()
        server.shutdown()
        for _ in range(20):
            shutil.rmtree(profile, ignore_errors=True)
            if not profile.exists():
                break
            await asyncio.sleep(0.5)
        check(not profile.exists(), "profile dir removed")


sys.exit(asyncio.run(main()))
