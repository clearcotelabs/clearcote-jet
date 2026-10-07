"""markdown.js keeps the spaces that live in whitespace-only text nodes, in Playwright's own Chromium. No Clearcote, no
network, no model calls. Skipped where no Chromium is installed, see conftest.py."""

import asyncio

import pytest

from clearcote_jet.browser import MARKDOWN_JS

# example.com wraps every character of its paragraphs in its own <span>, so each space is a text node of its own.
PER_CHAR = "".join(f"<span>{c}</span>" for c in "This domain is for use in documentation examples.")
PAGE = f"""<!doctype html><title>t</title><h1>Example Domain</h1><p>{PER_CHAR}</p>
<p><b>bold</b> <i>italic</i> <a href="https://example.org/">a link</a></p>
<ul><li>one</li>
    <li>two</li></ul>"""


async def scenario(chromium):
    async with chromium() as browser:
        page = await browser.new_page()
        await page.set_content(PAGE)
        return {"md": await page.evaluate(MARKDOWN_JS)}


@pytest.fixture(scope="module")
def result(chromium):
    return asyncio.run(scenario(chromium))


def test_per_character_spans_keep_their_spaces(result):
    assert "This domain is for use in documentation examples." in result["md"]


def test_space_between_inline_elements_is_kept(result):
    assert "bold italic [a link](https://example.org/)" in result["md"]


def test_list_items_and_blocks_get_no_stray_spaces(result):
    md = result["md"]
    assert "- one\n- two" in md
    assert " \n" not in md
