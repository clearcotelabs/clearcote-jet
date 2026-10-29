"""One task across many profiles: template goals with {{slots}}, secrets, confirming a click, and the worker protocol.

A scripted checkout page and model (no browser, no model calls). The worker is driven in-process through a real pipe
for stdin and a recorder for stdout.
"""

import asyncio
import json
import os
import threading

import pytest

from clearcote_jet import agent, model, worker
from clearcote_jet.model import NeedsInput, goal_spans
from clearcote_jet.skills import SkillStore, replay, run_with_skill, skill_key
from clearcote_jet.slots import Values, canonical, redact, slots

START = "https://shop.example/checkout"
GOAL = "Log in as {{email}} with password {{password}}, then place the order"
A = {"email": "anna@one.example", "password": "s3cret-A!"}
B = {"email": "bob@two.example", "password": "hunter2-B?"}
SECRET = ["password"]


class Page:
    def __init__(self, url=START):
        self.url = url

    async def title(self):
        return "Checkout"

    def is_closed(self):
        return False


class Capture:
    async def stop(self):
        return []


class Shop:
    """A checkout: an email field, a password field, and a button that places the order (it can't be taken back).

    echo: the page shows what was typed into the password field, in the field and in a line of text (a badly made
    page, or a field that is not type=password): the model must still never receive it.
    """

    closed = direct = False

    def __init__(self, echo=False, orders_needed=1, slow=0.0):
        self.email, self.password, self.orders, self.echo = "", "", 0, echo
        self.orders_needed, self.slow = orders_needed, slow
        self.acts, self.closed_calls, self.tabs = [], 0, []

    def actions(self):
        out = [{"id": "e1", "node": 1, "kind": "fill", "role": "textbox", "label": "Email", "value": self.email},
               {"id": "e2", "node": 2, "kind": "fill", "role": "textbox", "label": "Password",
                "value": self.password if self.echo else ""}]
        if self.orders < self.orders_needed:
            out.append({"id": "e3", "node": 3, "kind": "click", "role": "button", "label": "Place order",
                        "value": ""})
        out.append({"id": "wait", "kind": "wait", "label": "Wait for the page to update"})
        return out

    def text(self):
        lines = ["Checkout", "Email", "Password"]
        if self.echo and self.password:
            lines.append(f"Your password is {self.password}")
        if self.orders:
            lines.append(f"Order placed for {self.email}")
        return "\n".join(lines)

    async def new_tab(self, url=None, reuse_blank=True):
        self.tabs.append((url, reuse_blank))
        return Page(url or START)

    async def observe(self, page, after=None, menus=False):
        return {"url": page.url, "title": "Checkout", "text": self.text(), "actions": self.actions(),
                "marker": json.dumps([self.email, self.password, self.orders])}

    async def act(self, page, state, action, text=None):
        if self.slow:
            await asyncio.sleep(self.slow)
        await asyncio.sleep(0)
        self.acts.append((action["kind"], action["label"], text))
        if action["kind"] == "fill":
            setattr(self, action["label"].lower(), text)
        elif action["label"] == "Place order":
            self.orders += 1

    async def fresh(self, page, state, action=None):
        return True

    async def settled_markdown(self, page):
        return "# Checkout\n\n" + self.text().replace("\n", "\n\n")

    async def lists(self, page):
        return []

    async def extract(self, page, spec):
        return []

    async def fetch_json(self, page, request):
        return None

    def capture_json(self, page):
        return Capture()

    def is_gone(self, page):
        return False

    async def close(self):
        self.closed_calls += 1


PLAN = [("TYPE_TEXT", "Email"), ("TYPE_TEXT", "Password"), ("CLICK", "Place order"), ("DONE", None)]


