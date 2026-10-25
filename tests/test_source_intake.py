"""Whole-snapshot intake, historical protection and persistent household choices."""
from datetime import date
import unittest

from curatarr.adapters import AdapterError
from curatarr.integrations.tmdb import TmdbClient
from curatarr.services.source_intake import DiscoveryFilters, SourceIntake, TmdbDiscovery, WantedSnapshot, imdb_csv_snapshot
from curatarr.storage.journal import CoordinationJournal
from curatarr.storage.sqlite import Storage, StorageError


TODAY = date(2026, 10, 24)


def movie(identifier=1, **changes):
    return dict({'tmdb': str(identifier), 'title': 'A movie', 'genres': [18], 'release_date': '2026-01-01',
                 'rating': 8, 'votes': 200, 'adult': False, 'video': False}, **changes)


def native(identifier=1, **changes):
    return dict({'id': identifier, 'title': 'A movie', 'genre_ids': [18], 'release_date': '2026-01-01',
                 'vote_average': 8, 'vote_count': 200, 'adult': False, 'video': False}, **changes)


class ExportTests(unittest.TestCase):
    def test_complete_csv_preserves_all_title_identifiers_not_just_current_movies(self):
        value = '\ufeffConst,Title,Title Type\ntt1234567,"A, movie",movie\ntt2345678,Future project,tvSeries\n'
        self.assertEqual(imdb_csv_snapshot(value, expected_count=2, complete=True).keys, ('imdb:tt1234567', 'imdb:tt2345678'))

    def test_html_challenge_or_partial_duplicate_exports_never_become_empty_baseline(self):
        for value, count, complete in [('<html>challenge</html>', 0, True), ('Const\ntt1234567\n', 2, True),
                                       ('Const\ntt1234567\ntt1234567\n', 2, True), ('Const\n', 0, False),
                                       ('Const,Const\n', 0, True), ('Const\nwrong\n', 1, True),
                                       ('Const,Title\ntt1234567,one,extra\n', 1, True)]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                imdb_csv_snapshot(value, expected_count=count, complete=complete)


