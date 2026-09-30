# Clearcote Jet

**Tell it what you want done on a website. Jet reads the page, picks the next step in about a quarter of a second,
does it with a real mouse and keyboard in [Clearcote](https://clearcotelabs.com), and hands you the answer.**

![Three live Jet runs: a cookie banner, a search, a documentation lookup and a version switch](docs/demo.gif)

*Three live runs, recorded in real time; the captions are added afterwards from each run's log. Every step shows what
Jet did, how sure it was, and how long it took to decide. [Watch the MP4](docs/demo.mp4).*

## Why use it

- **Fast.** Each decision took 235 to 473 ms in our runs. Most of a run is the browser, not the thinking.
- **Cheap.** A finished task averaged 22,394 input tokens: about **$0.0009, or $0.94 per 1,000 tasks**.
- **You can see why.** Every step comes with the probability behind it and the options it passed over, so a shaky
  step stands out.
- **It uses the page like a person.** Curved mouse paths, key-by-key typing, dropdowns picked from the open list, and
  a cookie banner is just another thing to click (it declines when it can).
- **One loop for every site.** There is no per-site code: the same loop looks at the page again after every step.

## Try it in two minutes

```bash
git clone https://github.com/clearcotelabs/clearcote-jet && cd clearcote-jet
pip install -e .
cp .env.example .env          # add TYPESAFE_API_KEY
python examples/run_task.py   # watch it find the next UK bank holiday, cursor drawn
```

Just the library: `pip install git+https://github.com/clearcotelabs/clearcote-jet`.

## What it did in our runs

Live, in a visible Clearcote window, 29 September 2026:

| Task | Result | Steps | Decisions | Time | Input tokens | Cost |
|---|---|---|---|---|---|---|
| GOV.UK: find the next bank holiday in England and Wales | done: 25 December | 4 | 5 | 14.8 s | 13,654 | $0.0006 |
| Python docs: search `asyncio.gather` and open its entry | done | 3 | 4 | 10.9 s | 29,048 | $0.0012 |
| Python docs: switch the page to version 3.12 | done | 1 | 2 | 4.4 s | 19,349 | $0.0008 |
| Hacker News: open the top story's comments | done | 1 | 2 | 2.7 s | 29,877 | $0.0013 |
| RFC 9110: jump to the section on 404 Not Found | stopped at the 60-step cap | 60 | 60 | 92.3 s | 368,958 | $0.0155 |

The last row is a failure we kept on purpose: on a very long document it scrolled instead of using the table of
contents. The step cap is what bounds a run like that.

A GOV.UK run, step by step:

```text
1. click  "Reject additional cookies"   p=0.85   decided in 361 ms
2. type   "next bank holiday" into "Search"   p=1.00
3. click  "Search GOV.UK"   p=0.93
4. click  "UK bank holidays"   p=0.99
done · 4 actions · 5 decisions · 14.8 s · 13,654 input tokens ≈ $0.0006
```

## What it costs

- **Decision model:** $0.042 per million input tokens; output tokens are free. That is the published rate on
  29 September 2026; set `CLEARCOTE_JET_USD_PER_MTOK` if yours differs.
- **Measured:** 13,654 to 30,505 input tokens per finished task, so $0.0006 to $0.0013. Our worst case, the 60-step
  runaway above, cost $0.0155.
- **Your own runs:** every result carries `usage` (tokens, requests, `estimated_usd`), and
  `python examples/estimate_cost.py` totals everything in `runs/` and projects the cost per 1,000 tasks.
- **Not included:** your [Clearcote](https://clearcotelabs.com) licence, and an optional text model, which bills with its
  own provider.

## Use it

### Python

```python
import asyncio
from clearcote_jet import Session, run

async def main():
    session = await Session.launch(headless=False)
    page = await session.new_tab("https://www.gov.uk/")
    result = await run(session, "Find the date of the next bank holiday in England and Wales.", page=page)
    print(result["status"], result["url"], result["usage"]["estimated_usd"])
    for step in result["trace"]:
        print(step["step"], step["kind"], step["action"], step["probability"], step["alternatives"])
    print(result["markdown"])
    await session.close()

asyncio.run(main())
```

### Command line

```bash
clearcote-jet run --url https://docs.python.org/3/ --goal "Find the entry for asyncio.gather" --show-cursor

# keep one browser open and send it tasks
clearcote-jet browser --port 9222
clearcote-jet run --cdp http://127.0.0.1:9222 --url https://news.ycombinator.com/ --goal "Open the top story's comments"
```

Flags: `--show-cursor` (draw the mouse), `--keep-open`, `--headless`, `--no-humanize`, `--profile DIR`.

### As a tool for your AI assistant (MCP)

```json
{
  "mcpServers": {
    "clearcote-jet": {
      "command": "clearcote-jet-mcp",
      "env": { "TYPESAFE_API_KEY": "...", "CLEARCOTE_LICENSE_KEY": "..." }
    }
  }
}
```

Your assistant gets `browse(goal, url?, tab_id?)`, which runs a whole task and returns every step plus the page content,
and `close_tab(tab_id)`. Tabs stay open between calls, several tasks can run at once in their own tabs, and the browser
shuts itself down after a few idle minutes.

### Settings

Put them in `.env` (see `.env.example`) or the environment:

| Variable | Default | What it does |
|---|---|---|
| `TYPESAFE_API_KEY` | required | key for the decision model (from console.typesafe.ai) |
| `CLEARCOTE_LICENSE_KEY` | `~/.clearcote/license.key` | your Clearcote licence; without one the open build runs |
| `TEXT_MODEL_BASE_URL`, `TEXT_MODEL_API_KEY`, `TEXT_MODEL` | unset | optional OpenAI-compatible model that writes typed values; without it Jet types words taken from your goal |
| `CLEARCOTE_JET_PROFILE` | `~/.clearcote-jet/profile` | browser profile; cookies and consent choices persist |
| `CLEARCOTE_JET_PROXY` | unset | route through a proxy; timezone, language and location follow its exit IP |
| `CLEARCOTE_JET_HEADLESS` | off | `1` runs without a window |
| `CLEARCOTE_JET_HUMANIZE` | on | `0` switches to instant clicks and typing |
| `CLEARCOTE_JET_CDP` | unset | attach to a browser that is already running |
| `CLEARCOTE_JET_IDLE_MINUTES` | `5` | MCP server: close the browser after this long without calls |
| `CLEARCOTE_JET_USD_PER_MTOK` | `0.042` | rate used for the cost estimate |

## Reading a result

| Field | Meaning |
|---|---|
| `status` | `done`, `blocked` (nothing on the page can move it forward), `needs_input` (the goal lacks a value a field needs), `budget` (hit the step cap) or `error` |
| `trace` | every step: what was done, the text typed, its probability, the runner-up options and how long the decision took |
| `stale` | steps that were decided again because the page changed before Jet could act |
| `usage` | decision-model tokens and requests, `estimated_usd`, and any text-model tokens |
| `markdown` | the parts of the final page that answer the goal |

## How it works

1. **Look.** Jet lists the controls a person could use right now: visible, enabled, not hidden behind a pop-up,
   including those inside embedded frames (iframes), same-site or cross-site. Password fields are never read.
2. **Decide.** One request to the decision model picks the kind of step and the control together, with a probability for
   every option. The model only chooses from that list; it never writes code or coordinates.
3. **Act.** Jet checks that the page has not changed, then moves the mouse along a curved path, clicks, and types key by
   key. Page reads run in a separate, isolated context, so the page's own scripts don't see them.
4. **Answer.** When the goal is met, the page is turned into markdown and scored section by section against the goal.

## Examples

| File | What it shows |
|---|---|
| [`examples/quickstart.py`](examples/quickstart.py) | the smallest program: one goal, the steps, the answer |
| [`examples/run_task.py`](examples/run_task.py) | any goal in a visible window with the cursor drawn; the run is saved to `runs/` |
| [`examples/gallery.py`](examples/gallery.py) | four tasks on four kinds of site, with a summary |
| [`examples/estimate_cost.py`](examples/estimate_cost.py) | tokens and dollars for your saved runs, projected per 1,000 tasks |
| [`examples/record_demo.py`](examples/record_demo.py) | records live runs and turns them into a captioned MP4 and GIF (needs ffmpeg) |

## Known limits

- On very long pages it may scroll rather than jump through a table of contents (see the RFC run above).
- Links that open a new tab are not followed, and closed shadow roots, canvas apps, file uploads and CAPTCHAs are not
  handled. Controls inside iframes are; scrolling inside an iframe is not.
- `done` is the model's judgement. Check what matters.
- Jet only types words that appear in your goal (unless you configure a text model), so write the values into it.

Built for privacy, testing, research and lawful automation.

## Development

```bash
pip install -e . pytest ruff
ruff check . && pytest tests -q            # offline: no browser, no model calls
python tests/e2e/check_browser_layer.py    # visible browser, scripted steps, a local test page
python tests/e2e/check_full_loop.py        # the whole loop with a stand-in for the model
python tests/e2e/check_frames.py           # controls inside same-site and cross-site iframes
```

## License

MIT, see [LICENSE](LICENSE).
