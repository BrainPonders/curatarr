"""Availability is current evidence, never a substitute for history or intent."""

import unittest

from curatarr.services.display import availability, library_status


class AvailabilityTests(unittest.TestCase):
    def test_ready_copy_is_ready_to_watch(self):
        self.assertEqual(availability({"status": "ready"}), {"code": "ready", "label": "Ready to watch"})

    def test_unknown_does_not_become_negative_or_fake_preparation(self):
        for playback in ("unavailable", "needs_review"):
            self.assertEqual(availability({"status": playback}), {"code": "unknown", "label": "Check unavailable"})

    def test_successful_absence_is_not_available(self):
        self.assertEqual(availability({"status": "not_present"}), {"code": "not_available", "label": "Not available"})


class LibraryStatusTests(unittest.TestCase):
    def summary(self, *, history="no_completed_watch", states=(), playback="not_present", execution=None, deleted=False):
        return library_status({"status": history, "states": list(states)}, {"status": playback},
                              execution or {"status": "not_present"}, deletion_confirmed=deleted)

    def test_available_wanted_recorded_and_unknown_are_distinct(self):
        self.assertEqual(self.summary(playback="ready")["label"], "Available")
        for state in ("active_missing_monitored", "active_missing_unmonitored", "active_downloaded_unmonitored"):
            self.assertEqual(self.summary(execution={"status": "matched", "state": state})["label"], "Wanted")
        self.assertEqual(self.summary()["label"], "Recorded")
        self.assertEqual(self.summary(history="unknown")["label"], "Unknown")
        self.assertEqual(self.summary(states=["radarr"])["label"], "Wanted")

    def test_archived_intent_survives_drift_and_keeps_conflicts_visible(self):
        result = self.summary(states=["archived", "owned"], playback="ready", execution={"status": "matched"})
        self.assertEqual(result["label"], "Archived")
        self.assertIn("Removal needs review", result["note"])
        self.assertFalse(result["deleted"])

    def test_failed_reads_are_not_unknown_or_negative_intent(self):
        self.assertEqual(self.summary(history="unavailable", playback="ready")["label"], "Status unavailable")
        self.assertEqual(self.summary(playback="unavailable")["label"], "Status unavailable")
        self.assertEqual(self.summary(execution={"status": "unavailable"})["label"], "Status unavailable")
        self.assertEqual(self.summary(history="needs_review")["label"], "Identity needs review")

    def test_deleted_requires_explicit_evidence_and_confirmed_current_absence(self):
        for states in (["owned"], ["archived"], ["owned", "archived"]):
            self.assertFalse(self.summary(states=states)["deleted"])
        self.assertTrue(self.summary(states=["archived"], deleted=True)["deleted"])
        self.assertTrue(self.summary(states=["radarr"], deleted=True)["deleted"])
        for result in (self.summary(states=["archived"], deleted=True, playback="ready"),
                       self.summary(states=["archived"], deleted=True, playback="unavailable"),
                       self.summary(states=["archived"], deleted=True, execution={"status": "unavailable"}),
                       self.summary(states=["archived"], deleted=True, execution={"status": "matched", "file_present": True})):
            self.assertFalse(result["deleted"])

    def test_ryot_deleted_membership_is_durable_evidence_without_local_audit(self):
        result = self.summary(states=["library", "archived", "deleted"])
        self.assertTrue(result["deleted"])
        self.assertEqual(result["note"], "")
        for playback, execution in (("ready", {"status": "not_present"}),
                                    ("unavailable", {"status": "not_present"}),
                                    ("not_present", {"status": "unavailable"}),
                                    ("not_present", {"status": "matched"}),
                                    ("not_present", {"status": "matched", "file_present": True})):
            with self.subTest(playback=playback, execution=execution):
                self.assertFalse(self.summary(states=["library", "archived", "deleted"],
                                              playback=playback, execution=execution)["deleted"])
        self.assertFalse(self.summary(history="needs_review", states=["deleted"])["deleted"])
        self.assertFalse(self.summary(history="unavailable", states=["deleted"])["deleted"])

    def test_local_evidence_and_library_membership_drift_are_explicit(self):
        self.assertIn("not yet recorded in Ryot", self.summary(states=["library", "archived"], deleted=True)["note"])
        self.assertIn("not in Library", self.summary(states=["previously_acquired"])["note"])
        self.assertNotIn("not in the household Library", self.summary(states=["library"])["note"])
        self.assertFalse(self.summary(states=["library", "previously_acquired"])["deleted"])
