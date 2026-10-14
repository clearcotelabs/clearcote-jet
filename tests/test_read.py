"""`clearcote-jet read`: values described in words, turned into selectors that find them again.

The page half runs against a real browser — a selector is only worth anything if the DOM agrees —
and the model half runs against a stub, because what matters there is the question asked and what is
done with the answer, not what a paid model would reply.
"""

import asyncio
import json
import os

import pytest

from clearcote_jet.browser import Session
from clearcote_jet.read import pick_selectors, read_values, shortlist

PAGE = """
<!doctype html><html><body>
  <nav><a href="/">Home</a><span>Sale</span></nav>
  <main>
    <h1 id="title">Zenbook 14 OLED</h1>
    <div class="buybox">
      <span class="label">Price</span>
      <span class="a-price">1.249,00 €</span>
      <div id="availability">In stock</div>
    </div>
    <p class="blurb">A laptop with a screen and some memory.</p>
    <ul class="servers">
      <li class="card"><h3>Xeon Gold 6138</h3><span class="mem">32 GB</span><span class="price">129.95 €</span><a href="/a">Order</a></li>
      <li class="card"><h3>Ryzen 3700X</h3><span class="mem">128 GB</span><span class="price">69.90 €</span><a href="/b">Order</a></li>
      <li class="card"><h3>EPYC 7402P</h3><span class="mem">256 GB</span><span class="price">189.00 €</span><a href="/c">Order</a></li>
    </ul>
  </main>
  <footer><span>© 2026</span><span>99.99 €</span></footer>
</body></html>
"""


def stub_model(answers, seen=None):
    """A model that answers with `answers` and records the request it was given."""

    async def post_json(url, key, body, headers=None):
        if seen is not None:
            seen.append(body)
        out = {}
        for name, choice in answers.items():
            # validate_choice insists on a full distribution over this question's own criteria, so
            # the stub has to answer like the real thing: every id, summing to one.
            ids = list(body["questions"][name]["criteria"])
            rest = (1 - 0.9) / max(1, len(ids) - 1)
            out[name] = {
                "choice": choice,
                "probabilities": {i: (0.9 if i == choice else rest) for i in ids},
                "confidence": 0.9,
            }
        return {"answers": out, "usage": {"input_tokens": 100}}

    return post_json


async def observe(chromium):
    """Everything the page half can say about one page, read once."""
    async with chromium() as browser:
        context = await browser.new_context()
        session = Session(context)
        try:
            page = await session.new_tab()
            await page.set_content(PAGE)

            page_candidates = await session.candidates(page)
            row_candidates = await session.candidates(page, row="ul.servers > li.card")

            # Does every selector find its own element again, in the page itself?
            resolved = []
            for c in page_candidates:
                found = await page.evaluate(
                    "s => { const e = document.querySelector(s); return e ? (e.innerText || '').replace(/\\s+/g,' ').trim() : null; }",
                    c["sel"],
                )
                resolved.append((c["sel"], c["text"], found))

            # A row-relative selector has to work on EVERY row: that is what one is for.
            by_text = {c["text"]: c["sel"] for c in row_candidates}
            across_rows = await page.evaluate(
                "s => [...document.querySelectorAll('ul.servers > li.card')].map(r => { const e = r.querySelector(s); return e ? e.innerText.trim() : null; })",
                by_text.get("Xeon Gold 6138"),
            ) if "Xeon Gold 6138" in by_text else None

            # And the whole thing, with the model stubbed onto the price.
            price = next(c for c in page_candidates if c["text"] == "1.249,00 €")
            import clearcote_jet.read as read_module

            real_post = read_module.post_json
            # The model is stubbed, but the code still reads the key the real one needs.
            had_key = os.environ.get("TYPESAFE_API_KEY")
            os.environ["TYPESAFE_API_KEY"] = "test"
            read_module.post_json = stub_model({"field_price": str(price["i"])})
            try:
                end_to_end = await read_values(
                    session, page, {"fields": [{"name": "price", "type": "money", "ask": "the price in euros"}]}
                )
                read_back = await page.evaluate(
                    "s => document.querySelector(s).innerText.trim()", end_to_end["selectors"]["price"]
                )
                read_module.post_json = stub_model({"field_price": "NONE", "field_sku": "NONE"})
                nothing_found = await read_values(
                    session,
                    page,
                    {
                        "fields": [
                            {"name": "price", "type": "money", "ask": "the price"},
                            {"name": "sku", "type": "text", "ask": "the article number"},
                        ]
                    },
                )
            finally:
                read_module.post_json = real_post
                if had_key is None:
                    os.environ.pop("TYPESAFE_API_KEY", None)
                else:
                    os.environ["TYPESAFE_API_KEY"] = had_key

            return {
                "page": page_candidates,
                "resolved": resolved,
                "row": row_candidates,
                "across_rows": across_rows,
                "price_selector": price["sel"],
                "end_to_end": end_to_end,
                "read_back": read_back,
                "nothing_found": nothing_found,
            }
        finally:
            await context.close()


