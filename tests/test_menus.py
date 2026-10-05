"""Links inside closed menus through the real snapshot.js and executor, in Playwright's own Chromium: left out of a plain
snapshot, offered with their trigger when asked, and clicked after the menu is opened the way a person opens it. No
Clearcote, no network, no model calls. Skipped where no Chromium is installed (CI): run
`python -m playwright install chromium` to include it."""

import asyncio

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from clearcote_jet import agent
from clearcote_jet.browser import Session, StalePage

# Five kinds of closed menu, in a header that scrolls away with the page (not sticky):
#   Guides, Docs  open on hover, one at a time (pointing at one closes the other); a click soon after the hover keeps
#                 the menu open, a later click closes it. Docs opens off to the side and slides under its trigger
#                 150 ms later, as menus do that measure where they fit (a press aimed before that misses).
#   Products      opens on hover with CSS alone (li:hover > ul), no ARIA
#   Brands        never opens; its trigger is a link, so clicking it to open the menu would leave the page
#   Account       opens on click only
#   More links    a collapsed <details>
SITE = """<!doctype html><title>Example Outfitters</title>
<style>
  body { margin: 0; font: 16px sans-serif }
  header { height: 60px; display: flex; gap: 24px; align-items: center; padding: 0 20px }
  header ul { list-style: none; margin: 0; padding: 0; display: flex; gap: 18px }
  header li { position: relative }
  .panel { position: absolute; top: 100%; left: 0; width: 260px; padding: 8px; background: #fff;
           visibility: hidden; opacity: 0; transition: opacity .15s }
  .panel.open { visibility: visible; opacity: 1 }
  .panel a, .css ul a, #account-panel a { display: block; padding: 6px }
  .css ul { display: none; position: absolute; top: 100%; left: 0; width: 200px; padding: 8px; background: #fff }
  .css:hover ul { display: block }
  #account-panel { display: none; position: absolute; top: 100%; left: 0; width: 200px; padding: 8px; background: #fff }
  #account-panel.open { display: block }
  main { padding: 20px; height: 3000px }
</style>
<header>
  <a href="#home">Home</a>
  <nav aria-label="Main"><ul>
    <li class="group"><button aria-expanded="false" aria-controls="guides-panel">Guides</button>
      <div id="guides-panel" class="panel"><a href="#start">Getting started</a></div></li>
    <li class="group"><button aria-expanded="false" aria-controls="docs-panel">Docs</button>
      <div id="docs-panel" class="panel">
        <a href="#recommended">Recommended settings <span>What to set and what to leave alone</span></a>
        <a href="#install">Installation</a></div></li>
    <li class="css"><a href="#products">Products</a>
      <ul><li><a href="#widgets">Widgets</a></li><li><a href="#gadgets">Gadgets</a></li></ul></li>
    <li><a href="#brands">Brands</a><ul style="display: none"><li><a href="#acme">Acme</a></li></ul></li>
    <li><button id="account" aria-expanded="false" aria-controls="account-panel">Account</button>
      <div id="account-panel"><a href="#billing">Billing</a></div></li>
    <li><a href="#about">About</a></li>
  </ul></nav>
</header>
<main>
  <details><summary>More links</summary><a href="#changelog">Changelog</a></details>
  <p>Trail packs, tents and stoves for every season.</p>
</main>
<script>(() => {  // a function scope: set_content() writes into the same window, which keeps top-level consts
  window.clicks = [];
  window.moves = [];
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a");
    if (a) { clicks.push([a.getAttribute("href"), e.isTrusted]); e.preventDefault(); }
  }, true);
  document.addEventListener("pointermove", (e) => moves.push([e.clientX, e.clientY]), true);
  const groups = [...document.querySelectorAll("li.group")];
  let open = null, openedAt = 0, closing, placing;
  const show = (li) => {
    for (const g of groups) {
      const on = g === li, panel = g.querySelector(".panel");
      panel.classList.toggle("open", on);
      g.querySelector("button").setAttribute("aria-expanded", String(on));
      if (on && panel.id === "docs-panel") {  // opens off to the side, then slides under its trigger
        panel.style.left = "-170px";
        clearTimeout(placing);
        placing = setTimeout(() => { panel.style.left = "0px"; }, 150);
      }
    }
    open = li;
  };
  for (const li of groups) {
    li.addEventListener("pointerenter", (e) => {
      if (e.pointerType !== "mouse") return;
      clearTimeout(closing);
      if (open !== li) { openedAt = Date.now(); show(li); }
    });
    li.addEventListener("pointerleave", () => { closing = setTimeout(() => show(null), 200); });
    li.querySelector("button").addEventListener("click", () => {
      clearTimeout(closing);
      if (open === li && Date.now() - openedAt < 400) return;
      show(open === li ? null : li);
    });
  }
  const account = document.getElementById("account"), accountPanel = document.getElementById("account-panel");
  account.addEventListener("click", () => {
    const on = !accountPanel.classList.contains("open");
    accountPanel.classList.toggle("open", on);
    account.setAttribute("aria-expanded", String(on));
  });
})();</script>"""

