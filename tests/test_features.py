"""Skills, lists, the JSON shortcut, confirm and find, on a scripted site and model (no browser, no model calls)."""

import asyncio
import json
import re

import pytest

from clearcote_jet import agent, mcp_server
from clearcote_jet.describe import best_list, end_lines, irreversible, named, shape, target_info
from clearcote_jet.model import NeedsInput
from clearcote_jet.shortcut import find_shortcut, number, rows_from_json
from clearcote_jet.skills import SkillStore, locate, replay, run_with_skill, skill_key

START = "https://library.example/loan"
ROWS = [{"title": "Blue kettle", "link": "https://shop.example/p/1", "price": "€24,50"},
        {"title": "Red kettle", "link": "https://shop.example/p/2", "price": "€31,00"},
        {"title": "Steel kettle", "link": "https://shop.example/p/3", "price": "€19,99"}]
BODY = {"data": {"results": [{"name": "Blue kettle", "url": "/p/1", "pricing": {"sale": 24.5, "was": 30}},
                             {"name": "Red kettle", "url": "/p/2", "pricing": {"sale": 31.0, "was": 35}},
                             {"name": "Steel kettle", "url": "/p/3", "pricing": {"sale": 19.99, "was": 25}}]}}


class Page:
    def __init__(self, url=START):
        self.url = url

    async def title(self):
        return "Loan request"

    def is_closed(self):
        return False


class Capture:
    def __init__(self, items):
        self.items = items

    async def stop(self):
        return self.items


class Form:
    """The loan form as a scripted site: what it offers and shows follows from what was done to it."""

    closed = direct = False

    def __init__(self, send="Send request", cookie=False, broken_send=False, rows=None, body=None, long=False):
        self.reader, self.branch, self.sent, self.cookie = "", "central", False, cookie
        self.send, self.broken_send, self.rows, self.body, self.long = send, broken_send, rows, body, long
        self.acts, self.fetches, self.found = [], [], None

    def actions(self):
        out = [{"node": 9, "kind": "click", "role": "button", "label": "Reject all", "value": ""}] if self.cookie else []
        out += [{"node": 1, "kind": "fill", "role": "textbox", "label": "Reader number", "value": self.reader},
                {"node": 1, "kind": "click", "role": "textbox", "label": "Open Reader number", "value": self.reader}]
        out += [{"node": 3, "kind": "select", "role": "combobox", "label": f"Collect from → {name}", "value": value,
                 "current_value": "Airport kiosk" if self.branch == "airport" else "Central library"}
                for value, name in (("central", "Central library"), ("airport", "Airport kiosk")) if value != self.branch]
        out += [{"node": 2, "kind": "click", "role": "button", "label": self.send, "value": ""},
                {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560}]
        if self.long:
            out.append({"id": "find_text", "kind": "find", "label": "Jump to words from the goal on this long page"})
        out.append({"id": "wait", "kind": "wait", "label": "Wait for the page to update"})
        for i, a in enumerate(out):
            a.setdefault("id", f"e{i + 1}")
        return out

    async def new_tab(self, url=None):
        return Page(url or START)

    async def observe(self, page, after=None):
        text = "Loan request\nReader number\nCollect from" + (f"\nRequested: {self.reader} / {self.branch}"
                                                              if self.sent else "")
        return {"url": page.url, "title": "Loan request", "text": text, "actions": self.actions(),
                "marker": json.dumps([self.reader, self.branch, self.sent, self.cookie, self.found]),
                "scroll": {"y": 0, "height": 900, "vh": 800}}

    async def act(self, page, state, action, text=None):
        self.acts.append((action["kind"], action.get("label"), text))
        if action["kind"] == "fill":
            self.reader = text
        elif action["kind"] == "select":
            self.branch = action["value"]
        elif action["kind"] == "click" and action["label"] == self.send and not self.broken_send:
            self.sent = True
        elif action["kind"] == "click" and action["label"] == "Reject all":
            self.cookie = False
        elif action["kind"] == "find":
            self.found = text

    async def fresh(self, page, state, action=None):
        return True

    async def settled_markdown(self, page):
        return "# Loan request\n\nReader number\n\n" + (f"Requested: {self.reader} / {self.branch}" if self.sent else "")

    async def lists(self, page):
        return [{"item": "div.card", "fields": {"title": "a.name", "link": "a.name@href", "price": "span.sale"},
                 "count": len(self.rows), "rows": self.rows}] if self.rows else []

    async def extract(self, page, spec):
        return self.rows or []

    async def fetch_json(self, page, request):
        self.fetches.append(request)
        return self.body

    def capture_json(self, page):
        return Capture([{"url": "https://shop.example/api/search?q=kettle", "method": "GET", "post_data": None,
                         "content_type": None, "body": self.body}] if self.body else [])

    async def screenshot(self, page):
        return b"\x89PNG"

    def is_gone(self, page):
        return False


