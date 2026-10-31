"""Two-factor codes: RFC 6238's own test vectors, otpauth links, the next code near the end of a window, a live
{{slot}} worked out when it is typed (in a replay as in the loop), always secret and masked, and the worker's check."""

import asyncio
import base64
import json

import pytest

from clearcote_jet import totp
from clearcote_jet.skills import replay
from clearcote_jet.slots import Values
from clearcote_jet.worker import BadRequest, request_from

# RFC 6238 appendix B: the ASCII keys, at T = 59 s and 1111111109 s, 8 digits.
KEY1 = base64.b32encode(b"12345678901234567890").decode()
KEY256 = base64.b32encode(b"12345678901234567890123456789012").decode()
KEY512 = base64.b32encode(b"1234567890123456789012345678901234567890123456789012345678901234").decode()


@pytest.mark.parametrize("key, algorithm, t, want", [
    (KEY1, "SHA1", 59, "94287082"), (KEY256, "SHA256", 59, "46119246"), (KEY512, "SHA512", 59, "90693936"),
    (KEY1, "SHA1", 1111111109, "07081804"), (KEY256, "SHA256", 1111111109, "68084774"),
    (KEY512, "SHA512", 1111111109, "25091201"), (KEY1, "SHA1", 20000000000, "65353130"),
])
def test_rfc6238_vectors(key, algorithm, t, want):
    p = totp.parse({"secret": key, "digits": 8, "algorithm": algorithm})
    assert totp.at(p, t // 30) == want


def test_secret_forms_and_otpauth_links():
    spaced = " ".join(KEY1[i:i + 4] for i in range(0, len(KEY1), 4)).lower()
    assert totp.parse(spaced)["key"] == b"12345678901234567890"
    link = f"otpauth://totp/Shop:ana%40example.com?secret={KEY1}&issuer=Shop&algorithm=SHA256&digits=8&period=60"
    p = totp.parse(link)
    assert (p["digits"], p["period"], p["algorithm"]) == (8, 60, "SHA256")
    for bad in ("", "not base32!", "otpauth://totp/x?secret=", {"secret": KEY1, "digits": 4},
                {"secret": KEY1, "algorithm": "MD5"}):
        with pytest.raises(ValueError):
            totp.parse(bad)


def test_the_next_code_near_the_end_of_a_window():
    p = totp.parse(KEY1)
    assert totp.code(p, now=60 * 30 + 10) == totp.at(p, 60)        # 20 s left: this window's code
    assert totp.code(p, now=60 * 30 + 27) == totp.at(p, 61)        # 3 s left: the next one
    assert totp.around(p, now=60 * 30 + 5) == [totp.at(p, 59), totp.at(p, 60), totp.at(p, 61)]


def test_a_live_slot_is_secret_worked_out_when_read_and_masked(monkeypatch):
    clock = [1000 * 30 + 1]
    monkeypatch.setattr(totp.time, "time", lambda: clock[0])
    v = Values({"email": "ana@example.com"}, [], live={"otp": KEY1})
    assert "otp" in v.secret and v.missing(["email", "otp"]) == []
    first = v.fill("{{otp}}")
    clock[0] += 30
    assert v.fill("{{otp}}") != first == totp.at(totp.parse(KEY1), 1000)
    assert v.fill_static("{{email}} {{otp}}") == "ana@example.com {{otp}}"
    assert v.for_model("code {{otp}} for {{email}}") == "code {{otp}} for ana@example.com"
    assert v.mask(f"you typed {first}") == "you typed {{otp}}"  # the code before this one is masked too


class Login:
    """A sign-in page with a code field; the code is checked against the time it was typed."""

    def __init__(self, clock):
        self.clock, self.typed = clock, []

    async def observe(self, page, after=None, menus=False):
        acts = [{"id": "e1", "node": 1, "kind": "fill", "role": "textbox", "label": "Code"}]
        return {"url": page.url, "title": "Sign in", "text": "Sign in", "actions": acts, "marker": json.dumps(self.typed)}

    async def act(self, page, state, action, text=None):
        self.typed.append((self.clock[0], text))

    async def act_on_selector(self, page, css, kind, text=None):
        self.typed.append((self.clock[0], text))

    async def fresh(self, page, state, action=None):
        return True

    async def settled_markdown(self, page):
        return ""


class Page:
    url = "https://login.example/"

    async def title(self):
        return "Sign in"

    def is_closed(self):
        return False


def test_a_replay_types_the_code_of_the_moment_it_types(monkeypatch):
    clock = [5000 * 30 + 2]
    monkeypatch.setattr(totp.time, "time", lambda: clock[0])
    real_sleep = asyncio.sleep

    async def wait(s):  # a wait step moves the clock on a whole window, instantly
        clock[0] += s
        await real_sleep(0)
    monkeypatch.setattr("clearcote_jet.skills.asyncio.sleep", wait)
    site = Login(clock)
    skill = {"version": 2, "goal": "sign in with {{otp}}", "start_url": Page.url, "edited": True, "slots": ["otp"],
             "steps": [{"kind": "wait", "seconds": 30}, {"kind": "fill", "role": "textbox", "label": "Code", "text": "{{otp}}"},
                       {"kind": "fill", "selector": "#code", "text": "{{otp}}"}]}
    r = asyncio.run(replay(site, skill, Page(), values=Values({}, [], live={"otp": KEY1})))
    assert r["status"] == "done", r["detail"]
    p = totp.parse(KEY1)
    assert site.typed == [(5001 * 30 + 2, totp.at(p, 5001)), (5001 * 30 + 2, totp.at(p, 5001))]
    assert all("{{otp}}" in str(h.get("text")) or h["kind"] == "wait" for h in r["trace"])
    assert totp.at(p, 5001) not in json.dumps(r)


def test_the_worker_checks_totp_secrets():
    base = {"cmd": "run", "cdp": "http://127.0.0.1:9222", "goal": "sign in"}
    assert request_from({**base, "totp": {"otp": KEY1}})["live"] == {"otp": KEY1}
    assert request_from(base)["live"] is None
    with pytest.raises(BadRequest, match="totp otp"):
        request_from({**base, "totp": {"otp": "nope!"}})
    with pytest.raises(BadRequest, match="object"):
        request_from({**base, "totp": ["x"]})
