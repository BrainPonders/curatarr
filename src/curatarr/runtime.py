"""Composition root for the opt-in Telegram search/decision runtime."""

import logging
import hashlib
import sqlite3

from curatarr.adapters import AdapterError
from curatarr.config import ConfigError, CuratarrConfig
from curatarr.integrations.radarr import RadarrClient, RadarrReadAdapter
from curatarr.integrations.ryot.adapter import RyotReadAdapter, RyotStateCollections
from curatarr.integrations.ryot.client import RyotClient
from curatarr.integrations.telegram.bot import TelegramBot
from curatarr.integrations.telegram.client import TelegramClient
from curatarr.integrations.tmdb import TmdbClient, TmdbMetadataAdapter
from curatarr.services.decisions import PendingDecisionService
from curatarr.services.identity import IdentityMatcher
from curatarr.services.search import SearchDecisionService
from curatarr.services.reconciliation import MovieReconciliationService
from curatarr.services.telegram import TelegramDecisionCallbacks, TelegramSearchCommand
from curatarr.storage import Storage, StorageError
from curatarr.integrations.ryot.viewing import RyotViewingAdapter
from curatarr.services.viewing_workflow import ViewingWorkflow
from curatarr.services.owned_cleanup import OwnedCleanupAutomation
from curatarr.integrations.ryot.owned import RyotOwnedCleanup
from curatarr.services.prompt_cleanup import TelegramPromptCleanup
from curatarr.integrations.jellyfin import JellyfinReadAdapter
from curatarr.services.system_maintenance import SystemMaintenance


def build_ryot_reader(config: CuratarrConfig) -> RyotReadAdapter:
    collections = config.ryot.collections
    return RyotReadAdapter(
        RyotClient(url=config.ryot.url, api_key=config.ryot.api_key),
        RyotStateCollections(archived=collections.archived, library=collections.library,
                             previously_acquired=collections.previously_acquired, deleted=collections.deleted,
                             radarr=collections.radarr, sonarr=collections.sonarr),
    )


def build_movie_reconciliation(config: CuratarrConfig, storage: Storage) -> MovieReconciliationService:
    return MovieReconciliationService(
        build_ryot_reader(config), RadarrReadAdapter(RadarrClient(config.radarr.url, config.radarr.api_key)), storage)


def build_system_maintenance(config: CuratarrConfig, storage: Storage) -> SystemMaintenance:
    return SystemMaintenance(storage, build_ryot_reader(config),
                             RadarrReadAdapter(RadarrClient(config.radarr.url, config.radarr.api_key)),
                             JellyfinReadAdapter(config.jellyfin.url, config.jellyfin.api_key))


def removal_source(config):
    return hashlib.sha256(config.jellyfin.url.rstrip("/").encode()).hexdigest()


def build_movie_removals(config, storage, client=None):
    from curatarr.integrations.jellyfin.removals import JellyfinRemovalVerifier
    from curatarr.integrations.radarr.removals import RadarrRemovalWriter
    from curatarr.integrations.ryot.removals import RyotRemovalWriter
    from curatarr.services.removals import MovieRemovals
    return MovieRemovals(storage,
                         JellyfinRemovalVerifier(config.jellyfin.url, config.jellyfin.api_key),
                         RyotRemovalWriter(build_ryot_reader(config)),
                         RadarrRemovalWriter(RadarrClient(config.radarr.url, config.radarr.api_key)),
                         client or TelegramClient(config.telegram.bot_token), removal_source(config))


def build_acquisition(config, storage):
    from curatarr.integrations.radarr.acquisition import RadarrAcquisitionWriter
    from curatarr.integrations.ryot.catalogue import RyotCatalogueWriter
    from curatarr.services.acquisition import MovieAcquisition
    from curatarr.services.catalogue_watch import CatalogueWatch
    catalogue = RyotCatalogueWriter(build_ryot_reader(config))
    watch = CatalogueWatch(storage, catalogue, RyotViewingAdapter(catalogue.reader))
    return MovieAcquisition(storage, catalogue,
                            RadarrAcquisitionWriter(RadarrClient(config.radarr.url, config.radarr.api_key)), watch=watch)


