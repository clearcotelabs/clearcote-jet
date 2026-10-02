"""The MCP tools with a scripted session (no browser, no model calls): snapshot, act, busy tabs, errors."""

import asyncio

import pytest
from mcp.server.mcpserver import Image

from clearcote_jet import agent, mcp_server
from clearcote_jet.browser import StalePage

PNG = b"\x89PNG\r\n\x1a\n-fake"
ACTIONS = [
    {"id": "e1", "node": 1, "kind": "fill", "role": "textbox", "label": "Reader number", "value": ""},
    {"id": "e2", "node": 1, "kind": "click", "role": "textbox", "label": "Open Reader number", "value": ""},
    {"id": "e3", "node": 2, "kind": "click", "role": "button", "label": "Send request", "value": ""},
    {"id": "e4", "node": 3, "kind": "select", "role": "combobox", "label": "Collect from → Harbour branch",
     "value": "harbour", "current_value": "Central library"},
    {"id": "e5", "node": 3, "kind": "select", "role": "combobox", "label": "Collect from → Airport kiosk",
     "value": "airport", "current_value": "Central library"},
    {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560},
    {"id": "wait", "kind": "wait", "label": "Wait for the page to update"},
]


class Page:
    def __init__(self, url):
        self.url, self.closed = url, False

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True

    async def goto(self, url, wait_until=None):
        self.url = url


class Session:
    """observe() returns the loan form; every observation is a new page state (marker)."""

    closed = False

    def __init__(self, stale=False, observe_error=None):
        self.stale, self.observe_error = stale, observe_error
        self.observes, self.acts, self.shots = 0, [], 0

    async def new_tab(self, url):
        return Page(url)

    async def observe(self, page, after=None):
        if self.observe_error:
            raise self.observe_error
        self.observes += 1
        return {"url": page.url, "title": "Loan request", "text": "Loan request\nReader number",
                "actions": ACTIONS, "marker": str(self.observes), "scroll": {"y": 0, "height": 900, "vh": 800}}

    async def act(self, page, state, action, text=None):
        if self.stale:
            raise StalePage("Page changed since this decision. Observe again.")
        self.acts.append((action["kind"], action.get("node"), action.get("value"), text))

    async def screenshot(self, page):
        self.shots += 1
        return PNG

    def is_gone(self, page):
        return page.is_closed()


@pytest.fixture
def session(monkeypatch):
    """A fresh shared browser holding the scripted session; the model must not be called unless a test says so."""
    s = Session()
    b = mcp_server.Browser()
    b.session = s
    monkeypatch.setattr(mcp_server, "browser", b)

    async def no_model(*_):
        raise AssertionError("the model was called")
    monkeypatch.setattr(agent, "choose", no_model)
    monkeypatch.setattr(agent, "field_text", no_model)
    return s


def opened(url="https://library.example/loan"):
    """A snapshot that opens a new tab: its reply."""
    return asyncio.run(mcp_server.snapshot(url=url))


def run(*calls):
    """Run tool calls in order on one event loop (tab locks belong to one loop); returns their replies."""
    async def go():
        return [await c() for c in calls]
    return asyncio.run(go())


def decision(operation, action, p=0.9, alternatives=()):
    return {"action": action, "operation": operation, "target": "2" if operation == "CLICK" else None,
            "probability": p, "confidence": 1.0, "alternatives": list(alternatives),
            "usage": {"input_tokens": 1000}, "latency_ms": 1}


# --- snapshot ------------------------------------------------------------------------------------------------------

def test_snapshot_opens_a_tab_and_shows_what_the_agent_sees(session):
    out = opened()
    assert "tab_id: t1 (still open)" in out and "url: https://library.example/loan" in out
    assert "operations: TYPE_TEXT, CLICK, SELECT, SCROLL_DOWN, WAIT" in out
    assert '[1] textbox "Reader number" ops=TYPE_TEXT,CLICK' in out
    assert '[3] combobox "Collect from" value="Central library" ops=SELECT options: 3:1 Harbour branch; 3:2 Airport kiosk' in out
    # everything from the page sits inside the untrusted block
    assert out.index("<untrusted_page_content>") < out.index("[1] textbox") < out.index("</untrusted_page_content>")
    assert mcp_server.browser.tabs["t1"].view is not None


def test_snapshot_of_an_existing_tab_and_with_a_screenshot(session):
    opened()
    out = asyncio.run(mcp_server.snapshot(tab_id="t1", screenshot=True))
    assert isinstance(out, list) and isinstance(out[1], Image) and out[1].data == PNG
    assert "tab_id: t1" in out[0] and session.shots == 1