def stub_model(monkeypatch, plan=PLAN):
    """The decision model as a script; a field's value is chosen among the goal's own words, as Jet does without a
    text model (an email for Email, the {{password}} token for Password). Every goal and value context it is given is
    kept in calls["seen"]."""
    calls = {"choose": 0, "value": 0, "rank": 0, "seen": []}

    async def choose(state, goal, history):
        calls["seen"].append({"goal": goal, "state": state, "history": history})
        op, label = plan[min(calls["choose"], len(plan) - 1)]
        calls["choose"] += 1
        action = next(a for a in state["actions"] if a["label"] == label) if label else \
            {"id": op, "kind": op.lower(), "label": op}
        return {"action": action, "operation": op, "target": None, "probability": 0.9, "confidence": 1.0,
                "alternatives": [], "usage": {"input_tokens": 1000}, "latency_ms": 1}

    async def field_text(context):
        calls["value"] += 1
        calls["seen"].append(context)
        spans = goal_spans(context["goal"])
        want = {"Email": lambda s: "@" in s and " " not in s, "Password": lambda s: s.startswith("{{")}
        pick = next((s for s in spans if want[context["field"]["label"]](s)), None)
        if pick is None:
            raise NeedsInput(context["field"]["label"])
        return pick, {"latency_ms": 1, "usage": {"input_tokens": 100}, "billed": True}

    async def rank_blocks(goal, blocks):
        calls["rank"] += 1
        calls["seen"].append({"goal": goal, "blocks": blocks})
        return [0.9] * len(blocks), {"input_tokens": 500}

    monkeypatch.setattr(agent, "choose", choose)
    monkeypatch.setattr(agent, "field_text", field_text)
    monkeypatch.setattr(agent, "rank_blocks", rank_blocks)
    return calls


def no_model(monkeypatch):
    async def called(*_):
        raise AssertionError("the model was called")
    for name in ("choose", "field_text", "rank_blocks"):
        monkeypatch.setattr(agent, name, called)


def learn(monkeypatch, store, values=A, site=None):
    calls = stub_model(monkeypatch)
    site = site or Shop()
    result = asyncio.run(run_with_skill(site, GOAL, url=START, page=Page(), store=store, values=values,
                                        secret=SECRET))
    assert result["status"] == "done" and result["skill"]["used"] == "learned", result
    return result, site, calls


# --- slots --------------------------------------------------------------------------------------------------------
def test_slots_fill_show_mask_and_templatize():
    v = Values(A, SECRET)
    assert slots(GOAL) == ["email", "password"] and canonical("as {{ email }}") == "as {{email}}"
    assert v.for_model(GOAL) == "Log in as anna@one.example with password {{password}}, then place the order"
    assert v.fill("{{password}}") == "s3cret-A!" and v.show("{{email}} {{password}}") == "anna@one.example {{password}}"
    assert v.mask("is s3cret-A! here") == "is {{password}} here"
    assert v.templatize("anna@one.example", ["email", "password"]) == "{{email}}"
    assert v.templatize("Order placed for anna@one.example", ["email"]) == "Order placed for {{email}}"
    short = Values({"n": "1"})
    assert short.templatize("1", ["n"]) == "{{n}}" and short.templatize("page 1 of 10", ["n"]) == "page 1 of 10"


def test_a_slot_token_is_one_typeable_value_of_the_goal():
    spans = goal_spans("Log in as anna@one.example with password {{password}}.")
    assert "{{password}}" in spans and "password}}" not in spans and "{{password" not in spans
    assert "password {{password}}" in spans  # still part of longer spans, braces kept


def test_redact_takes_every_secret_out_of_a_request_at_any_depth():
    body = {"state": {"text": "pw: s3cret-\"A\"", "elements": ['[2] textbox "Password" value="s3cret-\"A\""']}}
    out = redact(body, (('s3cret-"A"', "{{password}}"),))
    assert "s3cret" not in json.dumps(out) and out["state"]["text"] == "pw: {{password}}"
    assert redact(body, ()) is body


