"""Controls scrolled out of view inside a scroll box of their own, through the real snapshot.js and executor, in
Playwright's own Chromium: left out of a plain snapshot, offered (with the menus) named by their group, and clicked
after wheeling over their box. No Clearcote, no network, no model calls. Skipped where no Chromium is installed,
see conftest.py."""

import asyncio

import pytest

from clearcote_jet import agent
from clearcote_jet.browser import Session

# A phone-sized page with its menu open, as a site shows itself in a small window: the page under the menu cannot
# scroll, the menu is a drawer that scrolls on its own, its "Docs" group is open and "Recommended settings" is below
# what the drawer shows. Inside the drawer: a row of cards that scrolls sideways ("Featured") and a smaller box that
# scrolls on its own ("News"), placed right in the middle of what the drawer shows, where a wheel would scroll it
# instead of the drawer.
PHONE = """<!doctype html><title>Example Outfitters</title>
<style>
  body { margin: 0; font: 16px/19px sans-serif; overflow: hidden }
  header { height: 56px; display: flex; align-items: center; padding: 0 16px; box-sizing: border-box }
  #drawer { position: fixed; top: 56px; left: 0; right: 0; max-height: calc(100vh - 56px); overflow-y: auto;
            background: #fff }
  #drawer > a, #drawer > button { display: block; box-sizing: border-box; width: 100%; height: 47px; padding: 14px 16px;
                                  text-align: left; font: inherit; border: 0; background: none }
  #featured { display: flex; overflow-x: auto; width: 300px; height: 45px; margin: 8px 16px }
  #featured a { flex: 0 0 140px; padding: 12px 8px; box-sizing: border-box }
  #news { height: 80px; overflow-y: auto; margin: 8px 16px; border: 1px solid #ccc }
  #news a { display: block; padding: 6px 8px }
  main { height: 2000px }
</style>
<header><button aria-expanded="true">Toggle menu</button></header>
<div id="drawer" aria-label="Site menu">
  <a href="#pricing">Pricing</a>
  <div id="featured" aria-label="Featured">
    <a href="#card1">Card one</a><a href="#card2">Card two</a><a href="#card3">Card three</a></div>
  <button aria-expanded="false">Features</button>
  <div id="news" aria-label="News"><a href="#n1">Release notes</a><a href="#n2">Blog</a><a href="#n3">Events</a>
    <a href="#roadmap">Roadmap</a></div>
  <button aria-expanded="false">Free tools</button>
  <button aria-expanded="true">Docs</button>
  <a href="#install">Installation</a>
  <a href="#start">Quick start</a>
  <a href="#config">Configuration</a>
  <a href="#profiles">Profiles</a>
  <a href="#proxies">Proxies</a>
  <a href="#recommended">Recommended settings</a>
  <a href="#trouble">Troubleshooting</a>
</div>
<main></main>
<script>(() => {  // a function scope: set_content() writes into the same window, which keeps top-level consts
  window.clicks = [];
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a");
    if (a) { clicks.push([a.getAttribute("href"), e.isTrusted]); e.preventDefault(); }
  }, true);
})();</script>"""

TARGETS = {"Docs › Recommended settings": "#recommended", "Featured › Card three": "#card3", "News › Roadmap": "#roadmap"}
BOXES = "() => ['drawer', 'featured', 'news'].map(id => [document.getElementById(id).scrollLeft, document.getElementById(id).scrollTop])"


def labels(state):
    return [a["label"] for a in state["actions"]]


async def click_in_box(session, page, label):
    await page.set_content(PHONE)
    state = await session.observe(page, menus=True)
    control = next(a for a in state["actions"] if a["label"] == label)
    await session.act(page, state, control)
    return {"clicks": await page.evaluate("clicks"), "boxes": await page.evaluate(BOXES)}


async def scenario(humanize, chromium):
    async with chromium() as browser:
        session = Session(await browser.new_context(viewport={"width": 420, "height": 450}), humanize=humanize)
        page = await session.new_tab()
        await page.set_content(PHONE)
        seen = {"plain": await session.observe(page), "menus": await session.observe(page, menus=True)}
        seen["clicked"] = {label: await click_in_box(session, page, label) for label in TARGETS}
        return seen


