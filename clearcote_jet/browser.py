"""Clearcote session: launch or connect over CDP, observe in an isolated world, act through humanize.

Public Clearcote SDK API only. launch_persistent_context(humanize=True) makes page.mouse and page.keyboard
human-like (curved, Fitts-timed moves; per-key typing; press holds), so the executor drives plain Playwright
input and needs no SDK internals. Page reads run in a CDP isolated world that shares the DOM but not the
page's JavaScript globals, so page scripts never see them.
"""

import asyncio
import json
import logging
import random
import sys
from pathlib import Path

from clearcote.async_api import launch_persistent_context
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

HERE = Path(__file__).parent
SNAPSHOT_JS = (HERE / "snapshot.js").read_text(encoding="utf-8")
MARKDOWN_JS = (HERE / "markdown.js").read_text(encoding="utf-8")
DEFAULT_PROFILE = Path.home() / ".clearcote-jet" / "profile"
SELECT_ALL = "Meta+A" if sys.platform == "darwin" else "Control+A"
log = logging.getLogger("clearcote-jet")


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class BrowserClosed(RuntimeError):
    """The browser or the task's tab was closed (e.g. by the user) while the agent was using it."""


class IsolatedWorld:
    """A CDP isolated world in the page's main frame (the mechanism extension content scripts use).

    It shares the DOM but has its own globals, so the node cache (window.__ca) and every read are invisible to
    page scripts. A navigation destroys the world; the next evaluate fails on the dead context id and creates a
    fresh one. evaluate returns None when the read is impossible; callers treat that as "not observed".
    """

    def __init__(self, page):
        self.page, self.cdp, self.ctx = page, None, None

    async def evaluate(self, expression):
        for _ in range(2):
            try:
                if self.cdp is None:
                    self.cdp = await self.page.context.new_cdp_session(self.page)
                if self.ctx is None:
                    tree = await self.cdp.send("Page.getFrameTree")
                    world = await self.cdp.send("Page.createIsolatedWorld", {"frameId": tree["frameTree"]["frame"]["id"]})
                    self.ctx = world["executionContextId"]
                result = await self.cdp.send("Runtime.evaluate", {
                    "expression": expression, "contextId": self.ctx, "returnByValue": True})
            except PlaywrightError:
                if self.page.is_closed():
                    raise
                self.ctx = None  # navigated away: the context id is gone, make a new world
                continue
            if "exceptionDetails" in result:
                return None
            return result.get("result", {}).get("value")
        return None


def aim_point(box):
    """Where a person would click: near the middle of a button, in the left part of a text field.

    Never the exact geometric center, which is what scripted clicks hit every time.
    """
    x, y, w, h = box["x"], box["y"], box["width"], box["height"]
    if box["input"] and w > 80:
        px = x + w * random.uniform(0.15, 0.45)
    else:
        px = x + w / 2 + random.gauss(0, w / 8)
    py = y + h / 2 + random.gauss(0, h / 8)
    return min(max(px, x + 2), x + w - 2), min(max(py, y + 2), y + h - 2)