# --- keys ---------------------------------------------------------------------------------------------------------
def test_the_key_of_a_plain_goal_is_unchanged():
    # computed with 0.2.5's skill_key: skills learned before templates existed are still found
    assert skill_key("Request a loan for A-4417 at the airport kiosk", "https://library.example/loan") \
        == "e7b59137c5b79151"
    assert skill_key("  request a LOAN for A-4417 at the airport kiosk ", "https://library.example/loan/?x=1") \
        == "e7b59137c5b79151"


def test_a_template_is_one_skill_whatever_the_values(monkeypatch, tmp_path):
    store = SkillStore(tmp_path)
    learn(monkeypatch, store, A)
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1 and files[0] == store.path(GOAL, START)
    assert files[0].name.endswith(skill_key(GOAL, START) + ".json")
    no_model(monkeypatch)
    again = asyncio.run(run_with_skill(Shop(), GOAL.replace("{{email}}", "{{ email }}"), url=START, page=Page(),
                                       store=store, values=B, secret=SECRET))
    assert again["skill"]["used"] == "replayed" and list(tmp_path.glob("*.json")) == files


def test_a_template_without_values_is_a_plain_goal(monkeypatch, tmp_path):
    """No values: braces and all, the goal is taken as written (as before), and a template's skill is not replayed."""
    store = SkillStore(tmp_path)
    learn(monkeypatch, store, A)
    no_model(monkeypatch)
    site = Shop()
    result = asyncio.run(run_with_skill(site, GOAL, url=START, page=Page(), store=store))
    assert result["status"] == "needs_input" and "{{email}}" in result["detail"] and site.acts == []


# --- learning and replaying a template ----------------------------------------------------------------------------
def test_typed_values_are_saved_as_their_slots_and_the_secret_never(monkeypatch, tmp_path):
    store = SkillStore(tmp_path)
    result, site, _ = learn(monkeypatch, store, A, Shop(echo=True))
    assert site.acts[:2] == [("fill", "Email", "anna@one.example"), ("fill", "Password", "s3cret-A!")]
    skill = store.get(GOAL, START)
    assert skill["version"] == 2 and skill["goal"] == GOAL
    assert skill["slots"] == ["email", "password"] and skill["secret"] == ["password"]
    assert [s.get("text") for s in skill["steps"]] == ["{{email}}", "{{password}}", None]
    assert "Order placed for {{email}}" in skill["end"]["lines"]
    saved = store.path(GOAL, START).read_text(encoding="utf-8")
    assert "s3cret-A!" not in saved and "anna@one.example" not in saved
    assert "s3cret-A!" not in json.dumps(result)  # the page echoed it; the result does not


def test_a_replay_fills_the_slots_with_its_own_values_and_no_model(monkeypatch, tmp_path):
    store = SkillStore(tmp_path)
    learn(monkeypatch, store, A)
    no_model(monkeypatch)
    site, steps = Shop(), []
    result = asyncio.run(run_with_skill(site, GOAL, url=START, page=Page(), store=store, values=B, secret=SECRET,
                                        on_step=steps.append))
    assert result["status"] == "done" and result["skill"]["used"] == "replayed" and result["usage"]["requests"] == 0
    assert site.acts == [("fill", "Email", "bob@two.example"), ("fill", "Password", "hunter2-B?"),
                         ("click", "Place order", None)]
    assert [s["text"] for s in steps] == ["bob@two.example", "{{password}}", None]
    assert "hunter2-B?" not in json.dumps(result)


def test_a_slot_with_no_value_is_needs_input_naming_it(monkeypatch, tmp_path):
    store = SkillStore(tmp_path)
    learn(monkeypatch, store, A)
    no_model(monkeypatch)
    site = Shop()
    result = asyncio.run(run_with_skill(site, GOAL, url=START, page=Page(), store=store,
                                        values={"email": "bob@two.example"}, secret=SECRET))
    assert result["status"] == "needs_input" and "{{password}}" in result["detail"] and site.acts == []
    result = asyncio.run(replay(site, store.get(GOAL, START), Page(), values=Values({"password": "x"})))
    assert result["status"] == "needs_input" and "{{email}}" in result["detail"] and site.acts == []


