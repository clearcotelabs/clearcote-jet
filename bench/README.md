# Jet benchmark

68 tasks Jet is run on end to end in a real Clearcote browser, each judged by a check of its own that does not trust
Jet's `done`: 34 on local fixture pages (`fixtures/`, served by `server.py`) that copy the patterns sites use
(custom dropdowns, autocomplete, date pickers, native dialogs, infinite scroll, row menus, iframes, shadow DOM…) and
34 on public practice sites (the-internet.herokuapp.com, demoqa.com, toscrape.com, Wikipedia, docs.python.org,
gov.uk, saucedemo.com). Task format: [CONTRACT.md](CONTRACT.md).

```bash
python bench/run.py --label mine --repeat 2 --concurrency 8      # everything, twice: ~10 minutes
python bench/run.py --label t --local --only dropdowns slider    # some local tasks (ids containing these words)
python bench/explain.py local-table-details --grep 1043          # one task: every decision, and what was offered
python bench/peek.py settings-dropdowns.html --click Language    # what Jet is offered on a page
```

The model key comes from `TYPESAFE_API_KEY`, else `./.env`, else the Profile Manager's saved key. Results go to
`results/<label>.json` after every run (a stopped run keeps what it did); `BENCH_TIMEOUT` sets the per-task limit
(default 300 s). `selfcheck_local.py` / `selfcheck_real.py` prove every check passes when the task is done by hand and
fails when nothing is done.

Run it before a release that touches the agent loop, the snapshot or the prompts, and compare with the last
`results/` file: real sites change, so read a new failure's trace before blaming the change.

## Results

Every task twice (136 runs), 8 browsers, on Clearcote r54 (2026-10-30):

| | local | real sites | all | ended blocked | done but wrong |
|---|---|---|---|---|---|
| Jet 0.3.1 | 46/68 | 42/68 | 88/136 (65%) | 47 | 6 |
| Jet 0.4.0 | 64/68 | 61/68 | 125/136 (92%) | 15 | 0 |

Still failing in 0.4.0: a row's "⋯" menu holding "View details" (the model stops after the order number's dead link), a
job far down an infinite feed (once of two), and demoqa's ad-heavy forms (text box, practice form, table edit), where
the model stops before the final Submit. A tag that differs by a few letters ("friends" for "friendship") was taken
once of two.
