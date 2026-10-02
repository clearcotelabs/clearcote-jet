"""The agent loop with a scripted session and model (no browser, no model calls): the request-saving rules."""

import asyncio

from clearcote_jet import agent, questions
from clearcote_jet.browser import StalePage

SCROLL = {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560}
FIELD = {"id": "e1", "node": 1, "kind": "fill", "role": "textbox", "label": "Reader number", "value": ""}
MARKDOWN = "\n\n".join(f"## Section {i}\n\nBody text of section number {i} goes here." for i in range(20))


class Page:
    url = "https://catalogue.example/item"

    async def title(self):
        return "Item"


class Session:
    """observe() returns a new page state each time (text and marker change), like a page that keeps moving."""

    direct = False
    closed = False

    def __init__(self, fresh=True, stale_acts=0, screen="Body text of section number 2 goes here."):
        self.fresh_result, self.stale_acts, self.screen = fresh, stale_acts, screen
        self.observes, self.acts = 0, []

    async def observe(self, page, after=None):
        self.observes += 1
        return {"url": Page.url, "title": "Item", "text": f"{self.screen}\nclock {self.observes}",
                "actions": [FIELD, SCROLL], "marker": str(self.observes)}

    async def fresh(self, page, state, action=None):
        return self.fresh_result

    async def act(self, page, state, action, text=None):
        if self.stale_acts:
            self.stale_acts -= 1
            raise StalePage("Page changed since this decision. Observe again.")
        self.acts.append((action["kind"], text))

    async def settled_markdown(self, page):
        return MARKDOWN

    async def lists(self, page):
        return []

    def is_gone(self, page):
        return False


def decision(operation, action=None):
    return {"action": action or {"id": operation, "kind": operation.lower(), "label": operation},
            "operation": operation, "target": None, "probability": 1.0, "confidence": 1.0, "alternatives": [],
            "usage": {"input_tokens": 1000}, "latency_ms": 1}


def script(monkeypatch, *steps):
    """The model's answers in order; the last one repeats. Returns the call counters."""
    calls = {"choose": 0, "rank": [], "value": 0}

    async def choose(state, goal, history):
        calls["choose"] += 1
        return steps[min(calls["choose"], len(steps)) - 1]

    async def rank_blocks(goal, blocks):
        calls["rank"].append(blocks)
        return [0.9] + [0.1] * (len(blocks) - 1), {"input_tokens": 500}

    async def field_text(context):
        calls["value"] += 1
        return "A-4417", {"latency_ms": 1, "usage": {"input_tokens": 100}, "billed": True}

    monkeypatch.setattr(agent, "choose", choose)
    monkeypatch.setattr(agent, "rank_blocks", rank_blocks)
    monkeypatch.setattr(agent, "field_text", field_text)
    return calls


def test_the_same_verdict_twice_on_a_changing_page_ends_the_run(monkeypatch):
    calls = script(monkeypatch, decision("DONE"))
    result = asyncio.run(agent.run(Session(fresh=False), "goal", page=Page()))
    assert result["status"] == "done" and result["decisions"] == 2 and calls["choose"] == 2
    assert len(result["stale"]) == 1  # the first DONE was checked against a changed page, the second stands


def test_a_scroll_streak_ends_the_run(monkeypatch):
    calls = script(monkeypatch, decision("SCROLL_DOWN", SCROLL))
    result = asyncio.run(agent.run(Session(), "goal", page=Page()))
    assert result["status"] == "blocked" and "Scrolled" in result["detail"]
    assert result["actions"] == questions.MAX_SCROLL_STREAK == calls["choose"]


def test_a_value_is_reused_when_the_step_is_decided_again(monkeypatch):
    calls = script(monkeypatch, decision("TYPE_TEXT", FIELD), decision("TYPE_TEXT", FIELD), decision("DONE"))
    session = Session(stale_acts=1)  # the page changes between the first decision and the typing
    result = asyncio.run(agent.run(session, "Reader A-4417", page=Page()))
    assert result["status"] == "done" and session.acts == [("fill", "A-4417")]
    assert calls["value"] == 1  # the second TYPE_TEXT on the same field reuses the value


def test_ranking_covers_the_final_screen_and_is_skipped_when_not_done(monkeypatch):
    calls = script(monkeypatch, decision("DONE"))
    result = asyncio.run(agent.run(Session(screen="Body text of section number 15 goes here."), "goal", page=Page()))
    assert len(calls["rank"]) == 1 and calls["rank"][0][0].startswith("## Section 13")  # 2 blocks above the screen
    assert len(calls["rank"][0]) == 7 and result["markdown"].startswith("## Section 13")  # 13..19: to the page end
    assert result["usage"]["requests"] == 2  # one decision, one ranking

    calls = script(monkeypatch, decision("BLOCKED"))
    result = asyncio.run(agent.run(Session(), "goal", page=Page()))
    assert result["status"] == "blocked" and calls["rank"] == [] and result["block_scores"] == []
    assert result["markdown"].startswith("## Section 0") and result["usage"]["requests"] == 1