def test_a_template_run_missing_a_value_does_nothing(monkeypatch, tmp_path):
    calls = stub_model(monkeypatch)
    site = Shop()
    result = asyncio.run(agent.run(site, GOAL, page=Page(), values={"email": "a@b.example"}, secret=SECRET))
    assert result["status"] == "needs_input" and "{{password}}" in result["detail"]
    assert calls["choose"] == 0 and site.acts == []


def test_the_model_never_sees_a_secret(monkeypatch, tmp_path):
    """Through the real request builders (choose, the value question, ranking): every request body that would leave
    for the model is recorded, and the page echoes the password back in its text and in the field."""
    sent = []
    plan = list(PLAN)

    class Response:
        status_code, is_error, text = 200, False, ""

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    class Client:  # the one way out to a model: model.post_json's httpx client
        async def post(self, url, json=None, headers=None):
            return Response(await stand_in(url, None, json))

    def target(body, ident):
        return next((e for e in body["state"]["elements"] if e.startswith(f"[{ident}] ")), "")

    async def stand_in(url, key, body, headers=None):
        sent.append(json.dumps(body, ensure_ascii=False))
        questions = body["questions"]
        if "blocks" in body["state"]:
            return {"answers": {q: {"noul": 0.9} for q in questions}}
        if "value" in questions:  # the field's value, among the goal's words
            crit = questions["value"]["criteria"]
            field = body["state"]["field"]["label"]
            pick = next(k for k, v in crit.items() if (v.startswith("{{") if field == "Password" else "@" in v
                                                       and " " not in v))
            return {"answers": {"value": {"choice": pick, "confidence": 1.0,
                                          "probabilities": {k: float(k == pick) for k in crit}}}}
        op, label = plan.pop(0)
        crit = questions["operation"]["criteria"]
        answers = {"operation": {"choice": op, "confidence": 1.0, "probabilities": {k: float(k == op) for k in crit}}}
        for name, q in questions.items():
            if name != "operation":
                ids = list(q["criteria"])
                hit = next((i for i in ids if label and f'"{label}"' in target(body, i)), ids[0])
                answers[name] = {"choice": hit, "confidence": 1.0, "probabilities": {i: float(i == hit) for i in ids}}
        return {"answers": answers}

    monkeypatch.setattr(model, "CLIENT", Client())
    monkeypatch.setenv("TYPESAFE_API_KEY", "stand-in")
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    store, site = SkillStore(tmp_path), Shop(echo=True)
    result = asyncio.run(run_with_skill(site, GOAL, url=START, page=Page(), store=store, values=A, secret=SECRET))
    assert result["status"] == "done" and len(sent) >= 5
    assert site.password == "s3cret-A!"  # the keys pressed were the secret's
    assert not any("s3cret-A!" in body for body in sent)
    assert any("{{password}}" in body for body in sent)  # the goal (and the echoed text) carry the token
    assert any("anna@one.example" in body for body in sent)  # a plain value is the model's to see
    assert "s3cret-A!" not in json.dumps(result) and "s3cret-A!" not in store.path(GOAL, START).read_text("utf-8")


def test_the_trace_and_model_history_keep_the_token(monkeypatch):
    calls = stub_model(monkeypatch)
    site = Shop()
    result = asyncio.run(agent.run(site, GOAL, page=Page(), values=A, secret=SECRET))
    assert result["status"] == "done" and site.password == "s3cret-A!"
    assert [h["text"] for h in result["trace"]] == ["anna@one.example", "{{password}}", None]
    # what the agent itself writes for the model: the goal, the steps so far (the page is swept in model.post_json)
    assert "s3cret-A!" not in json.dumps([[c.get("goal"), c.get("history"), c.get("recent_actions")]
                                          for c in calls["seen"]], default=str)
    assert "placeholders" in calls["seen"][1]  # the value question is told what a {{token}} is