MENU_LINKS = {
    "Guides › Getting started": "#start",
    "Docs › Recommended settings What to set and what to leave alone": "#recommended",
    "Docs › Installation": "#install",
    "Products › Widgets": "#widgets",
    "Products › Gadgets": "#gadgets",
    "Account › Billing": "#billing",
    "More links › Changelog": "#changelog",
}
DOCS = "Docs › Recommended settings What to set and what to leave alone"
NEVER_OPENS = "Brands › Acme"
GOAL = "Find the recommended settings"


def labels(state):
    return [a["label"] for a in state["actions"]]


async def click_menu_link(session, page, label, scroll=0):
    """Fresh page, optionally scrolled; look with the menus, click `label`. Returns what the page saw."""
    await page.set_content(SITE)
    if scroll:
        await page.mouse.move(400, 400)
        await page.mouse.wheel(0, scroll)
        await page.wait_for_function(f"scrollY >= {scroll - 5}")
    state = await session.observe(page, menus=True)
    link = next(a for a in state["actions"] if a["label"] == label)
    await page.evaluate("moves.length = 0")
    await session.act(page, state, link)
    trigger = await page.evaluate("""() => { const b = [...document.querySelectorAll('button')]
        .find(e => e.textContent.trim() === 'Docs').getBoundingClientRect(); return [b.x, b.y, b.width, b.height]; }""")
    return {"clicks": await page.evaluate("clicks"), "scroll_y": await page.evaluate("scrollY"),
            "moves": await page.evaluate("moves"), "docs_trigger": trigger}


async def scenario(humanize):
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except PlaywrightError as e:
            return str(e).splitlines()[0]
        try:
            session = Session(await browser.new_context(viewport={"width": 1280, "height": 800}), humanize=humanize)
            page = await session.new_tab()
            seen = {"humanize": humanize}
            await page.set_content(SITE)
            seen["plain"] = await session.observe(page)
            seen["menus"] = await session.observe(page, menus=True)
            seen["clicked"] = {label: await click_menu_link(session, page, label) for label in MENU_LINKS}
            seen["scrolled_away"] = await click_menu_link(session, page, DOCS, scroll=1200)
            seen["scrolled_a_little"] = await click_menu_link(session, page, DOCS, scroll=200)
            try:
                seen["never_opens"] = await click_menu_link(session, page, NEVER_OPENS)
            except StalePage as e:
                seen["never_opens"] = {"raised": str(e), "clicks": await page.evaluate("clicks"), "url": page.url}
            return seen
        finally:
            await browser.close()


@pytest.fixture(scope="module", params=[False, True], ids=["plain", "humanize"])
def seen(request):
    result = asyncio.run(scenario(request.param))
    if isinstance(result, str):
        pytest.skip(f"no Playwright Chromium: {result}")
    return result


