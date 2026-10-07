"""The redirect check on a real browser (Playwright's own headless Chromium): a Location padded with control
characters, which the browser trims and then follows, cannot take a page to a private address. The redirecting page
is on 127.0.0.1, counted as public here; the target is on 127.0.0.2, which stays private."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.async_api import Error as PlaywrightError

from clearcote_jet import egress

# Locations the browser follows to 127.0.0.2 once it has trimmed the control characters and spaces around them.
PADDED = ["\x01http://127.0.0.2:{port}/a", "\x08http://127.0.0.2:{port}/b", "\x1bhttp://127.0.0.2:{port}/c",
          "\x0bhttp://127.0.0.2:{port}/d", "\x0c\x1fhttp://127.0.0.2:{port}/e", " \x01http://127.0.0.2:{port}/f\x01 ",
          "\x01//127.0.0.2:{port}/g", "\x1b\\\\127.0.0.2:{port}/h"]
# Not trimmed by the browser: read as a path on the redirecting host (U+007F, and a no-break space sent as UTF-8; the
# header value goes out as latin-1), so the browser stays there.
KEPT = ["\x7fhttp://127.0.0.2:{port}/i", "\xc2\xa0http://127.0.0.2:{port}/j"]


def serve(handler, host):
    srv = ThreadingHTTPServer((host, 0), handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv


def page(title):
    return f"<!doctype html><html><head><title>{title}</title></head><body><p>{title}</p></body></html>".encode()


def test_a_redirect_padded_with_control_characters_cannot_reach_a_private_address(chromium, monkeypatch):
    hits = []

    class Private(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(page("Private"))

        def log_message(self, *_a):
            pass

    private = serve(Private, "127.0.0.2")
    locations = [loc.format(port=private.server_address[1]) for loc in PADDED + KEPT]

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/to/") and self.path[4:].isdigit():
                self.send_response(302)
                self.send_header("Location", locations[int(self.path[4:])])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(page("Public"))

        def log_message(self, *_a):
            pass

    public = serve(Redirect, "127.0.0.1")
    base = f"http://127.0.0.1:{public.server_address[1]}"
    real, allowed = egress.host_refusal, {"127.0.0.1"}

    async def narrowed(host):  # the real check, with the redirecting server counted as public
        return None if host in allowed else await real(host)
    monkeypatch.setattr(egress, "host_refusal", narrowed)

    async def go():
        async with chromium() as browser:
            context = await browser.new_context()
            await egress.guard_context(context)
            tab = await context.new_page()
            await egress.guard_redirects(context, tab)

            async def follow(index):
                hits.clear()
                try:
                    await tab.goto(f"{base}/to/{index}", wait_until="load")
                except PlaywrightError:
                    pass
                await asyncio.sleep(0.3)
                return list(hits), tab.url

            allowed.add("127.0.0.2")  # the control: with 127.0.0.2 allowed, the browser does follow a padded Location
            control = await follow(0)
            allowed.discard("127.0.0.2")
            results = [await follow(index) for index in range(len(locations))]
            await context.close()
            return control, results

    try:
        control, results = asyncio.run(go())
    finally:
        for srv in (private, public):
            srv.shutdown()
            srv.server_close()
    assert control[0] == ["/a"], f"control: the browser follows a padded Location to 127.0.0.2 ({control})"
    for location, (reached, url) in zip(locations, results):
        assert reached == [], f"{location!r} reached the private server ({url})"
    for location, (_, url) in zip(locations[len(PADDED):], results[len(PADDED):]):
        assert url.startswith(base + "/"), f"{location!r}: the browser reads it as a path here, not {url}"