class DiscoveryReadTests(unittest.TestCase):
    def feed(self, pages):
        self.calls = []
        def send(path, params):
            self.calls.append((path, params))
            return pages[int(params['page']) - 1]
        return TmdbDiscovery(TmdbClient('fixture', transport=send))

    def test_bounded_pages_and_default_dates_filters_are_native_read_only(self):
        rows = [native(i) for i in range(1, 21)]
        feed = self.feed([{'page': 1, 'total_pages': 2, 'total_results': 21, 'results': rows},
                          {'page': 2, 'total_pages': 2, 'total_results': 21, 'results': [native(21)]}])
        self.assertEqual(len(feed.collect(DiscoveryFilters(), today=TODAY)), 21)
        self.assertEqual(self.calls[0][0], '/discover/movie')
        params = self.calls[0][1]
        self.assertEqual(params['primary_release_date.gte'], '2025-04-24')
        self.assertEqual(params['primary_release_date.lte'], '2026-10-24')
        self.assertEqual(params['vote_count.gte'], '100')
        self.assertEqual(params['include_adult'], 'false')
        self.assertNotIn('without_genres', params)
        self.calls.clear()
        self.assertEqual(len(feed.collect(DiscoveryFilters(max_pages=1), today=TODAY)), 20)
        self.assertEqual(len(self.calls), 1)

    def test_empty_valid_snapshot_is_distinct_from_missing_data(self):
        self.assertEqual(self.feed([{'page': 1, 'total_pages': 0, 'total_results': 0, 'results': []}]).collect(DiscoveryFilters()), [])
        for data in ({}, {'page': 1, 'total_pages': 0, 'total_results': 1, 'results': []},
                     {'page': 1, 'total_pages': 1, 'total_results': 2, 'results': [native()]},
                     {'page': True, 'total_pages': 1, 'total_results': 1, 'results': [native()]}):
            with self.subTest(data=data), self.assertRaises(AdapterError):
                self.feed([data]).collect(DiscoveryFilters())

    def test_repeated_movie_or_changing_totals_reject_entire_collection(self):
        first = {'page': 1, 'total_pages': 2, 'total_results': 21, 'results': [native(i) for i in range(1, 21)]}
        for second in ({'page': 2, 'total_pages': 2, 'total_results': 21, 'results': [native(1)]},
                       {'page': 2, 'total_pages': 2, 'total_results': 22, 'results': [native(21), native(22)]}):
            with self.subTest(second=second), self.assertRaises(AdapterError):
                self.feed([first, second]).collect(DiscoveryFilters())

    def test_missing_genres_can_be_deferred_but_wrong_identity_cannot(self):
        feed = self.feed([{'page': 1, 'total_pages': 1, 'total_results': 1, 'results': [native(genre_ids=[])]}])
        self.assertIsNone(feed.collect(DiscoveryFilters())[0]['genres'])
        for identity in (None, True, 0, '1'):
            with self.subTest(identity=identity), self.assertRaises(AdapterError):
                self.feed([{'page': 1, 'total_pages': 1, 'total_results': 1, 'results': [native(identity)]}]).collect(DiscoveryFilters())

    def test_calendar_window_handles_month_end(self):
        self.assertEqual(DiscoveryFilters(months=1).start_date(date(2024, 3, 31)), date(2024, 2, 29))

    def test_invalid_filter_values_rejected(self):
        for values in ({'months': True}, {'rating': float('nan')}, {'rating': True}, {'votes': -1},
                       {'max_pages': 51}, {'excluded_genres': (18, 18)}, {'excluded_genres': [18]}, {'excluded_genres': (True,)}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                DiscoveryFilters(**values)


class SourceQueueTests(unittest.TestCase):
    def setUp(self):
        self.storage = Storage(':memory:'); self.storage.initialize(); self.addCleanup(self.storage.close)
        self.now = 1000
        self.intake = SourceIntake(self.storage, clock=lambda: self.now)
        self.journal = self.intake.journal
        self.source = self.journal.source('discovery', 'tmdb-discover')['id']
        self.filters = DiscoveryFilters()

    def poll(self, candidates, *, source=None, filters=None, known=()):
        claim = self.intake.begin(source or self.source)
        self.intake.discovery(claim, candidates, filters or self.filters, known_keys=known, today=TODAY)

    def candidate(self, key='tmdb:1'):
        return self.storage.connection.execute('SELECT * FROM discovery_candidate WHERE movie_key=?', (key,)).fetchone()

    def test_source_disappearance_does_not_expire_unanswered_proposal(self):
        self.poll([movie()]); self.poll([])
        self.assertEqual(len(self.intake.proposals()['items']), 1)
        self.assertEqual(self.candidate()['decision'], 'pending')

    def test_identical_repoll_preserves_revision_for_user_confirmation(self):
        self.poll([movie()]); revision = self.candidate()['revision']; self.poll([movie()])
        self.assertEqual(self.candidate()['revision'], revision)

    def test_no_is_household_shared_and_survives_filters_reconnect_and_source_disappearance(self):
        self.poll([movie()]); self.intake.decide('tmdb:1', revision=0, actor='telegram:1', accept=False)
        self.poll([], filters=DiscoveryFilters(excluded_genres=(18,))); self.poll([movie()])
        self.assertEqual(self.intake.proposals()['items'], [])
        row = self.candidate(); self.assertEqual(row['decision'], 'declined')
        self.intake.reopen('tmdb:1', revision=row['revision'], actor='telegram:2')
        self.assertEqual(len(self.intake.proposals()['items']), 1)
        self.assertEqual(self.storage.connection.execute('SELECT count(*) FROM coordination_operation').fetchone()[0], 0)

    def test_selected_proposal_has_no_acquisition_authority_or_native_write(self):
        self.poll([movie()]); self.intake.decide('tmdb:1', revision=0, actor='telegram:1', accept=True)
        self.assertEqual(self.candidate()['decision'], 'selected')
        self.assertEqual(self.intake.proposals()['items'][0]['decision'], 'selected')
        self.assertEqual(self.storage.connection.execute('SELECT count(*) FROM coordination_operation').fetchone()[0], 0)

    def test_stale_or_second_household_answer_cannot_overwrite_first(self):
        self.poll([movie()]); self.poll([movie(title='Updated title')])
        with self.assertRaises(StorageError): self.intake.decide('tmdb:1', revision=0, actor='telegram:1', accept=False)
        revision = self.candidate()['revision']
        self.intake.decide('tmdb:1', revision=revision, actor='telegram:1', accept=True)
        with self.assertRaises(StorageError): self.intake.decide('tmdb:1', revision=revision, actor='telegram:2', accept=False)

    def test_any_excluded_genre_and_missing_metadata_and_known_state_are_not_offered(self):
        self.poll([movie(1, genres=[18, 35]), movie(2, genres=[]), movie(3), movie(4, rating=6), movie(5, adult=True)],
                  filters=DiscoveryFilters(excluded_genres=(35,)), known={'tmdb:3'})
        self.assertEqual([self.candidate('tmdb:'+str(i))['eligibility'] for i in range(1, 6)], ['filtered', 'deferred', 'known', 'filtered', 'filtered'])
        self.assertEqual(self.intake.proposals()['items'], [])
        self.poll([movie(2)])
        self.assertIn('tmdb:2', [r['movie_key'] for r in self.intake.proposals()['items']])

    def test_retained_backlog_is_rechecked_against_new_known_movies_and_filter_edits(self):
        self.poll([movie(1), movie(2, genres=[35])]); self.poll([], known={'tmdb:1'}, filters=DiscoveryFilters(excluded_genres=(35,)))
        self.assertEqual(self.intake.proposals()['items'], [])
        self.assertEqual(self.candidate()['eligibility'], 'known')

    def test_multiple_feeds_merge_provenance_without_duplicate_cards(self):
        other = self.journal.source('discovery', 'second-feed')['id']
        self.poll([movie()]); self.poll([movie()], source=other)
        self.assertEqual(len(self.intake.proposals()['items']), 1)
        self.assertEqual(self.storage.connection.execute('SELECT count(*) FROM discovery_provenance').fetchone()[0], 2)

    def test_manual_ten_item_pages_do_not_delete_remainder(self):
        self.poll([movie(i) for i in range(1, 26)])
        first = self.intake.proposals(); self.assertEqual(len(first['items']), 10)
        second = self.intake.proposals(after=first['continue']); self.assertEqual(len(second['items']), 10)
        last = self.intake.proposals(after=second['continue']); self.assertEqual(len(last['items']), 5)
        self.assertIsNone(last['continue'])
        keys = [r['movie_key'] for page in (first, second, last) for r in page['items']]
        self.assertEqual(len(set(keys)), 25)

    def test_failed_poll_preserves_prior_queue_success_and_count(self):
        self.poll([movie()]); self.now += 1
        self.intake.failed(self.intake.begin(self.source))
        health = self.storage.connection.execute('SELECT * FROM intake_poll').fetchone()
        self.assertEqual(health['last_success'], 1000)
        self.assertEqual(health['item_count'], 1)
        self.assertEqual(health['last_error'], 'source_unavailable')
        self.assertEqual(len(self.intake.proposals()['items']), 1)

    def test_overlapping_or_expired_poll_cannot_publish(self):
        old = self.intake.begin(self.source); self.assertIsNone(self.intake.begin(self.source))
        self.now += 601; current = self.intake.begin(self.source)
        with self.assertRaises(StorageError): self.intake.discovery(old, [movie()], self.filters, known_keys=(), today=TODAY)
        self.intake.discovery(current, [movie(2)], self.filters, known_keys=(), today=TODAY)
        self.assertIsNone(self.candidate())

    def test_configuration_edit_fences_inflight_snapshot(self):
        claim = self.intake.begin(self.source)
        self.journal.source('discovery', 'tmdb-discover', config={'months': 12}, expected_revision=0)
        with self.assertRaises(StorageError): self.intake.discovery(claim, [movie()], self.filters, known_keys=(), today=TODAY)
        self.assertIsNone(self.candidate())

    def test_malformed_snapshot_is_atomic(self):
        claim = self.intake.begin(self.source)
        with self.assertRaises(ValueError): self.intake.discovery(claim, [movie(), movie()], self.filters, known_keys=(), today=TODAY)
        self.assertIsNone(self.candidate())
        self.assertIsNone(self.storage.connection.execute('SELECT last_success FROM intake_poll').fetchone()[0])

    def test_first_imdb_snapshot_is_all_historical_future_only_new_and_missing_never_reactivates(self):
        source = self.journal.source('wanted_list', 'imdb:user:fixture')['id']
        original = WantedSnapshot(('imdb:tt1234567', 'imdb:tt2345678'), 2)
        self.intake.wanted(self.intake.begin(source), original)
        self.intake.wanted(self.intake.begin(source), WantedSnapshot(('imdb:tt3456789',), 1))
        self.intake.wanted(self.intake.begin(source), original)
        rows = self.storage.connection.execute('SELECT item_key,state FROM wanted_request ORDER BY item_key').fetchall()
        self.assertEqual([tuple(r) for r in rows], [('imdb:tt1234567', 'historical'), ('imdb:tt2345678', 'historical'), ('imdb:tt3456789', 'new')])
        self.assertEqual(self.storage.connection.execute('SELECT count(*) FROM coordination_operation').fetchone()[0], 0)

    def test_invalid_wanted_snapshot_never_advances_baseline(self):
        source = self.journal.source('wanted_list', 'imdb:user:fixture')['id']
        claim = self.intake.begin(source)
        with self.assertRaises(ValueError): self.intake.wanted(claim, WantedSnapshot(('imdb:tt1234567',), 2))
        self.assertIsNone(self.journal.get_source(source)['baseline_hash'])

    def test_provider_failure_after_pages_or_known_state_outage_cannot_publish(self):
        class Feed:
            def collect(self, filters, *, today=None): return [movie()]
        def broken(): raise AdapterError('private provider URL and token')
        self.assertFalse(self.intake.collect_discovery(self.source, Feed(), self.filters, broken, today=TODAY))
        self.assertIsNone(self.candidate())
        self.assertEqual(self.intake.health(self.source)['status'], 'incomplete')
        self.assertNotIn('private', repr(self.intake.health(self.source)))
        self.assertTrue(self.intake.collect_discovery(self.source, Feed(), self.filters, lambda: set(), today=TODAY))
        self.assertEqual(self.intake.health(self.source)['status'], 'completed')

    def test_health_does_not_claim_success_after_interruption_or_config_edit(self):
        self.assertEqual(self.intake.health(self.source)['status'], 'not_run')
        self.intake.begin(self.source)
        self.assertEqual(self.intake.health(self.source)['status'], 'collecting')
        self.now += 601
        self.assertEqual(self.intake.health(self.source)['status'], 'incomplete')
        self.poll([movie()])
        self.journal.source('discovery', 'tmdb-discover', config={'months': 12}, expected_revision=0)
        self.assertEqual(self.intake.health(self.source)['status'], 'settings_changed')

    def test_source_failure_surfaces_in_read_only_maintenance_without_repair_action(self):
        from curatarr.services.system_maintenance import SystemMaintenance
        self.intake.failed(self.intake.begin(self.source))
        monitor = SystemMaintenance(self.storage, None, None, None, clock=lambda: self.now)
        self.assertIn('source_collection_incomplete', [i['kind'] for i in monitor.local_issues()])


if __name__ == '__main__': unittest.main()
