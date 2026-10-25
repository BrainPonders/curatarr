"""Authenticated movie reads, shared viewing input and admin maintenance."""

from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import secrets
import json
import sqlite3
import threading
import time
from typing import Literal
from urllib.parse import parse_qsl

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictInt

from curatarr.adapters import AdapterError
from curatarr.config import CuratarrConfig
from curatarr.domain import MatchConfidence, MediaIdentity, MediaItem, MediaType
from curatarr.integrations.jellyfin import JellyfinReadAdapter
from curatarr.integrations.ryot.adapter import RyotReadAdapter
from curatarr.integrations.ryot.corrections import RyotAcquisitionWriter
from curatarr.runtime import build_search_decisions
from curatarr.services.identity import IdentityMatcher
from curatarr.services.display import availability, library_status
from curatarr.services.upgrades import upgrade_goals
from curatarr.services.maintenance import MaintenanceService
from curatarr.services.viewing import ViewingFollowupService, pending_movie_count, visible_to, queue_version
from curatarr.services.viewing_workflow import bot_key, ViewingWorkflow
from curatarr.services.viewing_corrections import ViewingCorrections
from curatarr.services.system_maintenance import SystemMaintenance
from curatarr.integrations.ryot.viewing import RyotViewingAdapter, ViewingEventMissing, ViewingEventChanged
from curatarr.integrations.telegram.client import TelegramClient
from curatarr.services.reconciliation import MovieReconciliationService
from curatarr.services.settings import InstallationSettings
from curatarr.storage import Storage, StorageError
from curatarr.web.auth import AuthenticationError, validate_init_data
from curatarr.workflows.search_add import SearchAddPlanner


STATIC = Path(__file__).parent / "static"
SEARCH_LIFETIME_SECONDS = 1800


class MaintenanceNavigation(BaseModel):
    model_config = {"extra": "forbid"}
    action: Literal["open", "next", "previous", "skip", "refresh", "filter", "skipped", "pending"] = "open"
    category: str | None = Field(default=None, max_length=80)
    expected_cursor: str | None = Field(default=None, max_length=200)


class MaintenanceCorrection(BaseModel):
    model_config = {"extra": "forbid"}
    media_key: str = Field(min_length=1, max_length=200)
    finding_id: int = Field(gt=0, strict=True)
    confirm: Literal[True]


class AcquisitionPreview(BaseModel):
    model_config = {"extra": "forbid"}
    mode: Literal["catalogue_only", "add_search"]
    profile_id: int | None = Field(default=None, gt=0, strict=True)
    root_id: int | None = Field(default=None, gt=0, strict=True)
    reactivate: bool = Field(default=False, strict=True)
    watched: bool = Field(default=False, strict=True)
    watched_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    rating: int | None = Field(default=None, ge=1, le=5, strict=True)


class ProfileChangeConfirm(BaseModel):
    model_config = {'extra': 'forbid'}
    decision_id: int = Field(gt=0, strict=True)
    version: str = Field(min_length=64, max_length=64)
    profile_id: int = Field(gt=0, strict=True)
    confirm: Literal[True]


class AcquisitionConfirm(BaseModel):
    model_config = {"extra": "forbid"}
    decision_id: int = Field(gt=0, strict=True)
    version: str = Field(min_length=64, max_length=64)
    confirm: Literal[True]


class DiscoverySettingsChange(BaseModel):
    model_config = {'extra': 'forbid'}
    expected_revision: int = Field(ge=0, strict=True)
    enabled: bool = Field(strict=True)
    months: int = Field(ge=1, le=120, strict=True)
    rating: float = Field(ge=0, le=10, allow_inf_nan=False, strict=True)
    votes: int = Field(ge=0, le=1000000, strict=True)
    excluded_genres: list[StrictInt] = Field(max_length=100)
    max_pages: int = Field(default=5, ge=1, le=50, strict=True)


class WantedSettingsChange(BaseModel):
    model_config = {'extra': 'forbid'}
    expected_revision: int = Field(ge=0, strict=True)
    imdb_user: str = Field(pattern=r'^ur[0-9]{1,16}$')
    enabled: bool = Field(strict=True)
    mode: Literal['catalogue_only', 'add_search']
    profile_id: int | None = Field(default=None, gt=0, strict=True)
    root_id: int | None = Field(default=None, gt=0, strict=True)


class WantedImport(BaseModel):
    model_config = {'extra': 'forbid'}
    source_id: str = Field(min_length=64, max_length=64)
    expected_revision: int = Field(ge=0, strict=True)
    content: str = Field(max_length=5000000)
    count: int = Field(ge=0, le=50000, strict=True)
    complete: Literal[True]


class SuggestionAnswer(BaseModel):
    model_config = {'extra': 'forbid'}
    revision: int = Field(ge=0, strict=True)
    action: Literal['yes', 'no', 'reopen', 'open']


class WantedAction(BaseModel):
    model_config = {'extra': 'forbid'}
    source_id: str = Field(min_length=64, max_length=64)
    item_key: str = Field(pattern=r'^imdb:tt[0-9]{5,12}$')
    revision: int = Field(ge=0, strict=True)
    action: Literal['open', 'withdraw']


class ViewingNavigation(BaseModel):
    model_config = {"extra": "forbid"}
    action: Literal["open", "next", "previous", "refresh"] = "open"
    expected_cursor: str | None = Field(default=None, max_length=200)


class SettingsChange(BaseModel):
    model_config = {"extra": "forbid"}
    expected_revision: int = Field(ge=0, strict=True)
    display_name: str | None = Field(default=None, min_length=1, max_length=60, strict=True)
    owned_cleanup_enabled: bool | None = Field(default=None, strict=True)
    cleanup_answered_prompts: bool | None = Field(default=None, strict=True)
    cleanup_unanswered_prompts: bool | None = Field(default=None, strict=True)


class ViewingReopen(BaseModel):
    model_config = {"extra": "forbid"}
    decision_id: int = Field(gt=0, strict=True)
    media_key: str = Field(min_length=1, max_length=200)


