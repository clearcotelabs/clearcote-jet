import asyncio
import json
import time

import pytest

from clearcote_jet import model

ACTIONS = [
    {"id": "e1", "node": 1, "kind": "fill", "role": "searchbox", "label": "Search the catalogue", "value": ""},
    {"id": "e2", "node": 1, "kind": "click", "role": "searchbox", "label": "Open Search the catalogue", "value": ""},
    {"id": "e3", "node": 2, "kind": "click", "role": "checkbox", "label": "Available now", "checked": "false"},
    {"id": "e4", "node": 3, "kind": "select", "role": "combobox", "label": "Order by → Publication year",
     "value": "year", "current_value": "Shelf order"},
    {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560},
    {"id": "wait", "kind": "wait", "label": "Wait for the page to update"},
]


def test_action_space_one_index_per_node_and_per_operation_targets():
    elements, targets, controls = model.action_space(ACTIONS)
    assert [e["index"] for e in elements] == ["1", "2", "3"]
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert set(targets) == {"TYPE_TEXT", "CLICK", "SELECT"}
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["SELECT"]["3:1"]["value"] == "year"
    assert set(controls) == {"SCROLL_DOWN", "WAIT"}


def test_validate_choice_rejects_off_menu_and_bad_distributions():
    ok = {"choice": "a", "confidence": 0.9, "probabilities": {"a": 0.9, "b": 0.1}}
    assert model.validate_choice(ok, {"a": 1, "b": 1}) is ok
    for bad in (
        {**ok, "choice": "c"},
        {**ok, "probabilities": {"a": 0.9}},
        {**ok, "probabilities": {"a": 0.5, "b": 0.2}},
        {**ok, "choice": "b"},  # not the argmax
        {},
    ):
        with pytest.raises(ValueError):
            model.validate_choice(bad, {"a": 1, "b": 1})


def _fake_post(content):
    async def post(url, key, body, headers=None):
        post.body, post.headers = body, headers
        return {"choices": [{"message": {"content": content}}]}
    return post


CTX = {"goal": "Look up the opening hours", "field": {"label": "Search the catalogue"}, "page": {},
       "recent_actions": []}


def _text_model(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "k")
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://llm.example/v1/")
    monkeypatch.setenv("TEXT_MODEL", "small-model")


def test_field_text_generic_endpoint_headers_and_reasoning(monkeypatch):
    _text_model(monkeypatch)
    monkeypatch.setenv("TEXT_MODEL_REASONING", "high")
    monkeypatch.setenv("TEXT_MODEL_HEADERS", json.dumps({"x-session": "s"}))
    post = _fake_post('{"text": "opening hours"}')
    monkeypatch.setattr(model, "post_json", post)
    value, info = asyncio.run(model.field_text(CTX))
    assert value == "opening hours" and info["model"] == "small-model"
    assert post.body["reasoning_effort"] == "high" and post.headers == {"x-session": "s"}


def test_text_model_key_without_endpoint_or_model_is_a_clear_error(monkeypatch):
    for missing in ("TEXT_MODEL_BASE_URL", "TEXT_MODEL"):
        _text_model(monkeypatch)
        monkeypatch.delenv(missing)
        monkeypatch.setattr(model, "post_json", _fake_post('{"text": "hours"}'))
        with pytest.raises(ValueError, match="TEXT_MODEL_BASE_URL and TEXT_MODEL"):
            asyncio.run(model.field_text(CTX))


def test_field_text_null_means_needs_input_and_junk_is_rejected(monkeypatch):
    _text_model(monkeypatch)
    monkeypatch.setattr(model, "post_json", _fake_post('{"text": null}'))
    with pytest.raises(model.NeedsInput):
        asyncio.run(model.field_text(CTX))
    for junk in ('{"text": "a", "extra": 1}', "not json", '{"text": 5}'):
        monkeypatch.setattr(model, "post_json", _fake_post(junk))
        with pytest.raises(ValueError):
            asyncio.run(model.field_text(CTX))