# --- confirm with approve -----------------------------------------------------------------------------------------
def test_approve_yes_clicks_the_pending_control_and_the_task_goes_on(monkeypatch):
    calls = stub_model(monkeypatch)
    site, asked = Shop(), []

    async def approve(pending, detail):
        asked.append((pending, detail))
        return True

    result = asyncio.run(agent.run(site, GOAL, page=Page(), values=A, secret=SECRET, confirm=True, approve=approve))
    assert result["status"] == "done" and site.orders == 1
    assert asked == [({"kind": "click", "label": "Place order", "role": "button"},
                      "stopped before 'Place order', which can't be taken back: confirm to go ahead")]
    assert calls["choose"] == 4  # the confirmed click was not decided again


def test_each_irreversible_click_is_asked_about(monkeypatch):
    stub_model(monkeypatch, [*PLAN[:3], ("CLICK", "Place order"), ("DONE", None)])
    site, asked = Shop(orders_needed=2), []
    result = asyncio.run(agent.run(site, GOAL, page=Page(), values=A, secret=SECRET, confirm=True,
                                   approve=lambda p, d: asked.append(p) or True))
    assert result["status"] == "done" and site.orders == 2 and len(asked) == 2


def test_approve_no_cancels_and_clicks_nothing(monkeypatch):
    stub_model(monkeypatch)
    site = Shop()
    result = asyncio.run(agent.run(site, GOAL, page=Page(), values=A, secret=SECRET, confirm=True,
                                   approve=lambda p, d: False))
    assert result["status"] == "cancelled" and site.orders == 0 and "Place order" in result["detail"]


def test_confirm_without_approve_still_stops(monkeypatch):
    stub_model(monkeypatch)
    site = Shop()
    result = asyncio.run(agent.run(site, GOAL, page=Page(), values=A, secret=SECRET, confirm=True))
    assert result["status"] == "needs_confirmation" and result["pending"]["label"] == "Place order"
    assert site.orders == 0


def test_a_replay_asks_then_continues_from_the_next_step(monkeypatch, tmp_path):
    store = SkillStore(tmp_path)
    learn(monkeypatch, store, A)
    no_model(monkeypatch)
    site, asked = Shop(), []
    result = asyncio.run(run_with_skill(site, GOAL, url=START, page=Page(), store=store, values=B, secret=SECRET,
                                        confirm=True, approve=lambda p, d: asked.append(p) or True))
    assert result["status"] == "done" and result["skill"]["used"] == "replayed" and site.orders == 1
    assert [p["label"] for p in asked] == ["Place order"] and len(site.acts) == 3
    site = Shop()
    result = asyncio.run(run_with_skill(site, GOAL, url=START, page=Page(), store=store, values=B, secret=SECRET,
                                        confirm=True, approve=lambda p, d: False))
    assert result["status"] == "cancelled" and site.orders == 0 and len(site.acts) == 2


def test_max_steps_caps_the_run(monkeypatch):
    stub_model(monkeypatch)
    result = asyncio.run(agent.run(Shop(), GOAL, page=Page(), values=A, secret=SECRET, max_steps=2))
    assert result["status"] == "budget" and result["actions"] == 2


# --- the worker ---------------------------------------------------------------------------------------------------
class Out:
    """stdout of the worker: the events it writes, each checked to be one JSON object on one line. `respond` is
    called with each event and may answer through stdin."""

    def __init__(self, respond=None):
        self.events, self.raw, self.respond = [], b"", respond

    def write(self, data):
        assert isinstance(data, bytes) and data.endswith(b"\n") and data.count(b"\n") == 1
        data.decode("ascii")  # pure ASCII: no code page can garble it
        self.raw += data
        event = json.loads(data)
        self.events.append(event)
        if self.respond:
            self.respond(event)

    def flush(self):
        pass


