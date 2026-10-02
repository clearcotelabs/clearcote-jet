"""Headed end-to-end check of skills, lists, the JSON shortcut, confirm and find, on real Clearcote.

A local server serves a shop whose results come from a JSON API, the loan form (and a variant whose button was
renamed), and a long document with and without anchors. Learning runs use a scripted stand-in for the two models;
replays run with the model switched off, so any model call fails the check. The profile and skills dirs are deleted
afterwards.
"""

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from clearcote_jet import model
from clearcote_jet.agent import run
from clearcote_jet.browser import Session
from clearcote_jet.skills import SkillStore, run_with_skill

HERE = Path(__file__).parent
failures = []
API_HITS = []

RECORDER = """<script>
window.rec = {events: []};
for (const t of ['mousedown', 'mouseup', 'click', 'keydown', 'input', 'change', 'wheel'])
  addEventListener(t, e => rec.events.push({t, trusted: e.isTrusted}), true);
</script>"""
PRODUCTS = [{"id": i, "name": name, "url": f"/p/{i}", "pricing": {"sale": sale, "was": was}}
            for i, (name, sale, was) in enumerate([("Blue kettle", 24.5, 30), ("Red kettle", 31, 35),
                                                   ("Steel kettle", 19.99, 25), ("Glass kettle", 42, 49),
                                                   ("Travel kettle", 15.5, 18), ("Copper kettle", 64, 80)], 1)]
SHOP = f"""<!doctype html><html><head><meta charset="utf-8"><title>Kettle shop</title>{RECORDER}</head>
<body style="font:16px sans-serif;margin:30px">
<header><nav><a href="/">Home</a> <a href="/kettles">Kettles</a> <a href="/teapots">Teapots</a> <a href="/mugs">Mugs</a></nav></header>
<main><h1>Kettles on sale</h1><div id="results">Loading…</div></main>
<script>
const euro = n => '€' + n.toFixed(2).replace('.', ',');
fetch('/api/products?q=kettle').then(r => r.json()).then(d => {{
  document.getElementById('results').innerHTML = d.data.results.map(p =>
    `<div class="card" style="margin:12px 0"><h3 class="name"><a href="${{p.url}}">${{p.name}}</a></h3>` +
    `<span class="price old"><s>${{euro(p.pricing.was)}}</s></span> <span class="price sale">${{euro(p.pricing.sale)}}</span></div>`
  ).join('');
}});
</script></body></html>"""
FORM = (HERE / "fixture.html").read_text(encoding="utf-8")
SECTIONS = ["Getting started", "Accounts", "Billing", "Regions", "Storage", "Backups", "Networking", "Logs",
            "Alerts", "Teams", "Tokens", "Webhooks", "Quotas", "Errors", "Rate limits", "Support"]


def doc(anchors):
    def section(title):
        anchor = ' id="%s"' % title.lower().replace(" ", "-") if anchors else ""
        return f'<section><h2{anchor}>{title}</h2><p style="min-height:700px">About {title.lower()}.</p></section>'
    body = "".join(section(t) for t in SECTIONS)
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>Service guide</title>{RECORDER}</head>'
            f'<body style="font:16px sans-serif;margin:30px"><h1>Service guide</h1>{body}</body></html>')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path, _, query = self.path.partition("?")
        kind, body = "text/html", None
        if path == "/shop":
            body = SHOP
        elif path == "/api/products":
            API_HITS.append(self.path)
            kind, body = "application/json", json.dumps({"data": {"results": PRODUCTS}})
        elif path == "/form":
            body = FORM.replace(">Send request<", ">Submit request<") if "v=2" in query else FORM
        elif path in ("/doc", "/doc-plain"):
            body = doc(anchors=path == "/doc")
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


# --- the stand-in models: decisions follow a plan; values and rankings are fixed ------------------------------------
PLAN, VALUES, MODEL_ON = [], {}, [True]


def dist(keys, pick):
    return {k: float(k == pick) for k in keys}


def target_text(body, ident):
    index, _, option = ident.partition(":")
    line = next((e for e in body["state"]["elements"] if e.startswith(f"[{index}] ")), "")
    if option:
        found = re.search(rf"{re.escape(ident)} ([^;]*)", line)
        return found.group(1) if found else ""
    return line


