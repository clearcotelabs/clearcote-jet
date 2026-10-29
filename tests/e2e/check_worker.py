"""The worker as the Profile Manager runs it: a browser with a CDP port is already running (a person's profile, with a
tab of their own open), and `clearcote-jet worker` is started as its own process for one task, a JSON request on its
stdin and JSON events on its stdout.

A local checkout page: an email field, a password field and a "Place order" button that posts the order to this
server. One template goal, "Log in as {{email}} with password {{password}}, then place the order", with the password
secret and confirm on:
  1. learn with values A: the worker asks before "Place order", is approved, the order goes out with A's values;
  2. replay with values B and the model off: no model request, B's values typed, asked again, approved;
  3. values C, cancelled: no order; 4. values C, stdin closed while asking: cancelled, no order.
Throughout: stdout is only JSON lines, no secret appears in any event, on stderr, in the saved skill or in a request
to the model, the person's own tab is untouched and the browser is still running after every worker has exited.

The model is a stand-in (worker_stand_in.py). The browser is Clearcote (headless); JET_E2E_BROWSER=chromium uses
Playwright's Chromium instead. Profile, skills and temp dirs are deleted afterwards and the browser is closed.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
failures = []
ORDERS = []
GOAL = "Log in as {{email}} with password {{password}}, then place the order"
A = {"email": "anna@one.example", "password": "s3cret-A!"}
B = {"email": "bob@two.example", "password": "hunter2-B?"}
C = {"email": "cy@three.example", "password": "pa55-C#"}
PLAN = [["TYPE_TEXT", "Email"], ["TYPE_TEXT", "Password"], ["CLICK", "Place order"], ["DONE", None]]

CHECKOUT = """<!doctype html><html><head><meta charset="utf-8"><title>Checkout</title></head>
<body style="font:16px sans-serif;margin:40px">
<h1>Checkout</h1>
<p><label for="email">Email</label> <input id="email" type="email" style="width:300px"></p>
<p><label for="password">Password</label> <input id="password" type="password" style="width:300px"></p>
<p><button id="place" onclick="placeOrder()">Place order</button></p>
<p id="out"></p>
<script>
async function placeOrder() {
  const body = JSON.stringify({email: email.value, password: password.value});
  await fetch('/order', {method: 'POST', headers: {'Content-Type': 'application/json'}, body});
  document.getElementById('out').textContent = 'Order placed for ' + email.value;
}
</script></body></html>"""
HOME = """<!doctype html><html><head><meta charset="utf-8"><title>My own tab</title></head>
<body><h1>A tab the person has open</h1></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = {"/checkout": CHECKOUT, "/home": HOME}.get(self.path.partition("?")[0])
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):
        ORDERS.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cdp_tabs(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
        return [t for t in json.load(r) if t.get("type") == "page"]


class Worker:
    """One `clearcote-jet worker` process: events are read as they come; `answer` decides what to write back."""

    def __init__(self, env, request, answer=None):
        self.proc = subprocess.Popen([sys.executable, str(HERE / "worker_stand_in.py")], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(ROOT))
        self.events, self.raw, self.answer, self.stderr = [], [], answer, b""
        self.err_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self.err_thread.start()
        self.send(request)

    def _drain_stderr(self):
        self.stderr = self.proc.stderr.read()

    def send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        self.proc.stdin.flush()

    def close_stdin(self):
        self.proc.stdin.close()

    def run(self, timeout=180):
        for line in self.proc.stdout:  # the worker ends its stdout when it exits
            self.raw.append(line)
            try:
                event = json.loads(line)
            except ValueError:
                check(False, f"stdout carried a line that is not JSON: {line[:120]!r}")
                continue
            self.events.append(event)
            if self.answer:
                self.answer(self, event)
        code = self.proc.wait(timeout=timeout)
        self.err_thread.join(10)
        return code

    def types(self):
        return [e["type"] for e in self.events]


async def start_browser(work, port):
    """A browser left running with a CDP port, as the Profile Manager leaves a profile: Clearcote, or Chromium."""
    if os.environ.get("JET_E2E_BROWSER") == "chromium":
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, args=[f"--remote-debugging-port={port}"])
        context = await browser.new_context()

        async def close():
            await browser.close()
            await pw.stop()
        return "Playwright Chromium", context, close
    from clearcote_jet.browser import Session
    session = await Session.launch(profile=work / "profile", headless=True, cdp_port=port, humanize=False)
    return "Clearcote", session.context, session.close


