"""An autocomplete made of plain divs, through the real snapshot.js and executor, in Playwright's own Chromium.

The shape this was written for is 12306's station picker, and it is everywhere: a text field, a popup of <div> rows
with no role, no href and no tabindex, and a value that only a click on a row commits — the typed text alone leaves the
station code unset, so a run that cannot click a row can only type the same words again until its budget runs out.

Two things have to hold. The rows must be offered while the field is being typed into (and only then, and only once
each). And the two fields, which both read "Station" to anyone looking at their names alone, must be told apart by what
is written beside them, or a model choosing by label cannot aim at one of them.

No Clearcote, no network, no model calls. Skipped where no Chromium is installed, see conftest.py.
"""

import asyncio

import pytest

from clearcote_jet.browser import Session

# A booking form with two identically named station fields, a suggestion popup of bare divs, an ARIA-correct
# autocomplete next to it (which must not be offered twice), and pointer-cursor cards that are not suggestions at all.
SITE = """<!doctype html><title>Rail bookings</title>
<style>
  body { margin: 0; font: 16px sans-serif }
  .row { display: flex; gap: 20px; padding: 24px }
  .field { position: relative; width: 240px }
  .field input { width: 100%; box-sizing: border-box; padding: 8px }
  .cities { position: absolute; top: 100%; left: 0; width: 240px; background: #fff; border: 1px solid #ccc;
            display: none; z-index: 10 }
  .cities.open { display: block }
  .cityline { padding: 6px 8px; cursor: pointer }
  #hotels { list-style: none; margin: 0; padding: 0; position: absolute; top: 100%; left: 0; width: 240px;
            background: #fff; display: none }
  #hotels.open { display: block }
  #hotels li { cursor: pointer; padding: 6px }
  .card { cursor: pointer; padding: 10px; border: 1px solid #eee }
</style>
<div class="row">
  <div class="field">From<input id="from" placeholder="Station" autocomplete="off">
    <div class="cities" id="from_cities"></div></div>
  <div class="field">To<input id="to" placeholder="Station" autocomplete="off">
    <div class="cities" id="to_cities"></div></div>
  <div class="field">Date<input id="date" placeholder="Departure date" autocomplete="off"></div>
  <div class="field">Hotel<input id="hotel" placeholder="Hotel" autocomplete="off" role="combobox"
      aria-autocomplete="list" aria-controls="hotels">
    <ul class="cities" id="hotels" role="listbox">
      <li role="option" id="hotel_0">Grand Hotel</li><li role="option" id="hotel_1">Grand Palace</li></ul></div>
  <button id="search">Search</button>
</div>
<div class="row">
  <div class="card" id="card_a">Cheap fares</div>
  <div class="card" id="card_b">Travel insurance</div>
</div>
<input type="hidden" id="fromCode"><input type="hidden" id="toCode">
<script>(() => {
  window.picked = [];
  const CITIES = {
    from: [["Chongqingbei", "CQB"], ["Chongqingdong", "CQD"], ["Chongqing", "CQW"]],
    to: [["Chengdudong", "ICW"], ["Chengdu", "CDW"]],
  };
  for (const which of ["from", "to"]) {
    const input = document.getElementById(which);
    const panel = document.getElementById(which + "_cities");
    input.addEventListener("input", () => {
      const typed = input.value.trim().toLowerCase();
      panel.innerHTML = "";
      if (!typed) { panel.classList.remove("open"); return; }
      for (const [name, code] of CITIES[which]) {
        if (!name.toLowerCase().startsWith(typed)) continue;
        const row = document.createElement("div");
        row.className = "cityline";
        // The name in a span of its own: the pointer lands on the span, not on the row that is clickable.
        row.innerHTML = '<span>' + name + '</span>';
        row.addEventListener("click", (e) => {
          // What a station picker really does: the display text AND the code the search needs.
          input.value = name + "(" + code + ")";
          document.getElementById(which + "Code").value = code;
          picked.push([which, code, e.isTrusted]);
          panel.classList.remove("open");
        });
        panel.appendChild(row);
      }
      panel.classList.toggle("open", !!panel.children.length);
    });
    // Pressing inside the popup must not take the focus off the field, or the blur below would close the popup
    // before the click landed. Every picker of this shape does this.
    panel.addEventListener("mousedown", (e) => e.preventDefault());
    // Leaving the field without choosing throws the typed words away, as these pickers do.
    input.addEventListener("blur", () => {
      panel.classList.remove("open");
      if (!document.getElementById(which + "Code").value) input.value = "";
    });
  }
  const hotel = document.getElementById("hotel"), hotels = document.getElementById("hotels");
  hotel.addEventListener("input", () => hotels.classList.toggle("open", !!hotel.value.trim()));
  for (const li of hotels.querySelectorAll("li"))
    li.addEventListener("click", () => { hotel.value = li.textContent; picked.push(["hotel", li.id, true]); });
})()</script>
"""


def labels(state):
    return [a["label"] for a in state["actions"]]


def options(state):
    """The suggestion rows the snapshot offers: clicks on something whose role is option."""
    return [a for a in state["actions"] if a.get("role") == "option" and a["kind"] == "click"]