async def stand_in(url, key, body, headers=None):
    if not MODEL_ON[0]:
        raise AssertionError("a model was called during a replay")
    if url.endswith("/chat/completions"):  # the text model: a field's value
        label = json.loads(body["messages"][1]["content"])["field"]["label"]
        return {"choices": [{"message": {"content": json.dumps({"text": VALUES[label]})}}]}
    questions = body["questions"]
    if "blocks" in body["state"]:
        return {"answers": {q: {"noul": 0.9} for q in questions}}
    op, label = PLAN.pop(0)
    criteria = questions["operation"]["criteria"]
    assert op in criteria, f"{op} not offered: {list(criteria)}"
    answers = {"operation": {"choice": op, "confidence": 1.0, "probabilities": dist(criteria, op)}}
    for name, q in questions.items():
        if name != "operation":
            ids = list(q["criteria"])
            hit = next((i for i in ids if label and label in target_text(body, i)), ids[0])
            answers[name] = {"choice": hit, "confidence": 1.0, "probabilities": dist(ids, hit)}
    return {"answers": answers}


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


async def trusted(page):
    events = (await page.evaluate("window.rec"))["events"]
    return events, all(e["trusted"] for e in events)


IN_VIEW = """t => { const h = [...document.querySelectorAll('h2')].find(h => h.textContent === t);
  const r = h.getBoundingClientRect(); return [r.top >= -2 && r.bottom <= innerHeight + 2, Math.round(r.top), scrollY]; }"""