def build_profile_changes(config, storage):
    from curatarr.integrations.radarr.profiles import RadarrProfileWriter
    from curatarr.integrations.ryot.catalogue import RyotCatalogueWriter
    from curatarr.services.radarr_profiles import MovieProfileChanges
    return MovieProfileChanges(storage, RadarrProfileWriter(RadarrClient(config.radarr.url, config.radarr.api_key)),
                               RyotCatalogueWriter(build_ryot_reader(config)))


def build_sources(config, storage, acquisition=None):
    from curatarr.services.sources import MovieSources
    acquisition = acquisition or build_acquisition(config, storage)
    ryot = build_ryot_reader(config)
    radarr = RadarrReadAdapter(RadarrClient(config.radarr.url, config.radarr.api_key))
    def known():
        result = set()
        for item in (*ryot.scan_movies().value, *radarr.scan_movies().value):
            identifiers = [i for i in item.identities if i.namespace=='tmdb']
            if len(identifiers) != 1:
                raise AdapterError('Discovery suppression needs complete canonical movie identities.')
            result.add(str(identifiers[0]))
        return result
    if config.tmdb is None:
        raise ConfigError('TMDB credentials are required for movie sources.')
    return MovieSources(storage, TmdbClient(config.tmdb.api_key), acquisition, known)


def build_jellyfin_intake(config, storage):
    from curatarr.integrations.jellyfin.viewing import JellyfinViewingVerifier
    from curatarr.services.jellyfin_viewing import JellyfinViewingIntake
    if config.tmdb is None:
        raise ConfigError('TMDB credentials are required for direct Jellyfin viewing.')
    return JellyfinViewingIntake(storage, JellyfinViewingVerifier(
        config.jellyfin.url, config.jellyfin.api_key, TmdbClient(config.tmdb.api_key)))


def build_viewing_workflow(config, storage, client=None, *, reader=None, metadata=None):
    native = RyotViewingAdapter(reader or build_ryot_reader(config))
    direct = None
    if config.coordinator.enabled and config.jellyfin.viewing_webhook_token:
        from curatarr.integrations.ryot.catalogue import RyotCatalogueWriter
        from curatarr.integrations.ryot.completion import RyotCompletionWriter
        from curatarr.services.direct_viewing import DirectViewing, CandidateViewingAdapter
        catalogue = RyotCatalogueWriter(native.reader)
        direct = DirectViewing(storage, catalogue, RyotCompletionWriter(catalogue), native,
                               participants=config.users.admins + config.users.standard)
        adapter = CandidateViewingAdapter(native, direct)
    else:
        adapter = native
    return ViewingWorkflow(storage, adapter, client or TelegramClient(config.telegram.bot_token), metadata,
                           participants=config.users.admins + config.users.standard, direct=direct)


def build_search_decisions(config: CuratarrConfig, storage: Storage | None = None) -> SearchDecisionService:
    if config.tmdb is None:
        raise ConfigError("tmdb.api_key is required to run Telegram search.")
    users = frozenset(config.users.admins + config.users.standard)
    if not users:
        raise ConfigError("At least one configured Telegram user is required.")
    metadata = TmdbMetadataAdapter(TmdbClient(api_key=config.tmdb.api_key))
    ryot = build_ryot_reader(config)
    return SearchDecisionService(
        metadata_adapter=metadata, ryot_adapter=ryot,
        radarr_adapter=RadarrReadAdapter(RadarrClient(url=config.radarr.url, api_key=config.radarr.api_key)),
        identity_matcher=IdentityMatcher(storage=storage, metadata_adapter=metadata),
        pending_decision_service=PendingDecisionService(storage) if storage is not None else None,
    )


