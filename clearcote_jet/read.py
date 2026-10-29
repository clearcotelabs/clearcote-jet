"""Reading named values off a page from a description of them, so a watcher does not need selectors.

    "the monthly price in euros"  ->  {"price": "#buybox .a-price .a-offscreen"}

The model never writes a selector. The page offers the candidates it can be read from (candidates.js,
each one already proved to find itself again), a cheap filter drops the ones the field's TYPE rules
out, and the model only chooses among what is left. What comes back is therefore always a selector
that works on the page it was found on — the open question is only whether it is the right one.

That matters for what this is for: the selectors are kept, and every later read uses them with no
model at all. The model is the cost of the first read, not of every read.
"""

import json
import os
import re
import time

from .model import DECIDE_URL, post_json, validate_choice

# A money field is not asking for "4 reviews". A boolean is not asking for a paragraph. Filtering by
# the type the customer already told us costs nothing and takes the candidate list from everything on
# the page to a handful — which is most of the price of a decision, since the model is billed on what
# it is shown.
CURRENCY = r"[$€£¥₹₽₺₩]|\b(?:eur|usd|gbp|chf|pln|sek|nok|dkk|czk|huf|ron|rub|uah|inr|jpy|aud|cad|zar|try|brl|mxn|kr)\b"
MONEY = re.compile(rf"(?:{CURRENCY})\s?\d|\d[\d.,\s ]*\s?(?:{CURRENCY})", re.I)
NUMBER = re.compile(r"\d")
# Words a yes/no field is usually hiding behind, in the languages the agent already speaks elsewhere.
BOOLEAN = re.compile(
    r"\b(in stock|out of stock|sold out|available|unavailable|add to (?:cart|basket)|buy now|order now|"
    r"pre-?order|back ?order|in voorraad|uitverkocht|niet op voorraad|bestellen|in den warenkorb|"
    r"ausverkauft|verfügbar|nicht verfügbar|en stock|rupture|ajouter au panier|disponible|agotado|"
    r"añadir al carrito|disponibile|esaurito|aggiungi al carrello|yes|no)\b",
    re.I,
)
MAX_CANDIDATES = 24
MAX_TEXT = 120


def _fits(candidate, kind):
    """Could this candidate hold a value of that type at all?"""
    text = candidate.get("text") or ""
    if kind == "money":
        return bool(MONEY.search(text))
    if kind == "number":
        return bool(NUMBER.search(text)) and len(text) <= 60
    if kind == "boolean":
        return bool(BOOLEAN.search(text)) and len(text) <= 80
    if kind == "url":
        return False  # a link is read from an attribute, not from text: not a candidate for this
    return True  # text: anything on the page


def shortlist(candidates, field, limit=MAX_CANDIDATES):
    """The candidates worth showing the model for one field, best-looking first.

    Type first, because it is the customer's own statement about the value. Then the words of the
    description: a candidate whose own text or selector echoes them is likelier than one that does
    not, and ordering by that means the shortlist keeps the right one when it has to cut.
    """
    kind = field.get("type") or "text"
    fitting = [c for c in candidates if _fits(c, kind)]
    if not fitting:
        fitting = list(candidates)
    words = {w for w in re.findall(r"[a-z]{3,}", (field.get("ask") or "").lower())} - {
        "the", "and", "for", "with", "from", "this", "that", "page", "value"
    }

    def score(c):
        hay = f"{c.get('text', '')} {c.get('sel', '')}".lower()
        return (-sum(1 for w in words if w in hay), len(c.get("text") or ""))

    return sorted(fitting, key=score)[:limit]


def _criteria(shortlisted):
    """What the model is shown for one field: the text, and the selector when it says something."""
    out = {}
    for c in shortlisted:
        label = (c.get("text") or "")[:MAX_TEXT]
        sel = c.get("sel") or ""
        out[str(c["i"])] = f"{label}   [{sel}]" if sel else label
    out["NONE"] = "No candidate holds this value."
    return out