async def scenario(humanize, chromium):
    async with chromium() as browser:
        session = Session(await browser.new_context(viewport={"width": 1100, "height": 600}), humanize=humanize)
        page = await session.new_tab()
        await page.set_content(SITE)
        seen = {"idle": await session.observe(page)}

        # Type into the departure field exactly as the loop does (click, select all, backspace, type).
        fill = next(a for a in seen["idle"]["actions"] if a["kind"] == "fill" and a["label"] == "From Station")
        await session.act(page, seen["idle"], fill, "Chongqing")
        await page.wait_for_timeout(200)
        seen["typed"] = await session.observe(page)

        # Click the row that names the city itself, and read back what the page stored.
        row = next(a for a in options(seen["typed"]) if a["label"].endswith("Chongqing"))
        await session.act(page, seen["typed"], row, None)
        await page.wait_for_timeout(200)
        seen["after_pick"] = {
            "picked": await page.evaluate("picked"),
            "from": await page.evaluate("document.getElementById('from').value"),
            "code": await page.evaluate("document.getElementById('fromCode').value"),
            "state": await session.observe(page),
        }

        # The ARIA-correct autocomplete beside it: one action per row, not two.
        state = seen["after_pick"]["state"]
        hotel = next(a for a in state["actions"] if a["kind"] == "fill" and a["label"] == "Hotel")
        await session.act(page, state, hotel, "Grand")
        await page.wait_for_timeout(200)
        seen["hotel"] = await session.observe(page)
        return seen


@pytest.fixture(scope="module", params=[False, True], ids=["plain", "humanize"])
def seen(request, chromium):
    return asyncio.run(scenario(request.param, chromium))


# ── telling two identically named fields apart ───────────────────────────────────────────────────


def test_fields_that_read_alike_are_told_apart_by_what_is_beside_them(seen):
    offered = labels(seen["idle"])
    assert "From Station" in offered and "To Station" in offered
    assert "Station" not in offered, "two fields called the same thing cannot be chosen between"
    # The clash is in the name, not in the page: a field whose own name is already unique is left alone.
    assert "Departure date" in offered and "Date Departure date" not in offered
    assert "Open From Station" in offered and "Open To Station" in offered


# ── the suggestion rows ──────────────────────────────────────────────────────────────────────────


def test_nothing_extra_is_offered_before_a_field_is_typed_into(seen):
    assert options(seen["idle"]) == []
    # The pointer-cursor cards further down the page are not suggestions and never become actions.
    assert not any("Cheap fares" in label or "Travel insurance" in label for label in labels(seen["idle"]))


def test_the_rows_of_the_open_popup_are_offered_named_by_their_field(seen):
    offered = [a["label"] for a in options(seen["typed"])]
    assert offered == ["From Station › Chongqingbei", "From Station › Chongqingdong", "From Station › Chongqing"], offered


def test_only_a_popup_counts_as_suggestions_not_whatever_is_under_the_field(seen):
    # The cards below the form are clickable to the eye (pointer cursor, short text) and sit inside the strip this
    # looks at, with a field focused. They are page content in normal flow, not a popup over it: offering them would
    # put the page's furniture in every decision and cost tokens on every page with a focused field.
    assert not any("Cheap fares" in a["label"] or "Travel insurance" in a["label"] for a in options(seen["typed"]))


def test_each_row_is_offered_once_even_though_the_text_sits_in_a_span(seen):
    nodes = [a["node"] for a in options(seen["typed"])]
    assert len(nodes) == len(set(nodes))


def test_typing_alone_selects_nothing_which_is_why_the_rows_matter(seen):
    # Before the click: the popup is open, the field holds the typed words, and the code is still empty.
    assert options(seen["typed"]), "the rows are the only way to commit the value"


def test_clicking_a_row_commits_the_value_by_real_input(seen):
    after = seen["after_pick"]
    assert after["picked"] == [["from", "CQW", True]], after["picked"]  # isTrusted: a real mouse click
    assert after["from"] == "Chongqing(CQW)"
    assert after["code"] == "CQW"
    # The popup closed with it, so the rows are gone from the next snapshot.
    assert options(after["state"]) == []


def test_an_aria_autocomplete_is_not_offered_twice(seen):
    rows = [a for a in seen["hotel"]["actions"] if "Grand" in a["label"]]
    assert sorted(a["label"] for a in rows) == ["Grand Hotel", "Grand Palace"], [a["label"] for a in rows]
    assert all(a.get("role") == "option" for a in rows)
    assert len({a["node"] for a in rows}) == 2


def test_the_field_itself_says_its_typed_text_is_not_the_value_yet(seen):
    # Without this the model sees a field holding the right words and no reason to look further; the station code
    # behind it is still empty, and only a row sets it.
    field = next(a for a in seen["typed"]["actions"] if a["label"] == "From Station" and a["kind"] == "fill")
    assert field.get("suggestions") == "3"
    assert field["value"] == "Chongqing"
    # Gone once a row is picked.
    picked = next(a for a in seen["after_pick"]["state"]["actions"] if a["label"] == "From Station")
    assert "suggestions" not in picked
    assert picked["value"] == "Chongqing(CQW)"


def test_the_rows_are_part_of_what_makes_the_page_look_changed(seen):
    # The marker is what tells the loop the page moved on; a popup that opens has to show in it, or an action
    # decided on the popup would be dropped as stale (or worse, a change would go unnoticed).
    assert seen["typed"]["marker"] != seen["idle"]["marker"]
