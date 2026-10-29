"""The CLI's step lines and results print on Windows when output goes to a pipe (ANSI code page there)."""

import io

from clearcote_jet.cli import print_step, utf8_output


def test_piped_output_takes_an_arrow_and_page_text(capsys):
    raw = io.BytesIO()
    piped = io.TextIOWrapper(raw, encoding="cp1252")
    utf8_output(piped)
    piped.write("[  120 ms] fill   Quick search ← 'asyncio.gather'  Python 3.15 — docs, 東京\n")
    piped.flush()
    assert "←".encode() in raw.getvalue() and "東京".encode() in raw.getvalue()


def test_print_step_line(capsys):
    print_step({"at_ms": 120, "kind": "fill", "action": "Quick search", "text": "asyncio.gather", "probability": 0.92})
    assert "Quick search ← 'asyncio.gather'  (p=0.92)" in capsys.readouterr().out
