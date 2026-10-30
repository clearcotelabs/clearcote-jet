"""Local benchmark tasks: one or two per fixture in bench/fixtures/, served by bench/server.py.

Each check judges the world (what the fixture POSTed to /api/done/<id>, i.e. ``ctx.record``, or the page DOM),
never Jet's own status. See bench/CONTRACT.md.
"""

from __future__ import annotations

import datetime as _dt

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


# ---------------------------------------------------------------- helpers

def _rec(ctx) -> dict:
    r = getattr(ctx, "record", None)
    return r if isinstance(r, dict) else {}


def _no_record(ctx):
    return (False, "nothing was submitted") if not _rec(ctx) else None


def _expect(rec: dict, **want):
    """(ok, reason) comparing record fields to exact expected values."""
    for k, v in want.items():
        got = rec.get(k)
        if got != v:
            return False, f"{k}={got!r}, expected {v!r}"
    return True, "ok"


def _norm(s) -> str:
    return " ".join(str(s or "").split()).casefold()


async def _visible_text(page) -> str:
    try:
        return await page.inner_text("body", timeout=3000)
    except Exception:
        return ""


async def _answer_text(ctx) -> str:
    md = ""
    res = getattr(ctx, "result", None) or {}
    if isinstance(res, dict):
        md = str(res.get("markdown") or "") + "\n" + str(res.get("detail") or "")
    return md + "\n" + await _visible_text(ctx.page)


async def _attr(page, selector: str, name: str):
    try:
        el = await page.query_selector(selector)
        return await el.get_attribute(name) if el else None
    except Exception:
        return None


def _url(ctx) -> str:
    try:
        return ctx.page.url
    except Exception:
        return ""


def _next_month_dates():
    today = _dt.date.today()
    first = (today.replace(day=1) + _dt.timedelta(days=32)).replace(day=1)
    return first.replace(day=14), first.replace(day=17)


def _long(d: _dt.date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


CHECKIN, CHECKOUT = _next_month_dates()


# ---------------------------------------------------------------- checks

async def check_login(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), email="ana.silva@example.com", password="Hunter2!x", remember=True)


async def check_signup(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), first="Lucas", last="Bakker", email="lucas.bakker@example.org",
                                      username="lbakker88", password="Tulip-2026-Rain", confirm="Tulip-2026-Rain",
                                      country="Netherlands", terms=True, marketing=False)


async def check_wizard(ctx):
    r = _rec(ctx)
    if not r:
        return False, "no order placed"
    if str(r.get("postcode", "")).replace(" ", "").upper() != "1016GV":
        return False, f"postcode={r.get('postcode')!r}"
    return _expect(r, name="Emma de Jong", street="Prinsengracht 263", city="Amsterdam", country="NL",
                   delivery="express", payment="paypal")


async def check_phone(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), name="Jan de Vries", phone="+31612345678", time="afternoon")


async def check_disabled(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), name="Harbor Analytics", slug="harbor-analytics",
                                      region="eu-central-1", plan="pro", ack=True)


async def check_dropdowns(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), country="BE", language="nl", date_format="DD/MM/YYYY",
                                      display_name="Robin Claes")


async def check_autocomplete(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), from_code="ASD", to_code="UT")


async def check_datepicker(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), room="canal-view-double", checkin=CHECKIN.isoformat(),
                                      checkout=CHECKOUT.isoformat(), adults=2)


async def check_pizza(ctx):
    r = _rec(ctx)
    if not r:
        return False, "nothing added to the order"
    if sorted(r.get("toppings") or []) != ["mushrooms", "olives"]:
        return False, f"toppings={r.get('toppings')!r}"
    return _expect(r, size="large", crust="thin")


async def check_cookie(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), article="City council approves new bike lanes on Damrak")


async def check_modal(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), product="trail-runner-2", color="ocean-blue", size="42", qty="1")


async def check_search(ctx):
    if "article=refunds-annual" not in _url(ctx):
        return False, f"not on the annual refunds article: {_url(ctx)}"
    if "45 days" not in await _answer_text(ctx):
        return False, "answer '45 days' not found"
    return True, "ok"


