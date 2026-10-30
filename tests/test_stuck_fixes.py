"""What the stuck-task benchmark (bench/) turned up, pinned without a browser: values that keep their punctuation and
run past five words, keys named in the goal, Enter offered after typing, a BLOCKED that waits for the page first, the
same useless step not repeated, and a page dialog shown and answered."""

import asyncio
from types import SimpleNamespace

import pytest

from clearcote_jet import agent
from clearcote_jet.agent import goal_keys
from clearcote_jet.browser import answer_dialog, dialog_state
from clearcote_jet.model import goal_spans, long_values


def test_a_value_keeps_its_punctuation():
    spans = goal_spans("Log in with username tomsmith and password SuperSecretPassword!")
    assert "SuperSecretPassword!" in spans and "SuperSecretPassword" in spans
    assert "tomsmith" in spans  # sentence punctuation still comes off: "tomsmith," is never offered
    assert "Hunter2?" in goal_spans("Use the password Hunter2?.")


def test_long_values_come_whole_and_first():
    goal = "Write a blog post titled Autumn update with the text Our new office opens in October, and publish it."
    assert "Our new office opens in October" in long_values(goal)
    assert goal_spans(goal).index("Our new office opens in October") < goal_spans(goal).index("Autumn")
    assert long_values('Reply "Thanks, we will be there at nine on Friday" to Anna') == [
        "Thanks, we will be there at nine on Friday"]
    assert long_values("Set the budget to 750 euros") == []  # nothing longer than five words is introduced


def test_keys_only_when_the_goal_speaks_of_pressing():
    assert [k["key"] for k in goal_keys("Press the Escape key in the input box")] == ["Escape"]
    assert [k["key"] for k in goal_keys("Type hello and hit Enter, then press Tab")] == ["Enter", "Tab"]
    assert goal_keys("Enter your name and email") == []
    assert goal_keys("Go to the home page") == []


class Dialog:
    def __init__(self, kind, message, default=""):
        self.type, self.message, self.default_value = kind, message, default
        self.answer = None

    async def accept(self, text=None):
        self.answer = ("accept", text)

    async def dismiss(self):
        self.answer = ("dismiss", None)


def page_with(dialog):
    return SimpleNamespace(url="https://x.test/", _ca_dialogs=True, _ca_dialog=dialog, _ca_dialog_text=None)


def test_a_prompt_is_shown_typed_into_and_accepted():
    d = Dialog("prompt", "I am a JS prompt")
    page = page_with(d)
    state = dialog_state(page)
    assert "prompt dialog: I am a JS prompt" in state["text"]
    kinds = {a["dialog"]: a["kind"] for a in state["actions"]}
    assert kinds == {"accept": "click", "dismiss": "click", "type": "fill"}
    asyncio.run(answer_dialog(page, "type", "hello jet"))
    assert dialog_state(page)["marker"] != state["marker"]  # the typed answer is a change the loop sees
    asyncio.run(answer_dialog(page, "accept"))
    assert d.answer == ("accept", "hello jet") and page._ca_dialog is None


def test_a_confirm_can_be_cancelled_and_an_alert_has_ok_only():
    d = Dialog("confirm", "Delete this address?")
    asyncio.run(answer_dialog(page_with(d), "dismiss"))
    assert d.answer == ("dismiss", None)
    assert [a["dialog"] for a in dialog_state(page_with(Dialog("alert", "Saved")))["actions"]] == ["accept"]


# --- the loop, with a scripted session and model -------------------------------------------------------------------

def state(marker, actions, url="https://x.test/"):
    return {"url": url, "title": "", "text": marker, "marker": marker, "scroll": {"y": 0, "height": 1, "vh": 1},
            "actions": actions + [{"id": "wait", "kind": "wait", "label": "Wait for the page to update"}]}


class Session:
    direct = False
    closed = False
    last_diff = ""

    def __init__(self, states, change_after_wait=None):
        self.states, self.acted, self.waits = list(states), [], 0
        self.change_after_wait = change_after_wait

    async def new_tab(self, url=None):
        return SimpleNamespace(url="https://x.test/", context=SimpleNamespace(request=None), title=self._title)

    async def _title(self):
        return ""

    async def observe(self, page, after=None, menus=False):
        return self.states[0] if len(self.states) == 1 else self.states.pop(0)

    async def fresh(self, page, state, action=None):
        return True

    async def act(self, page, state, action, text=None):
        self.acted.append((action["kind"], action["label"], text))

    async def wait_for_change(self, page, state, seconds=8):
        self.waits += 1
        if self.change_after_wait:
            self.states = [self.change_after_wait]
            return True
        return False

    async def settled_markdown(self, page):
        return ""

    async def lists(self, page):
        return []

    def is_gone(self, page):
        return False

    def capture_json(self, page):
        return None


