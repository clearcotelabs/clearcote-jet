"""Skills edited by hand: if/while blocks, waits, addresses, keys, JavaScript and CSS selectors, on a scripted site
(no browser, no model calls), and `clearcote-jet skill get|put|forget`."""

import argparse
import asyncio
import io
import json

import pytest

from clearcote_jet import agent
from clearcote_jet.cli import skill_command
from clearcote_jet.skills import SkillStore, check_steps, get_skill, put_edited, replay, run_with_skill

START = "https://shop.example/"


class Page:
    def __init__(self, url=START):
        self.url = url

    async def title(self):
        return "Shop"

    def is_closed(self):
        return False


class Shop:
    """A shop page: a cookie bar that may show, a Load more button that adds rows, a search box, and a page script
    that sets window.flag. Every act is written down."""

    def __init__(self, cookie=False, more=0):
        self.cookie, self.more, self.rows, self.query, self.flag = cookie, more, 0, "", False
        self.acts, self.page = [], None

    def actions(self):
        out = [{"node": 9, "kind": "click", "role": "button", "label": "Accept cookies"}] if self.cookie else []
        if self.more:
            out.append({"node": 2, "kind": "click", "role": "button", "label": "Load more"})
        out.append({"node": 1, "kind": "fill", "role": "searchbox", "label": "Search"})
        for i, a in enumerate(out):
            a["id"] = f"e{i + 1}"
        return out

    async def observe(self, page, after=None, menus=False):
        text = "Shop\n" + ("We use cookies\n" if self.cookie else "") + "\n".join(f"Row {i + 1}" for i in range(self.rows))
        text += f"\nResults for {self.query}" if self.query else ""
        return {"url": page.url, "title": "Shop", "text": text, "actions": self.actions(),
                "marker": json.dumps([self.cookie, self.more, self.rows, self.query, self.flag, page.url])}

    async def act(self, page, state, action, text=None):
        self.acts.append((action["kind"], action.get("label"), text))
        if action["kind"] == "click" and action["label"] == "Load more":
            self.more -= 1
            self.rows += 5
        elif action["kind"] == "click" and action["label"] == "Accept cookies":
            self.cookie = False
        elif action["kind"] == "fill":
            self.query = text

    async def fresh(self, page, state, action=None):
        return True

    async def goto(self, page, url):
        self.acts.append(("goto", url, None))
        page.url = url

    async def run_js(self, page, code):
        self.acts.append(("js", code, None))
        if "window.flag = true" in code:
            self.flag = True

    async def js_true(self, page, expression):
        return expression.strip() == "window.flag" and self.flag

    async def has_selector(self, page, css):
        return css == "#search"

    async def act_on_selector(self, page, css, kind, text=None):
        if css != "#search":
            from clearcote_jet.browser import StalePage
            raise StalePage(f"{css} is not on the page")
        self.acts.append((f"{kind}@css", css, text))
        if kind == "fill":
            self.query = text

    async def settled_markdown(self, page):
        return "# Shop"

    async def extract(self, page, spec):
        return []


def done(result):
    assert result["status"] == "done", result["detail"]
    return result


def go(site, steps, **kw):
    skill = {"version": 1, "goal": "g", "start_url": START, "edited": True, "steps": steps}
    return asyncio.run(replay(site, skill, Page(), **kw))


TEXT = lambda v, neg=False: {"type": "text", "value": v, **({"not": True} if neg else {})}  # noqa: E731


def test_if_takes_then_or_else_by_what_the_page_shows():
    steps = [{"kind": "if", "condition": TEXT("We use cookies"),
              "then": [{"kind": "click", "role": "button", "label": "Accept cookies"}],
              "else": [{"kind": "fill", "role": "searchbox", "label": "Search", "text": "no banner"}]}]
    site = Shop(cookie=True)
    r = done(go(site, steps))
    assert site.acts == [("click", "Accept cookies", None)] and not site.cookie
    assert r["trace"][0]["action"].startswith("If the page shows") and r["trace"][0]["action"].endswith("yes")
    site = Shop(cookie=False)
    done(go(site, steps))
    assert site.acts == [("fill", "Search", "no banner")]


def test_while_repeats_until_the_condition_stops_and_never_past_max():
    loop = {"kind": "while", "condition": {"type": "control", "value": "Load more"},
            "then": [{"kind": "click", "role": "button", "label": "Load more"}], "max": 10}
    site = Shop(more=3)
    done(go(site, [loop]))
    assert site.rows == 15 and [a[1] for a in site.acts] == ["Load more"] * 3
    site = Shop(more=50)
    done(go(site, [{**loop, "max": 4}]))
    assert site.rows == 20


def test_wait_for_goes_on_when_true_and_stops_with_why_when_it_never_is():
    site = Shop(more=1)
    done(go(site, [{"kind": "click", "role": "button", "label": "Load more"},
                   {"kind": "wait_for", "condition": TEXT("Row 5"), "seconds": 2}]))
    r = go(Shop(), [{"kind": "wait_for", "condition": TEXT("Row 99"), "seconds": 0.6}])
    assert r["status"] == "diverged" and "still not the page shows 'Row 99'" in str(r["detail"])


def test_address_key_javascript_and_selector_steps():
    site = Shop()
    r = done(go(site, [{"kind": "goto", "url": "https://shop.example/search"},
                       {"kind": "fill", "selector": "#search", "text": "kettle"},
                       {"kind": "key", "key": "enter"},
                       {"kind": "js", "code": "window.flag = true"},
                       {"kind": "if", "condition": {"type": "js", "value": "window.flag"},
                        "then": [{"kind": "wait", "seconds": 0}]}]))
    assert site.acts == [("goto", "https://shop.example/search", None), ("fill@css", "#search", "kettle"),
                         ("key", "Press Enter", None), ("js", "window.flag = true", None)]
    assert site.query == "kettle" and [h["kind"] for h in r["trace"]] == ["goto", "fill", "key", "js", "if"]