async def check_pagination(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), name="Walnut Desk Organizer", sku="SKU-123")


async def check_pagination_price(ctx):
    if "page=3" not in _url(ctx):
        return False, f"not on page 3: {_url(ctx)}"
    if "34.95" not in await _answer_text(ctx):
        return False, "price 34.95 not found"
    return True, "ok"


async def check_infinite(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), id="J5034", title="Senior Rust Engineer", city="Rotterdam")


async def check_tabs(ctx):
    if await _attr(ctx.page, "#t3", "aria-selected") != "true":
        return False, "Shipping & Returns tab not open"
    if "DHL Express" not in await _answer_text(ctx):
        return False, "answer 'DHL Express' not found"
    return True, "ok"


async def check_accordion(ctx):
    if await _attr(ctx.page, "#q4", "aria-expanded") != "true":
        return False, "the pets question is not expanded"
    if "€250" not in await _answer_text(ctx) and "250" not in await _answer_text(ctx):
        return False, "answer 250 not found"
    return True, "ok"


async def check_hover(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), plan="premium", page="support-plans")


async def check_spinner(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), invoice="INV-2026-03")


async def check_spa(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), weekly_digest=False, product_updates=True, mentions=True,
                                      push_comments=True, push_reminders=False)


async def check_iframe(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), first="Sofia", last="Rossi", email="sofia.rossi@example.it",
                                      company="Rossi Logistica", size="51-200")


async def check_shadow(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), code="SPRING25", applied=True)


async def check_sticky(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), event="e10", response="going")


async def check_long(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), doc="annual-2025")


async def check_table_archive(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), archived=["1043"])


async def check_table_city(ctx):
    if await _attr(ctx.page, "#drawer", "data-order") != "1043":
        return False, "details of order #1043 are not open"
    if "Haarlem" not in await _answer_text(ctx):
        return False, "answer 'Haarlem' not found"
    return True, "ok"


async def check_toast(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), resent=["maria.lopez@acme.io"])


async def check_confirm(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), deleted=["Office"])


async def check_slider(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), budget=750, daily_cap=40, pause_weekends=False)


async def check_editor(ctx):
    r = _rec(ctx)
    if not r:
        return False, "note not saved"
    if r.get("title") != "Groceries":
        return False, f"title={r.get('title')!r}"
    if _norm(r.get("text")).rstrip(".") != "buy oat milk and eggs":
        return False, f"text={r.get('text')!r}"
    return True, "ok"


async def check_load_more(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), review="r11", name="Pieter K.")


async def check_icon(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), starred=["Q3-budget.xlsx"])


async def check_similar(ctx):
    r = _rec(ctx)
    if not r:
        return False, "nothing saved or published"
    if _norm(r.get("body")).rstrip(".") != "our new office opens in october":
        return False, f"body={r.get('body')!r}"
    return _expect(r, action="publish", title="Autumn update")


async def check_german(ctx):
    return _no_record(ctx) or _expect(_rec(ctx), vorname="Max", nachname="Müller", strasse="Hauptstraße 5", plz="10115",
                                      ort="Berlin", email="max.mueller@example.de", versand="express",
                                      zahlung="rechnung", agb=True)


# ---------------------------------------------------------------- tasks