def scripted(monkeypatch, answers):
    answers = list(answers)

    async def choose(state, goal, history):
        op, label = answers.pop(0)
        action = next((a for a in state["actions"] if a.get("label") == label), {"kind": op.lower(), "label": op,
                                                                                  "id": op})
        return {"action": action, "operation": op, "probability": 1.0, "confidence": 1.0, "alternatives": [],
                "usage": {}, "latency_ms": 1}
    monkeypatch.setattr(agent, "choose", choose)


def test_blocked_waits_for_the_page_once_then_decides_again(monkeypatch):
    loading = state("Loading...", [{"id": "e1", "node": 1, "kind": "click", "role": "button", "label": "Start"}])
    loaded = state("Hello World!", [])
    scripted(monkeypatch, [("BLOCKED", None), ("DONE", None)])
    session = Session([loading], change_after_wait=loaded)
    result = asyncio.run(agent.run(session, "Click Start and read the text", url="https://x.test/"))
    assert session.waits == 1 and result["status"] == "done"


def test_a_step_that_changed_nothing_is_not_offered_again_on_that_page(monkeypatch):
    save = {"id": "e1", "node": 1, "kind": "click", "role": "button", "label": "Save"}
    other = {"id": "e2", "node": 2, "kind": "click", "role": "button", "label": "Publish"}
    page = state("Editor", [save, other])
    seen = []

    async def choose(state, goal, history):
        labels = [a["label"] for a in state["actions"]]
        seen.append(labels)
        # A model that keeps wanting Save while it is offered, then Publish, then is done.
        want = "Save" if "Save" in labels else "Publish" if ("click", "Publish", None) not in session.acted else None
        if want:
            action = next(a for a in state["actions"] if a["label"] == want)
            return {"action": action, "operation": "CLICK", "probability": 1, "confidence": 1, "alternatives": [],
                    "usage": {}, "latency_ms": 1}
        return {"action": {"kind": "done", "label": "DONE"}, "operation": "DONE", "probability": 1, "confidence": 1,
                "alternatives": [], "usage": {}, "latency_ms": 1}
    monkeypatch.setattr(agent, "choose", choose)
    session = Session([page])
    asyncio.run(agent.run(session, "Publish the post", url="https://x.test/"))
    assert [a[1] for a in session.acted] == ["Save", "Publish"]  # Save changed nothing: not clicked twice
    assert "Save" in seen[1] and "Save" not in seen[2]  # asked again with Save, then shown the page without it


def test_enter_is_offered_after_typing(monkeypatch):
    field = {"id": "e1", "node": 1, "kind": "fill", "role": "searchbox", "label": "Search site"}
    seen = []

    async def choose(state, goal, history):
        seen.append([a["label"] for a in state["actions"]])
        if len(seen) == 1:
            return {"action": field, "operation": "TYPE_TEXT", "probability": 1, "confidence": 1, "alternatives": [],
                    "usage": {}, "latency_ms": 1}
        return {"action": {"kind": "done", "label": "DONE"}, "operation": "DONE", "probability": 1, "confidence": 1,
                "alternatives": [], "usage": {}, "latency_ms": 1}

    async def field_text(context):
        return "walnut", {"latency_ms": 1, "model": "x"}
    monkeypatch.setattr(agent, "choose", choose)
    monkeypatch.setattr(agent, "field_text", field_text)
    session = Session([state("Shop", [field]), state("Shop typed", [field])])
    asyncio.run(agent.run(session, "Search the shop for walnut", url="https://x.test/"))
    assert not any("Press Enter" in label for label in seen[0])
    assert any("Press Enter in 'Search site'" in label for label in seen[1])


@pytest.mark.parametrize("goal", ["Fill in the form", "Enter your name"])
def test_no_keys_without_pressing(goal):
    assert goal_keys(goal) == []


