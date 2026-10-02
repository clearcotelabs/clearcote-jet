"""The shared page view and index resolution, step()'s argument rules, and the raw-CDP screenshot."""

import asyncio
import base64
import json

import pytest

from clearcote_jet import agent, model
from clearcote_jet.browser import Session

ACTIONS = [
    {"id": "e1", "node": 1, "kind": "fill", "role": "searchbox", "label": "Search the catalogue", "value": ""},
    {"id": "e2", "node": 2, "kind": "click", "role": "link", "label": "Opening hours"},
    {"id": "e3", "node": 3, "kind": "select", "role": "combobox", "label": "Order by → Title", "value": "title",
     "current_value": "Relevance"},
    {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560},
]
PAGE = {"url": "https://library.example/", "title": "Catalogue", "text": "Catalogue", "actions": ACTIONS}


def test_choose_sends_exactly_the_page_view(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    history = [{"action": "Opening hours", "kind": "click", "text": None, "page_changed": True,
                "url": "https://library.example/", "led_to": "https://library.example/hours"}]

    async def post(url, key, body, headers=None):
        post.body = body
        return {"answers": {"operation": {"choice": "DONE", "confidence": 1.0, "probabilities": {
            k: float(k == "DONE") for k in body["questions"]["operation"]["criteria"]}}}}
    monkeypatch.setattr(model, "post_json", post)
    asyncio.run(model.choose(PAGE, "goal", history))
    view, _, _ = model.page_view(PAGE, history)
    assert json.dumps(post.body["state"]) == json.dumps(view)  # snapshot shows what the model is sent


def test_resolve_maps_indices_to_the_observed_actions():
    _, targets, controls = model.page_view(PAGE, [])
    assert model.resolve(targets, controls, "TYPE_TEXT", "1") is ACTIONS[0]
    assert model.resolve(targets, controls, "CLICK", "2") is ACTIONS[1]
    assert model.resolve(targets, controls, "SELECT", "3:1") is ACTIONS[2]
    assert model.resolve(targets, controls, "SCROLL_DOWN") is ACTIONS[3]


@pytest.mark.parametrize("operation, target, message", [
    ("CLICK", "1", "CLICK needs a target, one of: 2"),          # a field's index is not a click target here
    ("CLICK", None, "CLICK needs a target, one of: 2"),
    ("SELECT", "3", "SELECT needs a target, one of: 3:1"),      # an option, not the dropdown
    ("TYPE_TEXT", "7", "TYPE_TEXT needs a target, one of: 1"),
    ("SCROLL_UP", None, "operation 'SCROLL_UP' is not offered here; offered: TYPE_TEXT, CLICK, SELECT, SCROLL_DOWN"),
])
def test_resolve_errors_name_the_valid_choices(operation, target, message):
    _, targets, controls = model.page_view(PAGE, [])
    with pytest.raises(ValueError) as e:
        model.resolve(targets, controls, operation, target)
    assert str(e.value) == message


def test_resolve_sorts_indices_as_numbers():
    actions = [{"id": f"e{i}", "node": i, "kind": "click", "role": "link", "label": f"L{i}"} for i in range(1, 12)]
    _, targets, controls = model.page_view({**PAGE, "actions": actions}, [])
    with pytest.raises(ValueError) as e:
        model.resolve(targets, controls, "CLICK", "99")
    assert str(e.value).endswith("1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11")


@pytest.mark.parametrize("kwargs", [{}, {"op": "CLICK", "target": "2", "instruction": "open hours"}])
def test_step_needs_op_or_instruction_but_not_both(kwargs):
    with pytest.raises(ValueError, match="give either op"):
        asyncio.run(agent.step(None, None, state=PAGE, **kwargs))


def test_screenshot_goes_through_raw_cdp_and_detaches():
    png = b"\x89PNG\r\n\x1a\nimage"
    sent = []

    class CDP:
        async def send(self, method, params=None):
            sent.append((method, params))
            return {"data": base64.b64encode(png).decode()}

        async def detach(self):
            sent.append(("detach", None))

    class Context:
        async def new_cdp_session(self, page):
            return CDP()

        def on(self, *_):
            pass

    class Page:
        context = Context()

        def is_closed(self):
            return False

        async def screenshot(self, **_):
            raise AssertionError("page.screenshot() changes the page's DOM")

    assert asyncio.run(Session(Context()).screenshot(Page())) == png
    assert sent == [("Page.captureScreenshot", {"format": "png"}), ("detach", None)]