def drive(monkeypatch, request, respond=None, close_after_request=False, site=None, tmp=None):
    """Run a worker in-process on `site`: stdin is a real pipe. respond(event, send) may write further lines; the
    pipe closes after the request when close_after_request. -> (exit code, events, site)."""
    site = site or Shop()

    async def connect(cdp, humanize=True):
        assert cdp == "http://127.0.0.1:9222"
        return site

    monkeypatch.setattr(worker.Session, "connect", connect)
    read_fd, write_fd = os.pipe()
    lock = threading.Lock()
    closed = []

    def send(obj):
        with lock:
            if not closed:
                os.write(write_fd, (json.dumps(obj) + "\n").encode())

    def close():
        with lock:
            if not closed:
                closed.append(True)
                os.close(write_fd)

    out = Out(lambda e: respond(e, send, close) if respond else None)
    send(request)
    if close_after_request:
        close()
    try:
        code = asyncio.run(worker.Worker(out, read_fd).serve())
    finally:
        close()
        os.close(read_fd)
    return code, out.events, site


def request(**kw):
    return {"cmd": "run", "cdp": "http://127.0.0.1:9222", "url": START, "goal": GOAL, "values": A,
            "secret": SECRET, "confirm": True, "humanize": False, "reuse": True, **kw}


def test_worker_frames_ready_steps_and_result(monkeypatch, tmp_path):
    stub_model(monkeypatch)
    code, events, site = drive(monkeypatch, request(confirm=False, skills_dir=str(tmp_path)))
    assert code == 0 and [e["type"] for e in events] == ["ready", "step", "step", "step", "result"]
    assert events[0] == {"type": "ready", "version": "0.3.0"}
    assert [e["text"] for e in events[1:4]] == ["anna@one.example", "{{password}}", None]
    result = events[-1]
    assert result["status"] == "done" and result["skill"]["used"] == "learned" and result["items_count"] == 0
    assert "trace" not in result and isinstance(result["markdown"], str)
    assert site.tabs == [(START, False)]  # a new tab of its own, not a blank one the person has open
    assert site.closed_calls == 1  # attached: the connection is dropped (Session.close does not close the browser)
    assert "s3cret-A!" not in json.dumps(events) and list(tmp_path.glob("*.json"))


def test_worker_approve_clicks_the_pending_control_and_continues(monkeypatch, tmp_path):
    stub_model(monkeypatch)

    def respond(event, send, close):
        if event["type"] == "needs_confirmation":
            send({"cmd": "approve"})

    code, events, site = drive(monkeypatch, request(skills_dir=str(tmp_path)), respond)
    types = [e["type"] for e in events]
    assert code == 0 and types == ["ready", "step", "step", "needs_confirmation", "step", "result"]
    asked = events[3]
    assert asked["pending"] == {"kind": "click", "label": "Place order", "role": "button"} and asked["detail"]
    assert events[4]["action"] == "Place order" and site.orders == 1 and events[-1]["status"] == "done"

    # the same task with other values: replayed with no model, asked again, approved, done
    no_model(monkeypatch)
    code, events, site = drive(monkeypatch, request(values=B, skills_dir=str(tmp_path)), respond)
    assert code == 0 and [e["type"] for e in events] == ["ready", "step", "step", "needs_confirmation", "step",
                                                         "result"]
    assert events[-1]["skill"]["used"] == "replayed" and events[-1]["usage"]["requests"] == 0
    assert site.email == "bob@two.example" and site.password == "hunter2-B?" and site.orders == 1
    assert "hunter2-B?" not in json.dumps(events)


def test_worker_cancel_clicks_nothing(monkeypatch, tmp_path):
    stub_model(monkeypatch)

    def respond(event, send, close):
        if event["type"] == "needs_confirmation":
            send({"cmd": "cancel"})

    code, events, site = drive(monkeypatch, request(skills_dir=str(tmp_path)), respond)
    assert code == 0 and events[-1]["type"] == "result" and events[-1]["status"] == "cancelled"
    assert site.orders == 0 and not list(tmp_path.glob("*.json"))  # nothing learned from a cancelled run