PLAN = [("TYPE_TEXT", "Reader number"), ("SELECT", "Collect from → Airport kiosk"), ("CLICK", "Send request"),
        ("DONE", None)]
VALUES = {"Reader number": "A-4417", "Jump to words from the goal on this long page": "404 Not Found"}


def model(monkeypatch, *plan, values=VALUES):
    """A scripted decision model: each choose() answers the next step of the plan (the last one repeats)."""
    calls = {"choose": 0, "value": 0, "rank": 0}

    async def choose(state, goal, history):
        op, label = plan[min(calls["choose"], len(plan) - 1)]
        calls["choose"] += 1
        action = next(a for a in state["actions"] if a["label"] == label) if label else \
            next((a for a in state["actions"] if a["id"].upper() == op), {"id": op, "kind": op.lower(), "label": op})
        return {"action": action, "operation": op, "target": None, "probability": 0.9, "confidence": 1.0,
                "alternatives": [], "usage": {"input_tokens": 1000}, "latency_ms": 1}

    async def field_text(context):
        calls["value"] += 1
        value = values.get(context["field"]["label"])
        if value is None:
            raise NeedsInput(context["field"]["label"])
        return value, {"latency_ms": 1, "usage": {"input_tokens": 100}, "billed": True}

    async def rank_blocks(goal, blocks):
        calls["rank"] += 1
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


SHOP = "https://shop.example/search"


def learn(monkeypatch, store, site=None, plan=PLAN, goal="Request a loan for A-4417 at the airport kiosk", url=START):
    model(monkeypatch, *plan)
    site = site or Form()
    result = asyncio.run(run_with_skill(site, goal, url=url, page=Page(url), store=store))
    assert result["status"] == "done" and result["skill"]["used"] == "learned"
    return result, store.get(goal, url)


# --- describing steps ----------------------------------------------------------------------------------------------

def test_irreversible_clicks_and_their_neighbours():
    for label in ("Send request", "Place order", "Pay now", "Book this room", "Delete account", "Bestellen",
                  "Subscribe", "Envoyer"):
        assert irreversible({"kind": "click", "label": label}), label
    for label in ("Search", "Apply filters", "Next page", "Books", "Order by", "Show comments", "Postcode"):
        assert not irreversible({"kind": "click", "label": label}), label
    assert not irreversible({"kind": "fill", "label": "Send to"})  # typing is never the irreversible part


def test_a_label_keeps_its_shape_when_its_numbers_change():
    assert shape("213 comments") == shape("87 comments") == "# comments"
    assert shape("Page 1 of 1.234") == "page # of #"
    assert shape("Reject all") == "reject all"


def test_target_info_remembers_the_place_among_alike_controls():
    links = [{"node": i, "kind": "click", "role": "link", "label": label}
             for i, label in enumerate(["12 comments", "Open", "87 comments", "Open"], 1)]
    assert target_info(links[3], links) == {"nth": 1, "nth_shape": 1}
    assert target_info(links[2], links) == {"nth": 0, "nth_shape": 1}


def test_lists_need_names_and_the_best_named_list_wins():
    assert named(ROWS) and not named(ROWS[:2]) and not named([{"link": "x"}] * 5)
    bare = {"item": "nav li", "rows": [{"link": "x"}] * 9}
    assert best_list([bare, {"item": "div.card", "rows": ROWS}])["item"] == "div.card"
    assert best_list([bare]) is None


