"""Real-site tasks for the Jet stuck-task benchmark (see bench/CONTRACT.md).

Every site here is either built for automation/scraping practice (the-internet.herokuapp.com, demoqa.com,
books/quotes.toscrape.com, practicetestautomation.com, saucedemo.com) or a stable public reference site
(Wikipedia, docs.python.org, gov.uk). No CAPTCHAs, no real accounts, no purchases; the logins use the
demo credentials the sites publish on the login page itself.

Checks judge the world: final URL, DOM state read from ``ctx.page``, or the answer text
(``ctx.result["markdown"]`` plus the page's visible text). They never look at Jet's ``status``.
bench/selfcheck_real.py proves each check passes after the right actions and fails on the start page.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import re
import urllib.request
from urllib.parse import unquote, urlparse

# --------------------------------------------------------------------------------------------- helpers


def _norm(s: str | None) -> str:
    s = (s or "").replace(" ", " ").replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", s).strip().lower()


async def _page_text(ctx) -> str:
    try:
        return await ctx.page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        return ""


async def _answer(ctx) -> str:
    """Jet's answer markdown plus what the page visibly shows, normalised."""
    md = (ctx.result or {}).get("markdown") or ""
    return _norm(md + "\n" + await _page_text(ctx))


def _url(ctx) -> str:
    try:
        return ctx.page.url or ""
    except Exception:
        return (ctx.result or {}).get("url") or ""


def _path(ctx) -> str:
    return unquote(urlparse(_url(ctx)).path)


async def _js(ctx, expr: str, default=None, arg=None):
    try:
        return await ctx.page.evaluate(expr, arg)
    except Exception:
        return default


async def _text_of(ctx, selector: str) -> str:
    """Visible text of the first element matching selector ('' if missing or hidden)."""
    return _norm(await _js(ctx, """(sel) => {
        const e = document.querySelector(sel);
        if (!e) return '';
        const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
        if (cs.display === 'none' || cs.visibility === 'hidden' || (r.width === 0 && r.height === 0)) return '';
        return e.innerText;
    }""", "", selector))


async def _rows(ctx) -> list[str]:
    """Visible table rows (real <tr> or react-table .rt-tr), each normalised."""
    rows = await _js(ctx, """() => [...document.querySelectorAll('tr, .rt-tr')]
        .map(r => r.innerText).filter(t => t && t.trim())""", []) or []
    return [_norm(r) for r in rows]


def _row_has(rows: list[str], *values: str) -> bool:
    vals = [_norm(v) for v in values]
    for r in rows:
        cells = set(re.split(r"[\t ]+", r))
        if all((v in cells) if " " not in v else (v in r) for v in vals):
            return True
    return False


def _ok(cond: bool, good: str, bad: str) -> tuple[bool, str]:
    return (True, good) if cond else (False, bad)


# ------------------------------------------------------------------- the-internet.herokuapp.com

HK = "https://the-internet.herokuapp.com"


async def check_hk_login(ctx):
    flash = await _text_of(ctx, "#flash")
    return _ok(_path(ctx).rstrip("/").endswith("/secure") and "you logged into a secure area" in flash,
               "on /secure with the success flash", f"url={_url(ctx)} flash={flash[:80]!r}")


async def check_hk_dropdown(ctx):
    v = await _js(ctx, "() => { const s = document.querySelector('#dropdown'); return s ? s.value : null; }")
    return _ok(v == "2", "Option 2 selected", f"dropdown value={v!r}")


async def check_hk_checkboxes(ctx):
    st = await _js(ctx, "() => [...document.querySelectorAll('#checkboxes input[type=checkbox]')].map(c => c.checked)", [])
    return _ok(st == [True, True], "both boxes checked", f"states={st}")


async def check_hk_dynload1(ctx):
    fin = await _text_of(ctx, "#finish")
    return _ok("hello world!" in fin and "hello world" in await _answer(ctx),
               "Hello World! revealed", f"#finish visible text={fin!r}")