def test_a_click_that_opens_a_dialog_ends_the_step_and_finishes_once_answered():
    from clearcote_jet.browser import Session as Real

    page = page_with(None)
    finished = []

    async def press(page_, state_, action, text=None):  # the page's click handler waits on its confirm()
        page._ca_dialog = Dialog("confirm", "Delete the address 'Office'?")
        while page._ca_dialog is not None:
            await asyncio.sleep(0.01)
        finished.append(action["label"])

    session = object.__new__(Real)
    session.check_open = lambda p: None

    async def fresh(*_a, **_k):
        return True
    session.fresh, session._act = fresh, press

    async def flow():
        await asyncio.wait_for(session.act(page, {}, {"kind": "click", "label": "Delete"}), 2)
        assert finished == [] and dialog_state(page)["text"].startswith("The page shows a confirm dialog")
        await answer_dialog(page, "accept")
        assert finished == ["Delete"] and page._ca_pending is None
    asyncio.run(flow())


def test_enter_only_after_typing_into_a_search_like_field():
    from clearcote_jet.agent import searchlike
    assert searchlike({"role": "searchbox", "action": "Site"})
    assert searchlike({"role": "textbox", "action": "Search site"})
    assert not searchlike({"role": "textbox", "action": "Current Address"})


def test_blocked_after_a_dead_link_decides_again_without_it(monkeypatch):
    link = {"id": "e1", "node": 1, "kind": "click", "role": "link", "label": "#1043"}
    menu = {"id": "e2", "node": 2, "kind": "click", "role": "button", "label": "Actions for order #1043"}
    page = state("Orders", [link, menu])
    seen = []

    async def choose(state, goal, history):
        labels = [a["label"] for a in state["actions"]]
        seen.append(labels)
        if not history:
            pick, op = link, "CLICK"
        elif len(history) == 1 and "#1043" in labels:
            pick, op = {"kind": "blocked", "label": "BLOCKED"}, "BLOCKED"  # the link went nowhere; the model gives up
        elif len(history) == 1:
            pick, op = menu, "CLICK"  # shown the page without the dead link
        else:
            pick, op = {"kind": "done", "label": "DONE"}, "DONE"
        return {"action": pick, "operation": op, "probability": 1, "confidence": 1, "alternatives": [], "usage": {},
                "latency_ms": 1}
    monkeypatch.setattr(agent, "choose", choose)
    session = Session([page])
    result = asyncio.run(agent.run(session, "Open the details of order #1043", url="https://x.test/"))
    assert [a[1] for a in session.acted] == ["#1043", "Actions for order #1043"] and result["status"] == "done"


def test_blocked_tries_the_models_best_untried_click_once(monkeypatch):
    menu = {"id": "e2", "node": 2, "kind": "click", "role": "button", "label": "Row actions for #1043"}
    blocked = {"action": {"kind": "blocked", "label": "BLOCKED"}, "operation": "BLOCKED", "probability": 0.8,
               "confidence": 1, "alternatives": [], "usage": {}, "latency_ms": 1,
               "fallback": [{"action": menu, "probability": 0.12}]}
    answers = [blocked, blocked]  # it would give up twice; the first is turned into the click, the second stands

    async def choose(state, goal, history):
        return answers.pop(0)
    monkeypatch.setattr(agent, "choose", choose)
    session = Session([state("Orders", [menu])])
    result = asyncio.run(agent.run(session, "Open the details of order #1043", url="https://x.test/"))
    assert [a[1] for a in session.acted] == ["Row actions for #1043"] and result["status"] == "blocked"


def test_a_weak_fallback_is_not_taken(monkeypatch):
    menu = {"id": "e2", "node": 2, "kind": "click", "role": "button", "label": "Newsletter"}

    async def choose(state, goal, history):
        return {"action": {"kind": "blocked", "label": "BLOCKED"}, "operation": "BLOCKED", "probability": 0.9,
                "confidence": 1, "alternatives": [], "usage": {}, "latency_ms": 1,
                "fallback": [{"action": menu, "probability": 0.01}]}
    monkeypatch.setattr(agent, "choose", choose)
    session = Session([state("Orders", [menu])])
    asyncio.run(agent.run(session, "Open the details of order #1043", url="https://x.test/"))
    assert session.acted == []


def test_goal_times():
    from clearcote_jet.agent import goal_times
    assert goal_times("Click Add Element exactly three times") == 3
    assert goal_times("Press Next 5 times") == 5
    assert goal_times("Ring twice") == 2
    assert goal_times("Add the backpack to the cart") is None


def test_done_short_of_the_count_repeats_the_step(monkeypatch):
    add = {"id": "e1", "node": 1, "kind": "click", "role": "button", "label": "Add Element"}
    ops = iter(["CLICK", "CLICK", "DONE", "DONE"])

    async def choose(state, goal, history):
        op = next(ops)
        act = add if op == "CLICK" else {"kind": "done", "label": "DONE"}
        return {"action": act, "operation": op, "probability": 1, "confidence": 1, "alternatives": [], "usage": {},
                "latency_ms": 1}
    monkeypatch.setattr(agent, "choose", choose)
    session = Session([state("Add/Remove", [add])])
    asyncio.run(agent.run(session, "Click Add Element exactly three times", url="https://x.test/"))
    assert [a[1] for a in session.acted] == ["Add Element"] * 3


