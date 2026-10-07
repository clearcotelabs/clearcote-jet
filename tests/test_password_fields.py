"""Password fields through the real snapshot.js and executor, in Playwright's own Chromium: offered as fields to type
into, never read. No Clearcote, no network, no model calls. Skipped where no Chromium is installed, see
conftest.py."""

import asyncio
import json

import pytest

from clearcote_jet import model
from clearcote_jet.browser import Session

TYPED = "Zq9-typed-secret"  # what the hosted runner types in place of its CCSECRET_ placeholder
PRESET = "Kx7-preset-secret"  # a value already in the field (a browser's autofill, a page's own default)
FORM = f"""<!doctype html><title>Sign in</title><h1>Sign in</h1><form>
<p><label>Email <input id=email type=email></label></p>
<p><label>Password <input id=pw type=password></label>
   <button type=button id=show onclick="pw.type = pw.type === 'password' ? 'text' : 'password'">Show password</button></p>
<p><label>PIN <input id=pin type=password value="{PRESET}"></label></p>
<p><input type=hidden name=csrf value=hidden-token-value><label>Upload <input type=file></label></p>
<p><button type=button>Sign in</button></p></form>"""


def pick(state, kind, label):
    return next((a for a in state["actions"] if a["kind"] == kind and a["label"] == label), None)


def leaks(state, session=None):
    """Every place a snapshot's content goes: the raw state (with marker, page key and guards), the model's view,
    a typed value's context and the stale-retry diff. The values must be in none of them."""
    view, _, _ = model.page_view(state, [])
    field = pick(state, "fill", "Password")
    out = json.dumps([state, view, field and model.field_context("Sign in", field, state, []),
                      getattr(session, "last_diff", "")])
    return [v for v in (TYPED, PRESET, "hidden-token-value") if v in out]


async def scenario(chromium):
    async with chromium() as browser:
        session = Session(await browser.new_context(), humanize=False)
        page = await session.new_tab()
        await page.set_content(FORM)
        seen = {}

        state = seen["empty"] = await session.observe(page)
        field = pick(state, "fill", "Password")
        await session.act(page, state, field, TYPED)
        seen["typed_in_page"] = await page.input_value("#pw")
        state = seen["filled"] = await session.observe(page, after=field)
        seen["filled_diff"] = await session.fresh(page, seen["empty"]) or session.last_diff
        seen["filled_leaks"] = leaks(state, session)
        seen["markdown"] = await session.markdown(page)

        show = pick(state, "click", "Show password")
        await session.act(page, state, show)
        seen["type_after_show"] = await page.eval_on_selector("#pw", "e => e.type")
        state = seen["shown"] = await session.observe(page, after=show)
        await page.eval_on_selector("#pin", "e => { e.value = ''; }")
        seen["shown_diff"] = await session.fresh(page, state) or session.last_diff
        seen["shown_leaks"] = leaks(state, session)
        seen["cleared"] = await session.observe(page)
        return seen


@pytest.fixture(scope="module")
def seen(chromium):
    return asyncio.run(scenario(chromium))


def test_a_password_field_is_offered_to_type_into_with_no_value(seen):
    field = pick(seen["empty"], "fill", "Password")
    assert field is not None and field["role"] == "textbox" and field["value"] == "" and field["filled"] == "false"
    view, targets, _ = model.page_view(seen["empty"], [])
    assert '"Password" filled=false ops=TYPE_TEXT,CLICK' in "\n".join(view["elements"])
    assert field in targets["TYPE_TEXT"].values()
    assert seen["typed_in_page"] == TYPED  # the executor typed into it


def test_filled_says_whether_a_password_field_holds_anything(seen):
    assert pick(seen["empty"], "fill", "PIN")["filled"] == "true"  # preset value
    assert pick(seen["filled"], "fill", "Password")["filled"] == "true"  # after typing
    assert pick(seen["cleared"], "fill", "PIN")["filled"] == "false"  # emptied by the page
    assert pick(seen["filled"], "fill", "Email").get("filled") is None  # other fields keep their value instead


def test_a_password_value_is_never_read(seen):
    for name in ("empty", "filled", "shown", "cleared"):
        assert not leaks(seen[name]), name
    assert not seen["filled_leaks"] and not seen["shown_leaks"]
    assert TYPED not in seen["markdown"] and PRESET not in seen["markdown"]
    # typing changed the page (the page key saw filled flip), and the diff that explains it carries no value
    assert seen["filled_diff"] and TYPED not in seen["filled_diff"]
    assert seen["shown_diff"] and PRESET not in seen["shown_diff"]


def test_a_shown_password_stays_unread(seen):
    assert seen["type_after_show"] == "text"  # the page turned it into a plain text field
    field = pick(seen["shown"], "fill", "Password")
    assert field["value"] == "" and field["filled"] == "true"


def test_hidden_and_file_inputs_stay_out(seen):
    labels = [a["label"] for a in seen["empty"]["actions"]]
    assert "Upload" not in labels and "csrf" not in json.dumps(seen["empty"])