async def check_hk_dynload2(ctx):
    fin = await _text_of(ctx, "#finish h4")
    return _ok("hello world!" in fin, "Hello World! rendered", f"#finish h4={fin!r}")


async def check_hk_hovers(ctx):
    return _ok(_path(ctx).rstrip("/") == "/users/2", "on /users/2", f"url={_url(ctx)}")


async def check_hk_iframe(ctx):
    try:
        body = await ctx.page.frame_locator("#mce_0_ifr").locator("body").inner_text(timeout=5000)
    except Exception as e:
        return False, f"editor frame not readable: {e.__class__.__name__}"
    b = _norm(body)
    return _ok("jet was here" in b and "your content goes here" not in b,
               "editor text replaced", f"editor text={b[:80]!r}")


async def check_hk_confirm(ctx):
    r = await _text_of(ctx, "#result")
    return _ok(r == "you clicked: cancel", "confirm dismissed", f"#result={r!r}")


async def check_hk_prompt(ctx):
    r = await _text_of(ctx, "#result")
    return _ok(r == "you entered: hello jet", "prompt answered", f"#result={r!r}")


async def check_hk_add_remove(ctx):
    n = await _js(ctx, "() => document.querySelectorAll('#elements .added-manually').length", -1)
    return _ok(n == 3, "exactly 3 Delete buttons", f"{n} added elements")


async def check_hk_keypress(ctx):
    r = await _text_of(ctx, "#result")
    return _ok(r == "you entered: escape", "ESCAPE registered", f"#result={r!r}")


async def check_hk_slider(ctx):
    r = await _text_of(ctx, "#range")
    return _ok(r == "3.5", "slider at 3.5", f"#range={r!r}")


async def check_hk_dyncontrols(ctx):
    st = await _js(ctx, """() => { const i = document.querySelector('#input-example input[type=text]');
        return i ? [i.disabled, i.value] : null; }""")
    return _ok(bool(st) and st[0] is False and _norm(st[1]) == "jet bench",
               "input enabled with the text", f"input [disabled, value]={st}")


# ------------------------------------------------------------------------------------- demoqa.com

DQ = "https://demoqa.com"


async def check_dq_textbox(ctx):
    out = _norm(await _js(ctx, "() => { const o = document.querySelector('#output'); return o ? o.innerText : ''; }", ""))
    need = ["name:mila jansen", "email:mila@example.com", "keizersgracht 12, amsterdam", "damrak 1, amsterdam"]
    missing = [n for n in need if n not in out]
    return _ok(not missing, "submitted output shows all values", f"missing in #output: {missing}")


async def check_dq_practice_form(ctx):
    rows = await _js(ctx, """() => { const m = document.querySelector('.modal-content');
        if (!m) return null;
        const o = {}; m.querySelectorAll('tbody tr').forEach(tr => { const td = tr.querySelectorAll('td');
            if (td.length === 2) o[td[0].innerText.trim()] = td[1].innerText.trim(); });
        return o; }""")
    if not rows:
        return False, "no submission modal"
    want = {"Student Name": "ana jet", "Gender": "female", "Mobile": "0612345678",
            "Date of Birth": "15 march,1990", "Subjects": "maths", "Hobbies": "reading"}
    bad = {k: rows.get(k) for k, v in want.items() if _norm(rows.get(k)).replace(", ", ",") != v}
    return _ok(not bad, "modal shows all submitted values", f"wrong/missing: {bad}")


async def check_dq_webtables_add(ctx):
    rows = await _rows(ctx)
    return _ok(_row_has(rows, "lena", "jet", "31", "lena@example.com", "5500", "qa"),
               "new row present", "no row with Lena Jet 31 lena@example.com 5500 QA")


async def check_dq_webtables_edit(ctx):
    rows = await _rows(ctx)
    return _ok(_row_has(rows, "alden", "cantrell", "15000") and not _row_has(rows, "alden", "cantrell", "12000"),
               "Alden's salary is 15000", "Alden Cantrell row does not show salary 15000")