def test_snapshot_needs_a_tab_or_a_url(session):
    assert asyncio.run(mcp_server.snapshot()) == "status: error (give a start url, or a tab_id to continue)"


# --- act by index: no model ----------------------------------------------------------------------------------------

def test_act_clicks_by_index_without_the_model(session):
    opened()
    out = asyncio.run(mcp_server.act("t1", op="click", target=2))  # lower-case op and an int target are fine
    assert session.acts == [("click", 2, "", None)]
    assert "status: done" in out and "did: CLICK [2] 'Send request'" in out and "page_changed: True" in out
    assert "model:" not in out  # no model request was made
    assert '[2] button "Send request"' in out  # the new snapshot, for chaining


def test_act_types_text_exactly_as_given(session):
    opened()
    out = asyncio.run(mcp_server.act("t1", op="TYPE_TEXT", target="1", text="A-4417 "))
    assert session.acts == [("fill", 1, "", "A-4417 ")] and "= 'A-4417 '" in out


def test_act_selects_an_option_by_index_colon_option(session):
    opened()
    asyncio.run(mcp_server.act("t1", op="SELECT", target="3:2"))
    assert session.acts == [("select", 3, "airport", None)]


def test_act_scrolls_and_waits_without_a_target(session):
    opened()
    run(lambda: mcp_server.act("t1", op="SCROLL_DOWN"), lambda: mcp_server.act("t1", op="WAIT"))
    assert [a[0] for a in session.acts] == ["scroll", "wait"]


def test_act_chains_on_its_own_new_snapshot(session):
    opened()
    run(lambda: mcp_server.act("t1", op="TYPE_TEXT", target="1", text="A-4417"),
        lambda: mcp_server.act("t1", op="SELECT", target="3:2"),
        lambda: mcp_server.act("t1", op="CLICK", target="2"))
    assert [a[0] for a in session.acts] == ["fill", "select", "click"]


# --- act: errors that say how to fix the call ----------------------------------------------------------------------

def test_act_with_a_wrong_target_names_the_valid_ones(session):
    opened()
    out = asyncio.run(mcp_server.act("t1", op="CLICK", target="9"))
    assert out == "status: error (CLICK needs a target, one of: 1, 2)\ntab_id: t1"
    out = asyncio.run(mcp_server.act("t1", op="SELECT", target="3"))
    assert "SELECT needs a target, one of: 3:1, 3:2" in out
    assert session.acts == []


def test_act_with_an_operation_not_on_offer_lists_the_offered_ones(session):
    opened()
    out = asyncio.run(mcp_server.act("t1", op="HOVER", target="2"))
    assert "operation 'HOVER' is not offered here; offered: TYPE_TEXT, CLICK, SELECT, SCROLL_DOWN, WAIT" in out
    assert session.acts == []


def test_act_type_text_without_text_is_refused(session):
    opened()
    out = asyncio.run(mcp_server.act("t1", op="TYPE_TEXT", target="1"))
    assert "TYPE_TEXT needs text" in out and session.acts == []


def test_act_needs_op_or_instruction_but_not_both(session):
    opened()
    for kwargs in ({}, {"op": "CLICK", "target": "2", "instruction": "click send"}):
        out = asyncio.run(mcp_server.act("t1", **kwargs))
        assert "give either op (with target) or instruction" in out
    assert session.acts == []


def test_act_needs_a_snapshot_first(session):
    mcp_server.browser.tabs["t5"] = mcp_server.Tab(Page("https://library.example/"))
    out = asyncio.run(mcp_server.act("t5", op="CLICK", target="2"))
    assert "no snapshot of this tab yet: call snapshot first" in out and session.acts == []


def test_act_on_a_changed_page_is_stale_and_does_nothing(session):
    opened()
    session.stale = True
    out = asyncio.run(mcp_server.act("t1", op="CLICK", target="2"))
    assert out.startswith("status: stale (nothing was done: take a new snapshot)") and session.acts == []
    assert mcp_server.browser.tabs["t1"].view is None  # the old indices are no longer offered
    assert "call snapshot first" in asyncio.run(mcp_server.act("t1", op="CLICK", target="2"))


def test_a_gone_tab_lists_the_open_ones(session):
    opened()
    out = asyncio.run(mcp_server.act("t7", op="CLICK", target="2"))
    assert out == "status: error (tab 't7' is gone)\nopen tabs: t1"
    mcp_server.browser.tabs["t1"].page.closed = True  # closed by the user
    assert asyncio.run(mcp_server.snapshot(tab_id="t1")) == "status: error (tab 't1' is gone)\nopen tabs: none"


