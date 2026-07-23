import importlib.util
import sys
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "runtime_api" / "nats_subjects.py"
_SPEC = importlib.util.spec_from_file_location("nats_subjects_under_test", _MODULE_PATH)
_SUBJECTS = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _SUBJECTS
_SPEC.loader.exec_module(_SUBJECTS)

merge_subject_patterns = _SUBJECTS.merge_subject_patterns
subject_pattern_covers = _SUBJECTS.subject_pattern_covers


class NatsSubjectsTest(unittest.TestCase):
    def test_broad_tail_wildcard_covers_narrow_pattern(self):
        self.assertTrue(
            subject_pattern_covers("workflow.>", "workflow.*.*.*")
        )
        self.assertFalse(
            subject_pattern_covers("workflow.*.*.*", "workflow.>")
        )

    def test_merge_replaces_covered_subject(self):
        self.assertEqual(
            merge_subject_patterns(["workflow.*.*.*"], ["workflow.>"]),
            ["workflow.>"],
        )

    def test_merge_keeps_unrelated_subjects(self):
        self.assertEqual(
            merge_subject_patterns(["workflow.>"], ["events.>"]),
            ["workflow.>", "events.>"],
        )


if __name__ == "__main__":
    unittest.main()