def test_the_fallback_skips_a_click_already_tried_here(monkeypatch):
    link = {"id": "e1", "node": 1, "kind": "click", "role": "link", "label": "#1043"}
    menu = {"id": "e2", "node": 2, "kind": "click", "role": "button", "label": "Row actions for #1043"}
    ranked = [{"action": link, "probability": 0.3}, {"action": menu, "probability": 0.1}]
    answers = [{"action": link, "operation": "CLICK", "probability": 1, "confidence": 1, "alternatives": [],
                "usage": {}, "latency_ms": 1}] + [
        {"action": {"kind": "blocked", "label": "BLOCKED"}, "operation": "BLOCKED", "probability": 0.8, "confidence": 1,
         "alternatives": [], "usage": {}, "latency_ms": 1, "fallback": ranked}]

    async def choose(state, goal, history):
        return answers.pop(0) if len(answers) > 1 else answers[0]  # then BLOCKED for good
    monkeypatch.setattr(agent, "choose", choose)
    session = Session([state("Orders", [link, menu]), state("Orders #", [link, menu])])
    asyncio.run(agent.run(session, "Open the details of order #1043", url="https://x.test/"))
    assert [a[1] for a in session.acted] == ["#1043", "Row actions for #1043"]


def test_the_suggestion_matching_what_was_typed_is_chosen():
    from clearcote_jet.agent import _typed_suggestion
    pick = {"kind": "click", "label": "From › Amsterdam Centraal"}
    st = {"actions": [{"kind": "click", "label": "From › Amsterdam Zuid"}, pick]}
    assert _typed_suggestion({"action": "From", "text": "Amsterdam Centraal"}, st) is pick
    assert _typed_suggestion({"action": "From", "text": "Amsterdam"}, st) is None


def test_a_growing_feed_is_scrolled_further():
    from clearcote_jet.agent import _feed_grows
    down = {"id": "scroll_down", "kind": "scroll", "label": "Scroll down"}
    st = {"actions": [down]}
    assert _feed_grows([{"kind": "scroll", "grew": True}], st) is down
    assert _feed_grows([{"kind": "scroll", "grew": False}], st) is None  # it moved, but nothing new loaded
    assert _feed_grows([{"kind": "scroll", "grew": True}] * 11, st) is None
    assert _feed_grows([{"kind": "scroll", "grew": True}], {"actions": []}) is None  # at the bottom


def test_the_loop_never_clicks_something_irreversible_on_its_own(monkeypatch):
    pay = {"id": "e2", "node": 2, "kind": "click", "role": "button", "label": "Pay now"}
    blocked = {"action": {"kind": "blocked", "label": "BLOCKED"}, "operation": "BLOCKED", "probability": 0.8,
               "confidence": 1, "alternatives": [], "usage": {}, "latency_ms": 1,
               "fallback": [{"action": pay, "probability": 0.5}]}

    async def choose(state, goal, history):
        return blocked
    monkeypatch.setattr(agent, "choose", choose)
    session = Session([state("Checkout", [pay])])
    asyncio.run(agent.run(session, "Check the total", url="https://x.test/", confirm=False))
    assert session.acted == []


def test_a_count_belongs_to_the_control_it_follows():
    from clearcote_jet.agent import _short_of_count
    like = {"kind": "click", "label": "Like", "node": 1}
    st = {"actions": [like, {"kind": "click", "label": "Submit", "node": 2}]}
    hist = [{"kind": "click", "action": "Like"}]
    assert _short_of_count("Click Like twice, then click Submit", hist, st) is like
    assert _short_of_count("Click Like, then click Submit twice", hist, st) is None


def test_keys_need_the_pressing_next_to_them():
    assert goal_keys("Go to the home page and press Save") == []
    assert goal_keys("Open the Settings tab and hit Enter") == [g for g in goal_keys("hit Enter")]


def test_dialog_cancel_is_not_irreversible_and_ok_carries_the_message():
    from clearcote_jet.describe import irreversible
    acts = dialog_state(page_with(Dialog("confirm", "Delete the address 'Office'?")))["actions"]
    ok, cancel = acts[0], acts[1]
    assert irreversible(ok) and not irreversible(cancel)