TASKS = [
    {"id": "local-login-remember", "url": "/login.html", "tags": ["form", "login", "checkbox"], "check": check_login,
     "goal": "Sign in with the email ana.silva@example.com and the password Hunter2!x, and tick Remember me."},
    {"id": "local-signup-full", "url": "/signup.html", "tags": ["form", "signup", "many-fields", "terms-checkbox", "select"],
     "check": check_signup,
     "goal": "Create an account for Lucas Bakker with the email lucas.bakker@example.org, username lbakker88 and the "
             "password Tulip-2026-Rain (also in the confirm password field), country Netherlands. Accept the terms "
             "and do not sign up for marketing emails."},
    {"id": "local-checkout-wizard", "url": "/checkout-wizard.html", "tags": ["form", "wizard", "multi-step", "radio"],
     "check": check_wizard, "max_steps": 35,
     "goal": "Check out with delivery to Emma de Jong, Prinsengracht 263, postcode 1016 GV, Amsterdam. Choose Express "
             "delivery, pay with PayPal and place the order."},
    {"id": "local-validation-phone", "url": "/callback.html", "tags": ["form", "validation-error", "select"],
     "check": check_phone,
     "goal": "Request a callback for Jan de Vries in the afternoon. My phone number is 0612345678; if the form wants "
             "the international format, it is +31612345678."},
    {"id": "local-disabled-submit", "url": "/create-project.html", "tags": ["form", "disabled-until-valid", "select", "radio"],
     "check": check_disabled,
     "goal": "Create a new project named Harbor Analytics with the slug harbor-analytics in the region Frankfurt "
             "(eu-central-1) on the Pro plan, and confirm the billing notice."},
    {"id": "local-dropdowns", "url": "/settings-dropdowns.html", "tags": ["select", "custom-dropdown"],
     "check": check_dropdowns,
     "goal": "In the profile settings, set Country to Belgium and Language to Nederlands, then save the changes."},
    {"id": "local-autocomplete", "url": "/train-search.html", "tags": ["autocomplete", "combobox", "async-suggestions"],
     "check": check_autocomplete,
     "goal": "Search for trains from Amsterdam Centraal to Utrecht Centraal."},
    {"id": "local-datepicker", "url": "/hotel-dates.html", "tags": ["date-picker", "stepper", "month-navigation"],
     "check": check_datepicker, "max_steps": 35,
     "goal": f"Reserve the Canal View Double room with check-in on {_long(CHECKIN)} and check-out on "
             f"{_long(CHECKOUT)}, for 2 adults."},
    {"id": "local-radio-checkbox", "url": "/pizza-order.html", "tags": ["radio", "checkbox-group", "preselected"],
     "check": check_pizza,
     "goal": "Add a large pizza with thin crust to the order, with only mushrooms and olives as toppings (no extra cheese)."},
    {"id": "local-cookie-wall", "url": "/news-cookie.html", "tags": ["cookie-wall", "overlay"], "check": check_cookie,
     "goal": "Save the article 'City council approves new bike lanes on Damrak' to your reading list."},
    {"id": "local-newsletter-modal", "url": "/shoe-shop.html", "tags": ["delayed-modal", "overlay", "swatch", "size-buttons"],
     "check": check_modal,
     "goal": "Add the Trail Runner 2 in the colour Ocean Blue, size 42, to the cart."},
    {"id": "local-search-results", "url": "/help-search.html", "tags": ["search", "results", "read-value"],
     "check": check_search,
     "goal": "In the help center, search for refund, open the article 'Refunds for annual plans' and tell me within "
             "how many days you can request a refund."},
    {"id": "local-pagination", "url": "/catalog.html", "tags": ["pagination", "list"], "check": check_pagination,
     "goal": "Add the Walnut Desk Organizer from the home accessories catalog to the wishlist."},
    {"id": "local-pagination-price", "url": "/catalog.html", "tags": ["pagination", "read-value"],
     "check": check_pagination_price,
     "goal": "Find the Walnut Desk Organizer in the home accessories catalog and tell me its price."},
    {"id": "local-infinite-scroll", "url": "/jobs-feed.html", "tags": ["infinite-scroll", "lazy-load", "similar-items"],
     "check": check_infinite, "max_steps": 40,
     "goal": "Find the Senior Rust Engineer job in Rotterdam and click Apply on it."},
    {"id": "local-tabs", "url": "/product-tabs.html", "tags": ["tabs", "read-value"], "check": check_tabs,
     "goal": "On the Aurora Desk Lamp page, open the Shipping & Returns tab and tell me which carrier handles "
             "express delivery."},
    {"id": "local-accordion", "url": "/faq.html", "tags": ["accordion", "read-value"], "check": check_accordion,
     "goal": "In the FAQ, open the question about pets and tell me how much the pet deposit is."},
    {"id": "local-hover-menu", "url": "/hover-menu.html", "tags": ["hover-menu", "navigation"], "check": check_hover,
     "goal": "Go to the Support Plans page in the Services menu and request a quote for the Premium plan."},
    {"id": "local-spinner", "url": "/billing-spinner.html", "tags": ["spinner", "delayed-content", "table"],
     "check": check_spinner,
     "goal": "In billing, click Email invoice for the March 2026 invoice INV-2026-03."},
    {"id": "local-spa-views", "url": "/spa-settings.html", "tags": ["spa", "no-url-change", "switch"],
     "check": check_spa,
     "goal": "Go to Settings, then Notifications, turn off the Weekly digest email and save the preferences."},
    {"id": "local-iframe-form", "url": "/iframe-form.html", "tags": ["iframe", "form", "select"], "check": check_iframe,
     "goal": "Fill in the contact sales form for Sofia Rossi, email sofia.rossi@example.it, company Rossi Logistica, "
             "team size 51-200, and submit it."},
    {"id": "local-shadow-dom", "url": "/shadow-cart.html", "tags": ["shadow-dom", "web-component"], "check": check_shadow,
     "goal": "Apply the discount code SPRING25 to the basket."},
    {"id": "local-sticky-header", "url": "/events-sticky.html", "tags": ["sticky-header", "occlusion", "similar-items"],
     "check": check_sticky,
     "goal": "RSVP Going for the Team offsite in Zandvoort on 12 December."},
    {"id": "local-long-page", "url": "/annual-report.html", "tags": ["long-page", "scroll", "similar-items"],
     "check": check_long,
     "goal": "On the investor relations page, request a printed copy of the Annual Report 2025."},
    {"id": "local-table-archive", "url": "/orders-table.html", "tags": ["table", "row-menu", "confirm-dialog"],
     "check": check_table_archive,
     "goal": "Archive the order #1043."},
    {"id": "local-table-details", "url": "/orders-table.html", "tags": ["table", "row-menu", "drawer", "read-value"],
     "check": check_table_city,
     "goal": "Open the details of order #1043 and tell me which city it ships to."},
    {"id": "local-toast", "url": "/team-invites.html", "tags": ["toast", "undo", "similar-items"], "check": check_toast,
     "goal": "Resend the pending invitation to maria.lopez@acme.io."},
    {"id": "local-native-confirm", "url": "/address-book.html", "tags": ["native-confirm", "dialog", "similar-items"],
     "check": check_confirm,
     "goal": "Delete the saved address named Office."},
    {"id": "local-slider", "url": "/budget-slider.html", "tags": ["slider", "range"], "check": check_slider,
     "goal": "Set the monthly ad budget to 750 euros and save the budget."},
    {"id": "local-contenteditable", "url": "/notes-editor.html", "tags": ["contenteditable", "rich-text"],
     "check": check_editor,
     "goal": "Write a note with the title Groceries and the text Buy oat milk and eggs, then save it."},
    {"id": "local-load-more", "url": "/reviews-loadmore.html", "tags": ["load-more", "similar-items"],
     "check": check_load_more,
     "goal": "Mark the review by Pieter K. as helpful."},
    {"id": "local-icon-button", "url": "/files-icons.html", "tags": ["icon-button", "aria-label", "similar-items"],
     "check": check_icon,
     "goal": "Star the file Q3-budget.xlsx."},
    {"id": "local-similar-buttons", "url": "/blog-editor.html", "tags": ["similar-buttons", "form"],
     "check": check_similar,
     "goal": "Write a blog post titled Autumn update with the text Our new office opens in October, and publish it "
             "now (do not just save a draft)."},
    {"id": "local-german-checkout", "url": "/kasse.html", "tags": ["non-english", "form", "radio", "terms-checkbox"],
     "check": check_german,
     "goal": "Complete the order on this German checkout page for Max Müller, Hauptstraße 5, 10115 Berlin, email "
             "max.mueller@example.de, with express shipping and payment by invoice, accepting the terms."},
]