async def check_dq_select(ctx):
    old = _norm(await _js(ctx, """() => { const s = document.querySelector('#oldSelectMenu');
        return s && s.selectedIndex >= 0 ? s.options[s.selectedIndex].text : ''; }""", ""))
    title = _norm(await _js(ctx, """() => { const c = document.querySelector('#selectOne');
        const v = c && c.querySelector('[class*=singleValue]'); return v ? v.innerText : ''; }""", ""))
    return _ok(old == "purple" and title == "dr.", "Purple + Dr. chosen", f"old select={old!r} title={title!r}")


async def check_dq_tabs(ctx):
    sel = await _js(ctx, "() => { const t = document.querySelector('#demo-tab-use'); return t && t.getAttribute('aria-selected'); }")
    return _ok(sel == "true", "Use tab active", f"#demo-tab-use aria-selected={sel!r}")


async def check_dq_accordion(ctx):
    exp = await _js(ctx, """() => { const b = [...document.querySelectorAll('.accordion-button')]
        .find(x => /where does it come from/i.test(x.innerText)); return b ? b.getAttribute('aria-expanded') : null; }""")
    ans = await _answer(ctx)
    return _ok(exp == "true" and re.search(r"\b45\s*b\.?c", ans) is not None,
               "section opened and 45 BC read", f"section expanded={exp!r}, '45 BC' in answer={'45 bc' in ans}")


# ------------------------------------------------------------------------------- toscrape sites

BOOKS = "https://books.toscrape.com"
QUOTES = "https://quotes.toscrape.com"


async def check_books_mystery_p2(ctx):
    return _ok("38.38" in await _answer(ctx), "price 38.38 found", "answer lacks 38.38")


async def check_quotes_login(ctx):
    n = await _js(ctx, "() => document.querySelectorAll('a[href=\"/logout\"]').length", 0)
    return _ok(n > 0, "logged in (Logout link)", "no Logout link")


async def check_quotes_tag(ctx):
    return _ok("nietzsche" in await _answer(ctx), "Nietzsche named", "answer lacks Nietzsche")


async def check_quotes_page3(ctx):
    return _ok("pablo neruda" in await _answer(ctx), "Pablo Neruda named", "answer lacks Pablo Neruda")


async def check_quotes_js_p2(ctx):
    on_p2 = _path(ctx).rstrip("/") == "/js/page/2"
    return _ok(on_p2 and "marilyn monroe" in await _answer(ctx), "on /js/page/2, Monroe named",
               f"url={_url(ctx)}")


async def check_quotes_scroll(ctx):
    n = await _js(ctx, "() => document.querySelectorAll('.quote').length", 0)
    return _ok(n > 10 and "douglas adams" in await _answer(ctx), "scrolled past 10 and found Adams",
               f"{n} quotes loaded")


async def check_quotes_search(ctx):
    res = await _js(ctx, """() => [...document.querySelectorAll('.results .quote')].map(q => {
        const a = q.querySelector('.author'), t = q.querySelector('.tag');
        return [a ? a.innerText : '', t ? t.innerText : '']; })""", []) or []
    ok = bool(res) and all(_norm(a) == "albert einstein" and _norm(t) == "success" for a, t in res)
    return _ok(ok, f"{len(res)} Einstein/success result(s)", f"results={res}")


# -------------------------------------------------------------------------------- reference sites

async def check_wiki_brandenburg(ctx):
    on = "brandenburg_gate" in _path(ctx).lower()
    return _ok(on and "1791" in await _answer(ctx), "on the article, 1791 read", f"url={_url(ctx)}")


async def check_wiki_ggb(ctx):
    on = "golden_gate_bridge" in _path(ctx).lower()
    a = await _answer(ctx)
    date = re.search(r"may 27,? 1937|27 may,? 1937|1937-05-27", a) is not None
    return _ok(on and date, "on the article, May 27, 1937 read", f"url={_url(ctx)} date found={date}")


async def check_pydocs_groupby(ctx):
    u = _url(ctx)
    return _ok("/library/itertools.html" in u and u.endswith("#itertools.groupby"),
               "at itertools.groupby", f"url={u}")