class Session:
    """launch mode: we own the browser. connect mode: attach to a running one and leave it running."""

    def __init__(self, context, owner=None, humanize=True, direct=False):
        self.context = context
        self._owner = owner  # (playwright, browser) in connect mode
        # direct: we launched the browser with no proxy, so a request from this machine leaves from the same IP
        # the browser uses. Otherwise (a proxy, or a remote browser over CDP) nothing is fetched outside it.
        self.direct = direct
        # humanize=False: instant clicks and typing (still real input events). Faster, less human-looking.
        self.humanize = humanize
        self.closed = False
        context.on("close", lambda *_: setattr(self, "closed", True))
        if owner:
            owner[1].on("disconnected", lambda *_: setattr(self, "closed", True))

    def check_open(self, page):
        if self.closed:
            raise BrowserClosed("the browser was closed")
        if page.is_closed() or getattr(page, "_ca_crashed", False):
            raise BrowserClosed("the tab was closed")

    def is_gone(self, page):
        """Browser closed, tab closed, or tab crashed (e.g. its process was killed)."""
        return self.closed or page.is_closed() or getattr(page, "_ca_crashed", False)

    @classmethod
    async def launch(cls, profile=DEFAULT_PROFILE, headless=False, cdp_port=None, humanize=True, proxy=None,
                     **kwargs):
        """Launch Clearcote on a persistent profile.

        No --fingerprint seed: Clearcote's default persona is not re-rolled per launch, so a persistent profile
        is already the same device every time. Licence: CLEARCOTE_LICENSE_KEY or ~/.clearcote/license.key,
        picked up by the SDK; without one the open build runs.
        """
        args = [f"--remote-debugging-port={cdp_port}"] if cdp_port else []
        kwargs.setdefault("geoip", True)  # timezone, languages, location and WebRTC IP follow the exit IP
        if proxy:
            kwargs["proxy"] = proxy
        context = await launch_persistent_context(
            str(profile), headless=headless, humanize=humanize, args=args, **kwargs
        )
        return cls(context, humanize=humanize, direct=not proxy)

    @classmethod
    async def connect(cls, cdp_url, humanize=True):
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        if humanize:
            from clearcote.async_api import install_humanize_on_context  # the SDK's own launch path uses it
            await install_humanize_on_context(context, True, browser=browser)
        return cls(context, owner=(pw, browser), humanize=humanize)

    async def new_tab(self, url=None):
        # A fresh persistent context opens with one blank tab: use it instead of leaving it empty beside ours.
        blank = [p for p in self.context.pages if p.url == "about:blank" and not hasattr(p, "_ca_world")]
        page = blank[0] if blank else await self.context.new_page()
        page.on("crash", lambda *_: setattr(page, "_ca_crashed", True))
        page._ca_world = IsolatedWorld(page)
        if url:
            await page.goto(url, wait_until="domcontentloaded")
        return page

    async def close(self):
        """launch mode closes the browser; connect mode only drops our connection."""
        if self._owner:
            pw, _ = self._owner
            await pw.stop()  # disconnects; the remote browser keeps running
        else:
            await self.context.close()

    @staticmethod
    async def _eval(page, expression):
        return await page._ca_world.evaluate(expression)

    async def observe(self, page, after=None):
        if after and after["kind"] == "fill" and after.get("role") == "combobox":
            # Let autocomplete suggestions arrive before the model chooses from an incomplete popup.
            for _ in range(8):
                await asyncio.sleep(0.025)
                if await self._eval(page, _OPTIONS_VISIBLE):
                    break
        elif after and after["kind"] != "wait":
            await asyncio.sleep(0.05)
        state = previous = None
        for attempt in range(100):  # up to ~10 s while a navigation settles
            self.check_open(page)  # a closed browser/tab fails at once instead of spinning here
            state = await self._eval(page, SNAPSHOT_JS) or state
            # Pages keep mutating after load (late JS panels, lazy widgets), which invalidates decisions.
            # Wait until two snapshots 100 ms apart agree, capped at ~3 s.
            # A blank document (no text, no elements) is a page still booting, not a settled one.
            blank = state and not state["text"] and not any("node" in a for a in state["actions"])
            quiet = previous is not None and state and not blank and state["marker"] == previous["marker"]
            if state and ((state["ready"] == "complete" and quiet) or attempt >= 30):
                return state
            previous = state
            await asyncio.sleep(0.1)
        if state:
            return state
        raise StalePage("Page did not settle")

    async def fresh(self, page, state, action=None):
        self.last_diff = ""
        if action is not None and action["kind"] in {"click", "select"}:
            current = await self._eval(
                page,
                f"(() => {{ const c=window.__ca; return c ? [JSON.stringify(c.pageKey()),"
                f"JSON.stringify(c.guard(c.nodes.get({int(action['node'])})))] : null; }})()",
            )
            return current == [state["page_key"], state["guards"].get(str(action["node"]))]
        current = await self._eval(page, SNAPSHOT_JS)
        same = bool(current) and current["marker"] == state["marker"]
        if current and not same:
            self.last_diff = _marker_diff(state["marker"], current["marker"])  # reported in the stale list
        return same

    async def act(self, page, state, action, text=None):
        self.check_open(page)
        if not await self.fresh(page, state, action):
            raise StalePage("Page changed since this decision. Observe again.")
        kind = action["kind"]
        if kind == "wait":
            await asyncio.sleep(0.1)
            return
        if kind == "scroll":
            await page.mouse.wheel(0, action["delta"])  # humanized: stepped wheel ticks when humanize is on
            return
        node = int(action["node"])
        if kind == "select":
            await self._select_from_list(page, node, action["value"])
            return
        await self._press(page, node, kind)
        if kind == "fill":
            if self.humanize:
                await asyncio.sleep(random.uniform(0.1, 0.25))
            await page.keyboard.press(SELECT_ALL)
            if self.humanize:
                await asyncio.sleep(random.uniform(0.03, 0.08))
            await page.keyboard.press("Backspace")
            if self.humanize:
                await asyncio.sleep(random.uniform(0.05, 0.15))
            await page.keyboard.type(text)  # humanized per-key typing when humanize is on

    async def _press(self, page, node, kind="click"):
        """Move to the element and press it: the one mouse path every click, field and dropdown goes through."""
        box = await self._eval(page, f"window.__ca?.box({node}, {json.dumps(kind)})")
        if not box:
            raise StalePage("Target changed or is covered. Observe again.")
        if not self.humanize:
            # box() already hit-tested the center. Playwright's plain click: real input events, no delays.
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            return
        # An aim point can land on an overlay inside the box (e.g. a search icon over an input's left edge).
        # Pick one that actually hits the node before spending a mouse move on it.
        for _ in range(5):
            x, y = aim_point(box)
            if await self._eval(page, f"window.__ca?.hit({node}, {x}, {y})"):
                break
        else:
            raise StalePage("No aim point inside the target hits it; it is covered.")
        await page.mouse.move(x, y)  # humanized: curved, Fitts-timed path from where the cursor is
        # The move takes hundreds of ms; the page may have shifted. Re-hit-test before pressing.
        if not await self._eval(page, f"window.__ca?.hit({node}, {x}, {y})"):
            raise StalePage("Target moved or became covered during the approach.")
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.06, 0.14))  # a finger holds the button for a moment
        await page.mouse.up()

    async def _select_from_list(self, page, node, value):
        """Choose a native <select> option the way a person does: open the list, arrow to it, press Enter.

        That fires one trusted `change`. Arrow keys on a closed select change the value at every step (Windows), so a
        page that submits its form on `change` would reload before the wanted option is reached.
        """
        plan = await self._eval(page, _SELECT_PLAN % (node, json.dumps(value)))
        if not plan:
            raise StalePage("Dropdown or option is gone. Observe again.")
        await self._press(page, node)  # opens the option list, highlighting the current option
        await asyncio.sleep(random.uniform(0.25, 0.45))
        key = "ArrowDown" if plan["to"] > plan["from"] else "ArrowUp"
        for _ in range(plan["steps"]):  # the list skips disabled options by itself
            await page.keyboard.press(key)
            await asyncio.sleep(random.uniform(0.06, 0.14))
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.15)
        current = await self._eval(page, f"window.__ca?.nodes.get({node})?.selectedIndex")
        # None: the element is gone because the page reacted to the change (e.g. submitted its form).
        if current is not None and current != plan["to"]:
            raise RuntimeError("Dropdown selection was not confirmed; inspect before retrying.")

    async def markdown(self, page):
        return await self._eval(page, MARKDOWN_JS) or ""

    async def settled_markdown(self, page, quiet=1.0, cap=4.0, every=0.25):
        """Page markdown once it stopped changing for `quiet` seconds (cap `cap`).

        Pages that fill in progressively (search results that stream in, lists that load in batches) can look
        finished before their content arrives, and reading them then returns the frame without the results.
        """
        loop = asyncio.get_running_loop()
        start = last_change = loop.time()
        current = await self.markdown(page)
        while loop.time() - start < cap and loop.time() - last_change < quiet:
            await asyncio.sleep(every)
            self.check_open(page)
            latest = await self.markdown(page)
            if latest != current:
                current, last_change = latest, loop.time()
        return current