class ViewingAnswer(ViewingReopen):
    action: str = Field(min_length=1, max_length=30)
    expected_version: str = Field(min_length=64, max_length=64)


class ViewingCorrection(ViewingAnswer):
    confirm: bool = Field(default=False, strict=True)
    preview_token: str | None = Field(default=None, min_length=64, max_length=64)


def create_app(config: CuratarrConfig, *, database: Path | None = None, service=None, playback_adapter=None,
               acquisition_writer=None, acquisition_factory=None, source_factory=None, viewing_factory=None, intake_factory=None, profile_factory=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    base_service = service or build_search_decisions(config)
    playback_reader = playback_adapter or (JellyfinReadAdapter(config.jellyfin.url, config.jellyfin.api_key)
                                          if service is None else None)
    allowed = frozenset(config.users.admins + config.users.standard)
    searches = OrderedDict()
    requests = {}
    lock = threading.Lock()
    maintenance_lock = threading.Lock()
    viewing_lock = threading.Lock()
    if database is not None:
        database.parent.mkdir(parents=True, exist_ok=True)
        storage = Storage(database)
        try:
            storage.initialize()
            ViewingWorkflow(storage, RyotViewingAdapter(base_service.ryot_adapter),
                            TelegramClient(config.telegram.bot_token), participants=allowed).adopt_household_policy()
        finally:
            storage.close()

    @contextmanager
    def reader():
        # SQLite connections are created and closed on the same request worker thread.
        if database is None:
            yield base_service
            return
        storage = Storage(database)
        try:
            yield replace(base_service, identity_matcher=IdentityMatcher(
                storage=storage, metadata_adapter=base_service.metadata_adapter))
        finally:
            storage.close()

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            try:
                scheme, _, raw = request.headers.get("authorization", "").partition(" ")
                if scheme != "tma" or not raw:
                    raise AuthenticationError("Open Curatarr from Telegram to continue.")
                request.state.user_id = validate_init_data(raw, config.telegram.bot_token, allowed)
                signed_user = json.loads(dict(parse_qsl(raw))["user"])
                request.state.user_name = " ".join(str(signed_user.get(k) or "") for k in ("first_name", "last_name"))
                maintenance = request.url.path.startswith(("/api/maintenance", "/api/settings"))
                if maintenance and request.state.user_id not in config.users.admins:
                    return JSONResponse({"detail": "Maintenance is available to admins only."}, status_code=403,
                                        headers={"Cache-Control": "no-store"})
                poster = request.url.path.startswith("/api/maintenance/poster/")
                summary_read = request.method == "GET" and request.url.path in {"/api/maintenance", "/api/maintenance/viewing/declined", "/api/maintenance/viewing/issues", "/api/maintenance/health"}
                bucket = ("reads" if poster or summary_read else "maintenance" if maintenance else
                          "acquisition" if request.method == "POST" and (request.url.path.startswith(("/api/acquisition/", "/api/radarr-profile/", "/api/suggestions/", "/api/source-requests")) or request.url.path.endswith("/acquisition-preview")) else
                          "viewing" if request.method == "POST" and request.url.path.startswith("/api/viewing/") else
                          "search" if request.url.path == "/api/search" else "reads")
                now = time.monotonic()
                with lock:
                    queue = requests.setdefault((request.state.user_id, bucket), deque())
                    while queue and queue[0] <= now - 60:
                        queue.popleft()
                    if len(queue) >= {"search": 10, "maintenance": 30, "viewing": 30, "acquisition": 30, "reads": 120}[bucket]:
                        return JSONResponse({"detail": "Too many requests. Try again shortly."},
                                            status_code=429, headers={"Retry-After": "60", "Cache-Control": "no-store"})
                    queue.append(now)
            except AuthenticationError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=401,
                                    headers={"Cache-Control": "no-store"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org; "
            "style-src 'self'; img-src 'self' https://image.tmdb.org; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(AdapterError)
    async def adapter_error(request, exc):
        return JSONResponse({"detail": "Media service unavailable. Please try again."}, status_code=503)

    @app.exception_handler(StorageError)
    async def storage_error(request, exc):
        return JSONResponse({"detail": "Workflow state changed or is unavailable. Refresh before continuing."}, status_code=409)

    @app.exception_handler(sqlite3.Error)
    async def database_error(request, exc):
        return JSONResponse({"detail": "Workflow storage is unavailable. Please try again."}, status_code=503)

    @app.post("/hooks/jellyfin/removals", status_code=202)
    async def jellyfin_removal(request: Request):
        # Independent machine authentication; Telegram launch data grants no access.
        token = config.jellyfin.removal_webhook_token
        supplied = request.query_params.get("token", "")
        if not token or not secrets.compare_digest(supplied, token):
            raise HTTPException(404, "Not found.")
        if database is None:
            raise HTTPException(503, "Workflow storage is unavailable.")
        if len(supplied) > 128:
            raise HTTPException(404, "Not found.")
        # Read incrementally: neither a missing nor a forged Content-Length bypasses the bound.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 262144:
                raise HTTPException(413, "Webhook payload is too large.")
        from curatarr.integrations.jellyfin.removals import removal_hint
        from curatarr.services.removals import RemovalStore
        from curatarr.runtime import removal_source
        try:
            hint = removal_hint(json.loads(body))
        except (ValueError, TypeError, AdapterError):
            raise HTTPException(400, "Invalid movie removal payload.") from None
        if hint is None:
            return {"accepted": True, "queued": False}
        storage = Storage(database)
        try:
            RemovalStore(storage).enqueue(hint, removal_source(config))
        finally:
            storage.close()
        # No provider calls or Telegram sends in the webhook request.
        return {"accepted": True, "queued": True}

    @app.post('/hooks/jellyfin/viewing', status_code=202)
    async def jellyfin_viewing(request: Request):
        token = config.jellyfin.viewing_webhook_token
        supplied = request.query_params.get('token', '')
        if (not config.coordinator.enabled or not token or len(supplied)>128
                or not secrets.compare_digest(supplied.encode(), token.encode())):
            raise HTTPException(404, 'Not found.')
        if database is None:
            raise HTTPException(503, 'Workflow storage is unavailable.')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>262144:
                raise HTTPException(413, 'Webhook payload is too large.')
        from curatarr.services.jellyfin_viewing import JellyfinViewingIntake
        storage = Storage(database)
        try:
            # Parsing and durable enqueue only. Verification happens in the worker.
            queued = JellyfinViewingIntake(storage, None).receive(json.loads(body))
        except (ValueError, TypeError):
            raise HTTPException(400, 'Invalid movie viewing payload.') from None
        finally:
            storage.close()
        return {'accepted': True, 'queued': queued}

    @contextmanager
    def direct_intake():
        if not config.coordinator.enabled or not config.jellyfin.viewing_webhook_token:
            raise HTTPException(404, 'Direct viewing is not configured.')
        if database is None:
            raise HTTPException(503, 'Workflow storage is unavailable.')
        from curatarr.runtime import build_jellyfin_intake
        storage = Storage(database)
        try:
            yield (intake_factory or build_jellyfin_intake)(config, storage)
        finally:
            storage.close()

    @app.get('/api/settings/viewing')
    def direct_viewing_settings(request: Request):
        with direct_intake() as intake:
            return {'policy': intake.settings.get(), 'setup': intake.verifier.setup()}

    @app.post('/api/settings/viewing')
    def direct_viewing_save(request: Request, change: dict):
        if set(change)!={'revision','enabled','minimum','completed','accounts'} or type(change.get('revision')) is not int:
            raise HTTPException(400, 'Invalid playback settings.')
        with direct_intake() as intake:
            setup = intake.verifier.setup()
            values = {k: v for k,v in change.items() if k!='revision'}
            try:
                policy = intake.settings.save(values, revision=change['revision'],
                    actor='miniapp:user:'+str(request.state.user_id), setup=setup)
            except ValueError:
                raise HTTPException(400, 'Choose valid thresholds and verified playback accounts.') from None
            return {'policy': policy, 'setup': setup}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/me")
    def me(request: Request):
        name = "Household"
        if database is not None:
            storage = Storage(database)
            try:
                name = InstallationSettings(storage).get()["display_name"]
            finally:
                storage.close()
        return {"history_scope": "shared_household", "read_only": not config.coordinator.enabled,
                "display_name": name, "is_admin": request.state.user_id in config.users.admins,
                "maintenance_available": database is not None,
                "show_sync_diagnostics": config.maintenance.show_diagnostics,
                **({'direct_viewing_available': True} if config.coordinator.enabled and config.jellyfin.viewing_webhook_token else {}),
                **({"acquisition_available": True, "sources_available": True} if config.coordinator.enabled else {})}

    @app.get("/api/settings")
    def settings_read(request: Request):
        if database is None:
            raise HTTPException(503, "Settings storage is not configured.")
        storage = Storage(database)
        try:
            return InstallationSettings(storage).public()
        finally:
            storage.close()

    @app.post("/api/settings")
    def settings_save(request: Request, change: SettingsChange):
        if database is None:
            raise HTTPException(503, "Settings storage is not configured.")
        storage = Storage(database)
        try:
            settings = InstallationSettings(storage)
            try:
                settings.save(expected_revision=change.expected_revision, actor=f"telegram:user:{request.state.user_id}",
                              display_name=change.display_name, owned_cleanup_enabled=change.owned_cleanup_enabled,
                              cleanup_answered_prompts=change.cleanup_answered_prompts,
                              cleanup_unanswered_prompts=change.cleanup_unanswered_prompts)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            return settings.public()
        finally:
            storage.close()

    @contextmanager
    def maintenance_reader():
        if database is None or base_service.radarr_adapter is None:
            raise HTTPException(503, "Maintenance workflow storage is not configured.")
        storage = Storage(database)
        try:
            writer = acquisition_writer or (RyotAcquisitionWriter(base_service.ryot_adapter)
                                             if isinstance(base_service.ryot_adapter, RyotReadAdapter) else None)
            yield MaintenanceService(MovieReconciliationService(base_service.ryot_adapter, base_service.radarr_adapter, storage), writer)
        finally:
            storage.close()

    def system_monitor(storage):
        return SystemMaintenance(storage, base_service.ryot_adapter, base_service.radarr_adapter, playback_reader)

    def run_system_check(claim):
        storage = Storage(database)
        try:
            system_monitor(storage).run(claim)
        except (AdapterError, StorageError, sqlite3.Error, ValueError):
            with storage.connection:
                storage.connection.execute("UPDATE job_run SET status='failed',finished_at=CURRENT_TIMESTAMP "
                                           "WHERE id=? AND status='running'", (claim["run_id"],))
        finally:
            storage.close()

    @app.get("/api/maintenance/health")
    def maintenance_health(request: Request):
        if database is None:
            raise HTTPException(503, "Maintenance workflow storage is not configured.")
        storage = Storage(database)
        try:
            return system_monitor(storage).summary()
        finally:
            storage.close()

    @app.post("/api/maintenance/check")
    def maintenance_check(request: Request, background: BackgroundTasks):
        if database is None:
            raise HTTPException(503, "Maintenance workflow storage is not configured.")
        storage = Storage(database)
        try:
            monitor = system_monitor(storage)
            claim = monitor.claim(force=True)
            if claim:
                background.add_task(run_system_check, claim)
            return monitor.summary()
        finally:
            storage.close()

    @app.get("/api/maintenance")
    def maintenance_summary(request: Request):
        with maintenance_reader() as current:
            return current.summary(request.state.user_id)

    @app.post("/api/maintenance/viewing/review")
    @app.post("/api/viewing/review")
    def viewing_review(request: Request, navigation: ViewingNavigation):
        if database is None:
            raise HTTPException(503, "Viewing workflow storage is not configured.")
        if not viewing_lock.acquire(blocking=False):
            raise HTTPException(409, "Another watch check-in is loading. Try again shortly.")
        try:
            storage = Storage(database)
            try:
                try:
                    excluded = viewing_sync_ids(storage)
                    result = ViewingFollowupService(storage, base_service.ryot_adapter, bot_key(config.telegram.bot_token), excluded).review(
                        request.state.user_id, navigation.action, expected_cursor=navigation.expected_cursor)
                    if result.get("movie"):
                        workflow = viewing_workflow(storage)
                        questions = []
                        for decision in storage.viewing_followups():
                            if decision.media_key != result["movie"]["key"]:
                                continue
                            if decision.decision_id in excluded or not visible_to(decision, request.state.user_id):
                                continue
                            try:
                                questions.append(workflow.web_question(decision, user_id=request.state.user_id))
                            except ViewingEventMissing:
                                questions.append({"decision_id": decision.decision_id, "actions": [],
                                    "notice": "Answers are unavailable because the original viewing changed."})
                            except (ValueError, AdapterError):
                                questions.append({"decision_id": decision.decision_id, "actions": [],
                                                  "notice": "This record needs review; answers are currently unavailable."})
                        result["movie"]["check_ins"] = questions
                        result["updates_enabled"] = any(q["actions"] for q in questions)
                        result["read_only"] = not result["updates_enabled"]
                    return result
                except ValueError:
                    raise HTTPException(409, "Viewing position changed. Reopen this branch to continue.") from None
            finally:
                storage.close()
        finally:
            viewing_lock.release()

    @app.get("/api/viewing/summary")
    def viewing_summary(request: Request):
        if database is None:
            return {"available": False, "pending_movie_count": 0}
        storage = Storage(database)
        try:
            excluded = viewing_sync_ids(storage)
            return {"available": True, "pending_movie_count": pending_movie_count(storage, request.state.user_id, excluded),
                    "queue_version": queue_version(storage, request.state.user_id, excluded)}
        finally:
            storage.close()

    @app.post("/api/viewing/answer")
    def viewing_answer(request: Request, answer: ViewingAnswer):
        if database is None:
            raise HTTPException(503, "Viewing workflow storage is not configured.")
        storage = Storage(database)
        try:
            if storage.maintenance_progress(request.state.user_id, source="viewing_progress")["cursor"] != answer.media_key:
                raise HTTPException(409, "Viewing position changed. Refresh before answering.")
            workflow = viewing_workflow(storage)
            try:
                workflow.respond_web(answer.decision_id, answer.action, media_key=answer.media_key,
                                     expected_version=answer.expected_version, user_id=request.state.user_id,
                                     user_name=request.state.user_name)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            return {"notice": "Answer saved.", "pending_movie_count": pending_movie_count(storage, request.state.user_id)}
        finally:
            storage.close()

    @app.post("/api/maintenance/viewing/reopen")
    @app.post("/api/viewing/reopen")
    def viewing_reopen(request: Request, reopen: ViewingReopen):
        if database is None:
            raise HTTPException(503, "Viewing workflow storage is not configured.")
        storage = Storage(database)
        try:
            if storage.maintenance_progress(request.state.user_id, source="viewing_progress")["cursor"] != reopen.media_key:
                raise HTTPException(409, "Viewing position changed. Refresh before reopening.")
            workflow = viewing_workflow(storage)
            try:
                workflow.reopen(reopen.decision_id, reopen.media_key, user_id=request.state.user_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            return {"notice": "Queued for the next Telegram viewing check."}
        finally:
            storage.close()

    def viewing_workflow(storage):
        if viewing_factory is None and not (config.coordinator.enabled and config.jellyfin.viewing_webhook_token):
            return ViewingWorkflow(storage, RyotViewingAdapter(base_service.ryot_adapter),
                                   TelegramClient(config.telegram.bot_token), participants=allowed)
        from curatarr.runtime import build_viewing_workflow
        return (viewing_factory or build_viewing_workflow)(config, storage,
            reader=base_service.ryot_adapter)

    def viewing_records(storage, *, controls=True):
        workflow = viewing_workflow(storage)
        corrections = ViewingCorrections(workflow)
        movies, complete = [], True
        binding = workflow.binding()
        for decision in storage.viewing_followups():
            if (decision.context.get("workflow") != "viewing_v1" or not binding
                    or decision.context.get("source") != binding["source"]):
                continue
            pending_reset = workflow.store.reset_pending(decision.media_key)
            item = {"media_key": decision.media_key, "title": decision.context["title"],
                    "year": decision.context.get("year"), "identities": decision.context.get("identities", [])}
            state, sync_issue = "valid", pending_reset
            try:
                workflow.adapter.validate(decision.context)
                if not decision.context.get("declined_all") and not pending_reset:
                    continue
            except ViewingEventMissing:
                state, sync_issue = "missing", True
                item["notice"] = "The original Ryot viewing was removed. Clearing this request changes only Curatarr; history and ratings remain untouched."
            except ViewingEventChanged:
                state, sync_issue = "changed", True
                item["notice"] = "The original Ryot viewing is no longer completed. Review the native history before correcting this request."
            except (ValueError, AdapterError):
                state, complete = "unavailable", False
                if not decision.context.get("declined_all") and not pending_reset:
                    continue
                item["notice"] = "Viewing evidence could not be verified. Refresh before reviewing."
            if pending_reset:
                item["notice"] = "A viewing-history correction is pending. Recheck and continue the existing correction."
            item["sync_issue"] = bool(sync_issue)
            item["question"] = {"decision_id": decision.decision_id, "version": workflow.version(decision),
                                "date": decision.context.get("finished_on"),
                                "actions": corrections.actions(decision, state) if controls else []}
            movies.append(item)
        return {"movies": movies, "complete": complete}

    def viewing_sync_ids(storage):
        return frozenset(m["question"]["decision_id"] for m in viewing_records(storage, controls=False)["movies"]
                         if m["sync_issue"])

    def viewing_group(group):
        if database is None:
            raise HTTPException(503, "Viewing workflow storage is not configured.")
        storage = Storage(database)
        try:
            data = viewing_records(storage, controls=group != "sync" or config.maintenance.show_diagnostics)
            if not config.maintenance.show_diagnostics:
                for movie in data["movies"]:
                    movie["question"]["actions"] = [a for a in movie["question"]["actions"]
                                                      if a["action"] in {"confirm_declined", "review_again"}]
            data["movies"] = [m for m in data["movies"] if m["sync_issue"] == (group == "sync")]
            return data
        finally:
            storage.close()

    @app.get("/api/maintenance/viewing/declined")
    def declined_viewings(request: Request):
        return viewing_group("declined")

    @app.get("/api/maintenance/viewing/issues")
    def viewing_issues(request: Request):
        return viewing_group("sync")

    @app.post("/api/maintenance/viewing/confirm-declined")
    def confirm_declined(request: Request, answer: ViewingCorrection):
        if answer.action in {"clear_stale", "clear_history"} and not config.maintenance.show_diagnostics:
            raise HTTPException(403, "Technical repairs require maintainer diagnostics.")
        if database is None:
            raise HTTPException(503, "Viewing workflow storage is not configured.")
        storage = Storage(database)
        try:
            try:
                workflow = viewing_workflow(storage)
                corrections = ViewingCorrections(workflow)
                decision = corrections.decision(answer.decision_id, answer.media_key, answer.expected_version)
                if answer.action == "review_again":
                    corrections.review_again(decision, user_id=request.state.user_id)
                    return {"notice": "Returned to Viewing inbox. Previous answers were reset."}
                if answer.action == "clear_history":
                    if not answer.confirm or not answer.preview_token:
                        raise ValueError("Review and explicitly confirm this movie's history reset first.")
                    done = corrections.clear(decision, token=answer.preview_token, user_id=request.state.user_id)
                    return {"notice": ("Viewing history cleared. Ratings and reviews were retained." if done else
                                       "History reset is pending. Refresh and review before continuing the same reset.")}
                workflow.respond_web(answer.decision_id, answer.action, media_key=answer.media_key,
                                     expected_version=answer.expected_version, user_id=request.state.user_id,
                                     user_name=request.state.user_name, admin_review=True)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            current = storage.get_pending_decision(answer.decision_id)
            if current.context.get('direct_candidate') and current.context.get('confirmed_by') is not None and not current.context.get('native_linked'):
                return {'notice': 'Household confirmation saved. The Ryot update awaits verification; rating becomes available afterwards.'}
            return {"notice": ("Stale request cleared. Ryot history was not changed." if answer.action == "clear_stale" else
                               "Household viewing confirmed. Optional rating is available in Viewing inbox.")}
        finally:
            storage.close()

    @app.post("/api/maintenance/viewing/reset-preview")
    def reset_preview(request: Request, answer: ViewingAnswer):
        if not config.maintenance.show_diagnostics:
            raise HTTPException(403, "Technical repairs require maintainer diagnostics.")
        if database is None:
            raise HTTPException(503, "Viewing workflow storage is not configured.")
        storage = Storage(database)
        try:
            corrections = ViewingCorrections(viewing_workflow(storage))
            try:
                if answer.action != "clear_history":
                    raise ValueError("Choose Clear viewing history to preview a reset.")
                decision = corrections.decision(answer.decision_id, answer.media_key, answer.expected_version)
                preview = corrections.preview(decision)
                return {k: preview[k] for k in ("title", "record_count", "remaining_count", "resume", "token")}
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
        finally:
            storage.close()

    @app.get("/api/maintenance/poster/{tmdb_id}")
    @app.get("/api/viewing/poster/{tmdb_id}")
    def maintenance_poster(tmdb_id: str):
        if len(tmdb_id) > 10 or not tmdb_id.isascii() or not tmdb_id.isdigit() or not 0 < int(tmdb_id) <= 2147483647:
            raise HTTPException(400, "Invalid movie identifier.")
        resolve = getattr(base_service.metadata_adapter, "movie_poster", None)
        try:
            return {"poster_url": resolve(tmdb_id) if resolve else None}
        except AdapterError:
            return {"poster_url": None}

    @app.post("/api/maintenance/review")
    def maintenance_review(request: Request, navigation: MaintenanceNavigation):
        if not maintenance_lock.acquire(blocking=False):
            raise HTTPException(409, "Another maintenance check is running. Try again shortly.")
        try:
            with maintenance_reader() as current:
                try:
                    return current.review(request.state.user_id, navigation.action,
                                          category=navigation.category, expected_cursor=navigation.expected_cursor)
                except ValueError:
                    raise HTTPException(409, "Review options or position changed. Reopen maintenance to continue.") from None
        finally:
            maintenance_lock.release()

    @app.post("/api/maintenance/correct")
    def maintenance_correct(request: Request, correction: MaintenanceCorrection):
        if not config.maintenance.show_diagnostics:
            raise HTTPException(403, "Technical repairs require maintainer diagnostics.")
        if not maintenance_lock.acquire(blocking=False):
            raise HTTPException(409, "Another maintenance check is running. Try again shortly.")
        try:
            with maintenance_reader() as current:
                try:
                    return current.correct(request.state.user_id, media_key=correction.media_key,
                                           finding_id=correction.finding_id)
                except ValueError:
                    raise HTTPException(409, "Movie facts or review position changed. Refresh before correcting.") from None
        finally:
            maintenance_lock.release()

    @app.get("/api/search")
    def search(request: Request, q: str = ""):
        query = " ".join(q.split())
        if not query or len(query) > 200:
            raise HTTPException(400, "Enter a movie title of up to 200 characters.")
        candidates = base_service.search(query, MediaType.MOVIE)[:10]
        return remember_search(request.state.user_id, candidates)

    def remember_search(user_id, candidates, origin=None):
        search_id = secrets.token_urlsafe(24)
        with lock:
            now = time.monotonic()
            for key in list(searches):
                if searches[key][1] <= now:
                    del searches[key]
            while len(searches) >= 100:
                searches.popitem(last=False)
            searches[search_id] = (user_id, now + SEARCH_LIFETIME_SECONDS, candidates, origin)
        return {"search_id": search_id, "candidates": [_candidate(item) for item in candidates]}

    def selected(request, search_id, index):
        with lock:
            record = searches.get(search_id)
        if record is None or record[0] != request.state.user_id or record[1] <= time.monotonic():
            raise HTTPException(404, "Search expired. Search again to continue.")
        if not 0 <= index < len(record[2]):
            raise HTTPException(404, "Movie not found in this search.")
        return record[2][index]

    @contextmanager
    def acquisition_reader():
        if not config.coordinator.enabled or database is None:
            raise HTTPException(404, "Acquisition is not enabled.")
        from curatarr.runtime import build_acquisition
        storage = Storage(database)
        try:
            yield (acquisition_factory or build_acquisition)(config, storage)
        finally:
            storage.close()

    @contextmanager
    def profile_reader():
        if not config.coordinator.enabled or database is None:
            raise HTTPException(404, 'Profile editing is not enabled.')
        from curatarr.runtime import build_profile_changes
        storage = Storage(database)
        try:
            yield (profile_factory or build_profile_changes)(config, storage)
        finally:
            storage.close()

    def profile_candidate(request, search_id, index):
        candidate = selected(request, search_id, index)
        tmdb = [i.value for i in candidate.identities if i.namespace == 'tmdb']
        imdb = [i.value for i in candidate.identities if i.namespace == 'imdb']
        if len(tmdb) != 1 or len(imdb) > 1:
            raise HTTPException(409, 'Exact movie identity needs review.')
        return {'tmdb': tmdb[0], 'imdb': imdb[0] if imdb else None, 'title': candidate.title, 'year': candidate.year}

    def profile_public(operation):
        return {'operation_id': operation['id'], 'title': operation['expected']['candidate']['title'],
                'status': operation['status'], 'profile': operation['expected']['profile']['name']}

    def run_profile_change(identifier):
        with profile_reader() as service:
            try:
                service.run(identifier)
            except (AdapterError, StorageError, sqlite3.Error, ValueError):
                pass  # Recovery worker inspects this exact saved operation.

    @app.get('/api/search/{search_id}/{index}/radarr-profile')
    def profile_choices(request: Request, search_id: str, index: int):
        candidate = profile_candidate(request, search_id, index)
        with profile_reader() as service:
            pending = service.pending(candidate['tmdb'])
            if pending:
                actor = f'telegram:user:{request.state.user_id}'
                return {'pending': [profile_public(op) for op in pending if op['authorization']['actor'] == actor],
                        'busy': True, 'message': 'A profile change is already pending for this movie.'}
            try:
                return service.choices(candidate, actor=f'telegram:user:{request.state.user_id}')
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @app.post('/api/radarr-profile/confirm')
    def profile_confirm(request: Request, change: ProfileChangeConfirm, background: BackgroundTasks):
        with profile_reader() as service:
            try:
                operation = service.confirm(change.decision_id, actor=f'telegram:user:{request.state.user_id}',
                    version=change.version, profile_id=change.profile_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            if operation['status'] not in {'completed', 'cancelled'}:
                background.add_task(run_profile_change, operation['id'])
            return profile_public(operation)

    @app.get('/api/radarr-profile/{identifier}')
    def profile_result(request: Request, identifier: str):
        with profile_reader() as service:
            operation = service.journal.operation(identifier)
            if operation['kind'] != 'movie_profile' or operation['authorization']['actor'] != f'telegram:user:{request.state.user_id}':
                raise HTTPException(404, 'Profile operation not found.')
            return profile_public(operation)

    @contextmanager
    def sources_reader():
        with acquisition_reader() as acquisition:
            from curatarr.runtime import build_sources
            yield (source_factory or build_sources)(config, acquisition.storage, acquisition)

    @app.get('/api/settings/sources')
    def source_settings(request: Request):
        with sources_reader() as sources:
            data = sources.public()
            try:
                data.update(genres=sources.genres(), genres_available=True)
            except AdapterError:
                data.update(genres=[], genres_available=False)
            try:
                data.update(choices=acquisition_options(request), choices_available=True)
            except AdapterError:
                data.update(choices={'profiles':[], 'roots':[], 'default_profile':None,'default_root':None}, choices_available=False)
            return data

    @app.post('/api/settings/sources/discovery')
    def source_discovery_save(request: Request, change: DiscoverySettingsChange):
        with sources_reader() as sources:
            try:
                sources.save_discovery(change.model_dump(exclude={'expected_revision'}), revision=change.expected_revision,
                                       actor=f'telegram:user:{request.state.user_id}')
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            return sources.public()

    @app.post('/api/settings/sources/wanted')
    def source_wanted_save(request: Request, change: WantedSettingsChange):
        with sources_reader() as sources:
            try:
                sources.save_wanted(change.imdb_user, change.model_dump(exclude={'expected_revision', 'imdb_user'}),
                                    revision=change.expected_revision, actor=f'telegram:user:{request.state.user_id}')
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            return sources.public()

    @app.post('/api/settings/sources/imdb-import')
    def source_imdb_import(request: Request, snapshot: WantedImport):
        with sources_reader() as sources:
            try:
                return sources.import_wanted(snapshot.source_id, revision=snapshot.expected_revision, content=snapshot.content,
                    count=snapshot.count, complete=snapshot.complete, actor=f'telegram:user:{request.state.user_id}')
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None

    def run_source_check():
        with sources_reader() as sources:
            try:
                sources.collect(force=True)
            except (AdapterError, StorageError, sqlite3.Error, ValueError):
                pass

    @app.post('/api/settings/sources/check')
    def source_check(request: Request, background: BackgroundTasks):
        background.add_task(run_source_check)
        with sources_reader() as sources:
            return sources.public()

    @app.get('/api/suggestions')
    def suggestions(request: Request, after: str = '', view: Literal['pending','declined'] = 'pending'):
        if after and (not after.startswith('tmdb:') or not after[5:].isascii() or not after[5:].isdigit() or len(after)>15):
            raise HTTPException(400, 'Invalid suggestion position.')
        with sources_reader() as sources:
            return sources.suggestions(after=after, view=view)

    @app.post('/api/suggestions/{tmdb}')
    def suggestion_answer(request: Request, tmdb: str, answer: SuggestionAnswer):
        if not tmdb.isascii() or not tmdb.isdecimal() or not 0<int(tmdb)<2147483648 or str(int(tmdb))!=tmdb:
            raise HTTPException(400, 'Invalid movie identity.')
        key = 'tmdb:'+tmdb
        actor = f'telegram:user:{request.state.user_id}'
        with sources_reader() as sources:
            if answer.action=='reopen':
                sources.intake.reopen(key, revision=answer.revision, actor=actor)
                return {'reopened': True}
            if answer.action in {'yes','no'}:
                sources.intake.decide(key, revision=answer.revision, actor=actor, accept=answer.action=='yes')
                if answer.action=='no':
                    return {'declined': True}
            row = sources.db.execute('SELECT * FROM discovery_candidate WHERE movie_key=?', (key,)).fetchone()
            expected = answer.revision + (1 if answer.action=='yes' else 0)
            if row is None or row['revision']!=expected or row['decision']!='selected' or row['eligibility']!='eligible':
                raise StorageError('The suggestion changed. Refresh before continuing.')
            operation = sources.db.execute("SELECT id FROM coordination_operation WHERE request_key=? AND status!='cancelled'", ('discovery:'+key+':'+str(expected),)).fetchone()
            if operation:
                raise StorageError('This suggestion is already being handled.')
            value = json.loads(row['metadata_json'])
            candidate = MediaItem(media_type=MediaType.MOVIE,title=value['title'], year=int(value['release_date'][:4]),
                identities=frozenset({MediaIdentity('tmdb',tmdb)}), overview=value.get('overview'),
                poster_url='https://image.tmdb.org/t/p/w500'+value['poster_path'] if value.get('poster_path') else None)
            return remember_search(request.state.user_id, (candidate,), {'kind':'discovery','revision':expected})

    @app.get('/api/source-requests')
    def source_requests(request: Request):
        with sources_reader() as sources:
            return {'items': sources.requests()}

    @app.post('/api/source-requests')
    def source_request_action(request: Request, answer: WantedAction):
        with sources_reader() as sources:
            actor = f'telegram:user:{request.state.user_id}'
            if answer.action=='withdraw':
                sources.withdraw(answer.source_id, answer.item_key, revision=answer.revision, actor=actor)
                return {'withdrawn': True}
            row = sources.db.execute("SELECT r.* FROM wanted_resolution r JOIN wanted_request w USING(source_id,item_key) "
                                    "WHERE r.source_id=? AND r.item_key=? AND w.state='new'", (answer.source_id,answer.item_key)).fetchone()
            if row is None or row['revision']!=answer.revision or not row['candidate_json']:
                raise StorageError('This request changed or is still awaiting movie metadata.')
            saved = json.loads(row['candidate_json'])
            current = sources.resolve_imdb(answer.item_key[5:])
            if current is None or current['tmdb']!=saved['tmdb']:
                raise StorageError('Exact movie metadata changed. Keep this request waiting.')
            source = sources.journal.get_source(answer.source_id)
            candidate = MediaItem(media_type=MediaType.MOVIE,title=current['title'],year=int(current['year']) if current['year'] and current['year'].isdigit() else None,
                                  identities=frozenset({MediaIdentity('tmdb',current['tmdb']),MediaIdentity('imdb',current['imdb'])}))
            return remember_search(request.state.user_id, (candidate,), {'kind':'wanted','source_id':source['id'],'item_key':answer.item_key,'revision':source['revision']})

    def acquisition_public(operation):
        return {"operation_id": operation['id'], "title": operation['expected']['candidate']['title'],
                "status": operation['status'], "verified_steps": sum(s['status']=='verified' for s in operation['steps']),
                "total_steps": len(operation['steps']), "tmdb": operation['expected']['candidate']['tmdb']}

    def run_acquisition(identifier):
        with acquisition_reader() as service:
            try:
                service.run(identifier)
            except (AdapterError, StorageError, sqlite3.Error, ValueError):
                pass  # Durable attempted/claimed state is reconciled by its worker.

    @app.get("/api/acquisition/options")
    def acquisition_options(request: Request):
        from pathlib import PurePosixPath
        with acquisition_reader() as service:
            options = service.radarr.options()
            return {"profiles": [{"id": p['id'], "name": p['name']} for p in options['profiles']],
                    "roots": [{"id": r['id'], "name": PurePosixPath(r['path']).name or '/'} for r in options['roots']],
                    "default_profile": next((p['id'] for p in options['profiles'] if p['name']==config.radarr.default_profile), None),
                    "default_root": next((r['id'] for r in options['roots'] if r['path']==config.radarr.root_folder), None)}

    @app.post("/api/search/{search_id}/{index}/acquisition-preview")
    def acquisition_preview(request: Request, search_id: str, index: int, choice: AcquisitionPreview):
        candidate = selected(request, search_id, index)
        tmdb = [i.value for i in candidate.identities if i.namespace=='tmdb']
        imdb = [i.value for i in candidate.identities if i.namespace=='imdb']
        if len(tmdb)!=1 or len(imdb)>1:
            raise HTTPException(409, "Exact movie identity needs review.")
        with acquisition_reader() as service:
            try:
                return service.preview({'tmdb':tmdb[0], 'imdb':imdb[0] if imdb else None,
                                        'title':candidate.title, 'year':candidate.year},
                                       actor=f'telegram:user:{request.state.user_id}', mode=choice.mode,
                                       profile_id=choice.profile_id, root_id=choice.root_id, reactivate=choice.reactivate,
                                       origin=searches[search_id][3], watched=choice.watched, watched_on=choice.watched_on,
                                       rating=choice.rating, user_name=request.state.user_name)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @app.post("/api/acquisition/confirm")
    def acquisition_confirm(request: Request, choice: AcquisitionConfirm, background: BackgroundTasks):
        with acquisition_reader() as service:
            operation = service.confirm(choice.decision_id, actor=f'telegram:user:{request.state.user_id}', version=choice.version)
            if operation['status'] not in {'completed','cancelled'}:
                background.add_task(run_acquisition, operation['id'])
            return acquisition_public(operation)

    @app.get("/api/acquisition/pending")
    def acquisition_pending(request: Request, tmdb: str):
        if not tmdb.isascii() or not tmdb.isdecimal() or not 0<int(tmdb)<2147483648:
            raise HTTPException(400, "Invalid movie identity.")
        with acquisition_reader() as service:
            rows = service.storage.connection.execute(
                "SELECT id FROM coordination_operation WHERE kind='movie_acquisition' AND media_key=? "
                "AND json_extract(authorization_json,'$.actor')=? AND status NOT IN ('completed','cancelled') ORDER BY created_at DESC LIMIT 10",
                ('tmdb:'+tmdb, f'telegram:user:{request.state.user_id}')).fetchall()
            return {"operations": [acquisition_public(service.journal.operation(r[0])) for r in rows]}

    @app.get("/api/acquisition/{identifier}")
    def acquisition_result(request: Request, identifier: str):
        with acquisition_reader() as service:
            operation = service.journal.operation(identifier)
            if operation['authorization']['actor']!=f'telegram:user:{request.state.user_id}':
                raise HTTPException(404, "Acquisition operation not found.")
            return acquisition_public(operation)

    @app.get("/api/search/{search_id}/{index}/history")
    def history(request: Request, search_id: str, index: int):
        candidate = selected(request, search_id, index)
        with reader() as current:
            return _history(current, candidate)

    @app.get("/api/search/{search_id}/{index}/status")
    @app.get("/api/search/{search_id}/{index}/details")
    def details(request: Request, search_id: str, index: int):
        candidate = selected(request, search_id, index)
        with reader() as current:
            history = _history(current, candidate, internal=True)
            try:
                arr = current.lookup_execution(candidate)
                matched = arr is None or _strong_match(current, candidate, arr)
                runtime = {"status": "not_present" if arr is None else "matched" if matched else "needs_review"}
                if arr is not None and matched:
                    runtime.update(state=arr.arr_runtime_state.value, file_present=arr.file_present,
                                   identities=[str(identity) for identity in sorted(arr.identities)],
                                   quality=arr.quality_name, size_bytes=arr.size_bytes,
                                   imdb_rating=arr.imdb_rating)
            except AdapterError:
                runtime = {"status": "unavailable"}
                arr = None
                matched = False
            if request.url.path.endswith("/details") and arr is not None and matched:
                runtime["profile_status"] = "not_confirmed"
                if arr.quality_profile_id is not None:
                    try:
                        runtime["profile"] = current.radarr_adapter.quality_profile(arr.quality_profile_id)
                        runtime["profile_status"] = "matched"
                    except AdapterError:
                        runtime["profile_status"] = "unavailable"
                runtime["file_status"] = "not_confirmed" if arr.file_present else "not_present"
                movie_id = next((i.value for i in arr.identities if i.namespace == "radarr"), None)
                if arr.file_present and arr.movie_file_id is not None and movie_id and movie_id.isdigit():
                    try:
                        runtime["file"] = current.radarr_adapter.file_details(arr.movie_file_id, int(movie_id))
                        runtime["file_status"] = "matched"
                    except AdapterError:
                        runtime["file_status"] = "unavailable"
                if runtime.get("profile") is not None:
                    formats = []
                    if any(f["score"] > 0 for f in runtime["profile"].get("format_items", [])):
                        try:
                            formats = current.radarr_adapter.core_formats()
                        except AdapterError:
                            formats = None
                    runtime["upgrade_goals"] = upgrade_goals(
                        runtime["profile"], runtime.get("file"), formats,
                        has_file=arr.file_present, original_language_id=arr.original_language_id)
            try:
                playback = (playback_reader.playback_facts(candidate) if playback_reader is not None
                            else {"status": "unavailable", "versions": [], "version_count": 0})
            except AdapterError:
                playback = {"status": "unavailable", "versions": [], "version_count": 0}
            # A failed lookup must never be treated as an absent record or an add recommendation.
            path = None
            if history["status"] not in {"unavailable", "needs_review"} and matched:
                ryot = history.pop("_item", None)
                path = SearchAddPlanner(identity_matcher=current.identity_matcher).plan(
                    candidate=candidate, ryot_item=ryot, arr_item=arr).path.value
            history.pop("_item", None)
            deletion_confirmed = False
            storage = current.identity_matcher.storage
            if storage is not None:
                try:
                    deletion_confirmed = storage.has_confirmed_file_deletion(candidate.identities)
                except StorageError:
                    pass
            return {"candidate": _candidate(candidate), "history": history,
                    "radarr": runtime, "playback": playback, "availability": availability(playback),
                    "library_status": library_status(history, playback, runtime, deletion_confirmed=deletion_confirmed),
                    "decision_path": path, "read_only": True}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _candidate(item: MediaItem) -> dict:
    return {"title": item.title, "year": item.year, "poster_url": item.poster_url,
            "overview": item.overview, "identities": [str(i) for i in sorted(item.identities)],
            "tmdb_rating": item.tmdb_rating}


def _strong_match(service, candidate, item):
    if candidate.media_type is not item.media_type:
        return False
    match = service.identity_matcher.compare(candidate, item)
    return match.is_match and match.confidence is MatchConfidence.STRONG


def _history(service, candidate, *, internal=False):
    try:
        item = service.lookup_history(candidate)
        if item is None:
            return {"status": "unknown", "states": []}
        if not _strong_match(service, candidate, item):
            return {"status": "needs_review", "states": []}
        result = {"status": "watched" if item.is_watched else "no_completed_watch",
                  "states": sorted(state.value for state in item.durable_states),
                  "watch_count": item.completed_watch_count, "last_watched_date": item.last_watched_date,
                  "collections": list(item.collection_names)}
        if internal:
            result["_item"] = item
        return result
    except AdapterError:
        return {"status": "unavailable", "states": []}