RULES = (
    "Choose the candidate that holds the value the field describes, as a person reading the page would. "
    "Each candidate is the text an element holds, followed by the selector that finds it. "
    "Prefer the element that holds the value ITSELF over one that holds it among other things. "
    "A label is not the value: 'Price' is not the price, '129.95 €' is. "
    "Choose NONE when the page does not show this value at all — a wrong selector is worse than none, "
    "because it will be read on every check from now on."
)


async def pick_selectors(page_state, fields, candidates, note=None):
    """Ask the model which candidate each described field is. One request for all of them.

    `page_state` is what the model sees of the page (url, title); `fields` are the described fields
    ({name, type, ask}); `candidates` come from candidates.js. Returns
    {name: {"sel": str, "text": str, "probability": float}} with only the fields it could place.
    """
    questions = {}
    shortlists = {}
    for field in fields:
        short = shortlist(candidates, field)
        if not short:
            continue
        shortlists[field["name"]] = {str(c["i"]): c for c in short}
        questions[f"field_{field['name']}"] = {
            "type": "choice",
            "criteria": _criteria(short),
            "instructions": {
                "field": field["name"],
                "describes": field.get("ask", ""),
                "type": field.get("type") or "text",
                **({"note": note} if note else {}),
                "rules": RULES,
            },
        }
    if not questions:
        return {}, {}

    body = {"model": os.environ.get("TYPESAFE_MODEL", "jev-latest"), "state": page_state, "questions": questions}
    started = time.perf_counter()
    result = await post_json(DECIDE_URL, os.environ["TYPESAFE_API_KEY"], body)

    chosen = {}
    for field in fields:
        key = f"field_{field['name']}"
        if key not in questions:
            continue
        criteria = questions[key]["criteria"]
        answer = validate_choice(result["answers"].get(key, {}), criteria)
        if answer["choice"] == "NONE":
            continue
        candidate = shortlists[field["name"]][answer["choice"]]
        chosen[field["name"]] = {
            "sel": candidate["sel"],
            "text": candidate["text"],
            "probability": round(answer["probabilities"][answer["choice"]], 3),
        }
    meta = {
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "asked": len(questions),
    }
    return chosen, meta


async def read_values(session, page, want):
    """Work out a selector for every described field on the page this tab is already showing.

    `want`: {url?, note?, fields: [{name, type, ask}], container?: bool, row?: str}
      - `container: true` asks for the repeating row as well, taken from the lists the page offers.
      - `row` (or the container that was found) makes every field selector RELATIVE to one row.

    Returns {"selectors": {...}, "container": str|None, "values": {...}, "model": {...}}.
    """
    fields = [f for f in (want.get("fields") or []) if f.get("ask")]
    container = want.get("row")
    lists = []

    if want.get("container") and not container:
        lists = await session.lists(page)
        if lists:
            # The page's own lists, best first; its rows are what a watcher watches.
            container = lists[0]["item"]

    candidates = await session.candidates(page, row=container)
    state = {"url": page.url, "title": await page.title(), "rows": bool(container)}
    if container:
        state["row_selector"] = container

    selectors, meta = await pick_selectors(state, fields, candidates, note=want.get("note"))

    # Read the values back through the selectors, so what is returned is what a check would read.
    values = {}
    for name, got in selectors.items():
        values[name] = got["text"]

    return {
        "url": page.url,
        "container": container,
        "selectors": {name: got["sel"] for name, got in selectors.items()},
        "confidence": {name: got["probability"] for name, got in selectors.items()},
        "values": values,
        "missing": [f["name"] for f in fields if f["name"] not in selectors],
        "lists": [{"item": lst["item"], "count": lst.get("count")} for lst in lists[:3]],
        "model": meta,
    }


def as_json(result):
    """What the caller reads on stdout: compact, and only what it asked for."""
    return json.dumps(result, ensure_ascii=False, indent=2)
