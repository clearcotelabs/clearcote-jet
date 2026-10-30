"""Prove every check in bench/tasks_real.py without Jet.

For each task, in a fresh browser context:
  1. open the start URL and run the check untouched  -> must FAIL
  2. do the correct actions with plain Playwright     -> check must PASS
The ctx is minimal: result = {status, url, markdown = page innerText}, page, record=None, base=None.

    python -I bench/selfcheck_real.py [task-id-substring ...]

Exit code 0 when every selected task behaves; 1 otherwise.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tasks_real import TASKS  # noqa: E402

from playwright.async_api import async_playwright  # noqa: E402

CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe"
NAV_TIMEOUT = 45_000


# ------------------------------------------------------------------------------------------ solvers


async def _settle(page, ms=800):
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
    except Exception:
        pass
    await page.wait_for_timeout(ms)


async def js_click(page, selector):
    await page.locator(selector).first.evaluate("e => { e.scrollIntoView({block: 'center'}); e.click(); }")


async def hk_login(p):
    await p.fill("#username", "tomsmith")
    await p.fill("#password", "SuperSecretPassword!")
    await p.click("button[type=submit]")
    await _settle(p)


async def hk_dropdown(p):
    await p.select_option("#dropdown", "2")


async def hk_checkboxes(p):
    for cb in await p.locator("#checkboxes input[type=checkbox]").all():
        await cb.check()


async def hk_dynload1(p):
    await p.click("#start button")
    await p.wait_for_selector("#finish", state="visible", timeout=20_000)


async def hk_dynload2(p):
    await p.click("#start button")
    await p.wait_for_selector("#finish h4", state="visible", timeout=20_000)


async def hk_hovers(p):
    await p.locator(".figure").nth(1).hover()
    await p.click("a[href='/users/2']")
    await _settle(p)


async def hk_iframe(p):
    body = p.frame_locator("#mce_0_ifr").locator("body")
    await body.click()
    await p.keyboard.press("Control+A")
    await p.keyboard.type("Jet was here")


async def hk_confirm(p):
    p.once("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
    await p.click("button:has-text('Click for JS Confirm')")
    await p.wait_for_timeout(500)


async def hk_prompt(p):
    p.once("dialog", lambda d: asyncio.ensure_future(d.accept("hello jet")))
    await p.click("button:has-text('Click for JS Prompt')")
    await p.wait_for_timeout(500)


async def hk_add_remove(p):
    for _ in range(3):
        await p.click("button:has-text('Add Element')")


async def hk_keypress(p):
    await p.click("#target")
    await p.keyboard.press("Escape")


async def hk_slider(p):
    await p.click("input[type=range]")
    await p.keyboard.press("Home")
    for _ in range(7):
        await p.keyboard.press("ArrowRight")


async def hk_dyncontrols(p):
    await p.click("#input-example button")
    await p.wait_for_selector("#input-example input[type=text]:enabled", timeout=20_000)
    await p.fill("#input-example input[type=text]", "jet bench")


async def dq_textbox(p):
    await p.fill("#userName", "Mila Jansen")
    await p.fill("#userEmail", "mila@example.com")
    await p.fill("#currentAddress", "Keizersgracht 12, Amsterdam")
    await p.fill("#permanentAddress", "Damrak 1, Amsterdam")
    await js_click(p, "#submit")
    await p.wait_for_selector("#output #name", timeout=10_000)


async def dq_practice(p):
    await p.fill("#firstName", "Ana")
    await p.fill("#lastName", "Jet")
    await js_click(p, "label[for=gender-radio-2]")
    await p.fill("#userNumber", "0612345678")
    await p.click("#dateOfBirthInput")
    await p.select_option(".react-datepicker__month-select", "2")
    await p.select_option(".react-datepicker__year-select", "1990")
    await p.click(".react-datepicker__day--015:not(.react-datepicker__day--outside-month)")
    await p.click("#subjectsInput")
    await p.keyboard.type("Maths")
    await p.wait_for_timeout(500)
    await p.keyboard.press("Enter")
    await js_click(p, "label[for=hobbies-checkbox-2]")
    await js_click(p, "#submit")
    await p.wait_for_selector(".modal-content", timeout=10_000)


async def dq_wt_add(p):
    await js_click(p, "#addNewRecordButton")
    for sel, v in [("#firstName", "Lena"), ("#lastName", "Jet"), ("#userEmail", "lena@example.com"),
                   ("#age", "31"), ("#salary", "5500"), ("#department", "QA")]:
        await p.fill(sel, v)
    await js_click(p, "#submit")
    await p.wait_for_timeout(500)


async def dq_wt_edit(p):
    await js_click(p, "#edit-record-2")
    await p.fill("#salary", "15000")
    await js_click(p, "#submit")
    await p.wait_for_timeout(500)


async def dq_select(p):
    await p.select_option("#oldSelectMenu", label="Purple")
    await p.click("#selectOne")
    await p.get_by_text("Dr.", exact=True).first.click()


async def dq_tabs(p):
    await js_click(p, "#demo-tab-use")
    await p.wait_for_timeout(300)


async def dq_accordion(p):
    await p.get_by_role("button", name="Where does it come from?").click()
    await p.wait_for_timeout(800)


async def books_p2(p):
    await p.get_by_role("link", name="Mystery", exact=True).click()
    await _settle(p, 300)
    await p.click("li.next a")
    await _settle(p, 300)


async def quotes_login(p):
    await p.click("a[href='/login']")
    await _settle(p, 300)
    await p.fill("#username", "jet")
    await p.fill("#password", "bench123")
    await p.click("input[type=submit]")
    await _settle(p, 300)


async def quotes_tag(p):
    await p.click("a.tag[href='/tag/friendship/']")
    await _settle(p, 300)


async def quotes_page3(p):
    for _ in range(2):
        await p.click("li.next a")
        await _settle(p, 300)


async def quotes_js_p2(p):
    await p.click("li.next a")
    await _settle(p, 800)


async def quotes_scroll(p):
    for _ in range(10):
        if await p.locator(".quote").count() >= 20:
            break
        await p.mouse.wheel(0, 20_000)
        await p.wait_for_timeout(800)


async def quotes_search(p):
    await p.select_option("#author", label="Albert Einstein")
    await p.wait_for_function("document.querySelectorAll('#tag option').length > 1", timeout=10_000)
    await p.select_option("#tag", label="success")
    await p.click("input[type=submit]")
    await _settle(p, 500)


async def _wiki_search(p, q):
    box = p.locator("input[name=search]:visible").first
    await box.fill(q)
    await box.press("Enter")
    await _settle(p, 800)


async def wiki_bg(p):
    await _wiki_search(p, "Brandenburg Gate")


async def wiki_ggb(p):
    await _wiki_search(p, "Golden Gate Bridge")


async def pydocs_groupby(p):
    box = p.locator("input[name=q]:visible").first
    await box.fill("groupby")
    await box.press("Enter")
    await p.wait_for_selector("a:has-text('itertools.groupby')", timeout=30_000)
    await p.locator("a:has-text('itertools.groupby')").first.click()
    await _settle(p, 500)


async def pydocs_version(p):
    await p.locator("select.version-select:visible").first.select_option("3.12")
    await p.wait_for_url("**/3.12/**", timeout=20_000)
    await _settle(p, 500)


async def govuk_scotland(p):
    rej = p.locator("button:has-text('Reject additional cookies')")
    if await rej.count():
        await rej.first.click()
    await p.click("a[href='#scotland']")
    await p.wait_for_timeout(300)


async def pta_login(p):
    await p.fill("#username", "student")
    await p.fill("#password", "Password123")
    await p.click("#submit")
    await p.wait_for_url("**/logged-in-successfully/**", timeout=20_000)
    await _settle(p, 300)


async def sauce_cart(p):
    await p.fill("#user-name", "standard_user")
    await p.fill("#password", "secret_sauce")
    await p.click("#login-button")
    await p.click("#add-to-cart-sauce-labs-backpack")


SOLVERS = {
    "real-hk-login": hk_login, "real-hk-dropdown": hk_dropdown, "real-hk-checkboxes": hk_checkboxes,
    "real-hk-dynamic-loading-hidden": hk_dynload1, "real-hk-dynamic-loading-rendered": hk_dynload2,
    "real-hk-hovers": hk_hovers, "real-hk-iframe-editor": hk_iframe, "real-hk-js-confirm": hk_confirm,
    "real-hk-js-prompt": hk_prompt, "real-hk-add-remove": hk_add_remove, "real-hk-key-press": hk_keypress,
    "real-hk-slider": hk_slider, "real-hk-dynamic-controls": hk_dyncontrols,
    "real-dq-text-box": dq_textbox, "real-dq-practice-form": dq_practice, "real-dq-webtables-add": dq_wt_add,
    "real-dq-webtables-edit": dq_wt_edit, "real-dq-select-menu": dq_select, "real-dq-tabs": dq_tabs,
    "real-dq-accordion": dq_accordion,
    "real-books-mystery-page2": books_p2, "real-quotes-login": quotes_login, "real-quotes-tag": quotes_tag,
    "real-quotes-page3": quotes_page3, "real-quotes-js-page2": quotes_js_p2,
    "real-quotes-infinite-scroll": quotes_scroll, "real-quotes-search-form": quotes_search,
    "real-wiki-brandenburg-gate": wiki_bg, "real-wiki-golden-gate-bridge": wiki_ggb,
    "real-pydocs-search-groupby": pydocs_groupby, "real-pydocs-version-switch": pydocs_version,
    "real-govuk-scotland-bank-holiday": govuk_scotland, "real-pta-login": pta_login,
    "real-saucedemo-add-to-cart": sauce_cart,
}


# ------------------------------------------------------------------------------------------- runner


async def _ctx(page):
    try:
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        text = ""
    return SimpleNamespace(result={"status": "done", "url": page.url, "title": "", "markdown": text,
                                   "items": [], "trace": []},
                           page=page, record=None, base=None)


async def _open(browser, url):
    context = await browser.new_context(viewport={"width": 1366, "height": 900}, locale="en-GB")
    page = await context.new_page()
    page.set_default_timeout(20_000)
    await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    await page.wait_for_timeout(1500)
    return context, page


async def run_one(browser, task, sem):
    async with sem:
        t0 = time.time()
        out = {"id": task["id"], "untouched": None, "solved": None, "err": None}
        try:
            context, page = await _open(browser, task["url"])
            try:
                out["untouched"] = await task["check"](await _ctx(page))
            finally:
                await context.close()
            context, page = await _open(browser, task["url"])
            try:
                await SOLVERS[task["id"]](page)
                out["solved"] = await task["check"](await _ctx(page))
            finally:
                await context.close()
        except Exception as e:  # noqa: BLE001
            out["err"] = f"{e.__class__.__name__}: {str(e).splitlines()[0][:160]}"
        out["secs"] = round(time.time() - t0, 1)
        return out


async def main(filters):
    tasks = [t for t in TASKS if not filters or any(f in t["id"] for f in filters)]
    missing = [t["id"] for t in tasks if t["id"] not in SOLVERS]
    if missing:
        print("no solver for:", missing)
        return 1
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception:
            browser = await pw.chromium.launch(headless=True, executable_path=CHROME)
        sem = asyncio.Semaphore(4)
        results = await asyncio.gather(*(run_one(browser, t, sem) for t in tasks))
        await browser.close()
    bad = 0
    for r in results:
        ok = (r["err"] is None and r["untouched"] and not r["untouched"][0] and r["solved"] and r["solved"][0])
        bad += not ok
        u = r["untouched"][1] if r["untouched"] else "-"
        s = r["solved"][1] if r["solved"] else "-"
        print(f"{'OK  ' if ok else 'FAIL'} {r['id']:<36} {r['secs']:>5}s  start: {u} | solved: {s}"
              + (f" | ERROR {r['err']}" if r["err"] else ""))
    print(f"\n{len(results) - bad}/{len(results)} tasks: check fails on start page and passes after solving")
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(asyncio.run(main(sys.argv[1:])))