def test_end_lines_prefer_steady_lines_that_appeared():
    lines = end_lines("Home\nSearch", "Home\nSearch\nRequested: A-4417 / airport\nThank you\nTicket 9")
    assert lines[0] == "Thank you" and "Requested: A-4417 / airport" in lines and "Home" not in lines


# --- the JSON shortcut ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, value", [("€ 24,50", 24.5), ("1.234,50 EUR", 1234.5), ("$1,234.50", 1234.5),
                                         ("12 500 zł", 12500), ("from £9", 9), (31, 31), ("free", None)])
def test_number_reads_prices_in_their_local_formats(text, value):
    assert number(text) == value


def test_find_shortcut_locates_the_rows_and_fields_in_a_json_answer():
    shortcut = find_shortcut([{"url": "https://shop.example/api/other", "method": "GET", "body": {"ok": True}},
                              {"url": "https://shop.example/api/search?q=kettle", "method": "GET", "post_data": None,
                               "content_type": None, "body": BODY}], ROWS, "https://shop.example/search")
    assert shortcut["url"] == "https://shop.example/api/search?q=kettle"
    assert shortcut["items_path"] == ["data", "results"]
    assert shortcut["fields"] == {"title": ["name"], "link": ["url"], "price": ["pricing", "sale"]}  # not the old price


def test_find_shortcut_needs_most_rows_in_the_answer():
    unrelated = {"items": [{"name": n} for n in ("Teapot", "Mug", "Saucer", "Blue kettle")]}
    assert find_shortcut([{"url": "u", "body": unrelated}], ROWS, "https://shop.example/") is None
    assert find_shortcut([{"url": "u", "body": BODY}], ROWS[:2], "https://shop.example/") is None  # too few rows


def test_rows_from_a_new_answer():
    shortcut = find_shortcut([{"url": "u", "body": BODY}], ROWS, "https://shop.example/search")
    newer = {"data": {"results": [{"name": "Green kettle", "url": "/p/9", "pricing": {"sale": 12}}] * 3}}
    assert rows_from_json(newer, shortcut)[0] == {"title": "Green kettle", "link": "https://shop.example/p/9",
                                                 "price": "12"}
    assert rows_from_json({"data": {}}, shortcut) == [] and rows_from_json(None, shortcut) == []


# --- the store -----------------------------------------------------------------------------------------------------

def test_store_keys_on_goal_and_page_and_writes_whole_files(tmp_path):
    store = SkillStore(tmp_path)
    skill = {"version": 1, "goal": "Find kettles", "start_url": "https://shop.example/search/", "steps": []}
    path = store.put(skill)
    assert path.exists() and not list(tmp_path.glob("*.tmp"))
    assert store.get("  find  KETTLES ", "https://shop.example/search?ref=x") == skill  # case, spaces, query, slash
    assert store.get("Find teapots", "https://shop.example/search") is None
    assert skill_key("a", "https://x.example/p") != skill_key("a", "https://y.example/p")
    assert [s["goal"] for s in store.all()] == ["Find kettles"]
    assert store.forget("Find kettles", "https://shop.example/search") and store.get("Find kettles", START) is None
    path.write_text(json.dumps({**skill, "version": 99}))
    assert store.get("Find kettles", "https://shop.example/search/") is None  # a skill from another version


# --- learning and replaying ----------------------------------------------------------------------------------------

def test_a_finished_run_is_learned_then_replayed_with_no_model(monkeypatch, tmp_path):
    store = SkillStore(tmp_path)
    first, skill = learn(monkeypatch, store)
    assert first["usage"]["requests"] > 0 and skill["learned_with"]["requests"] == first["usage"]["requests"]
    assert [(s["kind"], s["label"]) for s in skill["steps"]] == [
        ("fill", "Reader number"), ("select", "Collect from → Airport kiosk"), ("click", "Send request")]
    assert skill["steps"][0]["text"] == "A-4417" and skill["steps"][1]["value"] == "airport"
    assert "Requested: A-4417 / airport" in skill["end"]["lines"]

    no_model(monkeypatch)
    site = Form()
    again = asyncio.run(run_with_skill(site, skill["goal"], url=START, page=Page(), store=store))
    assert again["status"] == "done" and again["skill"]["used"] == "replayed"
    assert again["decisions"] == 0 and again["usage"]["requests"] == 0 and again["usage"]["estimated_usd"] == 0
    assert site.acts == [("fill", "Reader number", "A-4417"), ("select", "Collect from → Airport kiosk", None),
                         ("click", "Send request", None)] and site.sent
    assert all(h["replayed"] for h in again["trace"]) and "Requested: A-4417 / airport" in again["markdown"]


