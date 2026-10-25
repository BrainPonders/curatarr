"""Single-movie acquisition repairs never imply filesystem checks or batch writes."""

from dataclasses import replace
import unittest

from curatarr.adapters import AdapterError, AdapterResult
from curatarr.domain import ArrRuntimeState as Runtime, DurableState as State, MediaIdentity
from curatarr.integrations.ryot.adapter import RyotReadAdapter
from curatarr.integrations.ryot.client import RyotClient, RyotError
from curatarr.integrations.ryot.corrections import RyotAcquisitionWriter
from curatarr.services.maintenance import MaintenanceService
import test_maintenance as maintenance_fixtures


class AcquisitionCorrectionTests(unittest.TestCase):
    def setUp(self):
        maintenance_fixtures.MaintenanceTests.setUp(self)
        self.radarr.items[0] = replace(self.radarr.items[0], arr_runtime_state=Runtime.ACTIVE_DOWNLOADED_MONITORED)
        self.reconciliation.scan()
        self.writes = []
        owner = self

        class Writer:
            def record_acquisition(self, identity):
                owner.writes.append(identity)
                owner.ryot.items[0] = replace(owner.ryot.items[0], durable_states=owner.ryot.items[0].durable_states | {State.PREVIOUSLY_ACQUIRED})
                return True

        self.writer = Writer()
        self.service = MaintenanceService(self.reconciliation, self.writer)

    def target(self):
        data = self.service.review(1)
        finding = next(f for f in data["movie"]["findings"] if f["kind"] == "previously_acquired_missing")
        return {"media_key": data["movie"]["key"], "finding_id": finding["id"]}

    def test_single_correction_reads_back_and_audits_without_changing_arr(self):
        target = self.target()
        before = tuple(self.radarr.items)
        result = self.service.correct(1, **target)
        self.assertEqual(self.writes, [MediaIdentity("ryot", "1")])
        self.assertEqual(tuple(self.radarr.items), before)
        self.assertIn("verified", result["notice"])
        actions = [row[0] for row in self.storage.connection.execute("SELECT action FROM audit_log ORDER BY id")]
        self.assertEqual(actions, ["acquisition_correction_requested", "acquisition_correction_verified"])
        self.assertFalse(any(f["media_key"] == "radarr:1" for f in self.storage.reconciliation_report()["findings"]))
        with self.assertRaises(ValueError):
            self.service.correct(1, **target)
        self.assertEqual(len(self.writes), 1)

    def test_changed_file_archival_identity_and_provider_outage_block_writes(self):
        target = self.target()
        original = self.radarr.items[0]
        for item in (replace(original, arr_runtime_state=Runtime.ACTIVE_MISSING_MONITORED),
                     replace(original, identities={MediaIdentity("radarr", "1"), MediaIdentity("tmdb", "999")})):
            self.radarr.items[0] = item
            with self.assertRaises(ValueError):
                self.service.correct(1, **target)
            self.radarr.items[0] = original
            self.reconciliation.scan()
            target = self.target()
        self.ryot.error = True
        with self.assertRaises(AdapterError):
            self.service.correct(1, **target)
        self.ryot.error = False
        self.ryot.items[0] = replace(self.ryot.items[0], durable_states={State.ARCHIVED})
        with self.assertRaises(ValueError):
            self.service.correct(1, **target)
        self.assertEqual(self.writes, [])

    def test_duplicate_identity_blocks_correction(self):
        target = self.target()
        self.ryot.items.append(replace(self.ryot.items[0], identities={MediaIdentity("ryot", "duplicate"), MediaIdentity("tmdb", "1")}))
        with self.assertRaises(ValueError):
            self.service.correct(1, **target)
        self.assertEqual(self.writes, [])

    def test_unconfirmed_submission_retains_finding(self):
        target = self.target()
        self.writer.record_acquisition = lambda identity: False
        result = self.service.correct(1, **target)
        self.assertIn("not yet confirmed", result["notice"])
        self.assertTrue(any(f["kind"] == "previously_acquired_missing" for f in self.storage.reconciliation_report()["findings"]))

    def test_failed_writer_does_not_claim_rollback_or_success(self):
        target = self.target()
        def fail(identity):
            raise AdapterError("SECRET")
        self.writer.record_acquisition = fail
        result = self.service.correct(1, **target)
        self.assertFalse(result["validated"])
        self.assertIn("may have been submitted", result["notice"])
        self.assertNotIn("SECRET", str(result))


class AcquisitionWriterTests(unittest.TestCase):
    def reader(self, *, verified=True, accepted=True, collections=None):
        self.requests = []
        def transport(query, variables):
            self.requests.append((query, variables))
            if query.startswith("mutation"):
                return {"data": {"deployAddEntitiesToCollectionJob": accepted}}
            return {"data": {"userDetails": {"id": "household"}, "userCollectionsList": {"response": collections if collections is not None else [
                {"name": "Previously acquired", "isDefault": False, "creator": {"id": "household"}}]}}}
        reader = RyotReadAdapter(RyotClient("https://example.invalid/graphql", "secret", transport))
        # The write request and its verification read are tested independently.
        class VerificationReader:
            client = reader.client
            collections = reader.collections
            def get_item(self, identity):
                from test_reconciliation import movie
                return AdapterResult(movie("ryot", states=(State.PREVIOUSLY_ACQUIRED,) if verified else ()))
        return VerificationReader()

    def test_only_one_metadata_entity_is_added_and_success_requires_readback(self):
        writer = RyotAcquisitionWriter(self.reader())
        self.assertTrue(writer.record_acquisition(MediaIdentity("ryot", "met_1")))
        payload = self.requests[1][1]["input"]
        self.assertEqual(payload, {"creatorUserId": "household", "collectionName": "Previously acquired",
                                   "entities": [{"entityId": "met_1", "entityLot": "METADATA"}]})
        self.assertFalse(RyotAcquisitionWriter(self.reader(verified=False)).record_acquisition(MediaIdentity("ryot", "met_1")))

    def test_missing_native_or_foreign_collection_is_rejected_before_mutation(self):
        for collections in ([], [{"name": "Previously acquired", "isDefault": True, "creator": {"id": "household"}}],
                            [{"name": "Previously acquired", "isDefault": False, "creator": {"id": "other"}}]):
            writer = RyotAcquisitionWriter(self.reader(collections=collections))
            with self.assertRaises(RyotError):
                writer.record_acquisition(MediaIdentity("ryot", "met_1"))
            self.assertEqual(len(self.requests), 1)

    def test_rejected_job_and_non_ryot_identity_fail(self):
        writer = RyotAcquisitionWriter(self.reader(accepted=False))
        with self.assertRaises(RyotError):
            writer.record_acquisition(MediaIdentity("ryot", "met_1"))
        with self.assertRaises(RyotError):
            writer.record_acquisition(MediaIdentity("tmdb", "1"))