async def main():
    model.post_json = stand_in
    for k, v in {"TYPESAFE_API_KEY": "stand-in", "TEXT_MODEL_API_KEY": "stand-in",
                 "TEXT_MODEL_BASE_URL": "https://llm.invalid/v1", "TEXT_MODEL": "stand-in"}.items():
        os.environ[k] = v
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    work = Path(tempfile.mkdtemp(prefix="ccagent-skills-", dir=HERE))
    store = SkillStore(work / "skills")
    session = None
    try:
        session = await Session.launch(profile=work / "profile", headless=False, humanize=True)
        # A spare tab that stays open: closing a scenario's tab must not leave the browser with none (it quits).
        keeper = session.context.pages[0] if session.context.pages else await session.context.new_page()
        keeper._ca_world = None  # marked, so new_tab() does not reuse it

        print("== lists and the JSON shortcut", flush=True)
        goal = "List the kettles on sale"
        PLAN[:] = [("DONE", None)]
        page = await session.new_tab(f"{base}/shop")
        result = await run_with_skill(session, goal, url=f"{base}/shop", page=page, store=store)
        items = result["items"]
        print("  rows:", items[:2], flush=True)
        check(result["skill"]["used"] == "learned" and len(items) == 6, f"learned, and read {len(items)} rows of 6")
        check([r.get("title") for r in items[:3]] == ["Blue kettle", "Red kettle", "Steel kettle"],
              "titles come from the cards, not the navigation")
        check(items[0].get("price") == "€24,50", f"the sale price, not the struck-out one ({items[0].get('price')})")
        check(items[0].get("link") == f"{base}/p/1", "links are absolute")
        skill = store.get(goal, f"{base}/shop")
        check(bool(skill and skill.get("shortcut")) and skill["shortcut"]["url"].endswith("/api/products?q=kettle"),
              "the skill keeps the JSON request the rows came from")
        await page.close()

        MODEL_ON[0], hits = False, len(API_HITS)
        page = await session.new_tab(f"{base}/shop")
        result = await run_with_skill(session, goal, url=f"{base}/shop", page=page, store=store)
        events, _ = await trusted(page)
        check(result["skill"]["used"] == "shortcut" and [r["title"] for r in result["items"]] == [p["name"] for p in PRODUCTS],
              "the replay read all rows through the request, with the model off")
        check(result["actions"] == 0 and not events, "no clicks or keys on the page")
        check(len(API_HITS) - hits == 2, f"the page loaded it once, the shortcut once more ({len(API_HITS) - hits})")
        await page.close()

        print("== skills: learn, replay, repair", flush=True)
        MODEL_ON[0] = True
        goal = "Request a loan for reader number A-4417, to be collected from the Airport kiosk."
        VALUES["Reader number"] = "A-4417"
        PLAN[:] = [("TYPE_TEXT", "Reader number"), ("SELECT", "Airport kiosk"), ("CLICK", "Send request"),
                   ("DONE", None)]
        page = await session.new_tab(f"{base}/form")
        result = await run_with_skill(session, goal, url=f"{base}/form", page=page, store=store)
        check(result["skill"]["used"] == "learned" and "Requested: A-4417 / airport" in result["markdown"],
              f"learned the form ({result['actions']} steps, {result['usage']['requests']} model requests)")
        await page.close()

        MODEL_ON[0] = False
        page = await session.new_tab(f"{base}/form")
        result = await run_with_skill(session, goal, url=f"{base}/form", page=page, store=store)
        events, ok = await trusted(page)
        check(result["skill"]["used"] == "replayed" and result["usage"]["requests"] == 0,
              f"replayed {result['actions']} steps with the model off")
        check(await page.text_content("#out") == "Requested: A-4417 / airport", "the replay sent the same request")
        check(bool(events) and ok, f"every replayed input event was trusted ({len(events)})")
        await page.close()

        MODEL_ON[0] = True
        PLAN[:] = [("CLICK", "Submit request"), ("DONE", None)]
        page = await session.new_tab(f"{base}/form?v=2")  # the site renamed its button
        result = await run_with_skill(session, goal, url=f"{base}/form?v=2", page=page, store=store)
        check(result["skill"]["used"] == "repaired" and "Send request" in result["skill"]["because"],
              f"the replay noticed the change: {result['skill'].get('because')}")
        check(result["decisions"] == 2 and await page.text_content("#out") == "Requested: A-4417 / airport",
              "the loop decided only the changed step (and done), and the request went out")
        await page.close()
        MODEL_ON[0] = False
        page = await session.new_tab(f"{base}/form?v=2")
        result = await run_with_skill(session, goal, url=f"{base}/form?v=2", page=page, store=store)
        check(result["skill"]["used"] == "replayed" and await page.text_content("#out") == "Requested: A-4417 / airport",
              "the rewritten skill replays on the renamed button, with the model off")
        await page.close()

        print("== confirm", flush=True)
        MODEL_ON[0] = True
        PLAN[:] = [("TYPE_TEXT", "Reader number"), ("SELECT", "Airport kiosk"), ("CLICK", "Send request")]
        page = await session.new_tab(f"{base}/form")
        result = await run(session, goal, page=page, confirm=True)
        check(result["status"] == "needs_confirmation" and result["pending"]["label"] == "Send request",
              f"stopped before the send button: {result['detail']}")
        check(await page.text_content("#out") == "" and await page.input_value("#reader") == "A-4417",
              "the form was filled in, the request not sent")
        state = await session.observe(page)
        check(not any(a["kind"] == "find" for a in state["actions"]), "a short page offers no jump")
        await page.close()

        print("== find on a long page", flush=True)
        label = "Jump to words from the goal on this long page (a section, heading or item further away)"
        VALUES[label] = "Rate limits"
        for path, anchored in (("/doc", True), ("/doc-plain", False)):
            PLAN[:] = [("FIND_TEXT", None), ("DONE", None)]
            page = await session.new_tab(f"{base}{path}")
            state = await session.observe(page)
            check(any(a["kind"] == "find" for a in state["actions"]), f"{path}: a long page offers the jump")
            result = await run(session, "Go to the section about rate limits.", page=page)
            events, ok = await trusted(page)
            seen, top, scrolled = await page.evaluate(IN_VIEW, "Rate limits")
            check(result["status"] == "done" and result["trace"][0]["text"] == "Rate limits" and seen,
                  f"{path}: the 'Rate limits' heading is on screen (top {top}px, page scrolled {scrolled}px)")
            if anchored:
                check(page.url.endswith("#rate-limits") and not events, "jumped through its anchor, no input events")
            else:
                wheels = [e for e in events if e["t"] == "wheel"]
                check(bool(wheels) and ok and "#" not in page.url, f"scrolled there with {len(wheels)} trusted wheel events")
            await page.close()
    finally:
        if session:
            await session.close()
        server.shutdown()
        for _ in range(20):
            shutil.rmtree(work, ignore_errors=True)
            if not work.exists():
                break
            await asyncio.sleep(0.5)
        check(not work.exists(), "profile and skills dirs removed")
    print("RESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}", flush=True)
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
