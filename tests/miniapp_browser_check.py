"""Optional browser QA against the real web app with isolated service fixtures."""

import argparse
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import socket
import threading
import time

from playwright.sync_api import sync_playwright, expect
import uvicorn

from curatarr.domain import ArrRuntimeState, DurableState, MediaIdentity
from curatarr.services.identity import IdentityMatcher
from curatarr.services.search import SearchDecisionService
from curatarr.web.app import create_app
from test_miniapp import MiniAppTests, signed_data
from test_telegram_service import _FakeMetadataAdapter, _FakeRyotAdapter, _FakeRadarrAdapter, _movie


def assert_dock(page, selector, buttons, safe, content):
    bar = page.locator(selector).bounding_box()
    viewport = page.viewport_size
    for button in buttons:
        box = page.locator(button).bounding_box()
        assert box["x"] >= safe.get("left", 0) + content.get("left", 0) + 11
        assert box["x"] + box["width"] <= viewport["width"] - safe.get("right", 0) - content.get("right", 0) - 11
        assert box["y"] >= bar["y"] + 10
        assert box["y"] + box["height"] <= viewport["height"] - safe.get("bottom", 0) - content.get("bottom", 0) - 11
    reserved = page.locator("main").evaluate("node => parseFloat(getComputedStyle(node).paddingBottom)")
    assert reserved >= bar["height"] + 19
    return bar


def refresh_with_fixture(page, payload):
    pattern = "**/api/search/*/0/details"
    handler = lambda route: route.fulfill(json=payload)
    page.route(pattern, handler)
    try:
        with page.expect_response(pattern):
            page.locator("#refresh").click()
        expect(page.locator("#refresh")).to_be_enabled()
    finally:
        page.unroute(pattern, handler)


