import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "runtime_api" / "frame_store.py"
_SPEC = importlib.util.spec_from_file_location("frame_store_under_test", _MODULE_PATH)
_FRAME_STORE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _FRAME_STORE
_SPEC.loader.exec_module(_FRAME_STORE)

FrameIntegrityError = _FRAME_STORE.FrameIntegrityError
FrameNotFound = _FRAME_STORE.FrameNotFound
FrameStore = _FRAME_STORE.FrameStore
FrameStoreError = _FRAME_STORE.FrameStoreError
FrameTooLarge = _FRAME_STORE.FrameTooLarge


class FrameStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = FrameStore(
            root=self.temp_dir.name,
            max_frame_bytes=16,
            max_store_bytes=24,
            ttl_seconds=60,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_round_trip_and_delete(self):
        data = b"abcdefghijk"
        stored = self.store.put(
            "frame-1",
            [data[:4], data[4:]],
            expected_size=len(data),
            expected_sha256=hashlib.sha256(data).hexdigest(),
        )

        self.assertEqual(stored.size_bytes, len(data))
        self.assertEqual(b"".join(self.store.iter_chunks("frame-1", 3)), data)
        self.assertTrue(self.store.delete("frame-1"))
        with self.assertRaises(FrameNotFound):
            self.store.get("frame-1")

    def test_rejects_oversized_or_corrupt_frames(self):
        with self.assertRaises(FrameTooLarge):
            self.store.put("large", [b"x" * 17])

        with self.assertRaises(FrameIntegrityError):
            self.store.put("corrupt", [b"data"], expected_sha256="0" * 64)

        self.assertEqual(list(Path(self.temp_dir.name).iterdir()), [])

    def test_rejects_new_frame_when_store_is_full(self):
        self.store.put("first", [b"a" * 16])

        with self.assertRaises(FrameTooLarge):
            self.store.put("second", [b"b" * 16])

        self.assertEqual(self.store.get("first").size_bytes, 16)

    def test_rejects_duplicate_or_empty_frame(self):
        original = self.store.put("duplicate", [b"data"])
        retried = self.store.put("duplicate", [b"data"])
        self.assertEqual(retried, original)
        with self.assertRaises(FrameStoreError):
            self.store.put("duplicate", [b"new"])
        with self.assertRaises(FrameIntegrityError):
            self.store.put("empty", [])

    def test_rejects_unsafe_frame_id(self):
        with self.assertRaises(FrameStoreError):
            self.store.put("../escape", [b"data"])


if __name__ == "__main__":
    unittest.main()
