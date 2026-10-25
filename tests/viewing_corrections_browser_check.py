"""Browser QA for poster-backed admin corrections using synthetic native history."""

import argparse
from pathlib import Path
import socket
import threading
import time

from playwright.sync_api import expect, sync_playwright
import uvicorn

from test_viewing_corrections import ViewingCorrectionTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--poster", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chromium", timeout=30000)
        try:
            for width, height in ((390, 844), (320, 640), (1280, 900), (844, 390)):
                fx = ViewingCorrectionTests()
                fx.setUp()
                fx.native.reviews.append({"id": "keep", "rating": "80", "textOriginal": "Keep this existing review",
                                          "visibility": "PRIVATE", "isSpoiler": False, "postedBy": {"id": "household"}})
                listener = socket.socket(); listener.bind(("127.0.0.1", 0)); port = listener.getsockname()[1]
                server = uvicorn.Server(uvicorn.Config(fx.fx.client.app, access_log=False, log_level="error"))
                thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True); thread.start()
                context = browser.new_context(viewport={"width": width, "height": height},
                                              color_scheme="dark" if width in (390, 844) else "light")
                try:
                    deadline = time.monotonic() + 10
                    while not server.started:
                        if time.monotonic() > deadline: raise RuntimeError("Fixture server did not start")
                        time.sleep(.02)
                    context.route("https://telegram.org/js/*", lambda route: route.fulfill(body=""))
                    context.route("**/api/maintenance/poster/1", lambda route: route.fulfill(json={"poster_url": "https://image.tmdb.org/t/p/w500/fixture.jpg"}))
                    context.route("https://image.tmdb.org/**", lambda route: route.fulfill(path=str(args.poster.resolve()), content_type="image/jpeg"))
                    context.add_init_script("window.Telegram={WebApp:{initData:" + repr(fx.fx.admin["Authorization"][4:]) +
                        ",onEvent(){},ready(){},expand(){},BackButton:{show(){},hide(){},onClick(){}}}};")
                    page = context.new_page(); page.set_default_timeout(10000)
                    errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
                    page.goto(f"http://127.0.0.1:{port}")
                    page.locator("#maintenance-toggle").click(); page.locator("#declined-branch").click()
                    expect(page.locator("#declined-movies .viewing-correction button")).to_have_count(4)
                    expect(page.get_by_role("button", name="Review again", exact=True)).to_be_enabled()
                    expect(page.get_by_role("button", name="Clear stale request", exact=True)).to_be_disabled()
                    expect(page.locator("#declined-movies img")).to_be_visible()
                    page.wait_for_function("document.querySelector('#declined-movies img').naturalWidth > 0")
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(args.output / f"actions-{width}.png"), full_page=True)
                    page.get_by_role("button", name="Review again", exact=True).click()
                    expect(page.locator("#declined-count")).to_have_text("0")
                    page.locator("#watch-check-in").click()
                    expect(page.get_by_role("button", name="Yes, I watched this", exact=True)).to_be_visible()
                    assert fx.fx.answer("no").status_code == 200
                    page.locator("#viewing-refresh").click()
                    page.get_by_role("button", name="No, I did not watch this", exact=True).click()
                    expect(page.locator("#viewing-empty")).to_have_text("You're all caught up.")
                    page.locator("#maintenance-toggle").click(); page.locator("#declined-branch").click()
                    clear = page.locator("#declined-movies").get_by_role("button", name="Clear viewing history", exact=True)
                    clear.click()
                    expect(page.locator("#viewing-reset-dialog")).to_be_visible()
                    expect(page.locator("#viewing-reset-movie")).to_have_text("Example (2026)")
                    expect(page.locator("#viewing-reset-description")).to_contain_text("all 2 viewing records")
                    assert fx.deletes() == []
                    page.screenshot(path=str(args.output / f"confirm-reset-{width}.png"), full_page=True)
                    page.locator("#viewing-reset-cancel").click()
                    assert len(fx.native.events) == 2 and fx.deletes() == []
                    clear.click(); page.locator("#viewing-reset-confirm").click()
                    expect(page.locator("#viewing-reset-dialog")).to_be_hidden()
                    expect(page.locator("#declined-count")).to_have_text("0")
                    assert fx.native.events == [] and "Completed" not in fx.native.reset_collections
                    assert len(fx.native.reviews) == 1 and "Library" in fx.native.reset_collections
                    fx.native.events.append(fx.native.event("stale")); fx.fx.fixture.workflow.tick(); fx.native.events.pop()
                    page.locator("#sync-branch").click()
                    page.locator("#maintenance-category").select_option("viewing_requests")
                    expect(page.locator("#sync-count")).to_have_text("1")
                    expect(page.locator("#watch-count")).to_have_text("0")
                    expect(page.locator("#maintenance-category")).to_be_visible()
                    expect(page.locator("#sync-viewing-movies .viewing-correction button")).to_have_count(4)
                    for name in ("Confirm household watched", "Review again", "Clear viewing history"):
                        expect(page.get_by_role("button", name=name, exact=True)).to_be_disabled()
                    expect(page.get_by_role("button", name="Clear stale request", exact=True)).to_be_enabled()
                    expect(page.locator("#sync-viewing-movies")).to_contain_text("No viewing history or watched flags remain.")
                    page.screenshot(path=str(args.output / f"disabled-{width}.png"), full_page=True)
                    context.route("https://image.tmdb.org/**", lambda route: route.abort())
                    page.locator("#sync-viewing-refresh").click()
                    expect(page.locator("#sync-viewing-movies img")).to_be_hidden()
                    expect(page.get_by_role("button", name="Clear stale request", exact=True)).to_be_enabled()
                    page.get_by_role("button", name="Clear stale request", exact=True).click()
                    expect(page.locator("#declined-count")).to_have_text("0")
                    assert not errors, errors
                    print(f"PASS {width}x{height}: poster/fallback, four action states, review again, cancel/reset, reviews preserved", flush=True)
                finally:
                    context.close(); server.should_exit = True; thread.join(timeout=5); fx.doCleanups()
        finally:
            browser.close()


if __name__ == "__main__":
    main()