def test_a_plain_snapshot_leaves_the_closed_menus_out(seen):
    offered = labels(seen["plain"])
    assert {"Home", "Guides", "Docs", "Products", "Brands", "Account", "About", "More links"} <= set(offered)
    hidden = ("Getting started", "Installation", "Widgets", "Acme", "Billing", "Changelog")
    assert not any(" › " in label or label in hidden for label in offered)


def test_the_menus_add_each_hidden_link_once_with_the_control_that_opens_it(seen):
    plain, menus = seen["plain"], seen["menus"]
    entries = [a for a in menus["actions"] if a.get("menu") is not None]
    assert {a["label"] for a in entries} == {*MENU_LINKS, NEVER_OPENS} and len(entries) == len(MENU_LINKS) + 1
    nodes = {a["label"]: a["node"] for a in plain["actions"] if "node" in a}
    triggers = {a["label"].split(" › ")[0]: a["menu"] for a in entries}
    assert triggers == {name: nodes[name] for name in ("Guides", "Docs", "Products", "Brands", "Account", "More links")}
    assert {a["label"].split(" › ")[0] for a in entries if a["menu_link"]} == {"Products", "Brands"}
    # The visible controls come first and keep their ids; the menu links follow, then scroll and wait.
    count = sum(1 for a in plain["actions"] if "node" in a)
    assert labels(menus)[:count] == labels(plain)[:count]
    assert [a["id"] for a in menus["actions"] if "node" in a] == [f"e{i}" for i in range(1, count + len(entries) + 1)]
    assert labels(menus)[count + len(entries):] == labels(plain)[count:]
    assert all(str(a["node"]) in menus["guards"] for a in entries)
    assert menus["marker"] == plain["marker"]  # offering the menus is not a change of the page


@pytest.mark.parametrize("label", MENU_LINKS)
def test_each_menu_is_opened_and_its_link_clicked_by_real_input(seen, label):
    assert seen["clicked"][label]["clicks"] == [[MENU_LINKS[label], True]]


@pytest.mark.parametrize("how_far", ["scrolled_away", "scrolled_a_little"])
def test_a_header_that_scrolled_away_is_brought_back_first(seen, how_far):
    after = seen[how_far]  # 1200 px: screens away; 200 px: only just out of view
    assert after["clicks"] == [["#recommended", True]] and after["scroll_y"] < 60


def test_the_pointer_goes_down_inside_the_menu_then_across_to_the_link(seen):
    """A diagonal from Docs to its link can slip off the menu (it closes soon after the pointer leaves) or pass over
    Guides (which opens Guides and closes Docs), with the slow curved paths of a humanized browser. The pointer stops
    straight under the trigger, at the link's row inside the panel, before it goes across to the link."""
    if not seen["humanize"]:
        pytest.skip("without humanize the pointer jumps straight to each point: there is no way in between")
    clicked = seen["clicked"][DOCS]
    x, y, w, h = clicked["docs_trigger"]
    *before, (link_x, link_y) = clicked["moves"]
    under = [(mx, my) for mx, my in before if abs(mx - (x + w / 2)) <= 1.5 and my > y + h]
    assert under and abs(under[-1][1] - link_y) <= 20 and link_x != under[-1][0]


def test_a_menu_whose_trigger_is_a_link_is_never_opened_by_clicking_it(seen):
    """Hovering Brands opens nothing. Clicking a button opens a menu; clicking a link goes to another page instead."""
    stuck = seen["never_opens"]
    assert "did not open" in stuck["raised"] and stuck["clicks"] == []


def test_a_control_is_pressed_only_once_it_stops_moving():
    """A menu that slides into place after it opens: aiming before it settles lands the press on what ends up there."""
    def session_seeing(boxes):
        session = Session.__new__(Session)  # only _shows() is used: no browser

        async def evaluate(page, expression, frame=None, await_promise=False):
            return next(boxes)
        session._eval = evaluate
        return session

    box = lambda x: {"x": x, "y": 80, "width": 120, "height": 30}  # noqa: E731
    settles = iter([None, box(-170), box(-170), box(0), box(0), box(0)])
    assert asyncio.run(session_seeing(settles)._shows(None, 7, timeout=1.0)) is True
    assert next(settles, "used up") == "used up"  # still for two looks after the first (a pause is not the end)
    keeps_moving = (box(x) for x in range(0, 10000, 20))
    assert asyncio.run(session_seeing(keeps_moving)._shows(None, 7, timeout=0.5)) is False