def build_telegram_bot(config: CuratarrConfig, storage: Storage) -> TelegramBot:
    decisions = build_search_decisions(config, storage)
    users = frozenset(config.users.admins + config.users.standard)
    client = TelegramClient(config.telegram.bot_token)
    followups = build_viewing_workflow(config, storage, client, metadata=decisions.metadata_adapter)

    def viewing_worker(stop):
        # Each thread owns its SQLite connection and provider reader.
        worker_storage = Storage(storage.path)
        try:
            worker_storage.initialize()
            workflow = build_viewing_workflow(config, worker_storage, client,
                metadata=TmdbMetadataAdapter(TmdbClient(api_key=config.tmdb.api_key)))
            intake = build_jellyfin_intake(config, worker_storage) if workflow.direct is not None else None
            while not stop.is_set():
                try:
                    if intake is not None:
                        intake.tick()
                    workflow.tick()
                except (AdapterError, StorageError, sqlite3.Error, ValueError):
                    logging.getLogger(__name__).warning("Viewing detection or delivery unavailable; unresolved questions were retained.")
                stop.wait(config.telegram.viewing_scan_interval.seconds)
        finally:
            worker_storage.close()

    def automation_worker(stop):
        worker_storage = Storage(storage.path)
        try:
            worker_storage.initialize()
            cleanup = OwnedCleanupAutomation(worker_storage, RyotOwnedCleanup(build_ryot_reader(config)))
            prompt_cleanup = TelegramPromptCleanup(worker_storage, client, followups.bot_key)
            removals = build_movie_removals(config, worker_storage, client) if config.jellyfin.removal_webhook_token else None
            acquisition = build_acquisition(config, worker_storage) if config.coordinator.enabled else None
            profiles = build_profile_changes(config, worker_storage) if config.coordinator.enabled else None
            while not stop.is_set():
                if acquisition:
                    try:
                        for identifier in acquisition.journal.ready_operations(limit=10):
                            kind = acquisition.journal.operation(identifier)['kind']
                            if kind == 'movie_acquisition':
                                acquisition.run(identifier)
                            elif kind == 'movie_profile':
                                profiles.run(identifier)
                    except (AdapterError, StorageError, sqlite3.Error, ValueError):
                        logging.getLogger(__name__).warning("Acquisition recovery unavailable; saved operations were retained.")
                if removals:
                    try:
                        removals.tick()
                    except (AdapterError, StorageError, sqlite3.Error, ValueError):
                        logging.getLogger(__name__).warning("Movie removal verification unavailable; requests were retained.")
                try:
                    cleanup.tick()
                except (AdapterError, StorageError, sqlite3.Error, ValueError):
                    logging.getLogger(__name__).warning("Owned cleanup unavailable; its result was retained.")
                try:
                    prompt_cleanup.tick()
                except (AdapterError, StorageError, sqlite3.Error, ValueError):
                    logging.getLogger(__name__).warning("Telegram cleanup unavailable; viewing questions were retained.")
                stop.wait(30)
        finally:
            worker_storage.close()

    def source_worker(stop):
        worker_storage = Storage(storage.path)
        try:
            worker_storage.initialize()
            sources = build_sources(config, worker_storage)
            while not stop.is_set():
                try:
                    sources.collect()
                    sources.wanted_tick()
                except (AdapterError, StorageError, sqlite3.Error, ValueError):
                    logging.getLogger(__name__).warning('Source collection unavailable; saved requests were retained.')
                stop.wait(60)
        finally:
            worker_storage.close()

    def maintenance_worker(stop):
        worker_storage = Storage(storage.path)
        try:
            worker_storage.initialize()
            monitor = build_system_maintenance(config, worker_storage)
            while not stop.is_set():
                try:
                    monitor.tick()
                except (AdapterError, StorageError, sqlite3.Error, ValueError):
                    logging.getLogger(__name__).warning("Maintenance check unavailable; previous evidence was retained.")
                stop.wait(60)
        finally:
            worker_storage.close()

    from curatarr.services.telegram_acquisition import TelegramAcquisition
    acquisition_ui = TelegramAcquisition(build_acquisition(config, storage)) if config.coordinator.enabled else None

    return TelegramBot(
        client=client, search=TelegramSearchCommand(decisions),
        callbacks=TelegramDecisionCallbacks(decisions, storage, acquisition=acquisition_ui), storage=storage, allowed_users=users,
        admins=frozenset(config.users.admins), followups=followups,
        removals=build_movie_removals(config, storage, client) if config.jellyfin.removal_webhook_token else None,
        viewing_worker=viewing_worker if str(storage.path) != ":memory:" else None,
        automation_worker=automation_worker if str(storage.path) != ":memory:" else None,
        source_worker=source_worker if config.coordinator.enabled and str(storage.path) != ':memory:' else None,
        maintenance_worker=maintenance_worker if str(storage.path) != ":memory:" else None,
    )
