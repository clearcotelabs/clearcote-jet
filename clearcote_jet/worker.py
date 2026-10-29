"""clearcote-jet worker: one task in a running browser, driven over stdin/stdout with one JSON object per line.

For a program that runs one task across many browsers (the Profile Manager starts one worker per profile). The caller
writes one request line; the worker attaches to the browser at `cdp`, runs the task in a new tab and writes events,
one per line, until the result. While it runs the caller may write {"cmd": "approve"}, {"cmd": "cancel"} (when it is
asked to confirm a click) and {"cmd": "stop"} (any time). Stdout carries nothing but these lines; logs go to stderr.
A secret value never appears in an event: it is written as its {{slot}}.

    -> {"cmd": "run", "cdp": "http://127.0.0.1:9222", "url": "https://...", "goal": "Log in as {{email}} ...",
        "values": {"email": "...", "password": "..."}, "secret": ["password"], "confirm": true, "humanize": true,
        "reuse": true, "skills_dir": null, "max_steps": null}
    <- {"type": "ready", "version": "0.3.0"}
    <- {"type": "step", ...}                          one per action, as the trace has it
    <- {"type": "needs_confirmation", "pending": {"kind", "label", "role"}, "detail": "..."}
    -> {"cmd": "approve"}                              or cancel; stdin closing is a cancel
    <- {"type": "result", "status": "done", ...}      exit code 0
    <- {"type": "error", "message": "..."}            exit code 1
"""

import asyncio
import json
import os
import sys
import threading

from . import __version__
from .agent import run, stopped
from .browser import Session
from .cli import load_env_file, open_or_reuse
from .describe import plain
from .skills import SkillStore, run_with_skill
from .slots import Values, each_string

MARKDOWN_LIMIT = 4000
ITEMS_LIMIT = 200
RESULT_KEYS = ("status", "detail", "url", "title", "elapsed_ms", "actions", "decisions", "text_calls", "timing",
               "usage", "skill", "pending", "stale")


class BadRequest(ValueError):
    pass


def request_from(line):
    """The run request, checked. BadRequest says what is wrong with it."""
    if not isinstance(line, dict) or line.get("cmd") != "run":
        raise BadRequest('the first line must be {"cmd": "run", ...}')
    for name in ("cdp", "goal"):
        if not isinstance(line.get(name), str) or not line[name].strip():
            raise BadRequest(f"{name} is required (a string)")
    values, secret = line.get("values"), line.get("secret") or []
    if values is not None and (not isinstance(values, dict)
                               or not all(isinstance(v, (str, int, float)) for v in values.values())):
        raise BadRequest("values must be an object of strings")
    if not isinstance(secret, list) or not all(isinstance(s, str) for s in secret):
        raise BadRequest("secret must be a list of slot names")
    max_steps = line.get("max_steps")
    if max_steps is not None and (not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 1):
        raise BadRequest("max_steps must be a positive integer")
    if line.get("url") is not None and not isinstance(line["url"], str):
        raise BadRequest("url must be a string")
    return {"cdp": line["cdp"], "url": line.get("url") or None, "goal": line["goal"],
            "values": {k: str(v) for k, v in values.items()} if values is not None else None, "secret": secret,
            "confirm": bool(line.get("confirm", False)), "humanize": bool(line.get("humanize", True)),
            "reuse": bool(line.get("reuse", True)), "skills_dir": line.get("skills_dir") or None,
            "max_steps": max_steps}


def result_event(result):
    """The result line: the result without its trace, the markdown cut to MARKDOWN_LIMIT, at most ITEMS_LIMIT rows."""
    out = {"type": "result", **{k: result[k] for k in RESULT_KEYS if k in result}}
    markdown, items = result.get("markdown") or "", result.get("items") or []
    out["markdown"] = markdown[:MARKDOWN_LIMIT]
    out["markdown_truncated"] = len(markdown) > MARKDOWN_LIMIT
    out["items"] = items[:ITEMS_LIMIT]
    out["items_count"] = len(items)
    return out


