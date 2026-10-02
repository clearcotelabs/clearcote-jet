"""The request behind a list: when the rows a page shows came from a JSON response, that request can read them again
without the clicks that led to it.

find_shortcut matches the rows read from the page against the JSON responses seen while the run went there, and
returns the request plus where the rows and each field sit in its answer. rows_from_json reads rows from a new answer.
"""

import re
from urllib.parse import urljoin, urlsplit

from .describe import norm


def number(value):
    """12.5 from 12.5, '€ 12,50', '1.234,50 EUR', '$1,234.50' or '12 500 zł'; None when there is no number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    found = re.search(r"-?\d[\d.,\s ']*", str(value or ""))
    if not found:
        return None
    t = re.sub(r"[\s ']", "", found.group(0)).rstrip(".,")
    if re.fullmatch(r"-?\d{1,3}([.,]\d{3})+", t):  # 1.234 / 1,234: a thousands separator
        t = re.sub(r"[.,]", "", t)
    elif "," in t and "." in t:  # the last one is the decimal point
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    else:
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def dig(obj, path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _scalars(obj, path=(), depth=0):
    """(path, value) for every text or number in an object, through nested objects (not lists), 3 levels deep."""
    if isinstance(obj, dict) and depth < 3:
        for key, value in obj.items():
            yield from _scalars(value, path + (key,), depth + 1)
    elif isinstance(obj, (str, int, float)) and not isinstance(obj, bool):
        yield path, obj


def _arrays(obj, path=(), depth=0):
    """(path, list) for every list of objects in a JSON document, 6 levels deep."""
    if depth > 6:
        return
    if isinstance(obj, list):
        if len(obj) >= 3 and sum(isinstance(x, dict) for x in obj) >= 0.8 * len(obj):
            yield path, obj
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _arrays(value, path + (key,), depth + 1)


def _same_text(x, want, base):
    return isinstance(x, str) and norm(x) == norm(want)


def _same_link(x, want, base):
    if not isinstance(x, str) or len(x) < 4:
        return False
    if urljoin(base, x) == want:
        return True
    path = urlsplit(want).path
    return len(x) >= 6 and ("/" in x or "-" in x) and (want.endswith(x) or (len(path) > 6 and x.endswith(path)))


def _same_number(x, want, base):
    a, b = number(x), number(want)
    return a is not None and b is not None and abs(a - b) < 0.01


MATCH = {"title": _same_text, "link": _same_link, "price": _same_number}


def find_shortcut(captured, rows, base):
    """The JSON request whose answer holds these rows, with where the rows and fields sit in it; None if none does.

    captured: [{url, method, post_data, content_type, body}] seen during the run (latest last). rows: the list read
    from the page (title, link, price). Each field's place must match at least 60% of the rows that have it.
    """
    rows = [r for r in rows[:10] if r.get("title")]
    if len(rows) < 3:
        return None
    for cap in reversed(captured or []):
        for path, items in _arrays(cap.get("body")):
            fields = {}
            for field, same in MATCH.items():
                have = [r[field] for r in rows if r.get(field)]
                if not have:
                    continue
                counts = {}
                for want in have:
                    hit = set()
                    for item in items:
                        for p, x in _scalars(item):
                            if same(x, want, base):
                                hit.add(p)
                    for p in hit:
                        counts[p] = counts.get(p, 0) + 1
                if counts:
                    p, n = max(counts.items(), key=lambda kv: kv[1])
                    if n >= 0.6 * len(have):
                        fields[field] = list(p)
            if "title" in fields:
                return {"url": cap["url"], "method": cap.get("method") or "GET", "post_data": cap.get("post_data"),
                        "content_type": cap.get("content_type"), "items_path": list(path), "fields": fields,
                        "base": base}
    return None


def rows_from_json(body, shortcut):
    """Rows from a new answer to the shortcut's request, read the way the learned run read them."""
    items = dig(body, shortcut["items_path"])
    if not isinstance(items, list):
        return []
    out = []
    for item in items[:50]:
        if not isinstance(item, dict):
            continue
        row = {}
        for field, path in shortcut["fields"].items():
            value = dig(item, path)
            if value is None or isinstance(value, (dict, list)):
                continue
            row[field] = urljoin(shortcut["base"], str(value)) if field == "link" else str(value)
        if row:
            out.append(row)
    return out
