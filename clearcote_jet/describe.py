"""Describing what a step did, so it can be found again; and the word lists the loop and the replay share."""

import re

# Clicks that cannot be taken back: sending, buying, booking, deleting, signing up. With `confirm`, the loop stops
# before one of these and asks. Search, filter and navigation buttons are not on the list.
IRREVERSIBLE = re.compile(
    r"\b(send|submit|post|publish|book|reserve|order(?!\s+by\b)|buy|purchase|pay|checkout|check out|place order|confirm|delete|"
    r"remove|unsubscribe|subscribe|sign up|register|donate|transfer|verzenden|versturen|bestellen|betalen|kopen|"
    r"boeken|verwijderen|senden|absenden|kaufen|bezahlen|buchen|löschen|envoyer|acheter|payer|réserver|supprimer|"
    r"enviar|comprar|pagar|reservar|eliminar|invia|acquista|paga|prenota|elimina)\b", re.I)

# Cookie and consent buttons: a replay skips one that does not show this time.
CONSENT = re.compile(
    r"\b(reject|decline|refuse|deny|necessary|essential|accept|agree|allow|consent|cookies?|got it|dismiss|close|"
    r"weigeren|afwijzen|akkoord|ablehnen|zustimmen|akzeptieren|refuser|accepter|rifiuta|accetta|rechazar|aceptar|"
    r"recusar|aceitar|odrzuć|akceptuj)\b", re.I)


class Quote(str):
    """Text from the page (an element's label, a sample of what changed) that a message quotes: in repr quotes, or as
    it is when `bare`."""

    def __new__(cls, text, bare=False):
        self = super().__new__(cls, text)
        self.bare = bare
        return self


class Said(str):
    """A message made of Jet's own words and Quotes from the page. As a str it reads as it always has; `parts` keeps
    the two apart, so a reply can mark the page's text as the page's (the MCP server fences it) and leave Jet's own
    words as they are."""

    def __new__(cls, *parts):
        self = super().__new__(cls, "".join(
            (str(p) if p.bare else repr(str(p))) if isinstance(p, Quote) else p for p in parts))
        self.parts = parts
        return self


def plain(result):
    """`result` (a run's or a replay's) with its messages as plain str: the detail, the stale retries and a repaired
    skill's reason. What the library hands out; only the MCP server asks for the Said parts (said=True)."""
    out = dict(result)
    if out.get("detail") is not None:
        out["detail"] = str(out["detail"])
    out["stale"] = [str(s) for s in out.get("stale") or []]
    if (out.get("skill") or {}).get("because") is not None:
        out["skill"] = {**out["skill"], "because": str(out["skill"]["because"])}
    return out


def norm(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def shape(text):
    """The label with its numbers blanked out: '213 comments' and '87 comments' have the same shape."""
    return re.sub(r"\d+(?:[.,]\d+)*", "#", norm(text))


def irreversible(action):
    return action.get("kind") == "click" and bool(IRREVERSIBLE.search(action.get("label") or ""))


def target_info(action, actions):
    """How to find this control again later: its place among controls with the same label (and with the same shape
    of label, for labels with numbers that change), plus what a replay needs to repeat it."""
    kind, role = action.get("kind"), action.get("role")
    same = [a for a in actions if a.get("kind") == kind and a.get("role") == role
            and norm(a.get("label")) == norm(action.get("label"))]
    alike = [a for a in actions if a.get("kind") == kind and a.get("role") == role
             and shape(a.get("label")) == shape(action.get("label"))]
    info = {"nth": next((i for i, a in enumerate(same) if a is action), 0),
            "nth_shape": next((i for i, a in enumerate(alike) if a is action), 0)}
    if kind == "select":
        info["value"] = action.get("value")
    if kind == "scroll":
        info["delta"] = action.get("delta")
    if action.get("frame"):
        info["in_frame"] = True
    if action.get("menu") is not None or action.get("panel") is not None:
        info["in_menu"] = True  # in a closed menu or a scroll box: a replay asks for those to find it again
    return info


def named(rows):
    """Real results have names: at least 3 rows, most with a title or a price. Rows of bare links are a wrong list."""
    return len(rows) >= 3 and sum(1 for r in rows if r.get("title") or r.get("price")) >= 0.6 * len(rows)


def best_list(lists):
    """The page's results: the highest-scoring list whose rows have names (None when there is none)."""
    return next((lst for lst in lists or [] if named(lst.get("rows") or [])), None)


def end_lines(before, after, n=5):
    """Lines that appeared during the run (on the final screen, not the first): evidence a replay can look for.
    Short lines without numbers first, as those are least likely to change from one day to the next."""
    seen = set((before or "").split("\n"))
    new = [line.strip() for line in (after or "").split("\n")
           if line.strip() and line not in seen and 4 <= len(line.strip()) <= 120]
    new.sort(key=lambda line: (bool(re.search(r"\d", line)), len(line)))
    return list(dict.fromkeys(new))[:n]