async def check_pydocs_version(ctx):
    return _ok(_path(ctx).startswith("/3.12/library/json.html"), "json docs for 3.12", f"url={_url(ctx)}")


def _next_scotland_holiday() -> _dt.date:
    with urllib.request.urlopen("https://www.gov.uk/bank-holidays.json", timeout=20) as r:
        data = json.load(r)
    today = _dt.date.today()
    dates = sorted(_dt.date.fromisoformat(e["date"]) for e in data["scotland"]["events"])
    return next(d for d in dates if d >= today)


async def check_govuk_scotland(ctx):
    try:
        d = await asyncio.to_thread(_next_scotland_holiday)
    except Exception as e:
        return False, f"could not read gov.uk JSON: {e.__class__.__name__}"
    a = await _answer(ctx)
    month = d.strftime("%B").lower()
    forms = [f"{d.day} {month}", f"{month} {d.day}", d.isoformat(), f"{d.day}/{d.month}/{d.year}",
             f"{d.day:02d}/{d.month:02d}/{d.year}"]
    hit = any(re.search(rf"(?<!\d){re.escape(f)}(?!\d)", a) for f in forms)
    # England's next date differs (St Andrew's Day etc. are Scotland-only), so the date alone tells them apart.
    return _ok(hit and "/bank-holidays" in _url(ctx), f"answer has {d.isoformat()}", f"expected {d.isoformat()} in answer, found={hit}")


async def check_pta_login(ctx):
    t = await _answer(ctx)
    return _ok("logged-in-successfully" in _url(ctx) and "logged in successfully" in t,
               "on the success page", f"url={_url(ctx)}")


async def check_saucedemo_cart(ctx):
    badge = await _text_of(ctx, ".shopping_cart_badge")
    btn = await _js(ctx, "() => !!document.querySelector('#remove-sauce-labs-backpack')", False)
    return _ok(badge == "1" and btn, "backpack in cart (badge 1)", f"badge={badge!r} backpack remove button={btn}")


# --------------------------------------------------------------------------------------- the tasks