MARKER_PARTS = ["timeOrigin", "url", "scrollX", "scrollY", "innerWidth", "innerHeight", "title", "text", "actions",
                "form_values"]


def _marker_diff(old, new):
    """Name the marker parts that changed, with a short sample, so stale retries explain themselves."""
    old, new = json.loads(old), json.loads(new)
    out = []
    for name, a, b in zip(MARKER_PARTS, old, new):
        if a == b:
            continue
        if name == "text":
            added = set(b.split("\n")) - set(a.split("\n"))
            removed = set(a.split("\n")) - set(b.split("\n"))
            out.append(f"text +{sorted(added)[:3]} -{sorted(removed)[:3]}")
        elif name == "actions":
            ka = {(x.get("label"), x.get("kind"), x.get("value")) for x in a}
            kb = {(x.get("label"), x.get("kind"), x.get("value")) for x in b}
            out.append(f"actions +{sorted(map(str, kb - ka))[:3]} -{sorted(map(str, ka - kb))[:3]}")
        else:
            out.append(f"{name} {str(a)[:60]} → {str(b)[:60]}")
    return "; ".join(out)


_OPTIONS_VISIBLE = """[...document.querySelectorAll('[role="option"]')].some(e => {
  const r = e.getBoundingClientRect();
  return r.width && r.height && r.bottom > 0 && r.top < innerHeight &&
    e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
})"""

# The current and wanted option index of a single <select>, and how many arrow presses apart they are once disabled
# options are skipped. null: not a usable select, the option is missing or disabled, or it is already chosen.
_SELECT_PLAN = """(() => {
  const e = window.__ca?.nodes.get(%d), want = %s;
  if (!e?.isConnected || e.tagName !== 'SELECT' || e.disabled || e.multiple) return null;
  const opts = [...e.options], usable = o => !o.disabled && !o.closest('optgroup[disabled]');
  const to = opts.findIndex(o => o.value === want && usable(o)), from = e.selectedIndex;
  if (to < 0 || to === from) return null;
  const dir = to > from ? 1 : -1;
  let steps = 0;
  for (let i = from + dir; dir > 0 ? i <= to : i >= to; i += dir) if (usable(opts[i])) steps++;
  return {from, to, steps};
})()"""