def test_a_cookie_banner_that_does_not_show_again_is_skipped(monkeypatch, tmp_path):
    store = SkillStore(tmp_path)
    _, skill = learn(monkeypatch, store, Form(cookie=True), [("CLICK", "Reject all"), *PLAN])
    assert skill["steps"][0] == {**skill["steps"][0], "label": "Reject all", "optional": True}
    no_model(monkeypatch)
    site = Form(cookie=False)
    result = asyncio.run(replay(site, skill, Page()))
    assert result["status"] == "done" and ("click", "Reject all", None) not in site.acts and site.sent


def test_a_choice_already_made_is_not_made_again(monkeypatch, tmp_path):
    _, skill = learn(monkeypatch, SkillStore(tmp_path))
    no_model(monkeypatch)
    site = Form()
    site.branch = "airport"  # the page remembers the last choice
    result = asyncio.run(replay(site, skill, Page()))
    assert result["status"] == "done" and [a[0] for a in site.acts] == ["fill", "click"]


class DocsSite:
    """A home page whose way on is a link inside a closed menu: offered only when the menus are asked for."""

    closed = direct = False
    MENU_LINK = {"node": 7, "kind": "click", "role": "link", "label": "Docs › Recommended settings", "value": "",
                 "menu": 3}

    def __init__(self):
        self.path, self.acts, self.menu_looks = "/", [], 0

    async def new_tab(self, url=None):
        return Page(url or "https://docs.example/")

    async def observe(self, page, after=None, menus=False):
        self.menu_looks += menus
        page.url = "https://docs.example" + self.path
        home = self.path == "/"
        actions = [{"node": 1, "kind": "click", "role": "link", "label": "Home", "value": ""}]
        actions += [dict(self.MENU_LINK)] if menus and home else []
        actions += [{"id": "wait", "kind": "wait", "label": "Wait for the page to update"}]
        for i, a in enumerate(actions):
            a.setdefault("id", f"e{i + 1}")
        text = "Welcome" if home else "Recommended settings\nWhat to set and what to leave alone"
        return {"url": page.url, "title": "Docs", "text": text, "actions": actions, "marker": self.path}

    async def act(self, page, state, action, text=None):
        self.acts.append(action["label"])
        if action.get("menu") is not None:
            self.path = "/docs/recommendations"

    async def fresh(self, page, state, action=None):
        return True

    async def settled_markdown(self, page):
        return "# Recommended settings\n\nWhat to set and what to leave alone" if self.path != "/" else "# Welcome"

    async def lists(self, page):
        return []

    def capture_json(self, page):
        return Capture([])

    def is_gone(self, page):
        return False


def test_a_link_inside_a_menu_is_learned_and_replayed_with_no_model(monkeypatch, tmp_path):
    store, goal, url = SkillStore(tmp_path), "Find the recommended settings", "https://docs.example/"
    model(monkeypatch, ("BLOCKED", None), ("CLICK", DocsSite.MENU_LINK["label"]), ("DONE", None))
    first = asyncio.run(run_with_skill(DocsSite(), goal, url=url, page=Page(url), store=store))
    assert first["status"] == "done" and first["skill"]["used"] == "learned"
    skill = store.get(goal, url)
    assert skill["steps"] == [{"kind": "click", "role": "link", "label": "Docs › Recommended settings", "nth": 0,
                               "nth_shape": 0, "in_menu": True}]
    no_model(monkeypatch)
    site = DocsSite()
    again = asyncio.run(run_with_skill(site, goal, url=url, page=Page(url), store=store))
    assert again["status"] == "done" and again["skill"]["used"] == "replayed" and again["usage"]["requests"] == 0
    assert site.acts == ["Docs › Recommended settings"] and site.menu_looks >= 1


