"""Negative checks for the planning gate: invalid records must block handover."""
import importlib.util
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('project_records', ROOT/'maintainer/test/check_project_records.py')
records = importlib.util.module_from_spec(spec)
spec.loader.exec_module(records)


class ProjectRecordsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        names = ['maintainer/FEATURES.md', 'maintainer/ROADMAP.md', 'maintainer/WORKFLOW.md',
                 'documentation/Decisions.md', 'documentation/System Architecture.md']
        for name in names:
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text((ROOT/name).read_text())
        for name in names:
            for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', (self.root/name).read_text()):
                if '://' in target or target.startswith('#'):
                    continue
                path = (self.root/name).parent/unquote(target.split('#')[0])
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    original = ROOT/path.relative_to(self.root)
                    path.write_text(original.read_text() if path.suffix == '.md' and original.is_file() else '')
        (self.root/'tests/fixtures').mkdir(parents=True, exist_ok=True)
        (self.root/'tests/fixtures/poster.png').touch()

    def change(self, before, after):
        path = self.root/'maintainer/FEATURES.md'
        path.write_text(path.read_text().replace(before, after, 1))

    def test_valid_records(self):
        self.assertEqual(records.check(self.root), [])

    def test_unknown_requirement_is_rejected(self):
        self.change('AC03, AC17', 'AC99, AC17')
        self.assertTrue(any('unknown reference AC99' in error for error in records.check(self.root)))

    def test_duplicate_feature_is_rejected(self):
        self.change('F02 — Public', 'F01 — Public')
        self.assertTrue(any('duplicate F IDs' in error for error in records.check(self.root)))

    def test_ambiguous_done_state_is_rejected(self):
        self.change('| Implemented |', '| Done |')
        self.assertTrue(any('invalid state Done' in error for error in records.check(self.root)))

    def test_broken_coverage_link_is_rejected(self):
        self.change('../tests/miniapp_browser_check.py', '../tests/missing_browser_check.py')
        self.assertTrue(any('missing link' in error for error in records.check(self.root)))

    def test_missing_heading_is_rejected(self):
        self.change('#sources-discovery-and-awaiting-metadata', '#nonexistent-section')
        self.assertTrue(any('missing anchor' in error for error in records.check(self.root)))

    def test_private_record_link_is_rejected(self):
        path = self.root/'.local/private.md'
        path.parent.mkdir()
        path.touch()
        self.change('../tests/miniapp_browser_check.py', '../.local/private.md')
        self.assertTrue(any('private link' in error for error in records.check(self.root)))

    def test_link_outside_repository_is_rejected(self):
        self.change('../tests/miniapp_browser_check.py', '../../outside.md')
        self.assertTrue(any('link outside repository' in error for error in records.check(self.root)))

    def test_private_browser_prerequisite_is_rejected(self):
        (self.root/'tests/example_browser_check.py').write_text("poster = '.local/private.png'\n")
        self.assertTrue(any('private browser dependency' in error for error in records.check(self.root)))


if __name__ == '__main__':
    unittest.main()
