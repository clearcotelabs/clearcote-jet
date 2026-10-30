# Jet stuck-task benchmark — shared contract

Goal: measure where Jet (clearcote_jet) gets stuck, on many realistic tasks, end to end in a real Clearcote
browser, with an objective pass/fail check per task that does not trust Jet's own `done`.

## Task modules

`bench/tasks_local.py` and `bench/tasks_real.py` each export `TASKS: list[Task]`, where `Task` is a dict:

```python
{
  "id": "local-login-3-fields",          # unique, kebab-case, prefixed local- or real-
  "url": "/login.html",                  # local: a path served by bench/server.py; real: an absolute https URL
  "goal": "Log in as ana@example.com with the password Hunter2! and ...",   # plain words, values written in
  "tags": ["form", "login"],             # what the task exercises (pattern names, for the failure report)
  "check": check_fn,                     # async def check_fn(ctx) -> tuple[bool, str]
  "max_steps": 25,                       # optional, default 30
}
```

`ctx` (built by bench/run.py) has:
- `ctx.result`  — Jet's result dict (status, detail, url, title, trace, markdown, items)
- `ctx.page`    — the Playwright page the task ran in, still open (read its URL/DOM; do not act on it)
- `ctx.record`  — local tasks: the JSON the fixture POSTed to the server for this task id (or None); real: None
- `ctx.base`    — local tasks: the server origin, e.g. "http://127.0.0.1:8765"

A check returns `(passed, short_reason)`. It must judge the WORLD (server record, final URL, DOM state, answer text),
never Jet's `status`. For "find/read" tasks it checks the answer: `ctx.result["markdown"]` or the page text must
contain the expected value.

## Local server (bench/server.py)

stdlib only (http.server, ThreadingHTTPServer). Serves `bench/fixtures/` as static files and:
- `POST /api/done/<task_id>`  body: JSON  → stored in memory (last write wins); returns 200 `{"ok":true}`
- `GET  /api/done/<task_id>`  → the stored JSON or `null`
- `POST /api/reset`           → clears all records
- Any path the fixtures need for realism (e.g. `/api/search?q=` returning JSON after a delay, `/api/slow` with
  `?ms=`), documented in the module docstring.
`start(port=0) -> (server, base_url)` runs it in a background thread; `python bench/server.py` runs it in the foreground.

A fixture reports success by `fetch('/api/done/<task_id>', {method:'POST', body: JSON.stringify({...})})` with what
was submitted (field values, chosen options), so the check can compare against the goal's values.

## Rules
- No external network in fixtures (no CDNs, fonts, analytics). Plain HTML/CSS/JS, realistic looking (not toy-bare):
  real sites have headers, nav, footers, extra links and noise that compete for attention.
- Each fixture file is self-contained. Name it after the task (`bench/fixtures/<short-name>.html`).
- Goals are written the way a user would type them, with the values in them.
- Don't import from or modify `clearcote_jet/`. Don't run Jet. Don't commit.