def test_target_info_marks_a_link_inside_a_menu():
    link = {"node": 7, "kind": "click", "role": "link", "label": "Docs › Install", "menu": 3}
    plain = {"node": 1, "kind": "click", "role": "link", "label": "Home"}
    assert target_info(link, [plain, link])["in_menu"] is True and "in_menu" not in target_info(plain, [plain, link])


def test_locate_finds_a_control_again_by_label_then_by_shape():
    actions = [{"node": i, "kind": "click", "role": "link", "label": label}
               for i, label in enumerate(["Story one", "143 comments", "Story two", "9 comments"], 1)]
    step = {"kind": "click", "role": "link", "label": "87 comments", "nth": 0, "nth_shape": 1}
    assert locate(step, actions)["label"] == "9 comments"  # the second story's comments, whatever their count
    assert locate({**step, "label": "Story two", "role": "button"}, actions)["node"] == 3  # the label alone
    assert locate({**step, "label": "Story three"}, actions) is None


def test_a_skill_that_no_longer_matches_is_repaired_by_the_loop(monkeypatch, tmp_path):
    store = SkillStore(tmp_path)
    _, skill = learn(monkeypatch, store)
    calls = model(monkeypatch, ("CLICK", "Submit request"), ("DONE", None))
    site = Form(send="Submit request")  # the site renamed its button
    result = asyncio.run(run_with_skill(site, skill["goal"], url=START, page=Page(), store=store))
    assert result["status"] == "done" and result["skill"]["used"] == "repaired"
    assert "Send request" in result["skill"]["because"] and calls["choose"] == 2  # only the changed step was decided
    assert [a[0] for a in site.acts] == ["fill", "select", "click"] and site.sent
    repaired = store.get(skill["goal"], START)
    assert [s["label"] for s in repaired["steps"]][-1] == "Submit request"

    no_model(monkeypatch)  # the rewritten skill replays on the renamed button
    site = Form(send="Submit request")
    assert asyncio.run(run_with_skill(site, skill["goal"], url=START, page=Page(), store=store))["skill"]["used"] \
        == "replayed" and site.sent


def test_a_replay_that_ends_somewhere_else_hands_over_to_the_loop(monkeypatch, tmp_path):
    store = SkillStore(tmp_path)
    _, skill = learn(monkeypatch, store)
    calls = model(monkeypatch, ("DONE", None))
    result = asyncio.run(replay(Form(broken_send=True), skill, Page()))  # every step found, nothing happened
    assert result["status"] == "diverged" and "different page" in result["detail"] and calls["choose"] == 0
    result = asyncio.run(run_with_skill(Form(broken_send=True), skill["goal"], url=START, page=Page(), store=store))
    assert result["skill"]["used"] == "repaired" and calls["choose"] == 1


def test_a_finished_run_carries_the_rows_of_its_list(monkeypatch):
    model(monkeypatch, ("DONE", None))
    result = asyncio.run(agent.run(Form(rows=ROWS), "List the kettles", page=Page()))
    assert result["items"] == ROWS and "learned" not in result
    result = asyncio.run(agent.run(Form(), "List the kettles", page=Page()))
    assert result["items"] == []


def learn_list(monkeypatch, store):
    return learn(monkeypatch, store, Form(rows=ROWS, body=BODY), [("DONE", None)], goal="List the kettles", url=SHOP)


def test_learning_keeps_the_list_and_the_request_behind_it(monkeypatch, tmp_path):
    _, skill = learn_list(monkeypatch, SkillStore(tmp_path))
    assert skill["list"]["item"] == "div.card"
    assert skill["shortcut"]["items_path"] == ["data", "results"] and skill["shortcut"]["url"].endswith("q=kettle")
    assert skill["shortcut"]["fields"]["link"] == ["url"] and skill["shortcut"]["base"] == SHOP


def test_the_shortcut_reads_the_rows_with_no_steps(monkeypatch, tmp_path):
    store = SkillStore(tmp_path)
    learn_list(monkeypatch, store)
    no_model(monkeypatch)
    site = Form(body=BODY)
    result = asyncio.run(run_with_skill(site, "List the kettles", url=SHOP, page=Page(SHOP), store=store))
    assert result["skill"]["used"] == "shortcut" and site.acts == [] and len(site.fetches) == 1
    assert [r["title"] for r in result["items"]] == ["Blue kettle", "Red kettle", "Steel kettle"]
    assert result["markdown"].startswith("- Blue kettle · 24.5 · https://shop.example/p/1")


