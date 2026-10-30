"""Local fixture server for the Jet stuck-task benchmark (stdlib only).

Serves the static pages in ``bench/fixtures/`` and a few JSON endpoints:

- ``POST /api/done/<task_id>``  body: JSON -> stored in memory (last write wins); returns ``{"ok": true}``
- ``GET  /api/done/<task_id>``  -> the stored JSON, or ``null``
- ``POST /api/reset``           -> clears all records
- ``GET  /api/search?type=station&q=<text>`` -> ``[{"name", "code", "city"}]`` station suggestions matching ``q``,
  answered after a deterministic 300-800 ms delay (depends on ``q``), like a real autocomplete backend.
- ``GET  /api/slow?ms=<n>``     -> ``{"ok": true, "ms": n}`` after ``n`` ms (capped at 10 s); fixtures use it to show a
  spinner for a while before rendering content (infinite scroll, load more, delayed tables).

``start(port=0) -> (server, base_url)`` runs it in a daemon thread; ``python bench/server.py [port]`` runs it in the
foreground (default port 8765).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FIXTURES = Path(__file__).resolve().parent / "fixtures"

STATIONS = [
    ("Amsterdam Centraal", "ASD", "Amsterdam"),
    ("Amsterdam Zuid", "ASDZ", "Amsterdam"),
    ("Amsterdam Sloterdijk", "ASS", "Amsterdam"),
    ("Amsterdam Bijlmer ArenA", "ASB", "Amsterdam"),
    ("Amsterdam Amstel", "ASA", "Amsterdam"),
    ("Amsterdam Muiderpoort", "ASDM", "Amsterdam"),
    ("Utrecht Centraal", "UT", "Utrecht"),
    ("Utrecht Overvecht", "UTO", "Utrecht"),
    ("Utrecht Terwijde", "UTT", "Utrecht"),
    ("Utrecht Vaartsche Rijn", "UTVR", "Utrecht"),
    ("Utrecht Zuilen", "UTZL", "Utrecht"),
    ("Rotterdam Centraal", "RTD", "Rotterdam"),
    ("Rotterdam Blaak", "RTB", "Rotterdam"),
    ("Den Haag Centraal", "GVC", "Den Haag"),
    ("Den Haag HS", "GV", "Den Haag"),
    ("Eindhoven Centraal", "EHV", "Eindhoven"),
    ("Groningen", "GN", "Groningen"),
    ("Haarlem", "HLM", "Haarlem"),
    ("Leiden Centraal", "LEDN", "Leiden"),
    ("Schiphol Airport", "SHL", "Haarlemmermeer"),
]

_records: dict[str, object] = {}
_lock = threading.Lock()


def _search_delay(q: str) -> float:
    return (300 + (sum(map(ord, q)) * 37) % 500) / 1000.0


class Handler(SimpleHTTPRequestHandler):
    server_version = "JetBench/1.0"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(FIXTURES), **kw)

    def log_message(self, fmt, *args):  # quiet
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path.startswith("/api/done/"):
            tid = u.path[len("/api/done/"):]
            with _lock:
                return self._json(_records.get(tid))
        if u.path == "/api/search":
            q = (qs.get("q") or [""])[0].strip().lower()
            time.sleep(_search_delay(q))
            out = []
            if len(q) >= 2:
                for name, code, city in STATIONS:
                    words = name.lower().split()
                    if name.lower().startswith(q) or any(w.startswith(q) for w in words):
                        out.append({"name": name, "code": code, "city": city})
            return self._json(out[:6])
        if u.path == "/api/slow":
            try:
                ms = max(0, min(10000, int((qs.get("ms") or ["500"])[0])))
            except ValueError:
                ms = 500
            time.sleep(ms / 1000.0)
            return self._json({"ok": True, "ms": ms})
        if u.path in ("", "/"):
            names = sorted(p.name for p in FIXTURES.glob("*.html"))
            html = "<!doctype html><title>Fixtures</title><ul>" + "".join(
                f'<li><a href="/{n}">{n}</a></li>' for n in names) + "</ul>"
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        return super().do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if u.path.startswith("/api/done/"):
            tid = u.path[len("/api/done/"):]
            try:
                data = json.loads(raw.decode("utf-8") or "null")
            except ValueError:
                return self._json({"ok": False, "error": "bad json"}, 400)
            with _lock:
                _records[tid] = data
            return self._json({"ok": True})
        if u.path == "/api/reset":
            with _lock:
                _records.clear()
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A page closed mid-request (e.g. during /api/slow) is normal in a benchmark; don't print a traceback.
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def start(port: int = 0):
    server = Server(("127.0.0.1", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    srv = Server(("127.0.0.1", p), Handler)
    print(f"serving {FIXTURES} on http://127.0.0.1:{p}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
