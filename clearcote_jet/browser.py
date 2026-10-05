"""Clearcote session: launch or connect over CDP, observe in an isolated world, act through humanize.

Public Clearcote SDK API only. launch_persistent_context(humanize=True) makes page.mouse and page.keyboard
human-like (curved, Fitts-timed moves; per-key typing; press holds), so the executor drives plain Playwright
input and needs no SDK internals. Page reads run in a CDP isolated world that shares the DOM but not the
page's JavaScript globals, so page scripts never see them.
"""

import asyncio
import base64
import json
import logging
import random
import sys
from pathlib import Path
from urllib.parse import quote

from clearcote.async_api import launch_persistent_context
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

HERE = Path(__file__).parent
SNAPSHOT_JS = (HERE / "snapshot.js").read_text(encoding="utf-8")
MARKDOWN_JS = (HERE / "markdown.js").read_text(encoding="utf-8")
LISTS_JS = (HERE / "lists.js").read_text(encoding="utf-8")
EXTRACT_JS = (HERE / "extract.js").read_text(encoding="utf-8")  # %s: the list spec, as JSON
FIND_JS = (HERE / "find.js").read_text(encoding="utf-8")  # %s: the words, as JSON
FIND_FLICKS = 12  # with no anchor to jump to, at most this many scrolls of about three screens each
DEFAULT_PROFILE = Path.home() / ".clearcote-jet" / "profile"
SELECT_ALL = "Meta+A" if sys.platform == "darwin" else "Control+A"
log = logging.getLogger("clearcote-jet")


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class BrowserClosed(RuntimeError):
    """The browser or the task's tab was closed (e.g. by the user) while the agent was using it."""