def test_a_stuck_run_finds_the_page_inside_a_menu(monkeypatch):
    """The whole loop on the real page: the model sees no way on and says BLOCKED; the run looks inside the menus,
    the model picks the link there, and the executor opens Docs and clicks it."""
    offered = []

    def decision(operation, action=None):
        return {"action": action or {"id": operation, "kind": operation.lower(), "label": operation},
                "operation": operation, "target": None, "probability": 1.0, "confidence": 1.0, "alternatives": [],
                "usage": {"input_tokens": 1000}, "latency_ms": 1}

    async def run():
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except PlaywrightError as e:
                pytest.skip(f"no Playwright Chromium: {str(e).splitlines()[0]}")
            try:
                session = Session(await browser.new_context(viewport={"width": 1280, "height": 800}), humanize=False)
                page = await session.new_tab()
                await page.set_content(SITE)

                async def choose(state, goal, history):
                    offered.append(labels(state))
                    if await page.evaluate("clicks.length"):
                        return decision("DONE")
                    link = next((a for a in state["actions"] if a["label"].startswith("Docs › Recommended")), None)
                    return decision("CLICK", link) if link else decision("BLOCKED")

                async def rank_blocks(goal, blocks):
                    return [0.9] * len(blocks), {"input_tokens": 10}

                monkeypatch.setattr(agent, "choose", choose)
                monkeypatch.setattr(agent, "rank_blocks", rank_blocks)
                result = await agent.run(session, GOAL, page=page)
                return result, await page.evaluate("clicks")
            finally:
                await browser.close()

    result, clicks = asyncio.run(run())
    assert result["status"] == "done" and clicks == [["#recommended", True]]
    assert [h["action"] for h in result["trace"]] == ["Docs › Recommended settings What to set and what to leave alone"]
    assert len(offered) == 3 and not any(" › " in label for label in offered[0])  # BLOCKED, CLICK, DONE


class ScriptedMenu(Session):
    """_press_in_menu() against a scripted menu (no browser): which of pointing and clicking opens it, and how many
    presses on its link slip (the menu closed on the way). Records what was done to the trigger (3) and link (7)."""

    def __init__(self, opens_on, slips=0):
        self.humanize, self.opens_on, self.slips, self.open, self.done = False, opens_on, slips, False, []

    async def _reveal(self, page, node):
        return True

    async def _shows(self, page, node, timeout=1.5, still=2):
        return self.open

    async def _point_at(self, page, node):
        self.done.append("point")
        self.open = self.open or self.opens_on == "hover"

    async def _press(self, page, node, kind="click", frame=None):
        if node == 3:
            self.done.append("click menu")
            self.open = (not self.open) if self.opens_on == "click" else self.open
            return
        if self.slips:
            self.slips -= 1
            self.open = False
            raise StalePage("Target changed or is covered. Observe again.")
        self.done.append("click link")


def test_a_menu_that_closed_on_the_way_is_pointed_at_again_not_clicked():
    menu = ScriptedMenu("hover", slips=1)
    asyncio.run(menu._press_in_menu(None, 3, 7))
    assert menu.done == ["point", "point", "click link"]  # clicking a hover menu's trigger could close it again


def test_a_menu_that_opens_on_click_only_is_clicked_open():
    menu = ScriptedMenu("click")
    asyncio.run(menu._press_in_menu(None, 3, 7))
    assert menu.done == ["point", "click menu", "click link"]


def test_a_menu_that_never_opens_is_given_up_without_clicking_its_link_trigger():
    menu = ScriptedMenu("never")
    with pytest.raises(StalePage, match="did not open"):
        asyncio.run(menu._press_in_menu(None, 3, 7, menu_link=True))
    assert menu.done == ["point"] * 3