def assert_status_pair(page):
    badges = page.locator("#radarr-facts .status-pair .badge")
    expect(badges).to_have_count(2)
    first, second = [badges.nth(index).bounding_box() for index in (0, 1)]
    container = page.locator("#radarr-facts .status-pair").locator("..").bounding_box()
    assert abs(first["y"] - second["y"]) < 1
    assert second["x"] >= first["x"] + first["width"] + 5
    assert second["x"] + second["width"] <= container["x"] + container["width"] + 1, (first, second, container)
    neutral = "rgb(68, 68, 68)" if page.evaluate("matchMedia('(prefers-color-scheme: dark)').matches") else "rgb(230, 230, 230)"
    expected = ["rgb(0, 133, 61)" if badges.first.inner_text() == "Downloaded" else neutral,
                "rgb(23, 106, 166)" if badges.last.inner_text() == "Monitored" else neutral]
    assert badges.evaluate_all("nodes => nodes.map(node => getComputedStyle(node).backgroundColor)") == expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poster", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    fixtures = MiniAppTests()
    fixtures.setUp()
    poster = replace(_movie(), poster_url="https://image.tmdb.org/t/p/w500/poster.jpg",
                     overview="Peter Parker struggles to balance his life with his responsibilities as Spider-Man.")
    second = replace(_movie(title="An Extremely Long Movie Title <script>alert('unsafe')</script>"),
                     identities=frozenset({MediaIdentity("tmdb", "999")}))
    metadata = _FakeMetadataAdapter((poster, second))
    ryot = _FakeRyotAdapter(replace(_movie(durable_states={DurableState.LIBRARY, DurableState.WATCHED, DurableState.PREVIOUSLY_ACQUIRED}),
                                    completed_watch_count=2, last_watched_date="2024-06-01"))
    arr = _FakeRadarrAdapter(replace(_movie(runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_UNMONITORED),
                                    identities=frozenset({MediaIdentity("tmdb", "558"), MediaIdentity("radarr", "123")}),
                                    quality_profile_id=3, quality_name="Bluray-2160p", movie_file_id=8))
    arr.quality_profile = lambda profile_id: {"name": "Movies > 4K EN+FR with a long household profile name",
        "upgrade_allowed": True, "cutoff_quality": "4K group", "score_target": 10000,
        "cutoff_qualities": [{"resolution": 2160, "modifier": "none"}, {"resolution": 2160, "modifier": "remux"}],
        "language": {"id": 1, "name": "English"}, "format_items": [{"id": 50, "score": 100}, {"id": 51, "score": 500}]}
    arr.file_details = lambda file_id, movie_id: {"score": 125, "quality": {"resolution": 2160, "modifier": "none"},
        "format_ids": [51], "language_ids": [1], "quality_cutoff_not_met": False}
    arr.core_formats = lambda: [{"id": 50, "key": "remux", "label": "Remux", "category": "remux"},
                                {"id": 51, "key": "language:2", "label": "French", "category": "language"}]
    service = SearchDecisionService(metadata, ryot, IdentityMatcher(), arr)
    class Playback:
        def playback_facts(self, candidate):
            return {"status": "ready", "version_count": 1,
                    "versions": [{"audio_languages": ["eng", "fra"], "audio_complete": True}]}
    app = create_app(fixtures.config, service=service, playback_adapter=Playback())
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("Fixture server did not start")
            time.sleep(.02)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chromium", timeout=30000)
            try:
                for width, height in ((390, 844), (320, 640), (1280, 900), (844, 390)):
                    context = browser.new_context(viewport={"width": width, "height": height},
                                                  color_scheme="dark" if width in (390, 844) else "light")
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.route("https://telegram.org/js/*", lambda route: route.fulfill(body=""))
                    page.route("https://image.tmdb.org/**", lambda route: route.fulfill(
                        path=str(args.poster.resolve()), content_type="image/jpeg"))
                    page.add_init_script("window.__tgEvents = {}; window.Telegram = {WebApp: {initData: " + repr(signed_data()) +
                                         ", safeAreaInset:{}, contentSafeAreaInset:{}, onEvent(name, fn){window.__tgEvents[name]=fn}, ready(){}, expand(){}, BackButton: {show(){}, hide(){}, onClick(){}}}};")
                    # Hold the startup counter to reproduce the Linux CI race deterministically.
                    # Search must remain usable and a late counter reply must preserve the new draft.
                    held_summaries = []
                    summary_pattern = "**/api/viewing/summary"
                    page.route(summary_pattern, lambda route: held_summaries.append(route))
                    page.add_init_script("sessionStorage.setItem('curatarr.query', 'Earlier query');")
                    with page.expect_request(summary_pattern):
                        page.goto(f"http://127.0.0.1:{port}")
                    expect(page.locator("#search-submit")).to_be_enabled()
                    expect(page.locator("#query")).to_have_value("Earlier query")
                    page.locator("#query").fill("Spider-Man")
                    assert len(held_summaries) == 1
                    with page.expect_response(summary_pattern):
                        held_summaries.pop().fulfill(json={"available": False, "pending_movie_count": 0})
                    expect(page.locator("#watch-count")).to_have_text("0")
                    expect(page.locator("#query")).to_have_value("Spider-Man")
                    page.unroute(summary_pattern)
                    page.screenshot(path=str(args.output / f"search-{width}.png"), full_page=True)
                    print(f"Checking {width}x{height}", flush=True)
                    page.locator("#search-submit").click()
                    expect(page.locator("#watch-status")).to_have_text("2 times")
                    expect(page.locator("#availability")).to_have_text("Available")
                    expect(page.locator("#deleted")).to_be_hidden()
                    expect(page.locator("#audio-languages")).to_contain_text("English")
                    expect(page.locator("#audio-languages")).to_contain_text("French")
                    expect(page.locator("#monitoring")).to_have_text("Unmonitored")
                    assert not page.locator("#credits").is_visible()
                    assert page.locator("footer").count() == 0
                    expect(page.locator("#previous")).to_be_visible()
                    expect(page.locator("#next")).to_be_visible()
                    assert not page.locator("#description").evaluate("element => element.open")
                    assert page.locator("#poster").evaluate("img => img.complete && img.naturalWidth > 0")
                    initial_nav = page.locator(".carousel-nav").bounding_box()
                    assert_dock(page, ".carousel-nav", ["#previous", "#select", "#next"], {}, {})
                    counter = page.locator("#position").bounding_box()
                    next_button = page.locator("#next").bounding_box()
                    assert counter["y"] + counter["height"] <= next_button["y"] - 6
                    # Native Telegram chrome can change its content insets after launch.
                    safe = {"bottom": 34, "left": 12, "right": 12}
                    content = {"bottom": 20, "left": 4, "right": 4}
                    page.evaluate("([safe,content]) => {Telegram.WebApp.safeAreaInset=safe; Telegram.WebApp.contentSafeAreaInset=content; __tgEvents.contentSafeAreaChanged();}", [safe, content])
                    page.wait_for_function("parseFloat(document.documentElement.style.getPropertyValue('--bottom-bar-height')) > 140")
                    assert_dock(page, ".carousel-nav", ["#previous", "#select", "#next"], safe, content)
                    browse_button = page.locator("#next").bounding_box()
                    browse_button_bottom = browse_button["y"] + browse_button["height"]
                    poster_bounds = page.locator(".poster-stage").bounding_box()
                    assert poster_bounds["height"] >= min(width * 1.2, height * .6)
                    page.screenshot(path=str(args.output / f"poster-{width}.png"))
                    page.locator(".household-summary").scroll_into_view_if_needed()
                    page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
                    summary = page.locator(".household-summary").bounding_box()
                    dock = page.locator(".carousel-nav").bounding_box()
                    assert summary["y"] + summary["height"] <= dock["y"] - 18
                    page.screenshot(path=str(args.output / f"summary-{width}.png"))
                    for execution, expected in (({"status": "matched", "state": "active_downloaded_monitored"}, "Monitored"),
                                                ({"status": "matched", "state": "active_downloaded_unmonitored"}, "Unmonitored"),
                                                ({"status": "unavailable"}, None), ({"status": "not_present"}, None),
                                                ({"status": "needs_review"}, None), ({"status": "matched", "state": "invalid"}, None)):
                        page.evaluate("execution => status({library_status:{code:'available',label:'Available'},radarr:execution})", execution)
                        assert page.locator("#availability").evaluate("node => getComputedStyle(node).backgroundColor") == "rgb(0, 133, 61)"
                        if expected:
                            expect(page.locator("#monitoring")).to_have_text(expected)
                            expect(page.locator("#monitoring")).to_be_visible()
                            expect(page.locator("#monitoring")).to_have_attribute("data-kind", expected.lower())
                        else:
                            expect(page.locator("#monitoring")).to_be_hidden()
                    page.locator("#description summary").click()
                    expect(page.locator("#overview")).to_be_visible()
                    page.locator("#description summary").click()
                    page.locator("#select").click()
                    expect(page.locator("#detail-summary")).to_have_text("Watched · Active in Radarr")
                    expect(page.locator("#detail-warning")).to_be_hidden()
                    expect(page.locator("#decision-note")).to_have_count(0)
                    assert page.locator("#detail-summary").bounding_box()["height"] <= 20
                    expect(page.locator("#radarr-facts")).to_contain_text("Unmonitored")
                    expect(page.locator("#ryot-facts")).to_contain_text("01-06-2024")
                    assert page.locator("#ryot-facts dt").all_text_contents() == ["Status", "Watched", "Last watched"]
                    expect(page.locator("#ryot-facts .badge")).to_have_text("Available")
                    assert not any(label in page.locator("#ryot-facts").inner_text() for label in ("Library record", "Inventory history", "Workflow record"))
                    assert "Previously owned" not in page.locator("#detail").inner_text()
                    expect(page.locator("#radarr-profile")).to_have_text("Movies > 4K EN+FR with a long household profile name")
                    expect(page.locator("#radarr-profile + .profile-summary")).to_have_count(0)
                    expect(page.locator("#radarr-score")).to_have_text("125/10.000")
                    expect(page.locator("#radarr-score + small")).to_have_count(0)
                    expect(page.locator("#radarr-facts [aria-label='Remux bonus: not matched']")).to_have_text("Remux bonus -")
                    expect(page.locator("#radarr-facts [aria-label='4K: matched']")).to_have_attribute("data-kind", "achieved")
                    expect(page.locator("#radarr-facts .status-pair .badge")).to_have_text(["Downloaded", "Unmonitored"])
                    expect(page.locator("#radarr-facts .status-pair .badge").first).to_have_attribute("data-kind", "downloaded")
                    assert page.locator("#radarr-facts .status-pair .badge").first.evaluate("node => getComputedStyle(node).backgroundColor") == "rgb(0, 133, 61)"
                    expect(page.locator("#radarr-facts .status-pair .badge").last).to_have_attribute("data-kind", "unmonitored")
                    assert_status_pair(page)
                    radarr_labels = ["Status", "Profile", "Upgrade goals", "Format score", "Current quality", "File size", "Movie ID"]
                    assert page.locator("#radarr-facts dt").all_text_contents() == radarr_labels
                    assert page.locator("#ryot-tags, #radarr-tags").count() == 0
                    for selector, colour in ((".radarr-section h2", "rgb(255, 194, 48)"),
                                             (".playback-section h2", "rgb(170, 92, 195)")):
                        assert page.locator(selector).evaluate("node => getComputedStyle(node).borderLeftColor") == colour
                    assert page.locator(".ryot-section h2").evaluate("node => getComputedStyle(node).borderLeftColor") in ("rgb(215, 215, 215)", "rgb(72, 72, 72)")
                    expect(page.locator("#playback-facts .badge")).to_have_count(3)
                    expect(page.locator("#playback-facts [data-kind='information']")).to_have_count(2)
                    assert page.locator("#playback-facts [data-kind='information']").first.evaluate(
                        "node => getComputedStyle(node).backgroundColor") != page.locator("#monitoring").evaluate(
                        "node => getComputedStyle(node).backgroundColor")
                    assert page.locator("#playback-facts dt").all_text_contents() == ["Availability", "Copy 1 audio"]
                    expect(page.locator(".carousel-nav")).to_be_hidden()
                    assert_dock(page, ".detail-toolbar", ["#back", "#refresh"], safe, content)
                    detail_button = page.locator("#refresh").bounding_box()
                    assert abs(browse_button_bottom - detail_button["y"] - detail_button["height"]) < 1
                    page.screenshot(path=str(args.output / f"details-{width}.png"))
                    page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
                    note = page.locator(".playback-section").bounding_box()
                    assert note["y"] + note["height"] <= page.locator(".detail-toolbar").bounding_box()["y"] - 18
                    page.screenshot(path=str(args.output / f"playback-{width}.png"))
                    baseline = page.evaluate("() => api(`/api/search/${state.searchId}/${state.index}/details`)")
                    for runtime, label, monitoring, kind in (
                        ("active_downloaded_monitored", "Downloaded", "Monitored", "monitored"),
                        ("active_missing_monitored", "Not downloaded", "Monitored", "monitored"),
                        ("active_missing_unmonitored", "Not downloaded", "Unmonitored", "unmonitored"),
                    ):
                        variant = deepcopy(baseline)
                        variant["radarr"]["state"] = runtime
                        variant["radarr"]["file_present"] = runtime.startswith("active_downloaded")
                        refresh_with_fixture(page, variant)
                        expect(page.locator("#radarr-facts .status-pair .badge")).to_have_text([label, monitoring])
                        expect(page.locator("#radarr-facts .status-pair .badge").last).to_have_attribute("data-kind", kind)
                        assert_status_pair(page)
                        assert page.locator("#radarr-facts dt").all_text_contents() == radarr_labels
                        expect(page.locator("#radarr-score")).to_have_text("125/10.000" if variant["radarr"]["file_present"] else "–")
                    page.locator("#radarr-facts .status-pair").evaluate("node => node.scrollIntoView({block:'center'})")
                    page.screenshot(path=str(args.output / f"not-downloaded-{width}.png"))
                    refresh_with_fixture(page, baseline)
                    if width == 390:
                        for score in (0, -5000, None):
                            variant = deepcopy(baseline)
                            variant["radarr"]["file"]["score"] = score
                            variant["radarr"]["upgrade_goals"][0]["status"] = "unknown"
                            refresh_with_fixture(page, variant)
                            expect(page.locator("#radarr-score")).to_have_text("Not confirmed" if score is None else f"{score:,}".replace(",", ".") + "/10.000")
                            expect(page.locator("#radarr-facts [aria-label='4K: unknown']")).to_have_text("4K ?")
                        variant = deepcopy(baseline)
                        variant["radarr"]["profile"] = {"name": "Movies > <script>unsafe</script>", "upgrade_allowed": False}
                        variant["history"]["states"] += ["archived", "radarr"]
                        variant["library_status"] = {"code": "archived", "label": "Archived", "deleted": False,
                                                     "note": "Archived in Ryot, but still present. Removal needs review."}
                        variant["playback"]["versions"] += [{"audio_languages": ["hin"], "audio_complete": False}]
                        refresh_with_fixture(page, variant)
                        expect(page.locator("#radarr-profile + .profile-summary")).to_have_text("Upgrades disabled")
                        expect(page.locator("#radarr-profile")).to_have_text("Movies > <script>unsafe</script>")
                        assert page.locator("#radarr-profile script").count() == 0
                        expect(page.locator("#ryot-facts .badge")).to_have_text("Archived")
                        expect(page.locator("#ryot-note")).to_contain_text("Removal needs review")
                        expect(page.locator("#detail-warning")).to_have_text("Archived in Ryot but active in Radarr.")
                        assert page.locator("#playback-facts dt").all_text_contents() == ["Availability", "Copy 1 audio", "Copy 2 audio"]
                        expect(page.locator("#playback-facts .badge")).to_have_text(["Ready to watch", "English", "French", "Hindi", "Unknown track"])
                        variant = deepcopy(baseline)
                        variant["radarr"].pop("profile")
                        variant["radarr"]["profile_status"] = "unavailable"
                        refresh_with_fixture(page, variant)
                        expect(page.locator("#radarr-profile")).to_have_text("Not confirmed")
                        expect(page.locator("#radarr-profile + .profile-summary")).to_have_text("Profile lookup unavailable")
                        expect(page.locator("#playback-facts .badge").first).to_have_text("Ready to watch")
                        for lookup_status in ("unavailable", "needs_review", "not_present"):
                            variant = deepcopy(baseline)
                            variant["radarr"] = {"status": lookup_status}
                            variant["history"] = {"status": "unavailable", "states": []}
                            variant["playback"] = {"status": "unavailable", "versions": []}
                            variant["availability"] = {"code": "unknown", "label": "Check unavailable"}
                            variant["library_status"] = {"code": "unavailable", "label": "Status unavailable", "deleted": False}
                            refresh_with_fixture(page, variant)
                            assert page.locator("#radarr-facts dt").all_text_contents() == radarr_labels
                            assert page.locator("#radarr-facts dd").all_text_contents()[1:] == ["–" if lookup_status == "not_present" else "Not confirmed"] * 6
                            assert page.locator("#radarr-facts .badge, #radarr-profile").count() == 0
                            expect(page.locator("#ryot-facts")).to_contain_text("History unavailable")
                            expect(page.locator("#detail-warning")).to_contain_text("Watch history could not be checked.")
                            expect(page.locator("#detail-summary")).not_to_contain_text("Active in Radarr")
                            if lookup_status == "needs_review":
                                expect(page.locator("#detail-warning")).to_contain_text("identity needs review")
                            if lookup_status == "unavailable":
                                expect(page.locator("#detail-warning")).to_contain_text("Radarr status could not be checked.")
                            expect(page.locator("#ryot-facts dd").nth(2)).to_have_text("Not confirmed")
                            expect(page.locator("#playback-facts")).to_contain_text("Library check")
                            expect(page.locator("#playback-facts dd").last).to_have_text("Not confirmed")
                        variant = deepcopy(baseline)
                        variant["library_status"] = {"code": "archived", "label": "Archived", "deleted": True}
                        variant["history"]["states"].append("archived")
                        variant["history"]["last_watched_date"] = None
                        variant["radarr"] = {"status": "not_present"}
                        variant["playback"] = {"status": "not_present", "versions": []}
                        variant["availability"] = {"code": "not_available", "label": "Not available"}
                        variant["decision_path"] = "reactivate_archived"
                        refresh_with_fixture(page, variant)
                        expect(page.locator("#ryot-facts .badge")).to_have_text(["Archived", "Deleted"])
                        assert page.locator("#ryot-facts dt").all_text_contents() == ["Status", "Watched", "Last watched"]
                        expect(page.locator("#ryot-facts dd").nth(2)).to_have_text("–")
                        expect(page.locator("#playback-facts dd").last).to_have_text("–")
                        for history_status in ("no_completed_watch", "unknown"):
                            variant["history"] = {"status": history_status, "watch_count": 0, "states": [], "last_watched_date": None}
                            refresh_with_fixture(page, variant)
                            expect(page.locator("#ryot-facts dd").nth(1)).to_have_text("0 recorded times")
                            expect(page.locator("#detail-summary")).to_have_text("Not in Radarr")
                            expect(page.locator("#ryot-facts dd").nth(2)).to_have_text("–")
                        page.screenshot(path=str(args.output / "archived-deleted-390.png"))
                        page.evaluate("() => status({library_status:{code:'wanted',label:'Wanted',deleted:true}})")
                        expect(page.locator("#deleted")).to_have_js_property("hidden", False)
                        page.evaluate("() => status({library_status:{code:'available',label:'Available',deleted:true}})")
                        expect(page.locator("#deleted")).to_have_js_property("hidden", True)
                        refresh_with_fixture(page, baseline)
                    page.locator("#status-info").click()
                    expect(page.locator("#credits")).to_be_visible()
                    expect(page.locator("#credits")).to_contain_text("This product uses the TMDB API")
                    assert page.locator("#credits img").evaluate("img => img.complete && img.naturalWidth > 0")
                    page.screenshot(path=str(args.output / f"info-{width}.png"))
                    page.locator("#legend-title").scroll_into_view_if_needed()
                    expect(page.locator("#definitions .legend .badge")).to_have_count(10)
                    page.screenshot(path=str(args.output / f"legend-{width}.png"))
                    page.locator("#close-definitions").click()
                    page.locator("#refresh").click()
                    expect(page.locator("#refresh")).to_be_enabled()
                    assert_dock(page, ".detail-toolbar", ["#back", "#refresh"], safe, content)
                    page.locator("#back").click()
                    expect(page.locator("#position")).to_have_text("Result 1 of 2")
                    page.locator("#previous").click()
                    expect(page.locator("#position")).to_have_text("Result 2 of 2")
                    expect(page.locator("#no-poster")).to_be_visible()
                    assert page.locator("#movie-title").text_content() == second.title
                    page.screenshot(path=str(args.output / f"missing-{width}.png"), full_page=True)
                    page.locator("#list-mode").click()
                    expect(page.locator(".list-item")).to_have_count(2)
                    page.locator("#poster-mode").click()
                    expect(page.locator("#position")).to_have_text("Result 2 of 2")
                    page.locator("#next").click()
                    expect(page.locator("#position")).to_have_text("Result 1 of 2")
                    final_nav = page.locator(".carousel-nav").bounding_box()
                    assert initial_nav["width"] == final_nav["width"]
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    page.locator("#status-info").click()
                    expect(page.locator("#definitions")).to_be_visible()
                    page.locator("#close-definitions").click()
                    assert not errors, errors
                    print(f"PASS {width}x{height}: artwork, navigation, detail/back, description, list, status definitions, no horizontal overflow")
                    context.close()
                context = browser.new_context()
                page = context.new_page()
                page.route("https://telegram.org/js/*", lambda route: route.fulfill(body=""))
                page.goto(f"http://127.0.0.1:{port}")
                expect(page.locator("#message")).to_contain_text("Open Curatarr from Telegram")
                expect(page.locator("#search-submit")).to_be_disabled()
                context.close()
                print("PASS ordinary browser: authentication required")
            finally:
                browser.close()
    finally:
        fixtures.client.close()
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


if __name__ == "__main__":
    main()