class Worker:
    def __init__(self, out, fd):
        self.out, self.fd = out, fd
        self.mask = Values()
        self.lines = asyncio.Queue()
        self.waiting = None  # the answer to a needs_confirmation, while one is asked
        self.eof = self.stopping = False
        self.task = None

    def emit(self, event):
        event = each_string(event, self.mask.mask)
        self.out.write((json.dumps(event, default=str) + "\n").encode("ascii", "backslashreplace"))
        self.out.flush()

    def read_lines(self, loop):
        """stdin, line by line, into self.lines (None at its end). os.read, not sys.stdin: a thread blocked in a
        buffered read would hold its lock when the process exits."""
        def put(line):
            try:
                loop.call_soon_threadsafe(self.lines.put_nowait, line)
                return True
            except RuntimeError:  # the worker has finished: nobody reads the rest
                return False

        buf = b""
        while True:
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                if raw.strip() and not put(raw):
                    return
        if buf.strip():
            put(buf)
        put(None)

    async def control(self):
        """Lines after the request: approve / cancel answer a question, stop ends the task, the end of stdin cancels."""
        while True:
            raw = await self.lines.get()
            if raw is None:
                self.eof = True
                self._answer(False)
                return
            try:
                cmd = json.loads(raw).get("cmd")
            except (ValueError, AttributeError):
                print(f"worker: ignored a line that is not a JSON object ({len(raw)} bytes)", file=sys.stderr)
                continue
            if cmd in ("approve", "cancel"):
                if not self._answer(cmd == "approve"):
                    print(f"worker: {cmd} with nothing asked, ignored", file=sys.stderr)
            elif cmd == "stop":
                self.stopping = True
                if self.task and not self.task.done():
                    self.task.cancel()
            else:
                print(f"worker: unknown cmd {cmd!r}, ignored", file=sys.stderr)

    def _answer(self, yes):
        if self.waiting is not None and not self.waiting.done():
            self.waiting.set_result(yes)
            return True
        return False

    async def approve(self, pending, detail):
        if self.eof or self.stopping:
            return False
        self.waiting = asyncio.get_running_loop().create_future()
        self.emit({"type": "needs_confirmation", "pending": pending, "detail": detail})
        try:
            return await self.waiting
        finally:
            self.waiting = None

    async def serve(self):
        loop = asyncio.get_running_loop()
        threading.Thread(target=self.read_lines, args=(loop,), daemon=True, name="worker-stdin").start()
        self.emit({"type": "ready", "version": __version__})
        raw = await self.lines.get()
        try:
            if raw is None:
                raise BadRequest("stdin closed before a request")
            try:
                line = json.loads(raw)
            except ValueError:
                raise BadRequest("the request is not JSON") from None
            req = request_from(line)
        except BadRequest as e:
            self.emit({"type": "error", "message": str(e)})
            return 1
        self.mask = Values(req["values"], req["secret"])
        try:
            session = await Session.connect(req["cdp"], humanize=req["humanize"])
        except Exception as e:  # noqa: BLE001  (no browser there, or not a CDP endpoint)
            self.emit({"type": "error", "message": f"could not attach to {req['cdp']}: {type(e).__name__}: {e}"})
            return 1
        control = asyncio.create_task(self.control())
        try:
            # a tab of its own: the window the person left stays as it is
            page = (await session.new_tab(req["url"], reuse_blank=False) if req["url"]
                    else await open_or_reuse(session, None))
            steps = []

            def on_step(step):
                steps.append(step)
                self.emit({"type": "step", **step})

            task = dict(on_step=on_step, confirm=req["confirm"], values=req["values"], secret=req["secret"],
                        approve=self.approve if req["confirm"] else None, max_steps=req["max_steps"])
            if req["reuse"] and req["url"]:  # learned once, then replayed with no model (see skills.py)
                go = run_with_skill(session, req["goal"], url=req["url"], page=page,
                                    store=SkillStore(req["skills_dir"]), **task)
            else:
                go = run(session, req["goal"], page=page, **task)
            self.task = asyncio.ensure_future(go)
            if self.stopping:
                self.task.cancel()
            try:
                result = await self.task
            except asyncio.CancelledError:
                if not self.stopping:
                    raise
                try:
                    title = await asyncio.wait_for(page.title(), 5)
                except Exception:  # noqa: BLE001  (the tab went away)
                    title = ""
                result = plain(stopped(page, "stopped", "stopped by the caller", title=title, trace=steps))
            self.emit(result_event(result))
            return 0
        except Exception as e:  # noqa: BLE001  (reported to the caller, which decides what to do with the profile)
            print(f"worker: {type(e).__name__}", file=sys.stderr)
            self.emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
            return 1
        finally:
            control.cancel()
            await session.close()  # attached: only our connection is dropped, the browser keeps running


def main():
    load_env_file()  # as the other commands do: TYPESAFE_API_KEY and the rest from ./.env
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # anything else that prints goes to stderr: stdout carries only the events
    try:
        code = asyncio.run(Worker(out, sys.stdin.fileno()).serve())
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