async def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    work = Path(tempfile.mkdtemp(prefix="ccagent-worker-", dir=HERE))
    skills, log, temp = work / "skills", work / "model.jsonl", work / "temp"
    temp.mkdir()
    port = free_port()
    close_browser = None
    secrets = [A["password"], B["password"], C["password"]]
    env = dict(os.environ, PYTHONPATH=str(ROOT), JET_E2E_LOG=str(log), JET_E2E_PLAN=json.dumps(PLAN),
               CLEARCOTE_JET_ALLOW_PRIVATE_EGRESS="1", TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp),
               PYTHONIOENCODING="utf-8")
    cdp = f"http://127.0.0.1:{port}"

    def request(values, **kw):
        return {"cmd": "run", "cdp": cdp, "url": f"{base}/checkout", "goal": GOAL, "values": values,
                "secret": ["password"], "confirm": True, "humanize": True, "reuse": True, "skills_dir": str(skills),
                **kw}

    def approve(w, event):
        if event["type"] == "needs_confirmation":
            w.send({"cmd": "approve"})

    def no_secrets(w, what):
        text = b"".join(w.raw).decode() + w.stderr.decode("utf-8", "replace")
        check(not any(s in text for s in secrets), f"{what}: no secret on stdout or stderr")

    try:
        name, context, close_browser = await start_browser(work, port)
        print(f"browser: {name}, CDP port {port}", flush=True)
        mine = context.pages[0] if context.pages else await context.new_page()
        await mine.goto(f"{base}/home")
        tabs_before = len(cdp_tabs(port))

        print("== 1. learn with values A, approve the order", flush=True)
        w = Worker(env, request(A), approve)
        code = await asyncio.to_thread(w.run)  # the browser this process launched keeps being served meanwhile
        result = w.events[-1] if w.events else {}
        print("  events:", w.types(), flush=True)
        check(code == 0 and w.types()[0] == "ready" and w.types()[-1] == "result", f"framed ready ... result, exit {code}")
        check(w.types().count("needs_confirmation") == 1 and w.types().count("step") == 3,
              "asked once before the order, three steps")
        asked = next((e for e in w.events if e["type"] == "needs_confirmation"), {})
        check(asked.get("pending", {}).get("label") == "Place order", f"asked about {asked.get('pending')}")
        check(result.get("status") == "done" and result.get("skill", {}).get("used") == "learned",
              f"done and learned: {result.get('status')} {result.get('skill')}")
        check(ORDERS == [A], f"the order went out with A's values ({len(ORDERS)} orders)")
        check("Order placed for anna@one.example" in result.get("markdown", ""), "the answer shows the order")
        no_secrets(w, "learn")
        learned_requests = sum(1 for _ in log.open(encoding="utf-8"))
        check(learned_requests >= 4, f"the stand-in model answered {learned_requests} requests")

        files = list(skills.glob("*.json"))
        saved = files[0].read_text(encoding="utf-8") if files else ""
        skill = json.loads(saved) if saved else {}
        check(len(files) == 1 and skill.get("version") == 2 and skill.get("slots") == ["email", "password"],
              f"one template skill saved, slots {skill.get('slots')}")
        check([s.get("text") for s in skill.get("steps", [])] == ["{{email}}", "{{password}}", None],
              "its typed steps are the slots")
        check(A["password"] not in saved and A["email"] not in saved, "no value of A in the skill file")

        print("== 2. replay with values B, model off, approve", flush=True)
        w = Worker(dict(env, JET_E2E_MODEL_OFF="1"), request(B), approve)
        code = await asyncio.to_thread(w.run)  # the browser this process launched keeps being served meanwhile
        result = w.events[-1] if w.events else {}
        print("  events:", w.types(), flush=True)
        check(code == 0 and result.get("status") == "done" and result.get("skill", {}).get("used") == "replayed",
              f"replayed: {result.get('status')} {result.get('skill')} {result.get('detail')}")
        check(result.get("usage", {}).get("requests") == 0
              and sum(1 for _ in log.open(encoding="utf-8")) == learned_requests, "0 model calls on the replay")
        check(w.types().count("needs_confirmation") == 1, "asked again before the order")
        check(ORDERS[1:] == [B], f"B's email and password were typed ({len(ORDERS) - 1} more orders)")
        typed = [e.get("text") for e in w.events if e["type"] == "step"]
        check(typed == ["bob@two.example", "{{password}}", None], "steps show B's email, the password as its slot")
        no_secrets(w, "replay")

        print("== 3. cancel", flush=True)
        w = Worker(dict(env, JET_E2E_MODEL_OFF="1"), request(C),
                   lambda w, e: w.send({"cmd": "cancel"}) if e["type"] == "needs_confirmation" else None)
        code = await asyncio.to_thread(w.run)  # the browser this process launched keeps being served meanwhile
        check(code == 0 and w.events[-1].get("status") == "cancelled" and len(ORDERS) == 2,
              f"cancelled, no order ({w.events[-1].get('status')}, exit {code})")
        no_secrets(w, "cancel")

        print("== 4. stdin closed while asking", flush=True)
        w = Worker(dict(env, JET_E2E_MODEL_OFF="1"), request(C),
                   lambda w, e: w.close_stdin() if e["type"] == "needs_confirmation" else None)
        code = await asyncio.to_thread(w.run)  # the browser this process launched keeps being served meanwhile
        check(code == 0 and w.events[-1].get("status") == "cancelled" and len(ORDERS) == 2,
              f"closing stdin is a cancel ({w.events[-1].get('status')}, exit {code})")
        if code:
            print(w.stderr.decode("utf-8", "replace")[-3000:], flush=True)

        print("== the browser and the person's tab", flush=True)
        model_log = log.read_text(encoding="utf-8")
        check(not any(s in model_log for s in secrets) and "{{password}}" in model_log,
              "no request to the model held a secret; they held the {{password}} token")
        tabs = cdp_tabs(port)
        check(any(t["url"] == f"{base}/home" for t in tabs) and await mine.title() == "My own tab",
              "the person's own tab is untouched")
        check(len(tabs) == tabs_before + 4, f"each worker opened a tab of its own ({tabs_before} -> {len(tabs)})")
        with urllib.request.urlopen(f"{cdp}/json/version", timeout=5) as r:
            alive = "Browser" in json.load(r)
        check(alive, "the browser is still running after every worker exited")
    finally:
        if close_browser:
            await close_browser()
        server.shutdown()
        for _ in range(20):
            shutil.rmtree(work, ignore_errors=True)
            if not work.exists():
                break
            await asyncio.sleep(0.5)
        check(not work.exists(), "profile, skills and temp dirs removed")
    print("RESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}", flush=True)
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