TASKS = [
    {"id": "real-hk-login", "url": f"{HK}/login", "tags": ["form", "login", "password"],
     "goal": "Log in with username tomsmith and password SuperSecretPassword!",
     "check": check_hk_login, "max_steps": 15},
    {"id": "real-hk-dropdown", "url": f"{HK}/dropdown", "tags": ["select", "native-dropdown"],
     "goal": "Choose Option 2 in the dropdown list.", "check": check_hk_dropdown, "max_steps": 8},
    {"id": "real-hk-checkboxes", "url": f"{HK}/checkboxes", "tags": ["checkbox", "already-set-state"],
     "goal": "Make sure both checkbox 1 and checkbox 2 are checked.", "check": check_hk_checkboxes, "max_steps": 8},
    {"id": "real-hk-dynamic-loading-hidden", "url": f"{HK}/dynamic_loading/1", "tags": ["wait", "hidden-element", "read"],
     "goal": "Click Start, wait for the loading to finish and tell me the text that appears.",
     "check": check_hk_dynload1, "max_steps": 10},
    {"id": "real-hk-dynamic-loading-rendered", "url": f"{HK}/dynamic_loading/2", "tags": ["wait", "late-render", "read"],
     "goal": "Click Start, wait until the result is rendered and tell me what it says.",
     "check": check_hk_dynload2, "max_steps": 10},
    {"id": "real-hk-hovers", "url": f"{HK}/hovers", "tags": ["hover-reveal", "navigation"],
     "goal": "Open the profile of the second user (user2) in the hover gallery.", "check": check_hk_hovers,
     "max_steps": 10},
    {"id": "real-hk-iframe-editor", "url": f"{HK}/iframe", "tags": ["iframe", "rich-text-editor", "replace-text"],
     "goal": "In the editor, replace the existing text with: Jet was here", "check": check_hk_iframe, "max_steps": 12},
    {"id": "real-hk-js-confirm", "url": f"{HK}/javascript_alerts", "tags": ["js-dialog", "confirm"],
     "goal": "Click the 'Click for JS Confirm' button and press Cancel in the dialog.",
     "check": check_hk_confirm, "max_steps": 8},
    {"id": "real-hk-js-prompt", "url": f"{HK}/javascript_alerts", "tags": ["js-dialog", "prompt", "type"],
     "goal": "Click the 'Click for JS Prompt' button and enter hello jet in the prompt, then OK.",
     "check": check_hk_prompt, "max_steps": 8},
    {"id": "real-hk-add-remove", "url": f"{HK}/add_remove_elements/", "tags": ["repeat-click", "count"],
     "goal": "Click Add Element exactly three times so there are three Delete buttons.",
     "check": check_hk_add_remove, "max_steps": 10},
    {"id": "real-hk-key-press", "url": f"{HK}/key_presses", "tags": ["keyboard", "special-key"],
     "goal": "Press the Escape key in the input box on the key presses page.", "check": check_hk_keypress,
     "max_steps": 8},
    {"id": "real-hk-slider", "url": f"{HK}/horizontal_slider", "tags": ["slider", "range-input"],
     "goal": "Set the horizontal slider to 3.5.", "check": check_hk_slider, "max_steps": 12},
    {"id": "real-hk-dynamic-controls", "url": f"{HK}/dynamic_controls", "tags": ["disabled-control", "wait", "type"],
     "goal": "Enable the text field and type jet bench into it.", "check": check_hk_dyncontrols, "max_steps": 12},

    {"id": "real-dq-text-box", "url": f"{DQ}/text-box", "tags": ["form", "textarea", "ads"],
     "goal": ("Fill in the text box form: full name Mila Jansen, email mila@example.com, current address "
              "Keizersgracht 12, Amsterdam, permanent address Damrak 1, Amsterdam, then submit it."),
     "check": check_dq_textbox, "max_steps": 15},
    {"id": "real-dq-practice-form", "url": f"{DQ}/automation-practice-form",
     "tags": ["form", "radio", "date-picker", "autocomplete", "checkbox", "ads"],
     "goal": ("Fill in the student registration form: first name Ana, last name Jet, gender Female, mobile "
              "0612345678, date of birth 15 March 1990, subject Maths, hobby Reading, then submit it."),
     "check": check_dq_practice_form, "max_steps": 35},
    {"id": "real-dq-webtables-add", "url": f"{DQ}/webtables", "tags": ["modal-form", "table", "add-row"],
     "goal": ("Add a new record to the table: first name Lena, last name Jet, email lena@example.com, age 31, "
              "salary 5500, department QA."), "check": check_dq_webtables_add, "max_steps": 20},
    {"id": "real-dq-webtables-edit", "url": f"{DQ}/webtables", "tags": ["table", "row-action", "edit", "replace-text"],
     "goal": "Change Alden Cantrell's salary in the table to 15000.", "check": check_dq_webtables_edit,
     "max_steps": 15},
    {"id": "real-dq-select-menu", "url": f"{DQ}/select-menu", "tags": ["select", "custom-dropdown", "native-dropdown"],
     "goal": "Choose Purple in the Old Style Select Menu and Dr. in the Select Title dropdown.",
     "check": check_dq_select, "max_steps": 15},
    {"id": "real-dq-tabs", "url": f"{DQ}/tabs", "tags": ["tabs"],
     "goal": "Open the Use tab.", "check": check_dq_tabs, "max_steps": 6},
    {"id": "real-dq-accordion", "url": f"{DQ}/accordian", "tags": ["accordion", "expand", "read"],
     "goal": ("Open the 'Where does it come from?' section and tell me from which year the Latin literature "
              "it mentions dates."), "check": check_dq_accordion, "max_steps": 8},

    {"id": "real-books-mystery-page2", "url": f"{BOOKS}/", "tags": ["category-nav", "pagination", "read"],
     "goal": "In the Mystery category, find the book 'In the Woods' and tell me its price.",
     "check": check_books_mystery_p2, "max_steps": 15},
    {"id": "real-quotes-login", "url": f"{QUOTES}/", "tags": ["login", "form", "password"],
     "goal": "Log in to Quotes to Scrape with username jet and password bench123.",
     "check": check_quotes_login, "max_steps": 12},
    {"id": "real-quotes-tag", "url": f"{QUOTES}/", "tags": ["tag-filter", "read"],
     "goal": "Show the quotes tagged friendship and tell me who wrote the first one.",
     "check": check_quotes_tag, "max_steps": 12},
    {"id": "real-quotes-page3", "url": f"{QUOTES}/", "tags": ["pagination", "read"],
     "goal": "Go to page 3 of the quotes and tell me the author of the first quote there.",
     "check": check_quotes_page3, "max_steps": 12},
    {"id": "real-quotes-js-page2", "url": f"{QUOTES}/js/", "tags": ["js-rendered", "pagination", "read"],
     "goal": "Go to page 2 of the quotes and tell me who wrote the first quote on it.",
     "check": check_quotes_js_p2, "max_steps": 12},
    {"id": "real-quotes-infinite-scroll", "url": f"{QUOTES}/scroll", "tags": ["infinite-scroll", "read"],
     "goal": ("Scroll the list until you find the quote that starts 'I may not have gone where I intended to go' "
              "and tell me who said it."), "check": check_quotes_scroll, "max_steps": 20},
    {"id": "real-quotes-search-form", "url": f"{QUOTES}/search.aspx", "tags": ["dependent-dropdowns", "form", "select"],
     "goal": "Use the search form to show the quotes by Albert Einstein with the tag success.",
     "check": check_quotes_search, "max_steps": 12},

    {"id": "real-wiki-brandenburg-gate", "url": "https://en.wikipedia.org/wiki/Main_Page",
     "tags": ["site-search", "infobox", "read"],
     "goal": "Search Wikipedia for the Brandenburg Gate and tell me the year its construction was completed.",
     "check": check_wiki_brandenburg, "max_steps": 12},
    {"id": "real-wiki-golden-gate-bridge", "url": "https://en.wikipedia.org/wiki/Main_Page",
     "tags": ["site-search", "infobox", "read"],
     "goal": "Search Wikipedia for the Golden Gate Bridge and tell me the date it opened.",
     "check": check_wiki_ggb, "max_steps": 12},
    {"id": "real-pydocs-search-groupby", "url": "https://docs.python.org/3/",
     "tags": ["site-search", "search-results", "anchor"],
     "goal": "Search the Python documentation for groupby and open the itertools.groupby entry.",
     "check": check_pydocs_groupby, "max_steps": 12},
    {"id": "real-pydocs-version-switch", "url": "https://docs.python.org/3/library/json.html",
     "tags": ["select", "version-switcher", "navigation"],
     "goal": "Switch this json module page to the Python 3.12 version of the docs.",
     "check": check_pydocs_version, "max_steps": 8},
    {"id": "real-govuk-scotland-bank-holiday", "url": "https://www.gov.uk/bank-holidays",
     "tags": ["tabs", "cookie-banner", "read"],
     "goal": "When is the next bank holiday in Scotland?", "check": check_govuk_scotland, "max_steps": 10},
    {"id": "real-pta-login", "url": "https://practicetestautomation.com/practice-test-login/",
     "tags": ["login", "form", "password"],
     "goal": "Log in with username student and password Password123.", "check": check_pta_login, "max_steps": 10},
    {"id": "real-saucedemo-add-to-cart", "url": "https://www.saucedemo.com/",
     "tags": ["login", "multi-step", "add-to-cart"],
     "goal": "Log in with username standard_user and password secret_sauce, then add the Sauce Labs Backpack to the cart.",
     "check": check_saucedemo_cart, "max_steps": 15},
]

assert len({t["id"] for t in TASKS}) == len(TASKS), "duplicate task id"
