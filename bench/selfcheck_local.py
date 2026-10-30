"""Self-check for the local benchmark, without Jet.

For every task in bench/tasks_local.py it:
1. drives the fixture with plain Playwright doing the right actions, then runs the task's check -> must PASS;
2. opens the fixture untouched and runs the check again -> must FAIL.

Run from the repo root:  python -I bench/selfcheck_local.py   (exit code 0 when every task behaves both ways)
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server  # noqa: E402
import tasks_local  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _reset(base):
    req = urllib.request.Request(base + "/api/reset", data=b"{}", method="POST")
    urllib.request.urlopen(req, timeout=10).read()


# ---------------------------------------------------------------- drivers (the right actions per task)

async def d_login(p):
    await p.fill("#email", "ana.silva@example.com")
    await p.fill("#password", "Hunter2!x")
    await p.check("#remember")
    await p.click("button[type=submit]")
    await p.wait_for_selector("text=Welcome back,")


async def d_signup(p):
    for sel, v in [("#first", "Lucas"), ("#last", "Bakker"), ("#email", "lucas.bakker@example.org"),
                   ("#username", "lbakker88"), ("#pw", "Tulip-2026-Rain"), ("#pw2", "Tulip-2026-Rain")]:
        await p.fill(sel, v)
    await p.select_option("#country", "Netherlands")
    await p.check("#terms")
    await p.click("button[type=submit]")
    await p.wait_for_selector("text=Check your inbox")


async def d_wizard(p):
    await p.fill("#name", "Emma de Jong")
    await p.fill("#street", "Prinsengracht 263")
    await p.fill("#postcode", "1016 GV")
    await p.fill("#city", "Amsterdam")
    await p.click("#n1")
    await p.check("input[name=delivery][value=express]")
    await p.click("#n2")
    await p.check("input[name=payment][value=paypal]")
    await p.click("#place")
    await p.wait_for_selector("text=has been placed")


async def d_phone(p):
    await p.fill("#name", "Jan de Vries")
    await p.fill("#phone", "0612345678")
    await p.select_option("#time", "afternoon")
    await p.click("button[type=submit]")
    await p.wait_for_selector("#e-phone:has-text('international format')")
    await p.fill("#phone", "+31612345678")
    await p.click("button[type=submit]")
    await p.wait_for_selector("text=We will call you")


async def d_disabled(p):
    assert await p.is_disabled("#create")
    await p.fill("#pname", "Harbor Analytics")
    await p.fill("#slug", "harbor-analytics")
    await p.select_option("#region", "eu-central-1")
    await p.check("input[name=plan][value=pro]")
    assert await p.is_disabled("#create")
    await p.check("#ack")
    await p.click("#create")
    await p.wait_for_selector("text=Project created")


async def d_dropdowns(p):
    await p.select_option("#country", "BE")
    await p.click("#lang-btn")
    await p.click(".dd-item:has-text('Nederlands')")
    await p.click("#save")
    await p.wait_for_selector("#saved:has-text('Saved')")


async def d_autocomplete(p):
    for field, q, name in [("#from", "Amsterdam", "Amsterdam Centraal"), ("#to", "Utrecht", "Utrecht Centraal")]:
        await p.click(field)
        await p.keyboard.type(q, delay=60)
        k = field[1:]
        await p.click(f"#{k}-s .s:has-text('{name}')", timeout=8000)
    await p.click("#go")
    await p.wait_for_selector("text=Trains from")


async def d_datepicker(p):
    ci, co = tasks_local.CHECKIN, tasks_local.CHECKOUT
    await p.click("#dates")
    await p.click("#next")
    await p.click(f"#grid button[aria-label$=', {tasks_local._long(ci)}']")
    await p.click(f"#grid button[aria-label$=', {tasks_local._long(co)}']")
    await p.click("#apply")
    await p.click("#plus")
    await p.click("[data-room=canal-view-double]")
    await p.wait_for_selector("text=Room held")


async def d_pizza(p):
    await p.check("input[name=size][value=large]")
    await p.check("input[name=crust][value=thin]")
    await p.uncheck("input[value=extra-cheese]")
    await p.check("input[value=mushrooms]")
    await p.check("input[value=olives]")
    await p.click("#add")
    await p.wait_for_selector("#summary:has-text('large')")


async def d_cookie(p):
    await p.click("#reject")
    await p.click("[data-title='City council approves new bike lanes on Damrak']")
    await p.wait_for_selector("text=Added to your reading list")


async def d_modal(p):
    await p.wait_for_selector("#ov:not(.hidden)", timeout=5000)
    await p.click("#x")
    await p.click("[data-color=ocean-blue]")
    await p.click(".sz[data-size='42']")
    await p.click("#add")
    await p.wait_for_selector("text=Added to your cart")


async def d_search(p):
    await p.fill("input[name=q]", "refund")
    await p.click("button[type=submit]")
    await p.click("a:has-text('Refunds for annual plans')")
    await p.wait_for_selector("h1:has-text('Refunds for annual plans')")


async def d_pagination(p):
    await p.click(".pager a:has-text('3')")
    await p.click("[aria-label='Add Walnut Desk Organizer to wishlist']")
    await p.wait_for_selector("text=was added to your wishlist")


async def d_pagination_price(p):
    await p.click(".pager a:has-text('3')")
    await p.wait_for_selector("text=Walnut Desk Organizer")


async def d_infinite(p):
    sel = "[aria-label='Apply for Senior Rust Engineer in Rotterdam']"
    for _ in range(30):
        if await p.query_selector(sel):
            break
        await p.mouse.wheel(0, 4000)
        await p.wait_for_timeout(400)
    await p.click(sel)
    await p.wait_for_selector("text=Application started")


async def d_tabs(p):
    await p.click("#t3")


async def d_accordion(p):
    await p.click("#q4")


async def d_hover(p):
    await p.hover(".dd .top:has-text('Services')")
    await p.click(".menu a:has-text('Support Plans')")
    await p.click("[data-plan=premium]")
    await p.wait_for_selector("text=An account manager")


async def d_spinner(p):
    await p.click("[data-inv='INV-2026-03']", timeout=10000)
    await p.wait_for_selector("text=was emailed")


async def d_spa(p):
    await p.click("#nav button[data-v=settings]")
    await p.click(".sub button[data-s=notifications]")
    await p.click(".switch[data-k=weekly_digest]")
    await p.click("#save")
    await p.wait_for_selector("text=Preferences saved")


async def d_iframe(p):
    f = p.frame_locator("iframe")
    await f.locator("#fn").fill("Sofia")
    await f.locator("#ln").fill("Rossi")
    await f.locator("#em").fill("sofia.rossi@example.it")
    await f.locator("#co").fill("Rossi Logistica")
    await f.locator("#size").select_option("51-200")
    await f.locator("button[type=submit]").click()
    await f.locator("text=Our team will contact you").wait_for()


async def d_shadow(p):
    await p.fill("promo-code-box #code", "SPRING25")
    await p.click("promo-code-box #apply")
    await p.wait_for_selector("promo-code-box .good")


async def d_sticky(p):
    await p.click("[data-id=e10][data-r=going]")
    await p.wait_for_selector("#st-e10:has-text('You are going')")


async def d_long(p):
    await p.click("[data-doc=annual-2025]")
    await p.wait_for_selector("text=has been received")


async def d_table_archive(p):
    await p.click("[aria-label='Actions for order #1043']")
    await p.click(".menu [data-a=archive]")
    await p.click("#ok")
    await p.wait_for_selector("text=Order #1043 was archived")


async def d_table_details(p):
    await p.click("[aria-label='Actions for order #1043']")
    await p.click(".menu [data-a=view]")
    await p.wait_for_selector("#drawer")


async def d_toast(p):
    await p.click("[data-email='maria.lopez@acme.io']")
    await p.wait_for_selector(".toast")


async def d_confirm(p):
    p.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
    await p.click("[aria-label='Delete address Office']")
    await p.wait_for_selector("text=Address Office deleted")


async def d_slider(p):
    await p.focus("#budget")
    for _ in range(9):  # 300 -> 750 in steps of 50, the way a keyboard user does it
        await p.keyboard.press("ArrowRight")
    await p.click("#save")
    await p.wait_for_selector("text=Budget saved")


async def d_editor(p):
    await p.fill("#title", "Groceries")
    await p.click("#ed")
    await p.keyboard.type("Buy oat milk and eggs")
    await p.click("#save")
    await p.wait_for_selector("text=Saved just now")


async def d_load_more(p):
    for _ in range(2):
        await p.click("#more")
        await p.wait_for_selector("#more:not([disabled])", state="attached")
        await p.wait_for_timeout(100)
    await p.click("[aria-label='Mark review by Pieter K. as helpful']", timeout=5000)
    await p.wait_for_selector("text=Thanks for your feedback")


async def d_icon(p):
    await p.click("tr[data-file='Q3-budget.xlsx'] .star")
    await p.wait_for_selector("text=Added to Starred")


async def d_similar(p):
    await p.fill("#t", "Autumn update")
    await p.fill("#b", "Our new office opens in October")
    await p.click("#publish")
    await p.wait_for_selector("text=Your post is live")


async def d_german(p):
    await p.click("#cb-no")
    for sel, v in [("#vorname", "Max"), ("#nachname", "Müller"), ("#strasse", "Hauptstraße 5"), ("#plz", "10115"),
                   ("#ort", "Berlin"), ("#email", "max.mueller@example.de")]:
        await p.fill(sel, v)
    await p.check("input[name=versand][value=express]")
    await p.check("input[name=zahlung][value=rechnung]")
    await p.check("#agb")
    await p.click("#order")
    await p.wait_for_selector("text=Vielen Dank")


DRIVERS = {
    "local-login-remember": d_login, "local-signup-full": d_signup, "local-checkout-wizard": d_wizard,
    "local-validation-phone": d_phone, "local-disabled-submit": d_disabled, "local-dropdowns": d_dropdowns,
    "local-autocomplete": d_autocomplete, "local-datepicker": d_datepicker, "local-radio-checkbox": d_pizza,
    "local-cookie-wall": d_cookie, "local-newsletter-modal": d_modal, "local-search-results": d_search,
    "local-pagination": d_pagination, "local-pagination-price": d_pagination_price, "local-infinite-scroll": d_infinite,
    "local-tabs": d_tabs, "local-accordion": d_accordion, "local-hover-menu": d_hover, "local-spinner": d_spinner,
    "local-spa-views": d_spa, "local-iframe-form": d_iframe, "local-shadow-dom": d_shadow,
    "local-sticky-header": d_sticky, "local-long-page": d_long, "local-table-archive": d_table_archive,
    "local-table-details": d_table_details, "local-toast": d_toast, "local-native-confirm": d_confirm,
    "local-slider": d_slider, "local-contenteditable": d_editor, "local-load-more": d_load_more,
    "local-icon-button": d_icon, "local-similar-buttons": d_similar, "local-german-checkout": d_german,
}


async def _ctx(base, task, page):
    rec = None
    for _ in range(10):
        rec = _get(base, "/api/done/" + task["id"])
        if rec is not None:
            break
        await asyncio.sleep(0.1)
    return SimpleNamespace(result={"status": "done", "detail": "", "url": page.url, "title": "", "trace": [],
                                   "markdown": "", "items": []},
                           page=page, record=rec, base=base)


async def _launch(pw):
    try:
        return await pw.chromium.launch(headless=True)
    except Exception as e:  # browsers not installed for this playwright version
        print(f"bundled chromium failed ({str(e).splitlines()[0]}); using installed Chrome")
        return await pw.chromium.launch(headless=True, executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe")


async def main(only=None):
    srv, base = server.start(0)
    ids = [t["id"] for t in tasks_local.TASKS]
    assert len(ids) == len(set(ids)), "duplicate task ids"
    missing = [i for i in ids if i not in DRIVERS]
    assert not missing, f"no driver for {missing}"
    rows, bad = [], 0
    async with async_playwright() as pw:
        browser = await _launch(pw)
        for task in tasks_local.TASKS:
            if only and task["id"] not in only:
                continue
            t0 = time.time()
            # done right
            _reset(base)
            c = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await c.new_page()
            errs = []
            page.on("pageerror", lambda e: errs.append(str(e)))
            try:
                await page.goto(base + task["url"])
                await DRIVERS[task["id"]](page)
                ok_done, why_done = await task["check"](await _ctx(base, task, page))
            except Exception as e:
                ok_done, why_done = False, f"driver error: {str(e).splitlines()[0]}"
            if errs:
                ok_done, why_done = False, f"page error: {errs[0]}"
            await c.close()
            # untouched
            _reset(base)
            c = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await c.new_page()
            await page.goto(base + task["url"])
            await page.wait_for_timeout(300)
            ok_fresh, why_fresh = await task["check"](await _ctx(base, task, page))
            await c.close()
            good = ok_done and not ok_fresh
            bad += not good
            rows.append((task["id"], ok_done, why_done, ok_fresh, why_fresh, good))
            print(f"{'OK  ' if good else 'FAIL'} {task['id']:<28} done-right={'pass' if ok_done else 'FAIL'} ({why_done})"
                  f" | untouched={'FAIL' if not ok_fresh else 'PASSED!'} ({why_fresh})  {time.time() - t0:.1f}s", flush=True)
        await browser.close()
    srv.shutdown()
    print(f"\n{len(rows) - bad}/{len(rows)} tasks behave correctly (pass when done right, fail when untouched)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(set(sys.argv[1:]) or None)))