def test_worker_stdin_closing_is_a_cancel(monkeypatch, tmp_path):
    stub_model(monkeypatch)
    code, events, site = drive(monkeypatch, request(skills_dir=str(tmp_path)), close_after_request=True)
    assert code == 0 and events[-1]["status"] == "cancelled" and site.orders == 0
    stub_model(monkeypatch)

    def respond(event, send, close):  # closed while it is asking
        if event["type"] == "needs_confirmation":
            close()

    code, events, site = drive(monkeypatch, request(skills_dir=str(tmp_path)), respond)
    assert code == 0 and events[-1]["status"] == "cancelled" and site.orders == 0


def test_worker_stop_ends_the_task(monkeypatch, tmp_path):
    stub_model(monkeypatch)

    def respond(event, send, close):
        if event["type"] == "step" and event["step"] == 1:
            send({"cmd": "stop"})

    code, events, site = drive(monkeypatch, request(skills_dir=str(tmp_path)), respond, site=Shop(slow=0.2))
    result = events[-1]
    assert code == 0 and result["type"] == "result" and result["status"] == "stopped"
    assert 1 <= result["actions"] < 3 and site.orders == 0 and site.closed_calls == 1


def test_worker_stop_while_asking(monkeypatch, tmp_path):
    stub_model(monkeypatch)

    def respond(event, send, close):
        if event["type"] == "needs_confirmation":
            send({"cmd": "stop"})

    code, events, site = drive(monkeypatch, request(skills_dir=str(tmp_path)), respond)
    assert code == 0 and events[-1]["status"] == "stopped" and site.orders == 0


@pytest.mark.parametrize("bad, message", [
    ({"cmd": "approve"}, "first line"),
    ({"cmd": "run", "goal": "x"}, "cdp is required"),
    ({"cmd": "run", "cdp": "http://127.0.0.1:9222", "goal": "x", "values": ["a"]}, "values"),
    ({"cmd": "run", "cdp": "http://127.0.0.1:9222", "goal": "x", "max_steps": 0}, "max_steps"),
])
def test_worker_refuses_a_bad_request_with_exit_code_1(monkeypatch, bad, message):
    code, events, site = drive(monkeypatch, bad)
    assert code == 1 and [e["type"] for e in events] == ["ready", "error"] and message in events[-1]["message"]
    assert site.acts == []


def test_worker_reports_a_missing_value_as_needs_input(monkeypatch, tmp_path):
    no_model(monkeypatch)
    code, events, site = drive(monkeypatch, request(values={"email": "x@y.example"}, skills_dir=str(tmp_path)))
    assert code == 0 and events[-1]["status"] == "needs_input" and "{{password}}" in events[-1]["detail"]


def test_worker_error_mid_task_is_an_error_event_without_the_secret(monkeypatch, tmp_path):
    stub_model(monkeypatch)

    class Broken(Shop):
        async def act(self, page, state, action, text=None):
            if action["label"] == "Password":
                raise RuntimeError(f"the page refused {text}")
            await super().act(page, state, action, text)

    code, events, site = drive(monkeypatch, request(skills_dir=str(tmp_path), reuse=False), site=Broken())
    assert code == 1 and events[-1]["type"] == "error" and "{{password}}" in events[-1]["message"]
    assert "s3cret-A!" not in json.dumps(events) and site.closed_calls == 1


# --- the run command's flags ----------------------------------------------------------------------------------------
def test_run_takes_values_and_secrets(monkeypatch):
    import argparse

    from clearcote_jet.cli import task_values
    args = argparse.Namespace(values='{"email": "a@b.example", "n": 3}', secret=["password"])
    assert task_values(args) == ({"email": "a@b.example", "n": "3"}, ["password"])
    assert task_values(argparse.Namespace(values=None, secret=None)) == (None, [])
    for bad in ('["a"]', "{not json"):
        with pytest.raises(SystemExit):
            task_values(argparse.Namespace(values=bad, secret=None))
    with pytest.raises(SystemExit):
        task_values(argparse.Namespace(values=None, secret=["password"]))
