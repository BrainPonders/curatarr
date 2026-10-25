"""Broad read-only diagnostics, durable scheduling and truthful public health."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from curatarr.adapters import AdapterError
from curatarr.config import MaintenanceConfig, ConfigError, parse_config
from curatarr.domain import DurableState as State, ArrRuntimeState as Runtime
from curatarr.integrations.jellyfin import JellyfinReadAdapter, JellyfinError
from curatarr.integrations.radarr.client import RadarrClient, RadarrError
from curatarr.services.system_maintenance import SystemMaintenance, catalogue_issues, playback_issues, INTERVAL, LEASE
from curatarr.storage import Storage
from curatarr.web.app import create_app
from test_reconciliation import Snapshot, movie
import test_miniapp as miniapp
import yaml
from test_config import VALID_CONFIG


class Playback:
    def __init__(self):
        self.records = [{"id": "copy", "identities": ["tmdb:1"], "ready": True}]
        self.error = False
        self.direct_records = None
        self.direct_error = False
        self.direct_calls = []
    def maintenance_snapshot(self):
        if self.error:
            raise AdapterError("SECRET private-path/token")
        return deepcopy(self.records)
    def maintenance_items(self, item_ids):
        self.direct_calls.append(list(item_ids))
        if self.direct_error:
            raise AdapterError("SECRET direct-read failure")
        records = self.records if self.direct_records is None else self.direct_records
        return deepcopy([record for record in records if record['id'] in item_ids])


class SystemMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'workflow.sqlite'
        self.storage = Storage(self.path); self.storage.initialize(); self.addCleanup(self.storage.close)
        self.ryot = Snapshot([replace(movie('ryot', states=(State.LIBRARY, State.RADARR, State.PREVIOUSLY_ACQUIRED)),
                                     completed_watch_count=0)])
        self.radarr = Snapshot([movie('radarr', runtime=Runtime.ACTIVE_DOWNLOADED_MONITORED)])
        self.radarr.client = RadarrClient('http://fixture', 'secret', lambda path, params:
                                         [] if path.endswith('health') else {'totalRecords': 0, 'records': []})
        self.playback = Playback(); self.now = 100000
        self.monitor = SystemMaintenance(self.storage, self.ryot, self.radarr, self.playback, clock=lambda:self.now)
    def run_check(self):
        self.now += 31
        claim = self.monitor.claim(force=True); self.assertIsNotNone(claim)
        self.monitor.run(claim)
        return self.monitor.summary()

    def test_clean_requires_all_scopes_and_cannot_be_inferred_before_first_check(self):
        self.assertIsNone(self.monitor.summary()['maintenance_required'])
        self.assertFalse(self.run_check()['maintenance_required'])
        self.assertTrue(self.monitor.summary()['complete'])
        self.assertEqual(len(self.monitor.report()['scopes']), 9)
        self.assertNotIn('issues', self.monitor.summary())

    def test_hidden_legacy_categories_continue_reporting_and_are_not_dismissed_by_skipping(self):
        self.ryot.items[0] = replace(self.ryot.items[0], durable_states=frozenset({State.LIBRARY, State.RADARR}))
        self.assertTrue(self.run_check()['maintenance_required'])
        report = self.storage.reconciliation_report()
        self.assertEqual(report['findings'][0]['kind'], 'previously_acquired_missing')
        self.storage.save_maintenance_progress(1, {'skipped':['radarr:1'], 'cursor':None, 'category':'all','parked':True})
        self.assertTrue(self.monitor.summary()['maintenance_required'])
        self.assertEqual(self.storage.reconciliation_report()['finding_count'], 1)

    def test_provider_failure_retains_known_findings_and_never_reports_clean(self):
        self.playback.records = []
        self.assertTrue(self.run_check()['maintenance_required'])
        self.playback.error = True
        result = self.run_check()
        self.assertTrue(result['maintenance_required']); self.assertFalse(result['complete'])
        self.assertTrue(self.monitor.report()['scopes']['playback_consistency']['issues'])
        self.assertNotIn('SECRET', json.dumps(self.monitor.report()))

    def test_failed_check_after_clean_means_unknown_not_no(self):
        self.run_check(); self.ryot.error = True
        result = self.run_check()
        self.assertIsNone(result['maintenance_required']); self.assertFalse(result['complete'])
        self.assertFalse(self.storage.reconciliation_report()['revalidated'])

    def test_completed_clean_status_expires_and_interrupted_claim_cannot_publish_over_newer_run(self):
        self.run_check(); self.now += INTERVAL + 1
        self.assertIsNone(self.monitor.summary()['maintenance_required'])
        first = self.monitor.claim(); self.assertIsNotNone(first)
        self.assertIsNone(self.monitor.claim(force=True))
        self.now += LEASE + 1
        second = self.monitor.claim(force=True); self.assertIsNotNone(second)
        self.monitor.run(second); saved = self.monitor.report()
        self.playback.records = []
        self.monitor.run(first)
        self.assertEqual(self.monitor.report(), saved)

    def test_daily_and_failed_retry_schedule_survive_new_instances(self):
        self.monitor.tick(); self.assertEqual(self.ryot.calls, 1)
        self.monitor.tick(); self.assertEqual(self.ryot.calls, 1)
        self.now += INTERVAL + 1; self.playback.error = True
        self.monitor.tick(); self.assertEqual(self.ryot.calls, 2)
        self.now += 3599; self.monitor.tick(); self.assertEqual(self.ryot.calls, 2)
        self.now += 2; self.monitor.tick(); self.assertEqual(self.ryot.calls, 3)
        self.assertNotIn('SECRET', json.dumps(self.monitor.report()))

    def test_local_pending_reset_is_detected_without_changing_it(self):
        payload=json.dumps({'state':'pending','snapshot':{'id':'original'}})
        with self.storage.connection:
            self.storage.connection.execute("INSERT INTO state_snapshot(media_key,source,fingerprint,payload_json) VALUES('ryot:1','viewing_reset','',?)",(payload,))
        self.assertTrue(self.run_check()['maintenance_required'])
        self.assertIn('viewing_reset_pending', str(self.monitor.report()))
        self.assertEqual(self.storage.connection.execute("SELECT payload_json FROM state_snapshot WHERE source='viewing_reset'").fetchone()[0],payload)

    def test_catalogue_covers_unreferenced_duplicates_and_contradictory_memberships(self):
        first = replace(self.ryot.items[0], durable_states=frozenset({State.ARCHIVED,State.RADARR,State.SONARR,State.DELETED}), collection_names=('Completed',))
        second = replace(first, identities=first.identities-{next(i for i in first.identities if i.namespace=='ryot')} | {next(i for i in movie('ryot','2').identities if i.namespace=='ryot')})
        kinds={f['kind'] for f in catalogue_issues([first,second])}
        self.assertTrue({'duplicate_external_identity','managed_movie_outside_library','archived_active_memberships',
                         'deletion_without_acquisition_record','movie_in_series_collection','watched_flag_without_viewing'} <= kinds)

    def test_playback_checks_both_directions_and_keeps_identity_conflicts_separate(self):
        self.assertEqual(playback_issues(self.ryot.items,self.radarr.items,self.playback.records),[])
        self.assertIn('imported_copy_not_ready_in_playback',str(playback_issues(self.ryot.items,self.radarr.items,[])))
        unknown=[{'id':'unknown','identities':['tmdb:999'],'ready':True}]
        self.assertIn('playback_missing_ryot_match',str(playback_issues(self.ryot.items,self.radarr.items,unknown)))
        conflict=[{'id':'wrong','identities':['tmdb:1','imdb:tt999'],'ready':True}]
        from curatarr.domain import MediaIdentity
        arr=[replace(self.radarr.items[0],identities=self.radarr.items[0].identities|{MediaIdentity('imdb','tt1')})]
        self.assertIn('playback_identity_conflict',str(playback_issues(self.ryot.items,arr,conflict)))

    def test_multiple_jellyfin_versions_are_not_misreported_as_duplicates(self):
        records=self.playback.records+[dict(self.playback.records[0],id='another-copy')]
        self.assertEqual(playback_issues(self.ryot.items,self.radarr.items,records),[])

    def test_corrected_exact_identity_clears_false_bulk_warning_without_provider_writes(self):
        self.playback.records = [{'id':'copy', 'identities':['imdb:old'], 'ready':True}]
        self.playback.direct_records = [{'id':'copy', 'identities':['tmdb:1'], 'ready':True}]
        self.assertFalse(self.run_check()['maintenance_required'])
        self.assertEqual(self.playback.direct_calls, [['copy']])
        self.assertEqual(self.playback.records[0]['identities'], ['imdb:old'])
        self.assertEqual(self.monitor.report()['scopes']['playback_consistency']['issues'], [])

    def test_healthy_bulk_records_do_not_trigger_extra_reads(self):
        self.run_check()
        self.assertEqual(self.playback.direct_calls, [])

    def test_fresh_sources_can_reveal_missing_copy_instead_of_hiding_the_warning(self):
        self.playback.records = [{'id':'copy', 'identities':['tmdb:999'], 'ready':True}]
        self.playback.direct_records = [{'id':'copy', 'identities':['tmdb:1'], 'ready':False}]
        self.assertTrue(self.run_check()['maintenance_required'])
        issues = self.monitor.report()['scopes']['playback_consistency']['issues']
        self.assertEqual([i['kind'] for i in issues], ['imported_copy_not_ready_in_playback'])

    def test_previous_warning_is_rechecked_even_when_bulk_now_looks_healthy(self):
        self.playback.records = [{'id':'copy', 'identities':['tmdb:999'], 'ready':True}]
        self.assertTrue(self.run_check()['maintenance_required'])
        old = self.monitor.report()['scopes']['playback_consistency']['issues']
        self.playback.records = [{'id':'copy', 'identities':['tmdb:1'], 'ready':True}]
        self.playback.direct_error = True
        result = self.run_check()
        self.assertFalse(result['complete'])
        self.assertTrue(result['maintenance_required'])
        self.assertEqual(self.monitor.report()['scopes']['playback_consistency']['issues'], old)
        self.assertNotIn('SECRET', json.dumps(self.monitor.report()))

    def test_unverifiable_new_warning_means_unknown_when_no_prior_finding_exists(self):
        self.run_check()
        self.playback.records = [{'id':'copy', 'identities':['tmdb:999'], 'ready':True}]
        self.playback.direct_error = True
        result = self.run_check()
        self.assertFalse(result['complete'])
        self.assertIsNone(result['maintenance_required'])

    def test_item_missing_from_direct_read_but_present_in_bulk_is_incomplete(self):
        self.playback.records = [{'id':'copy', 'identities':['tmdb:999'], 'ready':True}]
        self.run_check()
        self.playback.direct_records = []
        result = self.run_check()
        self.assertFalse(result['complete'])
        self.assertTrue(result['maintenance_required'])

    def test_item_absent_from_both_complete_reads_can_clear_its_prior_warning(self):
        ghost = {'id':'ghost', 'identities':['tmdb:999'], 'ready':True}
        self.playback.records.append(ghost)
        self.assertTrue(self.run_check()['maintenance_required'])
        self.playback.records.pop()
        self.playback.direct_records = []
        self.assertFalse(self.run_check()['maintenance_required'])
        self.assertEqual(self.playback.direct_calls[-1], ['ghost'])

    def test_item_found_directly_but_absent_from_bulk_keeps_scope_incomplete(self):
        ghost = {'id':'ghost', 'identities':['tmdb:999'], 'ready':True}
        self.playback.records.append(ghost)
        self.run_check()
        self.playback.records.pop()
        self.playback.direct_records = [ghost]
        self.assertFalse(self.run_check()['complete'])
        self.assertTrue(self.monitor.summary()['maintenance_required'])

    def test_logical_movie_findings_recheck_every_matching_version(self):
        self.ryot.items[0] = replace(self.ryot.items[0], durable_states=frozenset({State.LIBRARY, State.ARCHIVED}))
        self.playback.records.append(dict(self.playback.records[0], id='second-copy'))
        self.assertTrue(self.run_check()['maintenance_required'])
        self.assertEqual(self.playback.direct_calls, [['copy', 'second-copy']])

    def test_excessive_flagged_items_do_not_produce_a_partial_clean_result(self):
        self.playback.records = [{'id':str(i), 'identities':['tmdb:999'], 'ready':True} for i in range(501)]
        self.assertFalse(self.run_check()['complete'])
        self.assertIsNone(self.monitor.summary()['maintenance_required'])
        self.assertEqual(self.playback.direct_calls, [])

    def test_partial_provider_failure_still_checks_other_services_and_local_operations(self):
        self.ryot.error=True
        self.radarr.client=RadarrClient('http://fixture','secret',lambda path,params:
            [{'type':'error','message':'SECRET'}] if path.endswith('health') else {'totalRecords':0,'records':[]})
        with self.storage.connection:
            self.storage.connection.execute("INSERT INTO operational_issue(issue_type,severity,context_json) VALUES('test_failure','error','{}')")
        result=self.run_check()
        self.assertFalse(result['complete']);self.assertTrue(result['maintenance_required'])
        report=self.monitor.report()
        self.assertTrue(report['scopes']['jellyfin']['complete'])
        self.assertIn('operational_issue_open',str(report['scopes']['local_operations']))
        self.assertIn('radarr_health_warning',str(report['scopes']['radarr_operations']))

    def test_repaired_records_clear_report_only_after_complete_recheck(self):
        self.playback.records=[]
        self.assertTrue(self.run_check()['maintenance_required'])
        self.playback.records=[{'id':'copy','identities':['tmdb:1'],'ready':True}]
        self.assertFalse(self.run_check()['maintenance_required'])
        self.assertTrue(self.monitor.summary()['complete'])

    def test_repository_cli_exposes_details_without_restoring_ui_or_provider_mutations(self):
        from curatarr.cli import main
        from contextlib import redirect_stdout
        import io
        output=io.StringIO()
        with patch('curatarr.cli.load_config',return_value=parse_config(yaml.safe_load(VALID_CONFIG))), \
                patch('curatarr.runtime.build_system_maintenance',return_value=self.monitor),redirect_stdout(output):
            result=main(['--config','fixture.yaml','--database',str(self.path),'--check-maintenance'])
        self.assertEqual(result,0)
        report=json.loads(output.getvalue())
        self.assertIn('scopes',report['report']);self.assertFalse(report['summary']['maintenance_required'])

    def test_public_routes_are_admin_only_async_and_do_not_expose_diagnostics(self):
        shell=miniapp.MiniAppTests(); shell.setUp()
        service=replace(shell.service,ryot_adapter=self.ryot,radarr_adapter=self.radarr)
        client=TestClient(create_app(shell.config,database=self.path,service=service,playback_adapter=self.playback))
        headers={'Authorization':'tma '+miniapp.signed_data(1)}
        other={'Authorization':'tma '+miniapp.signed_data(2)}
        for path in ('/api/maintenance/health','/api/maintenance/check'):
            response=client.get(path,headers=other) if path.endswith('health') else client.post(path,headers=other)
            self.assertEqual(response.status_code,403)
        self.assertFalse(client.get('/api/me',headers=headers).json()['show_sync_diagnostics'])
        result=client.post('/api/maintenance/check',headers=headers)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['status'],'checking')
        result=client.get('/api/maintenance/health',headers=headers).json()
        self.assertTrue(result['complete']); self.assertFalse(result['maintenance_required'])
        self.assertNotIn('scopes',result); self.assertNotIn('SECRET',json.dumps(result))
        self.assertIsNotNone(self.storage.reconciliation_report()['run_id'])

    def test_diagnostics_are_opt_in_and_invalid_configuration_is_rejected(self):
        raw=yaml.safe_load(VALID_CONFIG)
        self.assertFalse(parse_config(raw).maintenance.show_diagnostics)
        raw['maintenance']={'show_diagnostics':True}; self.assertTrue(parse_config(raw).maintenance.show_diagnostics)
        raw['maintenance']={'show_diagnostics':'true'}
        with self.assertRaises(ConfigError):parse_config(raw)


class MaintenanceSnapshotTests(unittest.TestCase):
    @staticmethod
    def native_item(identifier, **updates):
        return {'Id':identifier, 'Type':'Movie', 'ProviderIds':{'Tmdb':'1'},
                'MediaSources':[{'Path':'/SECRET/movie.mkv'}], **updates}

    def test_exact_reads_are_batched_uncached_and_never_expose_paths(self):
        calls = []
        def transport(path, params):
            calls.append(params)
            items = [self.native_item(i) for i in params['Ids'].split(',')]
            return {'Items':items, 'TotalRecordCount':len(items)}
        adapter = JellyfinReadAdapter('http://fixture', 'SECRET', transport)
        records = adapter.maintenance_items([str(i) for i in range(51)])
        self.assertEqual(len(records), 51)
        self.assertEqual([len(p['Ids'].split(',')) for p in calls], [50, 1])
        self.assertNotIn('SECRET', json.dumps(records))
        adapter.maintenance_items(['0'])
        self.assertEqual(len(calls), 3)

    def test_exact_reads_accept_complete_absence_but_reject_partial_or_wrong_results(self):
        adapter = JellyfinReadAdapter('http://fixture', 'SECRET', lambda *_:{'Items':[], 'TotalRecordCount':0})
        self.assertEqual(adapter.maintenance_items(['gone']), [])
        invalid = [
            {'Items':[], 'TotalRecordCount':1},
            {'Items':[self.native_item('other')], 'TotalRecordCount':1},
            {'Items':[self.native_item('copy'), self.native_item('copy')], 'TotalRecordCount':2},
            {'Items':[self.native_item('copy', Type='Series')], 'TotalRecordCount':1},
            {'Items':[self.native_item('copy', MediaSources=None)], 'TotalRecordCount':1}]
        for payload in invalid:
            with self.subTest(payload=payload):
                adapter = JellyfinReadAdapter('http://fixture', 'SECRET', lambda *_, p=payload:p)
                with self.assertRaises(JellyfinError):adapter.maintenance_items(['copy'])

    def test_exact_read_identifier_validation_is_bounded_before_network_access(self):
        calls = []
        adapter = JellyfinReadAdapter('http://fixture', 'SECRET', lambda *args:calls.append(args))
        for ids in [['one,two'], ['copy', 'copy'], [None], [[]], [str(i) for i in range(501)]]:
            with self.subTest(ids=ids[:2]):
                with self.assertRaises(JellyfinError):adapter.maintenance_items(ids)
        self.assertEqual(calls, [])

    def test_jellyfin_snapshot_is_fresh_complete_and_contains_no_paths(self):
        item={'Id':'copy','Type':'Movie','ProviderIds':{'Tmdb':'1'},'MediaSources':[{'Path':'/SECRET/movie.mkv'}]}
        calls=[]
        adapter=JellyfinReadAdapter('http://fixture','SECRET',lambda path,params:calls.append(params) or {'Items':[item],'TotalRecordCount':1})
        self.assertTrue(adapter.maintenance_snapshot()[0]['ready'])
        self.assertNotIn('SECRET',json.dumps(adapter.maintenance_snapshot()))
        self.assertEqual(len(calls),2)
        item.pop('MediaSources')
        with self.assertRaises(JellyfinError):adapter.maintenance_snapshot()

    def test_jellyfin_repeated_or_partial_page_is_not_a_clean_snapshot(self):
        adapter=JellyfinReadAdapter('http://fixture','SECRET',lambda *_:{'Items':[],'TotalRecordCount':1})
        with self.assertRaises(JellyfinError):adapter.maintenance_snapshot()

    def test_radarr_warnings_and_import_errors_are_sanitized(self):
        def transport(path,params):
            if path.endswith('health'):return [{'type':'warning','message':'SECRET path','source':'secret'}]
            return {'totalRecords':1,'records':[{'id':1,'movieId':7,'trackedDownloadStatus':'error','outputPath':'SECRET','errorMessage':'SECRET'}]}
        result=RadarrClient('http://fixture','SECRET',transport).maintenance_health()
        self.assertEqual(len(result),2); self.assertNotIn('SECRET',json.dumps(result))

    def test_radarr_partial_or_duplicate_queue_fails(self):
        def transport(path,params):
            if path.endswith('health'):return []
            return {'totalRecords':1,'records':[]}
        with self.assertRaises(RadarrError):RadarrClient('http://fixture','SECRET',transport).maintenance_health()