def test_a_failing_shortcut_falls_back_to_the_list(monkeypatch, tmp_path):
    _, skill = learn_list(monkeypatch, SkillStore(tmp_path))
    no_model(monkeypatch)
    site = Form(rows=ROWS, body=None)  # the request no longer answers
    result = asyncio.run(replay(site, skill, Page(SHOP)))
    assert result["status"] == "done" and result["skill"]["used"] == "replayed" and result["items"] == ROWS


def test_a_list_on_another_page_does_not_count(monkeypatch, tmp_path):
    _, skill = learn_list(monkeypatch, SkillStore(tmp_path))
    no_model(monkeypatch)
    result = asyncio.run(replay(Form(rows=ROWS), skill, Page("https://shop.example/teapots")))  # rows, wrong page
    assert result["status"] == "diverged" and "different page" in result["detail"] and result["items"] == []


# --- confirm -------------------------------------------------------------------------------------------------------

def test_confirm_stops_before_an_irreversible_click(monkeypatch):
    model(monkeypatch, *PLAN)
    site = Form()
    result = asyncio.run(agent.run(site, "Request a loan", page=Page(), confirm=True))
    assert result["status"] == "needs_confirmation" and result["pending"]["label"] == "Send request"
    assert [a[0] for a in site.acts] == ["fill", "select"] and not site.sent  # everything up to it, not the click


def test_confirm_stops_a_replay_too(monkeypatch, tmp_path):
    _, skill = learn(monkeypatch, SkillStore(tmp_path))
    no_model(monkeypatch)
    site = Form()
    result = asyncio.run(replay(site, skill, Page(), confirm=True))
    assert result["status"] == "needs_confirmation" and not site.sent and len(site.acts) == 2


# --- find on page --------------------------------------------------------------------------------------------------

def test_find_jumps_to_words_taken_from_the_goal(monkeypatch):
    calls = model(monkeypatch, ("FIND_TEXT", None), ("DONE", None))
    site = Form(long=True)
    result = asyncio.run(agent.run(site, "Go to the 404 Not Found section", page=Page()))
    assert site.acts == [("find", "Jump to words from the goal on this long page", "404 Not Found")]
    assert result["status"] == "done" and calls["value"] == 1 and result["trace"][0]["text"] == "404 Not Found"


def test_find_without_words_in_the_goal_does_not_stop_the_run(monkeypatch):
    model(monkeypatch, ("FIND_TEXT", None), ("DONE", None), values={})
    site = Form(long=True)
    result = asyncio.run(agent.run(site, "Go somewhere", page=Page()))
    assert result["status"] == "done" and site.acts == [] and result["trace"][0]["page_changed"] is False


# --- through the MCP server ----------------------------------------------------------------------------------------

class Ctx:
    async def report_progress(self, *a, **k):
        pass


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("CLEARCOTE_JET_SKILLS", str(tmp_path))
    site = Form()
    b = mcp_server.Browser()
    b.session = site
    monkeypatch.setattr(mcp_server, "browser", b)
    return site


def test_browse_learns_a_task_then_replays_it(monkeypatch, server):
    model(monkeypatch, *PLAN)
    out = asyncio.run(mcp_server.browse("Request a loan", Ctx(), url=START))
    assert "skill: learned this task" in out and ", 6 model requests" in out  # 4 decisions, 1 value, 1 ranking
    no_model(monkeypatch)
    out = asyncio.run(mcp_server.browse("Request a loan", Ctx(), url=START))
    assert "skill: replayed the saved skill: no model requests" in out and ", 0 model requests" in out
    assert "(replayed)" in out
    listed = asyncio.run(mcp_server.list_skills())
    assert "'Request a loan' from https://library.example/loan: 3 steps" in listed
    assert asyncio.run(mcp_server.forget_skill("Request a loan", START)) == "forgot it"
    assert asyncio.run(mcp_server.list_skills()).startswith("no skills yet")