class IsolatedWorld:
    """A CDP isolated world in one frame (the mechanism extension content scripts use): the page's main frame, or
    the child frame `frame_id` reached through the CDP session `cdp` that hosts it.

    It shares the DOM but has its own globals, so the node cache (window.__ca) and every read are invisible to
    page scripts. A navigation destroys the world; the next evaluate fails on the dead context id and creates a
    fresh one. evaluate returns None when the read is impossible; callers treat that as "not observed".
    """

    def __init__(self, page, cdp=None, frame_id=None):
        self.page, self.cdp, self.frame_id, self.ctx = page, cdp, frame_id, None

    async def session(self):
        if self.cdp is None:
            self.cdp = await self.page.context.new_cdp_session(self.page)
        return self.cdp

    async def evaluate(self, expression, await_promise=False):
        for _ in range(2):
            try:
                cdp = await self.session()
                if self.ctx is None:
                    frame_id = self.frame_id or (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
                    world = await cdp.send("Page.createIsolatedWorld", {"frameId": frame_id})
                    self.ctx = world["executionContextId"]
                result = await cdp.send("Runtime.evaluate", {
                    "expression": expression, "contextId": self.ctx, "returnByValue": True,
                    "awaitPromise": await_promise})
            except PlaywrightError:
                if self.page.is_closed():
                    raise
                self.ctx = None  # navigated away: the context id is gone, make a new world
                continue
            if "exceptionDetails" in result:
                return None
            return result.get("result", {}).get("value")
        return None

    async def call_on(self, backend_node_id, function, *args):
        """function(...args) with `this` = a DOM node of this frame, run in this world. None when either is gone."""
        if await self.evaluate("1") is None:
            return None
        try:
            node = await self.cdp.send("DOM.resolveNode", {
                "backendNodeId": backend_node_id, "executionContextId": self.ctx, "objectGroup": "ca"})
            result = await self.cdp.send("Runtime.callFunctionOn", {
                "functionDeclaration": function, "objectId": node["object"]["objectId"],
                "arguments": [{"value": a} for a in args], "returnByValue": True})
            await self.cdp.send("Runtime.releaseObjectGroup", {"objectGroup": "ca"})
        except PlaywrightError:
            if self.page.is_closed():
                raise
            return None
        if "exceptionDetails" in result:
            return None
        return result.get("result", {}).get("value")


class JsonCapture:
    """The JSON answers to a tab's XHR and fetch requests, with the request that got each one. Read through the
    browser's protocol (the page sees nothing); the latest LIMIT are kept, answers over MAX_BYTES are skipped."""

    LIMIT, MAX_BYTES = 40, 2_000_000

    def __init__(self, page):
        self.page, self.items, self._tasks, self._on = page, [], set(), True
        page.on("response", self._on_response)

    def _on_response(self, response):
        request = response.request
        if request.resource_type not in ("xhr", "fetch") or response.status != 200:
            return
        if "json" not in (response.headers.get("content-type") or ""):
            return
        task = asyncio.create_task(self._read(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _read(self, response):
        try:
            body = await response.body()
            if len(body) > self.MAX_BYTES:
                return
            data = json.loads(body)
        except Exception:  # noqa: BLE001  gone with a navigation, or not JSON after all
            return
        request = response.request
        self.items.append({"url": response.url, "method": request.method, "post_data": request.post_data,
                           "content_type": request.headers.get("content-type"), "body": data})
        del self.items[:-self.LIMIT]

    async def stop(self):
        """Stop listening; the answers kept so far (waiting for any still being read). Safe to call again."""
        if self._on:
            self._on = False
            self.page.remove_listener("response", self._on_response)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        return self.items


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
        page._ca_main, page._ca_frames, page._ca_oopif = None, {}, {}  # see _frames()
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
    async def _eval(page, expression, frame=None, await_promise=False):
        """Evaluate in the main frame's isolated world, or in child frame `frame`'s (None if it is gone)."""
        world = page._ca_world if frame is None else page._ca_frames.get(frame, {}).get("world")
        if not world:
            return None
        return await (world.evaluate(expression, await_promise=True) if await_promise else world.evaluate(expression))

    async def _frames(self, page):
        """The page's child frames (iframes, at any depth): {frame id: info}, each with its own isolated world that is
        kept across calls, so node ids stay valid from observe to act.

        Same-process frames are reached through the page's CDP session; out-of-process ones (cross-site iframes)
        through the session Playwright attaches to each. info: the session hosting the frame's document, its parent
        frame, and its local root (the out-of-process frame whose coordinates its geometry is in; None = the tab's).
        """
        root = await page._ca_world.session()
        page._ca_oopif = {f: s for f, s in page._ca_oopif.items() if not f.is_detached()}
        sessions = [root]
        for frame in page.frames[1:]:
            url, session = page._ca_oopif.get(frame, (None, None))
            # A frame changes process only by navigating (a widget iframe starts as a same-process about:blank, then
            # loads its cross-site page), so look again whenever its URL changed.
            if url != frame.url:
                if session:  # still out of process? its session dies with the process it was attached to
                    try:
                        await session.send("Page.getFrameTree")
                    except PlaywrightError:
                        session = None
                if not session:
                    try:
                        session = await page.context.new_cdp_session(frame)
                    except PlaywrightError:
                        session = None  # same process as its parent: the parent's session reaches it
                page._ca_oopif[frame] = (frame.url, session)
            if session:
                sessions.append(session)
        found = {}
        for session in sessions:  # the page's first, so an out-of-process frame's own session wins
            try:
                tree = (await session.send("Page.getFrameTree"))["frameTree"]
            except PlaywrightError:
                continue
            if session is root:
                page._ca_main, local_root, stack = tree["frame"]["id"], None, list(tree.get("childFrames", []))
            else:
                local_root, stack = tree["frame"]["id"], [tree]
            while stack:
                node = stack.pop()
                stack.extend(node.get("childFrames", []))
                found[node["frame"]["id"]] = {"session": session, "parent": node["frame"].get("parentId"),
                                              "local_root": local_root}
        frames = {}
        for fid, info in found.items():
            old = page._ca_frames.get(fid)
            world = old["world"] if old and old["session"] is info["session"] else IsolatedWorld(page, info["session"], fid)
            frames[fid] = {**info, "world": world}
        page._ca_frames = frames
        return frames

    async def _frame_box(self, page, fid):
        """Where child frame `fid`'s viewport sits in the tab's viewport: (x, y, width, height, backend node id of its
        <iframe> element), or None when it is gone or not laid out. Geometry is read fresh on every call."""
        info = page._ca_frames.get(fid)
        parent = info and info["parent"]
        if parent and parent == page._ca_main:
            session, base = page._ca_world.cdp, (0, 0)
        elif parent in page._ca_frames:
            owner = page._ca_frames[parent]
            session, base = owner["session"], (0, 0)
            if owner["local_root"]:  # the parent is out of process: its CDP geometry is relative to its own viewport
                outer = await self._frame_box(page, owner["local_root"])
                if not outer:
                    return None
                base = outer[:2]
        else:
            return None
        try:
            element = (await session.send("DOM.getFrameOwner", {"frameId": fid}))["backendNodeId"]
            quad = (await session.send("DOM.getBoxModel", {"backendNodeId": element}))["model"]["content"]
        except PlaywrightError:
            return None
        return base[0] + quad[0], base[1] + quad[1], quad[2] - quad[0], quad[5] - quad[1], element

    async def _reachable(self, page, fid, points, box=None):
        """For points in tab coordinates: does each land on frame `fid`'s <iframe> element, in its parent and through
        every enclosing frame? A control under an overlay or another iframe is neither offered nor clicked."""
        box = box or await self._frame_box(page, fid)
        parent = page._ca_frames[fid]["parent"] if box else None
        if parent and parent == page._ca_main:
            world, origin = page._ca_world, (0, 0)
        else:
            outer = parent in page._ca_frames and await self._frame_box(page, parent)
            if not outer:
                return [False] * len(points)
            world, origin = page._ca_frames[parent]["world"], outer[:2]
        hits = await world.call_on(box[4], _OWNER_HITS, [[x - origin[0], y - origin[1]] for x, y in points])
        hits = hits or [False] * len(points)
        if parent != page._ca_main and any(hits):
            hits = [a and b for a, b in zip(hits, await self._reachable(page, parent, points, outer))]
        return hits

    async def _snapshot(self, page):
        """The main frame's snapshot plus, from every child frame a person can see, its text and the controls they can
        reach. Frame controls carry `frame`; each frame's page key and guards are kept in state["frames"]."""
        state = await self._eval(page, SNAPSHOT_JS)
        if not state:
            return state
        marker, texts, controls, frame_markers = json.loads(state["marker"]), [state["text"]], [], {}
        width, height = marker[4], marker[5]  # the tab's innerWidth, innerHeight
        state["frames"] = {}
        for fid in await self._frames(page):
            box = await self._frame_box(page, fid)
            if not box:
                continue
            left, top = max(box[0], 0), max(box[1], 0)
            right, bottom = min(box[0] + box[2], width), min(box[1] + box[3], height)
            if right - left < 10 or bottom - top < 10:  # off screen, collapsed, or a tracking pixel
                continue
            snap = await self._eval(page, SNAPSHOT_JS, fid)
            if not snap:
                continue
            items = [a for a in snap["actions"] if "node" in a]
            boxes = await self._eval(page, "[%s].map(id => window.__ca?.box(id, 'click'))"
                                     % ",".join(str(a["node"]) for a in items), fid) or [None] * len(items)
            points = [((left + right) / 2, (top + bottom) / 2)] + [
                (box[0] + b["x"] + b["width"] / 2, box[1] + b["y"] + b["height"] / 2) if b else (-1, -1) for b in boxes]
            hits = await self._reachable(page, fid, points, box)
            reach = [dict(a, frame=fid) for a, b, hit in zip(items, boxes, hits[1:]) if b and hit]
            if not hits[0] and not reach:  # covered where it shows
                continue
            controls += reach
            texts.append(snap["text"])
            state["frames"][fid] = {"page_key": snap["page_key"], "guards": snap["guards"]}
            frame_markers[fid] = snap["marker"]
        if controls:
            n = sum(1 for a in state["actions"] if "node" in a)  # the page's controls come first, then scroll and wait
            state["actions"][n:n] = controls
            for i, a in enumerate(state["actions"][:n + len(controls)]):
                a["id"] = f"e{i + 1}"
        state["text"] = "\n".join(t for t in texts if t)[:8000]
        marker.append(frame_markers)  # a change inside a frame (a box getting ticked) is a page change
        state["marker"] = json.dumps(marker)
        return state

    async def _with_menus(self, page, state):
        """`state` plus the links inside the page's closed menus, and the controls scrolled out of view inside a scroll
        box of their own (main frame), after the controls a person can see.

        A menu link carries `menu`, the node of the control that opens its menu: act() opens it before clicking the
        link. A control in a scroll box carries `panel`, the box: act() wheels over the box until it is in view.
        """
        entries = await self._eval(page, "window.__ca?.menus?.() ?? []") or []
        n = sum(1 for a in state["actions"] if "node" in a)  # the page's controls, then scroll, find and wait
        offered = {(a.get("frame"), a["node"]) for a in state["actions"][:n]}
        entries = [e for e in entries if (None, e["node"]) not in offered]
        if not entries:
            return state
        guards = dict(state.get("guards") or {})
        for e in entries:
            guards[str(e["node"])] = e.pop("guard")  # fresh() compares it, as for any other control
        actions = state["actions"][:n] + entries + state["actions"][n:]
        for i, a in enumerate(actions[:n + len(entries)]):
            a["id"] = f"e{i + 1}"
        return {**state, "actions": actions, "guards": guards}

    async def observe(self, page, after=None, menus=False):
        """The settled page. menus: also offer the links inside its closed menus (the agent asks when it is stuck)."""
        state = await self._observe(page, after)
        return await self._with_menus(page, state) if menus else state

    async def _observe(self, page, after=None):
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
            state = await self._snapshot(page) or state
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
            frame = action.get("frame")
            current = await self._eval(
                page,
                f"(() => {{ const c=window.__ca; return c ? [JSON.stringify(c.pageKey()),"
                f"JSON.stringify(c.guard(c.nodes.get({int(action['node'])})))] : null; }})()",
                frame,
            )
            seen = state.get("frames", {}).get(frame, {}) if frame else state  # a frame's control: that frame's keys
            return current == [seen.get("page_key"), seen.get("guards", {}).get(str(action["node"]))]
        current = await self._snapshot(page)
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
        if kind == "find":
            await self._find(page, text)
            return
        node, frame = int(action["node"]), action.get("frame")
        if kind == "select":
            await self._select_from_list(page, node, action["value"], frame)
            return
        if action.get("menu") is not None:
            await self._press_in_menu(page, int(action["menu"]), node, bool(action.get("menu_link")))
            return
        if action.get("panel") is not None:
            await self._press_in_panel(page, int(action["panel"]), node)
            return
        await self._press(page, node, kind, frame)
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

    async def _hits(self, page, node, x, y, frame=None):
        """Does the tab point (x, y) land on the node: inside its frame, and on that frame through every enclosing one?"""
        if frame is None:
            return bool(await self._eval(page, f"window.__ca?.hit({node}, {x}, {y})"))
        box = await self._frame_box(page, frame)  # fresh: the frame may have moved during the approach
        return bool(box and await self._eval(page, f"window.__ca?.hit({node}, {x - box[0]}, {y - box[1]})", frame)
                    and (await self._reachable(page, frame, [(x, y)], box))[0])

    async def _press(self, page, node, kind="click", frame=None):
        """Move to the element and press it: the one mouse path every click, field and dropdown goes through.

        A control inside an iframe is pressed at tab coordinates: its box in the frame, moved by the frame's position.
        """
        box = await self._eval(page, f"window.__ca?.box({node}, {json.dumps(kind)})", frame)
        origin = (await self._frame_box(page, frame) or [None])[:2] if box and frame else (0, 0)
        if not box or origin[0] is None:
            raise StalePage("Target changed or is covered. Observe again.")
        box = {**box, "x": box["x"] + origin[0], "y": box["y"] + origin[1]}
        if not self.humanize:
            # box() already hit-tested the center within its frame. Playwright's plain click: real input events.
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            if frame and not await self._hits(page, node, x, y, frame):
                raise StalePage("Target's frame is covered. Observe again.")
            await page.mouse.click(x, y)
            return
        # An aim point can land on an overlay inside the box (e.g. a search icon over an input's left edge).
        # Pick one that actually hits the node before spending a mouse move on it.
        for _ in range(5):
            x, y = aim_point(box)
            if await self._hits(page, node, x, y, frame):
                break
        else:
            raise StalePage("No aim point inside the target hits it; it is covered.")
        await page.mouse.move(x, y)  # humanized: curved, Fitts-timed path from where the cursor is
        # The move takes hundreds of ms; the page may have shifted. Re-hit-test before pressing.
        if not await self._hits(page, node, x, y, frame):
            raise StalePage("Target moved or became covered during the approach.")
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.06, 0.14))  # a finger holds the button for a moment
        await page.mouse.up()

    async def _press_in_menu(self, page, trigger, node, menu_link=False, attempts=3):
        """Click a link inside a closed menu the way a person does: point at the menu (most navigation menus open on
        hover); if that opens nothing, click it open, unless the trigger is itself a link (`menu_link`: clicking it
        would leave the page); then go down inside the menu and click the link. A menu that closes on the way (the
        pointer slipped off it) is opened again, up to `attempts` times. A trigger that scrolled out of view (a header
        that is not sticky) is brought back first."""
        for _ in range(attempts):
            if not await self._reveal(page, trigger):
                raise StalePage("The menu's trigger is gone or covered. Observe again.")
            # Open the menu only if it is closed: clicking the trigger of an open menu closes it again.
            if not await self._shows(page, node, timeout=0.25, still=1):
                await self._point_at(page, trigger)
                opened = await self._shows(page, node, timeout=0.8)
                if not opened and not menu_link:
                    await self._press(page, trigger)  # a menu that opens on click only, or a <details>
                    opened = await self._shows(page, node)
                if not opened:
                    continue
            if self.humanize and (way := await self._eval(page, f"window.__ca?.waypoint({trigger}, {node})")):
                await page.mouse.move(way["x"], way["y"])  # down inside the menu to the link's row, then across
            try:
                await self._press(page, node)
                return
            except StalePage:
                continue  # the menu closed on the way to the link: open it again
        raise StalePage("The menu did not open, or closed before its link could be clicked. Observe again.")

    async def _press_in_panel(self, page, panel, node):
        """Click a control scrolled out of view inside a scroll box of its own (a phone menu's panel, a dialog's body,
        a side list): wheel over that box, as a person does, until the control is in view and still; then click it."""
        for _ in range(FIND_FLICKS):
            if await self._shows(page, node, timeout=0.25, still=1):
                break
            way = await self._eval(page, f"window.__ca?.panelPoint({panel}, {node})")
            if not way:
                raise StalePage("The scroll box is gone, covered or out of view. Observe again.")
            await page.mouse.move(way["x"], way["y"])  # the wheel scrolls what is under the pointer
            reach = 600  # about a screen per flick of the wheel, like _wheel_to
            await page.mouse.wheel(max(-reach, min(reach, way["dx"])), max(-reach, min(reach, way["dy"])))
            await asyncio.sleep(0.2)
            if await self._eval(page, f"window.__ca?.scrolled({panel})") == way["at"]:
                break  # the box goes no further
        if not await self._shows(page, node):
            raise StalePage("The control did not come into view in its scroll box. Observe again.")
        await self._press(page, node)

    async def _point_at(self, page, node):
        """Rest the pointer on a control without pressing it (to open a menu that opens on hover). Near its middle, not
        anywhere on it: from an edge, the way on into the menu can slip off it, and a menu closes soon after."""
        box = await self._eval(page, f"window.__ca?.box({node}, 'click')")
        if not box:
            raise StalePage("The menu's trigger is gone or covered. Observe again.")
        spread = (0.4, 0.6) if self.humanize else (0.5, 0.5)
        await page.mouse.move(box["x"] + box["width"] * random.uniform(*spread),
                              box["y"] + box["height"] * random.uniform(*spread))  # humanized: a curved path

    async def _shows(self, page, node, timeout=1.5, still=2):
        """Wait until a control can be pressed and has stopped moving (a menu fading in, then sliding into place):
        the same place `still` looks in a row after the first, 0.1 s apart. Up to `timeout` seconds. Aiming while it
        still moves lands the press, or the release, on whatever ends up there."""
        last, steady = None, 0
        for _ in range(int(timeout / 0.1)):
            box = await self._eval(page, f"window.__ca?.box({node}, 'click')")
            now = box and tuple(round(box[k]) for k in ("x", "y", "width", "height"))
            steady = steady + 1 if now and now == last else 0
            if steady >= still:
                return True
            last = now
            await asyncio.sleep(0.1)
        return False

    async def _reveal(self, page, node):
        """Bring a control that is out of view back into view with the wheel, in flicks of up to three screens (as a
        person scrolls back up to a header); True once it can be pressed."""
        for _ in range(FIND_FLICKS):
            if await self._eval(page, f"window.__ca?.box({node}, 'click')"):
                return True
            where = await self._eval(page, f"(() => {{ const e = window.__ca?.nodes.get({node}); if (!e?.isConnected) "
                                           f"return null; const r = e.getBoundingClientRect(); "
                                           f"return {{y: r.y, vh: innerHeight, at: scrollY}}; }})()")
            if not where:
                return False
            vh = where["vh"] or 800
            await page.mouse.wheel(0, max(-3 * vh, min(3 * vh, where["y"] - vh * 0.2)))  # to the top fifth
            await asyncio.sleep(0.15)
            if await self._eval(page, "scrollY") == where["at"]:  # the page goes no further: it is covered, not away
                break
        return bool(await self._eval(page, f"window.__ca?.box({node}, 'click')"))

    async def _wheel_to(self, page, y, vh):
        """Scroll in flicks of the wheel (up to three screens each) until page position `y` is near the top."""
        vh = vh or 800
        for _ in range(FIND_FLICKS):
            now = await self._eval(page, "scrollY") or 0
            delta = y - now - vh * 0.2
            if abs(delta) < vh * 0.5:
                break
            await page.mouse.wheel(0, max(-3 * vh, min(3 * vh, delta)))
            await asyncio.sleep(0.15)

    async def _select_from_list(self, page, node, value, frame=None):
        """Choose a native <select> option the way a person does: open the list, arrow to it, press Enter.

        That fires one trusted `change`. Arrow keys on a closed select change the value at every step (Windows), so a
        page that submits its form on `change` would reload before the wanted option is reached.
        """
        plan = await self._eval(page, _SELECT_PLAN % (node, json.dumps(value)), frame)
        if not plan:
            raise StalePage("Dropdown or option is gone. Observe again.")
        await self._press(page, node, frame=frame)  # opens the option list, highlighting the current option
        await asyncio.sleep(random.uniform(0.25, 0.45))
        key = "ArrowDown" if plan["to"] > plan["from"] else "ArrowUp"
        for _ in range(plan["steps"]):  # the list skips disabled options by itself
            await page.keyboard.press(key)
            await asyncio.sleep(random.uniform(0.06, 0.14))
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.15)
        current = await self._eval(page, f"window.__ca?.nodes.get({node})?.selectedIndex", frame)
        # None: the element is gone because the page reacted to the change (e.g. submitted its form).
        if current is not None and current != plan["to"]:
            raise RuntimeError("Dropdown selection was not confirmed; inspect before retrying.")

    async def _find(self, page, words):
        """Go to where `words` appear on this page, the way a person reaches a section of a long document.

        The first heading with the words (else the first text) is the place. If it has an anchor, the tab goes to it,
        as a table-of-contents link would; otherwise the page is scrolled there in screen-sized flicks of the wheel.
        Words that are not on the page change nothing, which the loop then sees.
        """
        place = await self._eval(page, FIND_JS % json.dumps(words or ""))
        if not place:
            return
        if place.get("id"):
            await page.goto(page.url.split("#", 1)[0] + "#" + quote(place["id"], safe=""), wait_until="commit")
            await asyncio.sleep(0.3)
            return
        await self._wheel_to(page, place["y"], place["vh"])

    async def markdown(self, page):
        return await self._eval(page, MARKDOWN_JS) or ""

    async def lists(self, page):
        """Lists of results on the page, best first, each with a selector spec and its first rows (no model)."""
        return await self._eval(page, LISTS_JS) or []

    async def extract(self, page, spec):
        """Rows of a list, read with a saved spec ({item, fields})."""
        return await self._eval(page, EXTRACT_JS % json.dumps(spec)) or []

    async def fetch_json(self, page, request):
        """Repeat a recorded JSON request from inside the page: the tab's own cookies and network, from the isolated
        world, so page scripts don't see it. None when it fails or the answer is not JSON."""
        init = {"method": request.get("method") or "GET", "credentials": "include"}
        if request.get("post_data") is not None and init["method"] != "GET":
            init["body"] = request["post_data"]
            if request.get("content_type"):
                init["headers"] = {"content-type": request["content_type"]}
        expression = ("fetch(%s, %s).then(r => r.ok ? r.json() : null).catch(() => null)"
                      % (json.dumps(request["url"]), json.dumps(init)))
        return await self._eval(page, expression, await_promise=True)

    @staticmethod
    def capture_json(page):
        """Start keeping the JSON answers this tab receives (for finding the request behind a list of results)."""
        return JsonCapture(page)

    async def screenshot(self, page):
        """PNG of the visible part of the tab, through raw CDP. Not page.screenshot(): Playwright first sets an inline
        caret-color on every input, textarea and contenteditable (and restores it), changes the page can observe."""
        self.check_open(page)
        cdp = await page.context.new_cdp_session(page)
        try:
            shot = await cdp.send("Page.captureScreenshot", {"format": "png"})
        finally:
            await cdp.detach()
        return base64.b64decode(shot["data"])

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
                "form_values", "frames"]


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


# Called on an <iframe> element in its parent's isolated world: does each point (parent coordinates) hit it?
_OWNER_HITS = """function(points) {
  const at = (x, y) => { let t = document.elementFromPoint(x, y);
    while (t?.shadowRoot) { const i = t.shadowRoot.elementFromPoint(x, y); if (!i || i === t) break; t = i; }
    return t; };
  return points.map(([x, y]) => at(x, y) === this);
}"""

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