def test_choose_sends_step_urls_in_recent_actions(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")

    async def post(url, key, body, headers=None):
        post.body = body
        return {"model": "m", "answers": {"operation": {
            "choice": "WAIT", "confidence": 1.0, "probabilities": {"WAIT": 1.0, "DONE": 0.0, "BLOCKED": 0.0}}}}
    monkeypatch.setattr(model, "post_json", post)
    page = {"url": "https://catalogue.example/results", "title": "T", "text": "", "actions": [
        {"id": "wait", "kind": "wait", "label": "Wait for the page to update"}]}
    history = [{"action": "Next page", "kind": "click", "text": None, "page_changed": True,
                "url": "https://catalogue.example/results", "led_to": "https://catalogue.example/results?page=2"}]
    asyncio.run(model.choose(page, "goal", history))
    sent = post.body["state"]["recent_actions"][0]
    assert sent["url"] == "https://catalogue.example/results"
    assert sent["led_to"] == "https://catalogue.example/results?page=2"


def test_choose_request_is_compact(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")

    async def post(url, key, body, headers=None):
        post.body = body
        return {"model": "m", "answers": {
            "operation": {"choice": "CLICK", "confidence": 1.0, "probabilities": {
                k: float(k == "CLICK") for k in body["questions"]["operation"]["criteria"]}},
            "click_target": {"choice": "2", "confidence": 1.0, "probabilities": {
                k: float(k == "2") for k in body["questions"]["click_target"]["criteria"]}},
            "type_text_target": {"choice": "1", "confidence": 1.0, "probabilities": {"1": 1.0}},
            "select_target": {"choice": "3:1", "confidence": 1.0, "probabilities": {"3:1": 1.0}}}}
    monkeypatch.setattr(model, "post_json", post)
    page = {"url": "u", "title": "t", "text": "Search the catalogue\nAvailable now\n12 copies in the system",
            "actions": ACTIONS}
    asyncio.run(model.choose(page, "goal", []))
    body = json.dumps(post.body)
    assert body.count(json.dumps(model.NEXT_ACTION)) == 1  # full rules sent once, in the operation question
    # target questions get their own short element-choice rules (without them: Accept instead of Reject)
    assert "reject/decline" in post.body["questions"]["click_target"]["instructions"]["rules"]
    # target criteria are bare indices: each label is sent once, in `elements`
    assert post.body["questions"]["click_target"]["criteria"] == {"1": "[1]", "2": "[2]"}
    assert post.body["questions"]["select_target"]["criteria"] == {"3:1": "[3:1]"}
    # the page text goes whole, label lines included: they give the model the page's layout
    assert post.body["state"]["page"]["text"] == page["text"]
    # elements as one line each, explained by the legend
    assert post.body["state"]["element_format"] == model.ELEMENT_FORMAT
    assert post.body["state"]["elements"] == [
        '[1] searchbox "Search the catalogue" ops=TYPE_TEXT,CLICK',
        '[2] checkbox "Available now" checked=false',
        '[3] combobox "Order by" value="Shelf order" ops=SELECT options: 3:1 Publication year',
    ]


def test_settled_markdown_waits_for_late_content_and_caps():
    from clearcote_jet.browser import Session

    class FakeWorld:
        def __init__(self, frames):
            self.frames = iter(frames)

        async def evaluate(self, _):
            return next(self.frames, self.last)  # after the scripted frames, the page stays as the last one

    class FakePage:
        def __init__(self, frames):
            self._ca_world = FakeWorld(frames)
            self._ca_world.last = frames[-1]

        def is_closed(self):
            return False

    class FakeContext:
        def on(self, *_):
            pass

    s = Session(FakeContext())
    # results arrive after two reads, then stay: we must return the final content, not the early one
    page = FakePage(["Loading…", "Loading…", "3 copies available"])
    out = asyncio.run(s.settled_markdown(page, quiet=0.2, cap=2.0, every=0.05))
    assert out == "3 copies available"
    # a page that never stops changing returns at the cap
    endless = FakePage([str(i) for i in range(1000)])
    t = time.perf_counter()
    asyncio.run(s.settled_markdown(endless, quiet=0.5, cap=0.3, every=0.05))
    assert time.perf_counter() - t < 1.0


def test_scroll_gauge():
    assert model.scroll_gauge({"y": 0, "height": 800, "vh": 870}) == "all"
    assert model.scroll_gauge({"y": 0, "height": 2000, "vh": 800}) == "0-40% (more below)"
    assert model.scroll_gauge({"y": 1200, "height": 2000, "vh": 800}) == "60-100% (end)"


def test_launch_uses_public_sdk_options_without_a_seed(monkeypatch, tmp_path):
    from clearcote_jet import browser

    class FakeContext:
        def on(self, *_):
            pass

    async def fake_launch(user_data_dir, **kw):
        fake_launch.call = (user_data_dir, kw)
        return FakeContext()

    monkeypatch.setattr(browser, "launch_persistent_context", fake_launch)
    session = asyncio.run(browser.Session.launch(tmp_path, headless=False, cdp_port=9333, proxy="http://u:p@h:1"))
    user_data_dir, kw = fake_launch.call
    assert user_data_dir == str(tmp_path) and session.humanize is True and session.direct is False
    assert kw["humanize"] is True and kw["geoip"] is True and kw["headless"] is False
    assert kw["proxy"] == "http://u:p@h:1" and kw["args"] == ["--remote-debugging-port=9333"]
    assert not any(a.startswith("--fingerprint") for a in kw["args"])  # default persona: stable, no seed switch
    assert asyncio.run(browser.Session.launch(tmp_path)).direct is True  # no proxy: host IP is the browser IP
    assert "proxy" not in fake_launch.call[1] and fake_launch.call[1]["args"] == []


def test_aim_point_stays_inside_the_box_and_off_dead_center():
    from clearcote_jet.browser import aim_point
    box = {"x": 100, "y": 50, "width": 120, "height": 30, "input": False}
    points = {aim_point(box) for _ in range(200)}
    assert all(102 <= x <= 218 and 52 <= y <= 78 for x, y in points)
    assert len(points) > 150  # not one fixed spot
    field = {**box, "width": 400, "input": True}
    assert all(x < 100 + 400 * 0.5 for x, _ in (aim_point(field) for _ in range(100)))  # left part of a text field


def test_mcp_format_shows_probabilities_alternatives_and_stale():
    from clearcote_jet.mcp_server import _format
    result = {
        "status": "done", "detail": None, "url": "https://x", "title": "T", "actions": 1, "decisions": 2,
        "elapsed_ms": 10, "markdown": "md", "stale": ["120ms click 'A': moved []"],
        "trace": [{"step": 1, "kind": "click", "action": "Borrow this copy", "text": None,
                   "probability": 0.94, "alternatives": [("Place a hold", 0.05)]}],
    }
    out = _format("t1", result)
    assert "1. click 'Borrow this copy'  p=0.94 ['Place a hold' p=0.05]" in out
    assert "stale retries" in out and "moved" in out
    assert out.index("</untrusted_page_content>") > out.index("md")


def test_resolve_redirects_decodes_clear_text_google_links_without_network():
    md = "### [A](https://www.google.com/url?q=https://a.example/x&sa=U) and [B](https://b.example/)"
    out = asyncio.run(model.resolve_redirects(md))
    assert out == "### [A](https://a.example/x) and [B](https://b.example/)"


def test_resolve_redirects_opaque_links_go_through_the_browser_context_only():
    md = "[A](https://www.google.com/goto?url=OPAQUE)"

    class FakeRequest:  # stands in for page.context.request (the browser's proxy, cookies, user agent)
        async def get(self, url, **kw):
            FakeRequest.seen = (url, kw)
            return type("R", (), {"status": 302, "headers": {"location": "https://real.example/"}})()

    assert asyncio.run(model.resolve_redirects(md, FakeRequest())) == "[A](https://real.example/)"
    assert FakeRequest.seen == ("https://www.google.com/goto?url=OPAQUE", {"max_redirects": 0, "timeout": 5000})
    # no browser context: the link stays wrapped rather than being fetched from the host
    assert asyncio.run(model.resolve_redirects(md)) == md


def test_split_chunks_at_headings_and_size():
    md = "intro\n## [A](https://a)\nsnippet a\n## B\n" + "x" * 50 + "\n\n" + "y" * 50
    chunks = model.split_chunks(md, limit=60)
    assert chunks[0] == "intro" and chunks[1].startswith("## [A]") and "snippet a" in chunks[1]
    assert all(len(c) <= 60 for c in chunks)


def test_env_file_fills_only_unset_non_empty_values(tmp_path, monkeypatch):
    from clearcote_jet.cli import load_env_file
    env = tmp_path / ".env"
    env.write_text("# comment\nTYPESAFE_API_KEY=\nTEXT_MODEL='m1'\nTEXT_MODEL_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("TEXT_MODEL", raising=False)
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "from-env")
    load_env_file(env)
    import os
    assert "TYPESAFE_API_KEY" not in os.environ  # empty placeholder is not a key
    assert os.environ["TEXT_MODEL"] == "m1" and os.environ["TEXT_MODEL_API_KEY"] == "from-env"


def test_goal_spans_are_verbatim_runs_of_goal_words():
    spans = model.goal_spans("Search the catalogue for The Dispossessed and open its page.")
    assert "The Dispossessed" in spans and "catalogue" in spans
    assert "for The Dispossessed" in spans and all(not s.endswith((".", ",")) for s in spans)
    assert len(spans) == len(set(spans)) <= model.MAX_GOAL_SPANS


def test_field_text_without_text_model_picks_a_goal_span(monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    ctx = {**CTX, "goal": "Search the catalogue for The Dispossessed",
           "field": {"label": "Search the catalogue"}}

    def model_answer(pick):
        async def post(url, key, body, headers=None):
            post.url, post.body = url, body
            criteria = body["questions"]["value"]["criteria"]
            choice = next(k for k, v in criteria.items() if v == pick) if pick != "NONE" else "NONE"
            return {"answers": {"value": {"choice": choice, "confidence": 0.9,
                                          "probabilities": {k: float(k == choice) for k in criteria}}}}
        return post

    post = model_answer("The Dispossessed")
    monkeypatch.setattr(model, "post_json", post)
    value, info = asyncio.run(model.field_text(ctx))
    assert value == "The Dispossessed" and info["model"].startswith("model") and info["probability"] == 1.0
    assert post.url == model.DECIDE_URL
    assert post.body["state"]["field"] == {"label": "Search the catalogue"}
    monkeypatch.setattr(model, "post_json", model_answer("NONE"))
    with pytest.raises(model.NeedsInput):
        asyncio.run(model.field_text(ctx))


def test_usage_totals_accept_either_provider_naming_and_price_input_tokens():
    total = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
    model.add_usage(total, {"input_tokens": 1200, "output_tokens": 40})       # decision model
    model.add_usage(total, {"prompt_tokens": 800, "completion_tokens": 25})   # OpenAI-style text model
    model.add_usage(total, {})                                               # a response without usage
    assert total == {"input_tokens": 2000, "output_tokens": 65, "requests": 2}
    assert model.estimate_usd(2000, usd_per_million=0.042) == pytest.approx(0.000084)
    assert model.estimate_usd(0) == 0


def test_rank_blocks_returns_scores_and_usage(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")

    async def post(url, key, body, headers=None):
        n = len(body["state"]["blocks"])
        return {"answers": {f"b{i}": {"noul": i / 10} for i in range(n)}, "usage": {"input_tokens": 900}}
    monkeypatch.setattr(model, "post_json", post)
    scores, usage = asyncio.run(model.rank_blocks("goal", ["a", "b", "c"]))
    assert scores == [0.0, 0.1, 0.2] and usage == {"input_tokens": 900}
    assert asyncio.run(model.rank_blocks("goal", [])) == ([], {})


def test_screen_anchor_starts_two_blocks_above_the_final_screen():
    blocks = [f"## Section {i}\n\nBody text of section number {i} goes here." for i in range(20)]
    assert model.screen_anchor(blocks, "Body text of section number 15 goes here.\nshort") == 13
    assert model.screen_anchor(blocks, "Body text of section number 1 goes here.") == 0  # clamped at the top
    assert model.screen_anchor(blocks, "Nothing from this markdown is on screen") == 0
    assert model.screen_anchor(blocks, "") == 0