def test_browse_with_reuse_off_saves_nothing(monkeypatch, server):
    model(monkeypatch, *PLAN)
    out = asyncio.run(mcp_server.browse("Request a loan", Ctx(), url=START, reuse=False))
    assert "skill:" not in out and asyncio.run(mcp_server.list_skills()).startswith("no skills yet")


def test_browse_with_confirm_names_the_act_call_that_goes_ahead(monkeypatch, server):
    model(monkeypatch, *PLAN)
    out = asyncio.run(mcp_server.browse("Request a loan", Ctx(), url=START, confirm=True, reuse=False))
    assert out.startswith("status: needs_confirmation") and not server.sent
    target = re.search(r"to go ahead: act\(tab_id='t1', op='CLICK', target='(\d+)'\)", out).group(1)
    done = asyncio.run(mcp_server.act("t1", op="CLICK", target=target))
    assert "status: done" in done and server.sent


def test_browse_lists_the_rows_it_read(monkeypatch, server):
    server.rows = ROWS
    model(monkeypatch, ("DONE", None))
    out = asyncio.run(mcp_server.browse("List the kettles", Ctx(), url=START, reuse=False))
    assert "results on the page (3, read without a model):" in out
    assert "- Blue kettle · €24,50 · https://shop.example/p/1" in out
    assert out.index("results on the page") > out.index("<untrusted_page_content>")


# --- what the page said, inside the fence; Jet's own words, as they are --------------------------------------------

def test_a_stop_before_a_click_fences_only_its_label(monkeypatch, server):
    model(monkeypatch, *PLAN)
    out = asyncio.run(mcp_server.browse("Request a loan", Ctx(), url=START, confirm=True, reuse=False))
    assert out.startswith("status: needs_confirmation (stopped before <untrusted_page_content>Send request"
                          "</untrusted_page_content>, which can't be taken back: confirm to go ahead)\n")


def test_a_repaired_skill_says_why_with_only_the_old_label_fenced(monkeypatch, tmp_path):
    store = SkillStore(tmp_path)
    _, skill = learn(monkeypatch, store)
    model(monkeypatch, ("CLICK", "Submit request"), ("DONE", None))
    result = asyncio.run(run_with_skill(Form(send="Submit request"), skill["goal"], url=START, page=Page(), store=store,
                                        said=True))  # as the MCP server asks for it
    assert result["skill"]["because"] == "step 3 (click 'Send request') is not on the page"  # as a str, as before
    line = next(x for x in mcp_server._format("t1", result).splitlines() if x.startswith("skill: "))
    assert line.startswith("skill: the saved skill no longer matched (step 3 (click <untrusted_page_content>Send "
                           "request</untrusted_page_content>) is not on the page); "), line


def test_the_library_hands_out_plain_strings(monkeypatch, tmp_path):
    """A run's messages are plain str, exactly as before; only the MCP server asks for their parts."""
    store = SkillStore(tmp_path)
    _, skill = learn(monkeypatch, store)
    model(monkeypatch, ("CLICK", "Submit request"), ("DONE", None))
    result = asyncio.run(run_with_skill(Form(send="Submit request"), skill["goal"], url=START, page=Page(), store=store))
    because = result["skill"]["because"]
    assert type(because) is str and because == "step 3 (click 'Send request') is not on the page"
    model(monkeypatch, *PLAN)
    result = asyncio.run(agent.run(Form(), "Request a loan", page=Page(), confirm=True))
    assert type(result["detail"]) is str
    assert result["detail"] == "stopped before 'Send request', which can't be taken back: confirm to go ahead"
    assert all(type(s) is str for s in result["stale"])


class Progress:
    def __init__(self):
        self.messages = []

    async def report_progress(self, done, total=None, message=None):
        self.messages.append(message)


def test_progress_names_each_element_inside_the_fence(monkeypatch, server):
    model(monkeypatch, *PLAN)
    ctx = Progress()
    asyncio.run(mcp_server.browse("Request a loan", ctx, url=START, reuse=False))
    assert "fill <untrusted_page_content>Reader number</untrusted_page_content> = 'A-4417'" in ctx.messages, ctx.messages
    assert "click <untrusted_page_content>Send request</untrusted_page_content>" in ctx.messages, ctx.messages