def test_a_missing_selector_or_control_stops_with_the_step_number():
    r = go(Shop(), [{"kind": "wait", "seconds": 0},
                    {"kind": "if", "condition": TEXT("Shop"), "then": [{"kind": "click", "selector": "#gone"}]}])
    assert r["status"] == "diverged" and "step 2.1" in str(r["detail"]) and "#gone" in str(r["detail"])
    r = go(Shop(), [{"kind": "click", "role": "button", "label": "Checkout"}])
    assert r["status"] == "diverged" and "step 1" in str(r["detail"])


def test_slot_values_fill_nested_steps_and_conditions():
    skill = {"version": 2, "goal": "search for {{q}}", "start_url": START, "edited": True, "slots": ["q"],
             "steps": [{"kind": "if", "condition": TEXT("Shop"),
                        "then": [{"kind": "fill", "role": "searchbox", "label": "Search", "text": "{{q}}"}]},
                       {"kind": "wait_for", "condition": TEXT("Results for {{q}}"), "seconds": 1}]}
    from clearcote_jet.slots import Values
    site = Shop()
    done(asyncio.run(replay(site, skill, Page(), values=Values({"q": "teapot"}))))
    assert site.query == "teapot"


def test_an_edited_skill_that_stops_is_not_learned_over(monkeypatch, tmp_path):
    async def no_loop(*a, **k):
        raise AssertionError("the model loop must not take over an edited skill")
    monkeypatch.setattr("clearcote_jet.skills.run", no_loop)
    monkeypatch.setattr(agent, "run", no_loop)
    store = SkillStore(tmp_path)
    put_edited(store, "buy it", START, [{"kind": "click", "role": "button", "label": "Checkout"}])
    before = store.path("buy it", START).read_text(encoding="utf-8")
    r = asyncio.run(run_with_skill(Shop(), "buy it", url=START, page=Page(), store=store))
    assert r["status"] == "diverged" and r["skill"]["used"] == "edited"
    assert store.path("buy it", START).read_text(encoding="utf-8") == before


def test_an_edited_skill_ends_where_its_steps_end(tmp_path):
    store = SkillStore(tmp_path)
    put_edited(store, "search", START, [{"kind": "fill", "role": "searchbox", "label": "Search", "text": "x"}])
    r = asyncio.run(run_with_skill(Shop(), "search", url=START, page=Page(), store=store))
    assert r["status"] == "done" and r["skill"]["used"] == "replayed"


def test_put_keeps_the_learned_parts_drops_shortcut_and_end_and_keys_templates(tmp_path):
    store = SkillStore(tmp_path)
    goal = "Search for {{ query }}"
    learned = {"version": 2, "goal": "Search for {{query}}", "start_url": START, "slots": ["query"], "secret": [],
               "steps": [], "shortcut": {"url": "x"}, "end": {"url": START}, "list": {"item": "li"},
               "learned_at": "2026-10-30T00:00:00+00:00"}
    store.put(learned)
    assert get_skill(store, goal, START)["shortcut"] == {"url": "x"}
    s = put_edited(store, goal, START, [{"kind": "fill", "role": "searchbox", "label": "Search", "text": "{{query}}"},
                                        {"kind": "goto", "url": "https://shop.example/{{page}}"}])
    assert s["edited"] and s["goal"] == "Search for {{query}}" and sorted(s["slots"]) == ["page", "query"]
    assert "shortcut" not in s and "end" not in s and s["list"] == {"item": "li"} and s["learned_at"]
    assert get_skill(store, goal, START)["steps"] == s["steps"]


@pytest.mark.parametrize("steps, why", [
    ([{"kind": "teleport"}], "unknown kind"),
    ([{"kind": "click"}], "label or a CSS selector"),
    ([{"kind": "fill", "label": "Search"}], "text to type"),
    ([{"kind": "goto", "url": "file:///etc/passwd"}], "http"),
    ([{"kind": "key", "key": "F13"}], "unknown key"),
    ([{"kind": "if", "condition": {"type": "text"}, "then": []}], "condition"),
    ([{"kind": "while", "condition": TEXT("x"), "then": [], "max": 1000}], "max"),
    ([{"kind": "if", "condition": TEXT("x"), "then": [{"kind": "js"}]}], "JavaScript needs code"),
])
def test_check_steps_names_what_cannot_run(steps, why):
    with pytest.raises(ValueError, match=why):
        check_steps(steps)


def test_skill_command_get_put_forget(tmp_path):
    def call(action, body=""):
        args = argparse.Namespace(action=action, goal="find tea", url=START, skills_dir=str(tmp_path))
        return skill_command(args, io.StringIO(body))
    assert call("get") == {"ok": True, "skill": None}
    put = call("put", json.dumps({"steps": [{"kind": "goto", "url": "https://shop.example/tea"}]}))
    assert put["ok"] and put["skill"]["steps"][0]["kind"] == "goto"
    assert call("get")["skill"]["edited"] is True
    assert call("put", json.dumps({"steps": [{"kind": "nope"}]})) == {"ok": False, "error": "step 1: unknown kind 'nope'"}
    assert call("forget") == {"ok": True, "forgot": True} and call("get")["skill"] is None
