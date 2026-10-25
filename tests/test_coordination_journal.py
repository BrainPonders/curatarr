"""Transactional upgrades, immutable intake, historical baselines and fenced effects."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from curatarr.adapters import AdapterError
from curatarr.services.operations import Inspection, OperationRunner, OperationStep, Outcome
from curatarr.storage import Storage, StorageError
from curatarr.storage.journal import CoordinationJournal, encode
from curatarr.storage.sqlite import SCHEMA_SQL, SCHEMA_VERSION


class Clock:
    value = 1000.0
    def __call__(self):return self.value
    def advance(self, seconds=301):self.value += seconds


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.storage=Storage(':memory:');self.storage.initialize();self.addCleanup(self.storage.close)
        self.clock=Clock();self.j=CoordinationJournal(self.storage,self.clock)
        self.source=self.j.source('jellyfin','server',account='household')['id']
    def op(self, key='request', media='tmdb:123', steps=('add','verify')):
        return self.j.create_operation(key,media,'add',{'actor':'telegram:1','basis':'confirmed'}, {'profile':2},steps)
    def baseline(self, source, keys):
        return self.j.baseline_wanted(source, keys, total_count=len(keys), complete=True)
    def event(self, key='occurrence', media='tmdb:123', **kwargs):
        return self.j.event(self.source,key,media,'completed',{'session':'s'},**kwargs)
    def test_immutable_event_replay_returns_original_without_receipt_time_fabrication(self):
        event=self.event();self.clock.advance()
        replay=self.event();self.assertEqual(event,replay);self.assertIsNone(event['effective_at'])
    def test_event_occurrence_cannot_be_retargeted_or_rewritten(self):
        self.event()
        for media,payload in [('tmdb:999',{'session':'s'}),('tmdb:123',{'session':'different'})]:
            with self.assertRaises(StorageError):self.j.event(self.source,'occurrence',media,'completed',payload)
        self.assertEqual(self.storage.connection.execute('SELECT count(*) FROM coordination_event').fetchone()[0],1)
    def test_event_identity_is_scoped_to_source_account(self):
        first=self.event()
        source=self.j.source('jellyfin','server',account='other')['id']
        other=self.j.event(source,'occurrence','tmdb:123','completed',{'session':'s'})
        self.assertNotEqual(first['id'],other['id'])
    def test_event_claim_requires_expiry_and_old_owner_cannot_finish(self):
        event=self.event();first=self.j.claim_event(event['id']);self.assertIsNone(self.j.claim_event(event['id']))
        self.clock.advance();second=self.j.claim_event(event['id'])
        with self.assertRaises(StorageError):self.j.finish_event(first)
        self.j.finish_event(second);self.assertIsNone(self.j.claim_event(event['id']))
    def test_deferred_event_keeps_evidence_and_waits_for_due_time(self):
        e=self.event();claim=self.j.claim_event(e['id']);self.j.defer_event(claim,'source_unavailable')
        self.assertEqual(self.j.get_event(e['id'])['payload_json'],e['payload_json'])
        self.assertIsNone(self.j.claim_event(e['id']));self.clock.advance();self.assertIsNotNone(self.j.claim_event(e['id']))
    def test_event_cannot_link_a_different_movie_operation(self):
        e=self.event();claim=self.j.claim_event(e['id']);op=self.op(media='tmdb:999')
        with self.assertRaises(StorageError):self.j.finish_event(claim,operation_id=op['id'])
    def test_unresolved_event_has_separate_immutable_canonical_resolution(self):
        e=self.event(media='imdb:tt123');claim=self.j.claim_event(e['id'])
        self.j.resolve_event(claim,'tmdb:123')
        with self.assertRaises(StorageError):self.j.resolve_event(claim,'tmdb:456')
        op=self.op();self.j.finish_event(claim,operation_id=op['id'])
        self.assertEqual(self.j.get_event(e['id'])['media_key'],'imdb:tt123')
    def test_known_tmdb_event_cannot_change_canonical_identity(self):
        e=self.event();claim=self.j.claim_event(e['id'])
        with self.assertRaises(StorageError):self.j.resolve_event(claim,'tmdb:999')
    def test_source_reconfiguration_preserves_identity_and_requires_revision(self):
        source=self.j.source('wanted_list','imdb:watchlist',config={'profile':2})
        with self.assertRaises(StorageError):self.j.source('wanted_list','imdb:watchlist',config={'profile':3})
        changed=self.j.source('wanted_list','imdb:watchlist',config={'profile':3},expected_revision=source['revision'])
        self.assertEqual(source['id'],changed['id']);self.assertEqual(changed['revision'],1)
    def test_current_imdb_baseline_can_never_return_as_new_on_restart_or_reconnect(self):
        source=self.j.source('wanted_list','imdb:watchlist')['id']
        self.baseline(source,['imdb:tt1','imdb:tt2'])
        self.assertEqual(self.j.observe_wanted(source,['imdb:tt1','imdb:tt2']),())
        self.assertEqual(self.j.observe_wanted(source,[]),())
        self.assertEqual(self.j.observe_wanted(source,['imdb:tt1']),())
        reconnected=self.j.source('wanted_list','imdb:watchlist')['id'];self.assertEqual(source,reconnected)
        self.assertEqual(self.j.observe_wanted(reconnected,['imdb:tt1','imdb:tt2','imdb:tt3']),('imdb:tt3',))
        self.assertEqual(self.j.observe_wanted(source,['imdb:tt3']),())
        states=dict(self.storage.connection.execute('SELECT item_key,state FROM wanted_request'))
        self.assertEqual(states,{'imdb:tt1':'historical','imdb:tt2':'historical','imdb:tt3':'new'})
    def test_wanted_snapshot_requires_explicit_complete_baseline_even_if_empty(self):
        source=self.j.source('wanted_list','imdb:watchlist')['id']
        with self.assertRaises(StorageError):self.j.observe_wanted(source,['imdb:tt1'])
        self.baseline(source,[])
        self.assertEqual(self.j.observe_wanted(source,['imdb:tt1']),('imdb:tt1',))
    def test_partial_or_count_mismatched_snapshot_cannot_establish_imdb_baseline(self):
        source=self.j.source('wanted_list','imdb:watchlist')['id']
        for count,complete in [(2,True),(1,False),(True,True)]:
            with self.assertRaises(StorageError):self.j.baseline_wanted(source,['imdb:tt1'],total_count=count,complete=complete)
        self.assertIsNone(self.j.get_source(source)['baseline_hash'])
    def test_scheduler_does_not_offer_unresolved_movies_to_a_different_operation(self):
        a=self.op();b=self.op('second');c=self.op('other','tmdb:456')
        claim=self.j.claim_operation(a['id']);self.j.defer_operation(claim,'uncertain')
        self.assertNotIn(b['id'],self.j.ready_operations())
        self.assertIn(c['id'],self.j.ready_operations());self.clock.advance()
        self.assertIn(a['id'],self.j.ready_operations())
    def test_event_scheduler_omits_claimed_and_processed_and_retries_deferred(self):
        a=self.event();b=self.event('second');claim=self.j.claim_event(a['id'])
        self.assertEqual(self.j.ready_events(),[b['id']]);self.j.defer_event(claim,'offline')
        self.assertEqual(self.j.ready_events(),[b['id']]);self.clock.advance()
        self.assertEqual(self.j.ready_events(),[a['id'],b['id']])
    def test_replacing_baseline_is_rejected_and_historical_requests_stay_handled(self):
        source=self.j.source('wanted_list','imdb:watchlist')['id'];self.baseline(source,['imdb:tt1'])
        self.assertEqual(self.baseline(source,['imdb:tt1']),1)
        with self.assertRaises(StorageError):self.baseline(source,['imdb:tt2'])
        self.assertEqual(self.j.observe_wanted(source,['imdb:tt1','imdb:tt2']),('imdb:tt2',))
    def test_invalid_payload_or_snapshot_commits_nothing(self):
        for payload in [{'x':float('nan')},{'x':'x'*262145}]:
            with self.assertRaises(ValueError):self.j.event(self.source,'bad','tmdb:1','completed',payload)
        source=self.j.source('wanted_list','imdb:watchlist')['id']
        with self.assertRaises(ValueError):self.baseline(source,['imdb:tt1','imdb:tt1'])
        self.assertIsNone(self.j.get_source(source)['baseline_hash'])
    def test_operation_replay_has_identical_authorization_plan_and_snapshot(self):
        first=self.op();self.assertEqual(first,self.op())
        with self.assertRaises(StorageError):self.j.create_operation('request','tmdb:123','add',{'actor':'telegram:2','basis':'confirmed'},{'profile':2},['add','verify'])
        self.assertEqual(len(self.storage.connection.execute('SELECT * FROM coordination_operation').fetchall()),1)
    def test_write_operations_require_a_canonical_identity_and_authorization(self):
        for media in ['imdb:tt1','ryot:movie','plain-title']:
            with self.assertRaises(ValueError):self.op(media=media)
        with self.assertRaises(ValueError):self.j.create_operation('bad','tmdb:1','add',{}, {},['add'])
    def test_two_operations_for_same_movie_cannot_claim_together(self):
        a=self.op();b=self.op('second');claim=self.j.claim_operation(a['id'])
        self.assertIsNotNone(claim);self.assertIsNone(self.j.claim_operation(b['id']))
        self.clock.advance();self.assertIsNone(self.j.claim_operation(b['id']))
        # Expiry is permission to recover the same operation, not bypass it.
        self.assertIsNotNone(self.j.claim_operation(a['id']))
    def test_other_movies_can_progress_while_one_outcome_is_uncertain(self):
        a=self.op();b=self.op('second','tmdb:456');claim=self.j.claim_operation(a['id'])
        self.j.begin_step(claim,'add');self.j.defer_operation(claim,'lost_reply')
        self.assertIsNotNone(self.j.claim_operation(b['id']))
    def test_attempt_saved_before_native_call_and_never_started_twice(self):
        op=self.op();claim=self.j.claim_operation(op['id'])
        self.assertTrue(self.j.begin_step(claim,'add'));self.assertFalse(self.j.begin_step(claim,'add'))
        self.j.step_result(claim,'add',verified=False,evidence={'job':'accepted'})
        self.assertFalse(self.j.begin_step(claim,'add'))
    def test_steps_cannot_skip_order_or_complete_without_verification(self):
        op=self.op();claim=self.j.claim_operation(op['id'])
        with self.assertRaises(StorageError):self.j.begin_step(claim,'verify')
        with self.assertRaises(StorageError):self.j.step_result(claim,'verify',verified=True,evidence={'checked':True})
        with self.assertRaises(StorageError):self.j.finish_operation(claim)
    def test_expired_claim_cannot_publish_any_step_or_completion(self):
        op=self.op();old=self.j.claim_operation(op['id']);self.clock.advance();fresh=self.j.claim_operation(op['id'])
        self.assertGreater(fresh.generation,old.generation)
        with self.assertRaises(StorageError):self.j.begin_step(old,'add')
        with self.assertRaises(StorageError):self.j.step_result(old,'add',verified=True,evidence={'checked':True})
        with self.assertRaises(StorageError):self.j.defer_operation(old,'stale')
        with self.assertRaises(StorageError):self.j.finish_operation(old)
    def test_verified_operation_releases_movie_and_audits_once(self):
        a=self.op();b=self.op('second');claim=self.j.claim_operation(a['id'])
        for name in ['add','verify']:self.j.step_result(claim,name,verified=True,evidence={'already_correct':True})
        self.assertEqual(self.j.finish_operation(claim)['status'],'completed')
        self.assertIsNone(self.j.claim_operation(a['id']))
        with self.assertRaises(StorageError):self.j.finish_operation(claim)
        self.assertIsNotNone(self.j.claim_operation(b['id']))
        self.assertEqual(self.storage.connection.execute("SELECT count(*) FROM audit_log WHERE action='coordination_operation_verified'").fetchone()[0],1)
    def test_only_unstarted_unclaimed_operations_can_be_cancelled(self):
        a=self.op();self.assertTrue(self.j.cancel_unstarted(a['id']));self.assertFalse(self.j.cancel_unstarted(a['id']))
        b=self.op('second');claim=self.j.claim_operation(b['id']);self.j.begin_step(claim,'add');self.clock.advance()
        with self.assertRaises(StorageError):self.j.cancel_unstarted(b['id'])
    def test_nested_transaction_is_rejected_instead_of_committing_callers_work(self):
        self.storage.connection.execute("INSERT INTO runtime_cursor VALUES('test',1)")
        with self.assertRaises(StorageError):self.event()
        self.storage.connection.rollback()
        self.assertIsNone(self.storage.connection.execute("SELECT * FROM runtime_cursor WHERE name='test'").fetchone())


class MigrationAndConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state.sqlite';self.s=Storage(self.path);self.addCleanup(self.s.close)
        # Actual schema3 structure with representative durable data.
        self.s.connection.executescript(SCHEMA_SQL)
        self.s.connection.execute('INSERT INTO schema_version VALUES(1,3)')
        self.s.connection.execute("INSERT INTO runtime_cursor VALUES('bot',77)")
        self.s.connection.execute("INSERT INTO state_snapshot(media_key,source,fingerprint,payload_json) VALUES('household','settings','f','{\"name\":\"Chickenhouse\"}')")
        self.s.connection.execute("INSERT INTO pending_decision(media_key,decision_type,audience,state_fingerprint,context_json) VALUES('ryot:movie','viewing_followup','shared_household','f','{}')")
        self.s.connection.execute("INSERT INTO viewing_event(source,event_id,media_key,payload_json,decision_id) VALUES('account','seen','ryot:movie','{}',1)")
        self.s.connection.commit()
    def preserved(self):
        return {t:[tuple(r) for r in self.s.connection.execute('SELECT * FROM '+t)] for t in ['runtime_cursor','state_snapshot','pending_decision','viewing_event']}
    def test_schema3_upgrade_is_additive_and_idempotent(self):
        before=self.preserved();self.s.initialize();self.s.initialize()
        self.assertEqual(self.s.schema_version(),SCHEMA_VERSION);self.assertEqual(before,self.preserved())
        self.assertEqual(self.s.connection.execute('PRAGMA quick_check').fetchone()[0],'ok')
    def test_schema4_upgrade_preserves_existing_operations_events_and_source_intent(self):
        self.s.initialize()
        journal=CoordinationJournal(self.s)
        source=journal.source('fixture','source',account='household')
        event=journal.event(source['id'],'occurrence','tmdb:123','fixture',{'keep':True})
        operation=journal.create_operation('fixture','tmdb:123','fixture',{'actor':'admin','basis':'fixture'},{'keep':True},['verify'])
        with self.s.connection:
            self.s.connection.execute('DROP TABLE playback_candidate')
            self.s.connection.execute('DROP TABLE playback_session')
            self.s.connection.execute('UPDATE schema_version SET version=4')
        tables=[r[0] for r in self.s.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('schema_version','sqlite_sequence')")]
        before={t:[tuple(r) for r in self.s.connection.execute('SELECT * FROM '+t)] for t in tables}
        self.s.initialize();self.s.initialize()
        self.assertEqual(self.s.schema_version(),5)
        self.assertEqual(before,{t:[tuple(r) for r in self.s.connection.execute('SELECT * FROM '+t)] for t in tables})
        self.assertEqual(journal.get_event(event['id'])['status'],'received')
        self.assertEqual(journal.operation(operation['id'])['status'],'pending')
        self.assertEqual(self.s.connection.execute('PRAGMA quick_check').fetchone()[0],'ok')
        self.assertEqual(self.s.connection.execute('PRAGMA foreign_key_check').fetchall(),[])
    def test_failed_migration_rolls_back_all_tables_and_schema_version(self):
        from curatarr.storage.journal import JOURNAL_SCHEMA
        before=self.preserved()
        with patch('curatarr.storage.journal.JOURNAL_SCHEMA',JOURNAL_SCHEMA+'\nINVALID MIGRATION;\n'):
            with self.assertRaises(sqlite3.Error):self.s.initialize()
        self.assertEqual(self.s.schema_version(),3);self.assertEqual(self.preserved(),before)
        self.assertIsNone(self.s.connection.execute("SELECT name FROM sqlite_master WHERE name='coordination_event'").fetchone())
        self.s.initialize();self.assertEqual(self.s.schema_version(),SCHEMA_VERSION)
    def test_future_schema_is_rejected_without_any_changes(self):
        self.s.connection.execute('UPDATE schema_version SET version=99');self.s.connection.commit()
        before=self.preserved()
        with self.assertRaises(StorageError):self.s.initialize()
        self.assertEqual(self.s.schema_version(),99);self.assertEqual(self.preserved(),before)
        self.assertIsNone(self.s.connection.execute("SELECT name FROM sqlite_master WHERE name='coordination_event'").fetchone())
    def test_missing_schema_version_is_not_silently_repaired(self):
        self.s.connection.execute('DELETE FROM schema_version');self.s.connection.commit()
        with self.assertRaises(StorageError):self.s.initialize()
        self.assertIsNone(self.s.connection.execute("SELECT name FROM sqlite_master WHERE name='coordination_event'").fetchone())
    def test_invalid_schema_version_is_not_silently_upgraded(self):
        for value in ('broken',0,-1):
            self.s.connection.execute('UPDATE schema_version SET version=?',(value,));self.s.connection.commit()
            with self.subTest(value=value),self.assertRaises(StorageError):self.s.initialize()
            self.assertIsNone(self.s.connection.execute("SELECT name FROM sqlite_master WHERE name='coordination_event'").fetchone())
    def test_operation_and_attempt_survive_process_restart(self):
        self.s.initialize();j=CoordinationJournal(self.s,lambda:1000)
        op=j.create_operation('r','tmdb:1','add',{'actor':'user','basis':'confirmed'}, {}, ['write']);claim=j.claim_operation(op['id']);j.begin_step(claim,'write')
        other=Storage(self.path);self.addCleanup(other.close);other.initialize();nextj=CoordinationJournal(other,lambda:1400)
        claim=nextj.claim_operation(op['id']);self.assertIsNotNone(claim)
        self.assertFalse(nextj.begin_step(claim,'write'));self.assertEqual(nextj.operation(op['id'])['steps'][0]['status'],'attempted')
    def test_concurrent_connections_get_one_operation_owner(self):
        self.s.initialize();j=CoordinationJournal(self.s)
        op=j.create_operation('r','tmdb:1','add',{'actor':'user','basis':'confirmed'}, {}, ['write'])
        def claim(_):
            s=Storage(self.path)
            try:return CoordinationJournal(s).claim_operation(op['id'])
            finally:s.close()
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(claim,range(8)))
        self.assertEqual(sum(x is not None for x in results),1)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.s=Storage(':memory:');self.s.initialize();self.addCleanup(self.s.close);self.clock=Clock();self.j=CoordinationJournal(self.s,self.clock)
        self.op=self.j.create_operation('r','tmdb:1','add',{'actor':'user','basis':'confirmed'}, {}, ['write'])
        self.correct=False;self.calls=0
    def check(self,op):return Inspection(Outcome.VERIFIED if self.correct else Outcome.NEEDS_WRITE, {'correct':self.correct})
    def apply(self,op):self.calls+=1;self.correct=True
    def runner(self,write=None,check=None):return OperationRunner(self.j,{'add':[OperationStep('write',check or self.check,write or self.apply)]})
    def test_native_effect_is_claimed_then_verified_and_not_replayed(self):
        def native(op):
            self.assertEqual(self.j.operation(op['id'])['steps'][0]['status'],'attempted');self.apply(op)
        runner=self.runner(native);self.assertEqual(runner.run(self.op['id'])['status'],'completed')
        runner.run(self.op['id']);self.assertEqual(self.calls,1)
    def test_already_satisfied_effect_never_writes(self):
        self.correct=True;self.assertEqual(self.runner().run(self.op['id'])['status'],'completed');self.assertEqual(self.calls,0)
    def test_lost_reply_after_effect_is_reconciled_without_repeat(self):
        def lost(op):self.apply(op);raise AdapterError('credential-bearing-url must not leak')
        runner=self.runner(lost);self.assertEqual(runner.run(self.op['id'])['status'],'uncertain');self.clock.advance()
        self.assertEqual(runner.run(self.op['id'])['status'],'completed');self.assertEqual(self.calls,1)
        self.assertNotIn('credential-bearing',encode(self.j.operation(self.op['id'])))
    def test_lost_request_without_effect_never_blindly_resubmits(self):
        def lost(op):self.calls+=1;raise AdapterError('Timeout')
        runner=self.runner(lost);runner.run(self.op['id']);self.clock.advance();result=runner.run(self.op['id'])
        self.assertEqual(result['status'],'uncertain');self.assertEqual(self.calls,1)
    def test_accepted_async_job_waits_until_read_back(self):
        def accepted(op):self.calls+=1
        runner=self.runner(accepted);self.assertEqual(runner.run(self.op['id'])['status'],'uncertain')
        self.clock.advance();runner.run(self.op['id']);self.assertEqual(self.calls,1)
        self.correct=True;self.clock.advance();self.assertEqual(runner.run(self.op['id'])['status'],'completed')
    def test_conflict_or_unregistered_plan_never_mutates(self):
        runner=self.runner(check=lambda op:Inspection(Outcome.CONFLICT,{'conflict':True}))
        self.assertEqual(runner.run(self.op['id'])['status'],'blocked');self.assertEqual(self.calls,0)
        self.clock.advance();self.assertEqual(OperationRunner(self.j,{}).run(self.op['id'])['status'],'blocked')
    def test_process_death_leaves_attempt_for_recovery_instead_of_a_second_create(self):
        class Crash(BaseException):pass
        def crash(op):self.apply(op);raise Crash()
        runner=self.runner(crash)
        with self.assertRaises(Crash):runner.run(self.op['id'])
        self.clock.advance();self.assertEqual(runner.run(self.op['id'])['status'],'completed');self.assertEqual(self.calls,1)
    def test_expiry_during_inspection_cannot_reach_native_write(self):
        def expired(op):self.clock.advance();return self.check(op)
        with self.assertRaises(StorageError):self.runner(check=expired).run(self.op['id'])
        self.assertEqual(self.calls,0)
