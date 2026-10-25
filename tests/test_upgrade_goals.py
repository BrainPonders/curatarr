"""Compact goals preserve native evidence and do not reproduce Arr eligibility."""

import unittest

from curatarr.services.upgrades import upgrade_goals


class UpgradeGoalTests(unittest.TestCase):
    def setUp(self):
        self.profile = {"cutoff_quality": "4K group", "cutoff_qualities": [
            {"resolution": 2160, "modifier": "none"}, {"resolution": 2160, "modifier": "remux"}],
            "language": {"id": 1, "name": "English"},
            "format_items": [{"id": 50, "score": 100}, {"id": 51, "score": 500}, {"id": 52, "score": -10000}]}
        self.file = {"quality": {"resolution": 2160, "modifier": "none"}, "language_ids": [1],
                     "format_ids": [51], "quality_cutoff_not_met": False}
        self.formats = [{"id": 50, "key": "remux", "label": "Remux", "category": "remux"},
                        {"id": 51, "key": "language:2", "label": "French", "category": "language"},
                        {"id": 52, "key": "bad", "label": "Excluded", "category": "language"}]

    def goals(self, **kwargs):
        return upgrade_goals(self.profile, self.file, self.formats, has_file=True, **kwargs)

    def test_quality_remux_language_and_bonuses_use_separate_evidence(self):
        goals = self.goals()
        self.assertEqual([g["label"] for g in goals], ["4K", "Remux", "English", "French"])
        self.assertEqual([g["status"] for g in goals], ["met", "not_matched", "met", "met"])
        self.assertEqual([g["bonus"] for g in goals], [False, True, False, True])
        # French format match is not independently asserted as an audio track.
        self.assertIn("custom format", goals[-1]["evidence"])

    def test_strict_remux_target_is_not_downgraded_to_a_bonus_or_duplicated(self):
        self.profile["cutoff_qualities"] = [{"resolution": 2160, "modifier": "remux"}]
        remux = next(g for g in self.goals() if g["key"] == "remux")
        self.assertFalse(remux["bonus"])
        self.assertEqual(remux["status"], "not_matched")

    def test_mixed_resolution_cutoff_does_not_become_a_4k_requirement(self):
        self.profile["cutoff_qualities"].append({"resolution": 1080, "modifier": "none"})
        self.assertEqual(self.goals()[0]["label"], "Quality target")

    def test_missing_file_read_is_unknown_but_confirmed_absence_is_unmatched(self):
        unknown = upgrade_goals(self.profile, None, self.formats, has_file=True)
        absent = upgrade_goals(self.profile, None, self.formats, has_file=False)
        self.assertTrue(all(g["status"] == "unknown" for g in unknown))
        self.assertTrue(all(g["status"] == "not_matched" for g in absent))

    def test_original_audio_and_incomplete_languages_remain_unknown(self):
        self.profile["language"] = {"id": -2, "name": "Original"}
        self.assertEqual(next(g for g in self.goals() if g["key"] == "language:-2")["status"], "unknown")
        self.assertEqual(next(g for g in self.goals(original_language_id=1) if g["key"] == "language:-2")["status"], "met")
        self.file["language_ids"] = [0, 1]
        self.assertEqual(next(g for g in self.goals(original_language_id=2) if g["key"] == "language:-2")["status"], "unknown")
        self.assertEqual(next(g for g in self.goals(original_language_id=1) if g["key"] == "language:-2")["status"], "met")

    def test_format_service_failure_and_missing_matches_are_not_absence(self):
        self.file["format_ids"] = None
        self.assertEqual(next(g for g in self.goals() if g["key"] == "remux")["status"], "unknown")
        goals = upgrade_goals(self.profile, self.file, None, has_file=True)
        self.assertEqual(goals[-1]["label"], "Format goals")
        self.assertEqual(goals[-1]["status"], "unknown")

    def test_zero_weight_formats_are_not_upgrade_goals(self):
        self.profile["format_items"] = [{"id": 50, "score": 0}]
        self.assertNotIn("Remux", [g["label"] for g in self.goals()])
