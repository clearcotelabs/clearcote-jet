"""Two-factor codes (TOTP, RFC 6238), for a {{slot}} whose value is a code that changes every 30 seconds.

A run is given the secret, not a code: the code is worked out when it is typed, so a login that takes a minute to reach
the code field still types one that is valid. In the last seconds of a code's window the next one is typed instead, so
it does not run out between typing and sending (sites accept the code next to the current one).

A secret is base32 as sites show it (spaces and case don't matter) or an otpauth:// link (the QR code's text), or a dict
{"secret", "digits"?, "period"?, "algorithm"?}.
"""

import base64
import hashlib
import hmac
import re
import struct
import time
from urllib.parse import parse_qs, unquote, urlsplit

ALGORITHMS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}
LATE = 3  # seconds before a code's window ends when the next code is typed instead


def parse(spec):
    """-> {"key": bytes, "digits", "period", "algorithm"}. ValueError when it is not a usable secret."""
    if isinstance(spec, dict):
        raw, digits, period, algorithm = (spec.get("secret"), spec.get("digits", 6), spec.get("period", 30),
                                          str(spec.get("algorithm") or "SHA1").upper())
    else:
        raw, digits, period, algorithm = str(spec or ""), 6, 30, "SHA1"
        if raw.strip().lower().startswith("otpauth://"):
            u = urlsplit(raw.strip())
            q = {k.lower(): v[0] for k, v in parse_qs(u.query).items()}
            raw = unquote(q.get("secret", ""))
            digits, period = int(q.get("digits", 6)), int(q.get("period", 30))
            algorithm = q.get("algorithm", "SHA1").upper()
    secret = re.sub(r"[\s-]", "", str(raw or "")).upper().rstrip("=")
    if not secret or not re.fullmatch(r"[A-Z2-7]+", secret):
        raise ValueError("a 2FA secret is base32 (letters A-Z and digits 2-7)")
    if algorithm not in ALGORITHMS:
        raise ValueError(f"unknown 2FA algorithm {algorithm}")
    digits, period = int(digits), int(period)
    if digits not in (6, 7, 8) or not 10 <= period <= 300:
        raise ValueError("a 2FA code has 6 to 8 digits and a period of 10 to 300 seconds")
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    return {"key": key, "digits": digits, "period": period, "algorithm": algorithm}


def at(spec, counter):
    """The code for one time step (RFC 4226 HOTP)."""
    p = spec if isinstance(spec, dict) and "key" in spec else parse(spec)
    mac = hmac.new(p["key"], struct.pack(">Q", counter), ALGORITHMS[p["algorithm"]]).digest()
    offset = mac[-1] & 0x0F
    number = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10 ** p["digits"]).zfill(p["digits"])


def code(spec, now=None):
    """The code to type now: the current one, or the next one in the last LATE seconds of its window."""
    p = spec if isinstance(spec, dict) and "key" in spec else parse(spec)
    now = time.time() if now is None else now
    counter, into = divmod(int(now), p["period"])
    return at(p, counter + 1 if p["period"] - into <= LATE else counter)


def around(spec, now=None):
    """The codes a site may accept now (the window before, this one, the next): what is masked wherever it shows."""
    p = spec if isinstance(spec, dict) and "key" in spec else parse(spec)
    counter = int(time.time() if now is None else now) // p["period"]
    return [at(p, counter + d) for d in (-1, 0, 1)]