def test_a_failure_in_the_browser_comes_back_as_an_error_reply(session):
    opened()
    session.observe_error = RuntimeError("renderer hung")
    out = asyncio.run(mcp_server.snapshot(tab_id="t1"))
    assert out == "status: error (RuntimeError: renderer hung)\ntab_id: t1\nurl: https://library.example/loan"
    assert "t1" in mcp_server.browser.tabs  # the tab stays open for a retry


# --- one call per tab ----------------------------------------------------------------------------------------------

def test_a_second_call_on_a_busy_tab_answers_busy(session):
    opened()

    async def go():
        async with mcp_server.browser.tabs["t1"].lock:  # another call holds the tab
            return await mcp_server.act("t1", op="CLICK", target="2")
    assert asyncio.run(go()) == ("status: error (tab 't1' is busy with another call)\n"
                                 "try again when that call has returned")
    assert session.acts == []


def test_browse_and_act_on_one_tab_do_not_overlap(session, monkeypatch):
    opened()
    started = asyncio.Event()

    async def slow_run(session_, goal, page=None, on_step=None):
        started.set()
        await asyncio.sleep(0.2)
        return {"status": "done", "detail": None, "url": page.url, "title": "T", "actions": 0, "decisions": 1,
                "elapsed_ms": 200, "trace": [], "stale": [], "markdown": "md"}
    monkeypatch.setattr(mcp_server, "run", slow_run)

    class Ctx:
        async def report_progress(self, *a, **k):
            pass

    async def go():
        task = asyncio.create_task(mcp_server.browse("goal", Ctx(), tab_id="t1"))
        await started.wait()
        during = await mcp_server.act("t1", op="CLICK", target="2")  # browse is still running in t1
        done = await task
        after = await mcp_server.act("t1", op="CLICK", target="2")  # browse moved the page on: snapshot again
        return during, done, after
    during, done, after = asyncio.run(go())
    assert "is busy with another call" in during and "status: done" in done
    assert "call snapshot first" in after and session.acts == []


def test_other_tabs_stay_usable_while_one_is_busy(session):
    opened()
    opened("https://library.example/other")

    async def go():
        async with mcp_server.browser.tabs["t1"].lock:
            return await mcp_server.act("t2", op="CLICK", target="2")
    assert "status: done" in asyncio.run(go())


# --- act by instruction: one decision ------------------------------------------------------------------------------

def test_act_by_instruction_takes_one_decision(session, monkeypatch):
    opened()
    calls = []

    async def choose(state, goal, history):
        calls.append(goal)
        return decision("CLICK", ACTIONS[2], 0.9, [("Open Reader number", 0.08)])
    monkeypatch.setattr(agent, "choose", choose)
    out = asyncio.run(mcp_server.act("t1", instruction="press the send button"))
    assert calls == ["press the send button"] and session.acts == [("click", 2, "", None)]
    assert "did: CLICK [2] 'Send request'  p=0.9 ['Open Reader number' p=0.08]" in out
    assert "model: 1 requests, 1000 input tokens" in out


def test_act_by_instruction_types_the_value_from_its_words(session, monkeypatch):
    opened()

    async def choose(state, goal, history):
        return decision("TYPE_TEXT", ACTIONS[0])

    async def field_text(context):
        assert context["goal"] == "type A-4417 as the reader number"
        return "A-4417", {"latency_ms": 1, "usage": {"input_tokens": 300}, "billed": True}
    monkeypatch.setattr(agent, "choose", choose)
    monkeypatch.setattr(agent, "field_text", field_text)
    out = asyncio.run(mcp_server.act("t1", instruction="type A-4417 as the reader number"))
    assert session.acts == [("fill", 1, "", "A-4417")] and "model: 2 requests, 1300 input tokens" in out


def test_act_by_instruction_that_is_already_done_does_nothing(session, monkeypatch):
    opened()

    async def choose(state, goal, history):
        return decision("DONE", {"id": "DONE", "kind": "done", "label": "DONE"}, 1.0)
    monkeypatch.setattr(agent, "choose", choose)
    out = asyncio.run(mcp_server.act("t1", instruction="close the cookie banner"))
    assert out.startswith("status: not_done (the agent judged this DONE: nothing was done)") and session.acts == []


# --- close_tab -----------------------------------------------------------------------------------------------------

def test_close_tab(session):
    opened()
    page = mcp_server.browser.tabs["t1"].page
    assert asyncio.run(mcp_server.close_tab("t1")) == "closed t1" and page.closed
    assert asyncio.run(mcp_server.close_tab("t1")) == "unknown tab_id 't1'; open tabs: none"


def test_the_server_offers_the_four_tools():
    names = {t.name for t in asyncio.run(mcp_server.server.list_tools())}
    assert {"browse", "snapshot", "act", "close_tab"} <= names
