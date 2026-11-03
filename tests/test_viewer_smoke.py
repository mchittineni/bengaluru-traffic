"""Load the built 3D viewer in headless Chromium and check it runs cleanly.

Opt-in: needs `pip install playwright && playwright install chromium` and the output of
`python sim3d.py`. Run with `pytest -m browser`. Set CHROMIUM_PATH to use an existing
Chromium instead of Playwright's download.
"""

import functools
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

SITE = Path("output/sim3d")
pytestmark = pytest.mark.browser


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site_url():
    if not (SITE / "data.json").exists():
        pytest.skip("no viewer build: run `python sim3d.py` first")
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(SITE)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


@pytest.fixture(scope="module")
def page(site_url):
    with sync_api.sync_playwright() as p:
        # Software WebGL, so it also runs on CI machines without a GPU.
        browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None,
                                    args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.problems = []
        page.on("pageerror", lambda e: page.problems.append(f"page error: {e}"))
        page.on("console", lambda m: m.type == "error" and page.problems.append(f"console: {m.text}"))
        page.goto(site_url)
        page.wait_for_function("document.body.dataset.state", timeout=90_000)
        yield page
        browser.close()


def test_viewer_loads_without_errors(page):
    assert page.evaluate("document.body.dataset.state") == "ready", page.text_content("#loading-msg")
    assert page.is_hidden("#loading")
    assert page.problems == []


# Software WebGL renders this scene far too slowly to wait for animation frames, so the
# checks below use controls whose effects on the page are synchronous.

def test_controls_are_wired(page):
    assert page.evaluate("document.querySelectorAll('#scenarios button').length") == 3
    assert page.get_attribute("#scenarios button[data-key=waze]", "aria-pressed") == "true"
    assert page.text_content("#compare").count("%") == 3


def test_keyboard_switches_scenario_and_rides_along(page):
    page.evaluate("document.activeElement && document.activeElement.blur()")
    page.keyboard.press("1")
    assert page.get_attribute("#scenarios button[data-key=habitual]", "aria-pressed") == "true"
    page.keyboard.press("f")
    assert page.is_visible("#follow-card")
    assert page.get_attribute("#v-follow", "aria-pressed") == "true"
    page.keyboard.press("Escape")
    assert page.is_hidden("#follow-card")
    assert page.problems == []
