"""Browser QA for admin maintenance using real routes and isolated provider fixtures."""

import argparse
import json
from dataclasses import replace
from pathlib import Path
import socket
import threading
import time

from playwright.sync_api import sync_playwright, expect
import uvicorn

from curatarr.domain import ArrRuntimeState as Runtime, DurableState as State, MediaIdentity
from curatarr.web.app import create_app
from miniapp_browser_check import assert_dock
from test_maintenance import MaintenanceTests
from test_miniapp import MiniAppTests, signed_data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--poster", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    fixtures = MaintenanceTests()
    fixtures.setUp()
    shell = MiniAppTests()
    shell.setUp()
    shell.metadata.movie_poster = lambda tmdb_id: ("https://image.tmdb.org/t/p/w500/poster.jpg" if tmdb_id == "1"
                                                  else "https://image.tmdb.org/t/p/w500/broken.jpg" if tmdb_id == "3" else None)
    fixtures.radarr.items[0] = replace(fixtures.radarr.items[0], title="Spider-Man: Brand New Day")
    fixtures.ryot.items[0] = replace(fixtures.ryot.items[0], title="Spider-Man: Brand New Day")
    fixtures.radarr.items[1] = replace(fixtures.radarr.items[1], arr_runtime_state=Runtime.ACTIVE_DOWNLOADED_MONITORED)
    fixtures.ryot.items[2] = replace(fixtures.ryot.items[2], durable_states=frozenset({State.LIBRARY, State.ARCHIVED}))
    service = replace(shell.service, ryot_adapter=fixtures.ryot, radarr_adapter=fixtures.radarr)
    config = replace(shell.config, maintenance=replace(shell.config.maintenance, show_diagnostics=True), users=replace(shell.config.users, admins=(1, 3, 4, 5)))
    class FixtureWriter:
        calls = 0
        def record_acquisition(self, identity):
            self.calls += 1
            assert identity.value == "2"
            fixtures.ryot.items[1] = replace(fixtures.ryot.items[1], durable_states=fixtures.ryot.items[1].durable_states | {State.PREVIOUSLY_ACQUIRED})
            return True
    writer = FixtureWriter()
    app = create_app(config, database=fixtures.path, service=service, acquisition_writer=writer)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()

    def initialize(context, user=1):
        context.route("https://telegram.org/js/*", lambda route: route.fulfill(body=""))
        context.route("https://image.tmdb.org/**", lambda route: route.abort() if route.request.url.endswith("broken.jpg")
                      else route.fulfill(path=str(args.poster.resolve()), content_type="image/jpeg"))
        context.add_init_script("window.Telegram={WebApp:{initData:" + repr(signed_data(user)) +
            ",safeAreaInset:{bottom:34,left:12,right:12},contentSafeAreaInset:{bottom:20,left:4,right:4},"
            "onEvent(){},ready(){},expand(){},BackButton:{show(){},hide(){},onClick(){}}}};")

    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("Fixture server did not start")
            time.sleep(.02)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chromium", timeout=30000)
            try:
                for user_id, (width, height) in zip((1, 3, 4, 5), ((390, 844), (320, 640), (1280, 900), (844, 390))):
                    fixtures.ryot.items[1] = replace(fixtures.ryot.items[1], durable_states=fixtures.ryot.items[1].durable_states - {State.PREVIOUSLY_ACQUIRED})
                    fixtures.storage.save_maintenance_progress(user_id, {"cursor": None, "skipped": [], "category": "all", "parked": False})
                    fixtures.reconciliation.scan()
                    context = browser.new_context(viewport={"width": width, "height": height},
                                                  color_scheme="dark" if width in (390, 844) else "light")
                    initialize(context, user=user_id)
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(f"http://127.0.0.1:{port}")
                    expect(page.locator("#maintenance-toggle")).to_be_visible()
                    page.locator("#watch-check-in").click()
                    expect(page.locator("#viewing-panel")).to_be_visible()
                    expect(page.locator("#viewing-empty")).to_have_text("You're all caught up.")
                    expect(page.locator("#viewing-notice")).to_contain_text("not enabled yet")
                    expect(page.locator("#sync-panel")).to_be_hidden()
                    page.screenshot(path=str(args.output / f"viewing-empty-{width}.png"))
                    page.locator("#maintenance-toggle").click()
                    expect(page.locator("#maintenance-movie")).to_be_visible()
                    expect(page.locator("#maintenance-position")).to_have_text("Movie 1 of 3")
                    expect(page.locator("#maintenance-title")).to_contain_text("Spider-Man")
                    expect(page.locator("#maintenance-findings")).to_contain_text("Radarr itself permits")
                    expect(page.locator("#maintenance-findings button")).to_have_count(0)
                    expect(page.locator("#sync-count")).to_have_text(str(fixtures.storage.reconciliation_report()["finding_count"]))
                    expect(page.locator("#declined-count")).to_have_text("0")
                    expect(page.locator("#maintenance-poster")).to_be_visible()
                    page.wait_for_function("document.getElementById('maintenance-poster').naturalWidth > 0")
                    assert page.locator("#maintenance-poster").evaluate("node => getComputedStyle(node).objectFit") == "contain"
                    assert page.locator("#workspace-nav").count() == 0
                    assert page.locator("#maintenance-toggle").locator("xpath=ancestor::header").count() == 1
                    assert page.locator(".brand-line #status-info svg circle").count() == 1
                    toggle = page.locator("#maintenance-toggle-control").bounding_box()
                    info = page.locator("#status-info").bounding_box()
                    assert info["x"] + info["width"] < toggle["x"]
                    scan = page.locator("#maintenance-scan-time").bounding_box()
                    count = page.locator("#maintenance-summary").bounding_box()
                    assert abs(scan["y"] - count["y"]) < 1, (scan, count)
                    assert_dock(page, "#sync-panel .maintenance-toolbar", ["#maintenance-previous", "#maintenance-next", "#maintenance-skip", "#maintenance-refresh"],
                                {"bottom": 34, "left": 12, "right": 12}, {"bottom": 20, "left": 4, "right": 4})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(args.output / f"maintenance-{width}.png"), full_page=True)
                    page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
                    content = page.locator("#maintenance-findings").bounding_box()
                    assert content["y"] + content["height"] <= page.locator("#sync-panel .maintenance-toolbar").bounding_box()["y"] - 18
                    page.screenshot(path=str(args.output / f"maintenance-bottom-{width}.png"))
                    held = []
                    poster_pattern = "**/api/maintenance/poster/1"
                    if width == 390:
                        page.route(poster_pattern, lambda route: held.append(route))
                        with page.expect_request(poster_pattern):
                            page.evaluate("renderMaintenance(state.maintenanceData)")
                    page.locator("#maintenance-skip").click()
                    expect(page.locator("#sync-count")).to_have_text(str(fixtures.storage.reconciliation_report()["finding_count"]))
                    expect(page.locator("#maintenance-category option[value=skipped]")).to_have_text("Skipped (1)")
                    expect(page.locator("#maintenance-title")).to_have_text("Movie 2 (2026)")
                    if held:
                        held[0].fulfill(json={"poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg"})
                        page.unroute(poster_pattern)
                    expect(page.locator("#maintenance-no-poster")).to_be_visible()
                    expect(page.locator("#maintenance-poster")).to_be_hidden()
                    page.reload()
                    page.locator("#maintenance-toggle").click()
                    page.locator("#sync-branch").click()
                    expect(page.locator("#maintenance-movie")).to_be_visible()
                    expect(page.locator("#maintenance-title")).to_have_text("Movie 2 (2026)")
                    page.locator("#maintenance-category").select_option("archived_active")
                    expect(page.locator("#maintenance-title")).to_have_text("Movie 3 (2026)")
                    expect(page.locator("#maintenance-findings")).to_contain_text("deletion of remaining managed files")
                    expect(page.locator("#maintenance-poster")).to_have_attribute("src", "https://image.tmdb.org/t/p/w500/broken.jpg")
                    expect(page.locator("#maintenance-no-poster")).to_be_visible()
                    page.locator("#maintenance-category").select_option("all")
                    expect(page.locator("#maintenance-movie")).to_be_visible()
                    page.locator("#maintenance-category").select_option("skipped")
                    expect(page.locator("#maintenance-title")).to_contain_text("Spider-Man")
                    fixtures.radarr.error = True
                    page.locator("#maintenance-refresh").click()
                    expect(page.locator("#maintenance-notice")).to_contain_text("scan could not be completed")
                    expect(page.locator("#maintenance-movie")).to_be_hidden()
                    expect(page.locator("#maintenance-skip")).to_be_disabled()
                    expect(page.locator("#maintenance-technical")).to_contain_text("radarr")
                    page.screenshot(path=str(args.output / f"unavailable-{width}.png"), full_page=True)
                    fixtures.radarr.error = False
                    page.locator("#maintenance-refresh").click()
                    expect(page.locator("#maintenance-movie")).to_be_visible()
                    page.evaluate("renderMaintenance({...state.maintenanceData,movie:{...state.maintenanceData.movie,title:'<script>unsafe</script>'}})")
                    expect(page.locator("#maintenance-title")).to_contain_text("<script>unsafe</script>")
                    assert page.locator("#maintenance-title script").count() == 0
                    if width == 1280:
                        for findings, movies, expected in ((74, 74, "74 findings"), (75, 74, "75 findings · 74 movies"),
                                                           (1, 1, "1 finding")):
                            page.evaluate("([findings,movies]) => renderMaintenance({...state.maintenanceData,finding_count:findings,movie_count:movies})",
                                          [findings, movies])
                            expect(page.locator("#maintenance-summary")).to_have_text(expected)
                    page.locator("#maintenance-category").select_option("previously_acquired_missing")
                    expect(page.locator("#maintenance-title")).to_have_text("Movie 2 (2026)")
                    expect(page.locator(".maintenance-reason h3")).to_have_text("Downloaded copy not recorded in Ryot")
                    assert page.locator(".maintenance-reason h3").evaluate("node => getComputedStyle(node).fontWeight") == "700"
                    calls = writer.calls
                    page.locator(".maintenance-correct").click()
                    expect(page.locator("#correction-dialog")).to_be_visible()
                    page.locator("#correction-cancel").click()
                    assert writer.calls == calls
                    page.locator(".maintenance-correct").click()
                    page.screenshot(path=str(args.output / f"correction-{width}.png"))
                    page.locator("#correction-confirm").click()
                    expect(page.locator("#maintenance-notice")).to_contain_text("added in Ryot and verified")
                    expect(page.locator("#maintenance-movie")).to_be_hidden()
                    assert writer.calls == calls + 1
                    original_history = fixtures.ryot.items[2]
                    fixtures.ryot.items[2] = replace(original_history, year=2004, durable_states={State.LIBRARY},
                                                    identities={MediaIdentity("ryot", "3"), MediaIdentity("tmdb", "999")})
                    page.locator("#maintenance-refresh").click()
                    page.locator("#maintenance-category").select_option("identity_review")
                    expect(page.locator(".maintenance-reason h3")).to_have_text("Same title, different catalogue record")
                    expect(page.locator(".identity-comparison")).to_contain_text("Radarr · acquisition reference")
                    expect(page.locator(".identity-comparison")).to_contain_text("Movie 3 (2004)")
                    expect(page.locator(".identity-comparison")).to_contain_text("tmdb:999")
                    expect(page.locator(".maintenance-correct")).to_have_count(0)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(args.output / f"identity-{width}.png"), full_page=True)
                    fixtures.ryot.items[2] = original_history
                    question = fixtures.storage.create_pending_decision(
                        media_key="ryot:1", decision_type="viewing_followup", audience="shared_household",
                        state_fingerprint="browser", context={"questions": ["watched", "rating", "retention"]})
                    page.locator("#watch-check-in").click()
                    expect(page.locator("#viewing-movie")).to_be_visible()
                    expect(page.locator("#viewing-title")).to_contain_text("Spider-Man")
                    expect(page.locator("#viewing-questions")).to_contain_text("How do you rate it?")
                    expect(page.locator("#sync-panel")).to_be_hidden()
                    expect(page.locator("#viewing-poster")).to_be_visible()
                    page.wait_for_function("document.getElementById('viewing-poster').naturalWidth > 0")
                    page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
                    assert_dock(page, "#viewing-panel .maintenance-toolbar", ["#viewing-previous", "#viewing-next", "#viewing-refresh"],
                                {"bottom": 34, "left": 12, "right": 12}, {"bottom": 20, "left": 4, "right": 4})
                    page.screenshot(path=str(args.output / f"viewing-{width}.png"), full_page=True)
                    expect(page.locator("#viewing-skip, #viewing-skipped, #viewing-pending, #maintenance-skipped, #maintenance-pending")).to_have_count(0)
                    page.locator("#viewing-next").click()
                    expect(page.locator("#viewing-movie")).to_be_visible()
                    assert fixtures.storage.get_pending_decision(question.decision_id).status == "open"
                    with fixtures.storage.connection:
                        fixtures.storage.connection.execute("UPDATE pending_decision SET context_json=? WHERE id=?",
                            (json.dumps({"questions": ["watched", "rating"], "workflow": "viewing_v1",
                                         "delivery": {"state": "removed"}}), question.decision_id))
                    page.locator("#viewing-refresh").click()
                    expect(page.locator("#viewing-reopen")).to_be_visible()
                    def reopen(route):
                        assert route.request.post_data_json == {"decision_id": question.decision_id, "media_key": question.media_key}
                        with fixtures.storage.connection:
                            fixtures.storage.connection.execute("UPDATE pending_decision SET context_json=? WHERE id=?",
                                (json.dumps({"questions": ["watched", "rating"], "workflow": "viewing_v1",
                                             "delivery": {"state": "pending"}}), question.decision_id))
                        route.fulfill(content_type="application/json", body=json.dumps({"notice": "Queued for the next Telegram viewing check."}))
                    context.route("**/api/viewing/reopen", reopen)
                    page.locator("#viewing-reopen").click()
                    expect(page.locator("#viewing-notice")).to_contain_text("Queued for the next Telegram")
                    expect(page.locator("#viewing-reopen")).to_be_hidden()
                    context.unroute("**/api/viewing/reopen", reopen)
                    assert fixtures.storage.get_pending_decision(question.decision_id).status == "open"
                    fixtures.storage.close_open_decision(question.decision_id, "resolved")
                    page.locator("#maintenance-toggle").click()
                    page.locator("#settings-branch").click()
                    expect(page.locator("#settings-panel")).to_be_visible()
                    expect(page.locator("#owned-cleanup")).not_to_be_checked()
                    expect(page.locator("#owned-last-run")).to_have_text("Last run: Never run")
                    expect(page.locator("#cleanup-answered")).not_to_be_checked()
                    expect(page.locator("#cleanup-unanswered")).not_to_be_checked()
                    page.locator("#cleanup-answered").check()
                    expect(page.locator("#settings-notice")).to_have_text("Saved.")
                    expect(page.locator("#cleanup-unanswered")).not_to_be_checked()
                    page.locator("#cleanup-unanswered").check()
                    expect(page.locator("#settings-notice")).to_have_text("Saved.")
                    expect(page.locator("#cleanup-answered")).to_be_checked()
                    page.locator("#cleanup-answered").uncheck()
                    expect(page.locator("#settings-notice")).to_have_text("Saved.")
                    expect(page.locator("#cleanup-unanswered")).to_be_checked()
                    page.locator("#cleanup-unanswered").uncheck()
                    expect(page.locator("#settings-notice")).to_have_text("Saved.")
                    page.locator("#prompt-info").click()
                    expect(page.locator("#prompt-explanation")).to_contain_text("36 hours")
                    page.locator("#prompt-info").click()
                    page.locator("#installation-name").fill("Chickenhouse")
                    page.locator("#name-save").click()
                    expect(page.locator("#settings-notice")).to_have_text("Saved.")
                    expect(page.locator(".mode")).to_have_text("Chickenhouse · Settings")
                    page.locator("#owned-info").click()
                    expect(page.locator("#owned-explanation")).to_be_visible()
                    expect(page.locator("#owned-explanation")).to_contain_text("Series, other collections")
                    page.locator("#owned-info").click()
                    page.locator("#owned-cleanup").check()
                    expect(page.locator("#settings-notice")).to_have_text("Saved.")
                    expect(page.locator("#owned-cleanup")).to_be_checked()
                    expect(page.locator("#correction-dialog")).to_be_hidden()
                    page.locator("#owned-cleanup").uncheck()
                    expect(page.locator("#settings-notice")).to_have_text("Saved.")
                    page.locator("#installation-name").fill("<b>Chickenhouse</b>")
                    page.locator("#name-save").click()
                    expect(page.locator(".mode")).to_have_text("<b>Chickenhouse</b> · Settings")
                    assert page.locator(".mode b").count() == 0
                    page.locator("#installation-name").fill("Chickenhouse")
                    page.locator("#name-save").click()
                    expect(page.locator(".mode")).to_have_text("Chickenhouse · Settings")
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(args.output / f"settings-{width}.png"), full_page=True)
                    page.locator("#settings-branch").press("Home")
                    expect(page.locator("#sync-panel")).to_be_visible()
                    page.locator("#maintenance-toggle").click()
                    expect(page.locator("#search-form")).to_be_visible()
                    expect(page.locator("#maintenance")).to_be_hidden()
                    assert not errors, errors
                    print(f"PASS {width}x{height}: compact header/counts, poster/fallback, safe-area dock, filters, skip/resume, outages, return to movies", flush=True)
                    context.close()
                context = browser.new_context()
                initialize(context, user=2)
                page = context.new_page()
                page.goto(f"http://127.0.0.1:{port}")
                expect(page.locator("#search-submit")).to_be_enabled()
                expect(page.locator("#maintenance-toggle-control")).to_be_hidden()
                status = page.evaluate("async () => (await fetch('/api/maintenance',{headers:{Authorization:'tma '+Telegram.WebApp.initData}})).status")
                assert status == 403
                status = page.evaluate("async () => (await fetch('/api/settings',{headers:{Authorization:'tma '+Telegram.WebApp.initData}})).status")
                assert status == 403
                context.close()
                print("PASS standard user: maintenance hidden and API forbidden", flush=True)
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        fixtures.doCleanups()


if __name__ == "__main__":
    main()
