"""Mobile and desktop check-in QA against isolated native Ryot/Telegram fixtures."""

import argparse
from pathlib import Path
import socket
import threading
import time
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
import uvicorn

from miniapp_browser_check import assert_dock
from test_miniapp import signed_data
from test_watch_checkin import WatchCheckInTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--poster", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    fixture = WatchCheckInTests()
    fixture.setUp()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(fixture.client.app, access_log=False, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    with patch("test_miniapp.TOKEN", "fake-token"):
        launch = signed_data(2)
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("Fixture server did not start")
            time.sleep(.02)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chromium", timeout=30000)
            try:
                for index, (width, height) in enumerate(((390, 844), (320, 640), (1280, 900), (844, 390))):
                    if index:
                        fixture.fixture.native.events.append(fixture.fixture.native.event(f"view-{index}"))
                        fixture.fixture.workflow.tick()
                    context = browser.new_context(viewport={"width": width, "height": height})
                    context.route("https://telegram.org/js/*", lambda route: route.fulfill(body=""))
                    context.route("https://image.tmdb.org/**", lambda route: route.fulfill(path=str(args.poster.resolve()), content_type="image/png"))
                    context.route("**/api/viewing/poster/1", lambda route: route.fulfill(json={"poster_url": "https://image.tmdb.org/t/p/w500/fixture.png"}))
                    context.add_init_script("window.Telegram={WebApp:{initData:" + repr(launch) +
                        ",safeAreaInset:{bottom:34,left:12,right:12},contentSafeAreaInset:{bottom:20,left:4,right:4},"
                        "onEvent(){},ready(){},expand(){},BackButton:{show(){},hide(){},onClick(){}}}};")
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(f"http://127.0.0.1:{port}")
                    observer_context = browser.new_context(viewport={"width": width, "height": height})
                    observer_context.route("https://telegram.org/js/*", lambda route: route.fulfill(body=""))
                    observer_context.add_init_script("window.Telegram={WebApp:{initData:" + repr(fixture.admin["Authorization"][4:]) +
                        ",onEvent(){},ready(){},expand(){},BackButton:{show(){},hide(){},onClick(){}}}};")
                    observer = observer_context.new_page()
                    observer.goto(f"http://127.0.0.1:{port}")
                    observer.locator("#watch-check-in").click()
                    expect(observer.get_by_role("button", name="Yes, I watched this", exact=True)).to_be_visible()
                    expect(page.locator("#watch-count")).to_have_text("1")
                    expect(page.locator("#maintenance-toggle-control")).to_be_hidden()
                    page.locator("#watch-check-in").click()
                    expect(page.locator("#maintenance-heading")).to_have_text("Viewing inbox")
                    expect(page.locator(".maintenance-branches")).to_be_hidden()
                    expect(page.locator("#viewing-actions button")).to_have_count(2)
                    expect(page.get_by_role("button", name="No, I did not watch this", exact=True)).to_be_visible()
                    page.screenshot(path=str(args.output / f"confirm-{width}.png"), full_page=True)
                    page.get_by_role("button", name="Yes, I watched this", exact=True).click()
                    expect(page.get_by_role("button", name="No rating", exact=True)).to_be_visible()
                    observer.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                    expect(observer.locator("#viewing-empty")).to_have_text("You're all caught up.")
                    expect(observer.locator("#watch-count")).to_be_hidden()
                    expect(observer.locator("#viewing-actions button:visible")).to_have_count(0)
                    expect(page.get_by_role("button", name="Confirm rating", exact=False)).to_have_count(0)
                    page.get_by_role("button", name="4", exact=True).click()
                    expect(page.get_by_role("button", name="4", exact=True)).to_have_attribute("aria-pressed", "true")
                    expect(page.get_by_role("button", name="Confirm rating (4/5)", exact=True)).to_be_visible()
                    expect(page.locator("#watch-count")).to_have_text("1")
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(args.output / f"rate-{width}.png"), full_page=True)
                    assert_dock(page, "#viewing-panel .maintenance-toolbar", ["#viewing-previous", "#viewing-next", "#viewing-refresh"],
                                {"bottom": 34, "left": 12, "right": 12}, {"bottom": 20, "left": 4, "right": 4})
                    page.get_by_role("button", name="Confirm rating (4/5)", exact=True).click()
                    expect(page.locator("#viewing-empty")).to_have_text("You're all caught up.")
                    expect(page.locator("#watch-count")).to_be_hidden()
                    expect(page.locator("#watch-check-in")).to_be_visible()
                    page.screenshot(path=str(args.output / f"empty-{width}.png"), full_page=True)
                    page.locator("#watch-check-in").click()
                    expect(page.locator("#search-form")).to_be_visible()
                    assert not errors, errors
                    context.close()
                    observer_context.close()
                    print(f"PASS {width}x{height}: ordinary user, eye/count, first Yes clears other user, optional rating, empty, back", flush=True)
                assert len(fixture.fixture.native.reviews) == 4
                assert len(fixture.fixture.native.events) == 5
                fixture.fixture.native.events.append(fixture.fixture.native.event("declined"))
                fixture.fixture.workflow.tick()
                context = browser.new_context(viewport={"width": 390, "height": 844})
                context.route("https://telegram.org/js/*", lambda route: route.fulfill(body=""))
                context.add_init_script("window.Telegram={WebApp:{initData:" + repr(fixture.admin["Authorization"][4:]) +
                    ",onEvent(){},ready(){},expand(){},BackButton:{show(){},hide(){},onClick(){}}}};")
                page = context.new_page(); page.goto(f"http://127.0.0.1:{port}")
                page.locator("#watch-check-in").click()
                expect(page.get_by_role("button", name="Yes, I watched this", exact=True)).to_be_visible()
                assert fixture.answer("no").status_code == 200
                page.locator("#viewing-refresh").click()
                page.get_by_role("button", name="No, I did not watch this", exact=True).click()
                expect(page.locator("#viewing-empty")).to_have_text("You're all caught up.")
                assert len(fixture.fixture.native.events) == 6
                assert len(fixture.fixture.native.reviews) == 4
                page.locator("#maintenance-toggle").click()
                page.locator("#declined-branch").click()
                expect(page.get_by_role("button", name="Confirm household watched", exact=True)).to_be_visible()
                expect(page.locator("#declined-count")).to_have_text("1")
                page.screenshot(path=str(args.output / "declined-admin-390.png"), full_page=True)
                page.get_by_role("button", name="Confirm household watched", exact=True).click()
                expect(page.locator("#declined-movies section")).to_have_count(0)
                expect(page.locator("#declined-count")).to_have_text("0")
                page.locator("#watch-check-in").click()
                expect(page.get_by_role("button", name="No rating", exact=True)).to_be_visible()
                page.get_by_role("button", name="No rating", exact=True).click()
                expect(page.locator("#viewing-empty")).to_have_text("You're all caught up.")
                fixture.fixture.native.events.append(fixture.fixture.native.event("stale"))
                fixture.fixture.workflow.tick()
                fixture.fixture.native.events.pop()
                page.locator("#maintenance-toggle").click()
                page.locator("#sync-branch").click()
                expect(page.locator("#sync-viewing-open")).to_be_hidden()
                expect(page.locator("#sync-viewing-panel")).to_be_hidden()
                expect(page.get_by_role("button", name="Clear stale request", exact=True)).not_to_be_visible()
                expect(page.locator("#sync-count")).to_have_text("!")
                expect(page.locator("#declined-count")).to_have_text("0")
                page.screenshot(path=str(args.output / "stale-alert-only-390.png"), full_page=True)
                assert any(d.context["event_id"] == "stale" for d in fixture.fixture.storage.viewing_followups())
                context.close()
                print("PASS all No → admin decline review → confirm → optional rating; no history deletion", flush=True)
                print("PASS missing native viewing → alert only; request retained for maintainer; no repair UI", flush=True)
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        fixture.doCleanups()


if __name__ == "__main__":
    main()