@pytest.fixture(scope="module")
def seen(chromium):
    return asyncio.run(observe(chromium))


# ── the page half ────────────────────────────────────────────────────────────────────────────────


def test_every_candidate_finds_itself_again(seen):
    assert seen["page"], "a page with text has candidates"
    for sel, text, found in seen["resolved"]:
        assert found is not None, f"{sel} finds nothing"
        assert text in found or found in text, f"{sel} finds other text"


def test_candidates_skip_the_page_chrome(seen):
    texts = [c["text"] for c in seen["page"]]
    assert "Zenbook 14 OLED" in texts
    # Navigation and the footer are not what a page is about, and a footer price is a trap.
    assert "Home" not in texts
    assert "© 2026" not in texts
    assert "99.99 €" not in texts


def test_candidates_inside_a_row_work_on_every_row(seen):
    by_text = {c["text"]: c["sel"] for c in seen["row"]}
    assert "Xeon Gold 6138" in by_text, by_text
    assert seen["across_rows"] == ["Xeon Gold 6138", "Ryzen 3700X", "EPYC 7402P"], seen["across_rows"]


# ── the shortlist ────────────────────────────────────────────────────────────────────────────────


def test_shortlist_keeps_only_what_the_type_allows():
    candidates = [
        {"i": 0, "sel": "h1", "text": "Zenbook 14 OLED"},
        {"i": 1, "sel": ".a-price", "text": "1.249,00 €"},
        {"i": 2, "sel": ".rev", "text": "4 reviews"},
        {"i": 3, "sel": "#availability", "text": "In stock"},
    ]
    assert [c["text"] for c in shortlist(candidates, {"name": "price", "type": "money", "ask": "the price in euros"})] == ["1.249,00 €"]
    assert [c["text"] for c in shortlist(candidates, {"name": "available", "type": "boolean", "ask": "in stock?"})] == ["In stock"]
    # Text rules nothing out: a title can be anything.
    assert len(shortlist(candidates, {"name": "title", "type": "text", "ask": "the title"})) == 4


def test_shortlist_falls_back_rather_than_offering_nothing():
    candidates = [{"i": 0, "sel": "p", "text": "nothing numeric here"}]
    assert shortlist(candidates, {"name": "price", "type": "money", "ask": "the price"}) == candidates


def test_shortlist_puts_the_described_words_first():
    candidates = [
        {"i": 0, "sel": ".a", "text": "17 GB"},
        {"i": 1, "sel": ".memory", "text": "32 GB"},
    ]
    first = shortlist(candidates, {"name": "memory_gb", "type": "number", "ask": "the memory in gigabytes"})[0]
    assert first["sel"] == ".memory"


# ── the model half ───────────────────────────────────────────────────────────────────────────────


def test_one_question_per_field_and_the_answer_is_kept(monkeypatch):
    asked = []
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr("clearcote_jet.read.post_json", stub_model({"field_price": "1"}, asked))
    chosen, meta = asyncio.run(
        pick_selectors(
            {"url": "https://example.test/p"},
            [{"name": "price", "type": "money", "ask": "the price in euros"}],
            [{"i": 0, "sel": "h1", "text": "Zenbook"}, {"i": 1, "sel": ".a-price", "text": "1.249,00 €"}],
        )
    )
    assert chosen["price"]["sel"] == ".a-price"
    assert chosen["price"]["text"] == "1.249,00 €"
    assert meta["asked"] == 1

    criteria = asked[0]["questions"]["field_price"]["criteria"]
    assert set(asked[0]["questions"]) == {"field_price"}
    assert "NONE" in criteria, "the model must always be able to say the value is not there"
    # The selector is shown with the text: the choice is about the element, not the words alone.
    assert any(".a-price" in label for label in criteria.values())


def test_none_is_honoured(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr("clearcote_jet.read.post_json", stub_model({"field_price": "NONE"}))
    chosen, _ = asyncio.run(
        pick_selectors(
            {"url": "https://example.test/p"},
            [{"name": "price", "type": "money", "ask": "the price"}],
            [{"i": 0, "sel": ".x", "text": "1,00 €"}],
        )
    )
    assert chosen == {}, "a wrong selector is worse than none: it would be read on every check"


# ── the two halves together ──────────────────────────────────────────────────────────────────────


def test_read_values_answers_with_a_selector_the_page_agrees_with(seen):
    got = seen["end_to_end"]
    assert got["selectors"]["price"] == seen["price_selector"]
    assert got["values"]["price"] == "1.249,00 €"
    assert got["missing"] == []
    assert seen["read_back"] == "1.249,00 €"
    assert json.loads(json.dumps(got)), "the answer is JSON"


def test_read_values_says_which_fields_it_could_not_place(seen):
    got = seen["nothing_found"]
    assert got["selectors"] == {}
    assert sorted(got["missing"]) == ["price", "sku"]