@pytest.fixture(scope="module", params=[False, True], ids=["plain", "humanize"])
def seen(request, chromium):
    return asyncio.run(scenario(request.param, chromium))


def test_a_plain_snapshot_leaves_what_a_scroll_box_hides_out(seen):
    offered = labels(seen["plain"])
    assert {"Pricing", "Card one", "Docs", "Installation"} <= set(offered)
    assert not any(label in offered for label in ("Recommended settings", "Troubleshooting", "Card three", "Roadmap"))


def test_scroll_boxes_offer_what_they_hide_named_by_their_group(seen):
    entries = {a["label"]: a for a in seen["menus"]["actions"] if a.get("panel") is not None}
    assert set(TARGETS) | {"Docs › Troubleshooting"} <= set(entries)
    assert entries["Docs › Recommended settings"]["panel"] == entries["Docs › Troubleshooting"]["panel"]  # the drawer
    assert len({entries[label]["panel"] for label in TARGETS}) == 3  # drawer, sideways row, the box inside the drawer
    assert "Pricing" not in {label.split(" › ")[-1] for label in entries}  # in view in its box: offered as it is
    # The drawer holds closed groups (Features, Free tools) too: none of what it scrolls away is taken for their links.
    assert not any(a.get("menu") is not None for a in seen["menus"]["actions"])
    assert seen["menus"]["marker"] == seen["plain"]["marker"]


@pytest.mark.parametrize("label", TARGETS)
def test_each_is_brought_into_view_in_its_box_and_clicked_by_real_input(seen, label):
    assert seen["clicked"][label]["clicks"] == [[TARGETS[label], True]]


def test_the_wheel_scrolls_the_box_itself_not_a_smaller_one_inside_it(seen):
    (_, drawer), (_, _), (_, news) = seen["clicked"]["Docs › Recommended settings"]["boxes"]
    assert drawer > 0 and news == 0  # the middle of the drawer is over News: the wheel went elsewhere on the drawer
    (_, drawer), (row, _), _ = seen["clicked"]["Featured › Card three"]["boxes"]
    assert row > 0 and drawer == 0  # sideways, in the row only
    (_, drawer), _, (_, news) = seen["clicked"]["News › Roadmap"]["boxes"]
    assert news > 0 and drawer == 0


def test_a_stuck_run_finds_a_link_scrolled_away_in_the_phone_menu(monkeypatch, chromium):
    """The whole loop on the real page: the model sees no way on and says BLOCKED; the run offers what the drawer
    hides, the model picks the link there, and the executor wheels the drawer and clicks it."""
    offered = []

    def decision(operation, action=None):
        return {"action": action or {"id": operation, "kind": operation.lower(), "label": operation},
                "operation": operation, "target": None, "probability": 1.0, "confidence": 1.0, "alternatives": [],
                "usage": {"input_tokens": 1000}, "latency_ms": 1}

    async def run():
        async with chromium() as browser:
            session = Session(await browser.new_context(viewport={"width": 420, "height": 450}), humanize=False)
            page = await session.new_tab()
            await page.set_content(PHONE)

            async def choose(state, goal, history):
                offered.append(labels(state))
                if await page.evaluate("clicks.length"):
                    return decision("DONE")
                link = next((a for a in state["actions"] if a["label"] == "Docs › Recommended settings"), None)
                return decision("CLICK", link) if link else decision("BLOCKED")

            async def rank_blocks(goal, blocks):
                return [0.9] * len(blocks), {"input_tokens": 10}

            monkeypatch.setattr(agent, "choose", choose)
            monkeypatch.setattr(agent, "rank_blocks", rank_blocks)
            result = await agent.run(session, "Find the recommended settings", page=page)
            return result, await page.evaluate("clicks")

    result, clicks = asyncio.run(run())
    assert result["status"] == "done" and clicks == [["#recommended", True]]
    assert [h["action"] for h in result["trace"]] == ["Docs › Recommended settings"]
    assert result["trace"][0]["target"]["in_menu"] is True  # a learned skill asks for it the same way
    assert len(offered) == 3 and "Docs › Recommended settings" not in offered[0]  # BLOCKED, CLICK, DONE
