"""Shared by the tests that run Playwright's own Chromium."""

import asyncio
import shutil
import tempfile
from contextlib import asynccontextmanager

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright


@asynccontextmanager
async def launch_chromium():
    """Playwright's own headless Chromium, closed afterwards. Skips the test where it is not installed (CI): run
    `python -m playwright install chromium` to include it.

    Playwright's driver, and the browser it starts, keep their temp files in a directory of their own, removed at the
    end. A launch makes a profile and an artifacts directory in temp before it looks for the browser, and leaves both
    behind when the browser is not there."""
    temp = tempfile.mkdtemp(prefix="jet-test-playwright-")
    try:
        with pytest.MonkeyPatch.context() as env:  # the driver copies the environment when it starts
            for name in ("TMPDIR", "TEMP", "TMP"):
                env.setenv(name, temp)
            pw = await async_playwright().start()
        try:
            try:
                browser = await pw.chromium.launch(headless=True)
            except PlaywrightError as e:
                pytest.skip(f"no Playwright Chromium: {str(e).splitlines()[0]}")
            try:
                yield browser
            finally:
                await browser.close()
        finally:
            await pw.stop()  # returns once the driver has exited
    finally:
        await remove(temp)


async def remove(path, timeout=10.0):
    """Remove `path`, waiting while files in it are still held (on Windows, a browser's helper processes can outlive
    it for a moment). Raises if it is still there after `timeout` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if loop.time() > deadline:
                raise
            await asyncio.sleep(0.2)


@pytest.fixture(scope="session")
def chromium():
    """`async with chromium() as browser:` see launch_chromium()."""
    return launch_chromium
