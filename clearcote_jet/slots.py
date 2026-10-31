"""Goals with {{slots}}: one task written once and run with different values (an account per browser profile).

A goal such as "Log in as {{email}} with password {{password}}" is a template; `values` fill its slots. The model is
given the goal with the plain slots filled in (Jet only types words the goal contains) and every secret slot left as
its token, `{{password}}`: where the model chooses to type that token, the keys pressed are the secret's, and the
model, the trace, the result and a saved skill only ever hold the token. Every request to the model is also swept for
a secret's value (a page that echoes it back), see model.post_json.

A skill learned from a template is keyed on the template, not on the values, and keeps a typed value as its slot's
token, so the same skill replays for every set of values.
"""

import contextvars
import json
import re

from . import totp

SLOT = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}")
# A value shorter than this is only swapped for its token where it is the whole text (a typed "1" is a slot; every
# "1" in a page line is not).
MIN_INSIDE = 3
# The secrets of the run in progress (value, token): model.post_json takes them out of every request.
SECRETS = contextvars.ContextVar("clearcote_jet_secrets", default=())


class MissingValue(Exception):
    """A slot the task needs has no value; str() is the slot's name."""


def token(name):
    return "{{" + name + "}}"


def slots(text):
    """The slot names in `text`, in order, each once."""
    return list(dict.fromkeys(SLOT.findall(text or "")))


def canonical(text):
    """The template with its tokens written one way: '{{ email }}' and '{{email}}' are the same slot."""
    return SLOT.sub(lambda m: token(m.group(1)), text or "")


class Values:
    """The values for one run of a template; the names in `secret` are never shown to the model or written down."""

    def __init__(self, values=None, secret=(), live=None):
        """live: {slot: 2FA secret} (totp.py). Such a slot's value is the code at the moment it is used, and it is
        always secret."""
        plain = {str(k): "" if v is None else str(v) for k, v in (values or {}).items()}
        self.live = {str(k): totp.parse(v) for k, v in (live or {}).items()}
        self.values = _LiveValues(plain, self.live)
        self.secret = {str(s) for s in secret or ()} | set(self.live)

    def __bool__(self):
        return bool(self.values)

    def missing(self, names):
        return [n for n in names if n not in self.values]

    def for_model(self, template):
        """What the model is given: plain slots filled in, secret slots (and slots with no value) left as tokens."""
        return SLOT.sub(lambda m: self.values[m.group(1)] if m.group(1) in self.values and m.group(1) not in self.secret
                        else token(m.group(1)), template or "")

    def fill_static(self, text):
        """fill(), but a live slot's token stays: its code is worked out when it is typed (fill() then)."""
        if text is None or not self.live:
            return self.fill(text)
        return SLOT.sub(lambda m: m.group(0) if m.group(1) in self.live else self.fill(m.group(0)), text)

    def fill(self, text):
        """The keys to press for `text`: every token replaced by its value. MissingValue for a token with none."""
        if text is None:
            return None

        def value(m):
            if m.group(1) not in self.values:
                raise MissingValue(m.group(1))
            return self.values[m.group(1)]
        return SLOT.sub(value, text)

    def show(self, text):
        """`text` as a person may read it: plain tokens filled in, secret tokens kept."""
        if text is None:
            return None
        return SLOT.sub(lambda m: self.values[m.group(1)] if m.group(1) in self.values and m.group(1) not in self.secret
                        else m.group(0), text)

    def _pairs(self, names):
        pairs = [(v, token(n)) for n in names for v in (totp.around(self.live[n]) if n in self.live else
                                                         [self.values.get(n)]) if v]
        return sorted(pairs, key=lambda p: -len(p[0]))  # the longest first, so one value inside another stays whole

    def templatize(self, text, names):
        """`text` with the values of slots `names` written as their tokens: the whole text when it is one value,
        and any value of MIN_INSIDE characters or more inside it."""
        if not isinstance(text, str) or not text:
            return text
        pairs = self._pairs(names)
        for value, tok in pairs:
            if text == value:
                return tok
        for value, tok in pairs:
            if len(value) >= MIN_INSIDE:
                text = text.replace(value, tok)
        return text

    def mask(self, text):
        """`text` with every secret value replaced by its token: for anything that leaves the process."""
        if not isinstance(text, str) or not text:
            return text
        for value, tok in self._pairs(sorted(self.secret)):
            text = text.replace(value, tok)
        return text

    def secrets(self):
        return tuple(self._pairs(sorted(self.secret)))


class _LiveValues(dict):
    """Plain values, and live ones whose value is the 2FA code at the moment it is read."""

    def __init__(self, plain, live):
        super().__init__(plain)
        self.live = live
        for name in live:
            dict.__setitem__(self, name, "")

    def __getitem__(self, name):
        return totp.code(self.live[name]) if name in self.live else dict.__getitem__(self, name)

    def get(self, name, default=None):
        return self[name] if name in self else default


def each_string(obj, fn):
    """`obj` (dicts, lists, tuples of str and other values) with fn applied to every str in it, keys included."""
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, dict):
        return {each_string(k, fn): each_string(v, fn) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(each_string(v, fn) for v in obj)
    return obj


def redact(body, secrets=None):
    """A model request with every secret value of the run in progress written as its token (any depth, any field)."""
    secrets = SECRETS.get() if secrets is None else secrets
    if not secrets:
        return body
    text = json.dumps(body)
    for value, tok in secrets:
        text = text.replace(json.dumps(value)[1:-1], json.dumps(tok)[1:-1])
    return json.loads(text)
