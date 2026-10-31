import os
import unittest
from uuid import uuid4

from test_usage import WORK, panel, event, line, usage
from test_speed import started, generated, settled
from session_cache import SessionReadCache


class SessionCacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = WORK / "cache" / uuid4().hex[:8]
        self.folder.mkdir(parents=True)
        self.path = self.folder / "session.jsonl"
        self.path.write_bytes(line(event(usage(), usage())))
        self.cache = SessionReadCache()
        self.cache.activate(1)

    def checkpoint(self):
        reader, reused = self.cache.acquire(self.path, 1, panel.LogTailer)
        self.assertFalse(reused)
        reader.poll()
        self.cache.store(self.path, 1, reader)
        self.cache.activate(2)
        return reader

    def test_switch_back_resumes_without_reparsing_old_records(self):
        first = self.checkpoint()
        with self.path.open("ab") as stream:
            stream.write(line(event(usage(4000, 3600, 400), usage(3000, 3000, 300))))
        reader, reused = self.cache.acquire(self.path, 2, panel.LogTailer)
        self.assertTrue(reused)
        self.assertEqual(reader.offset, first.offset)
        records = []
        accept = reader.accept
        reader.accept = lambda record: (records.append(record), accept(record))
        snapshot = reader.poll()
        self.assertEqual(len(records), 1)
        self.assertEqual(snapshot.total.total_tokens, 4400)
        self.assertEqual(snapshot.total.cache_rate, 90)
        self.assertEqual(first.snapshot.total.total_tokens, 1100)

    def test_partial_record_and_speed_window_survive_switch(self):
        complete = line(settled(output=1000))
        self.path.write_bytes(line(started()) + line(generated()) + complete[:35])
        first = self.checkpoint()
        self.assertTrue(first.pending)
        with self.path.open("ab") as stream:
            stream.write(complete[35:])
        reader, reused = self.cache.acquire(self.path, 2, panel.LogTailer)
        self.assertTrue(reused)
        self.assertEqual(reader.poll().speed.tokens_per_second, 250)

    def test_truncation_invalidates(self):
        self.checkpoint()
        self.path.write_bytes(b"{}\n")
        reader, reused = self.cache.acquire(self.path, 2, panel.LogTailer)
        self.assertFalse(reused)
        self.assertIsNone(reader.poll().total.total_tokens)

    def test_replacement_with_same_size_invalidates(self):
        self.checkpoint()
        replacement = self.folder / "new.jsonl"
        replacement.write_bytes(self.path.read_bytes())
        replacement.replace(self.path)
        self.assertFalse(self.cache.acquire(self.path, 2, panel.LogTailer)[1])

    def test_rewrite_with_growth_is_rejected_by_anchors(self):
        self.checkpoint()
        self.path.write_bytes(line(event(usage(9000), usage(9000))) + b"{}\n"*50)
        reader, reused = self.cache.acquire(self.path, 2, panel.LogTailer)
        self.assertFalse(reused)
        self.assertEqual(reader.poll().total.total_tokens, 9100)

    def test_same_size_modification_is_not_mistaken_for_append(self):
        self.checkpoint()
        stat = self.path.stat()
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        self.assertFalse(self.cache.acquire(self.path, 2, panel.LogTailer)[1])

    def test_failed_read_does_not_reuse_old_totals(self):
        self.checkpoint()
        missing = self.folder / "moved.jsonl"
        self.path.rename(missing)
        reader, reused = self.cache.acquire(self.path, 2, panel.LogTailer)
        self.assertFalse(reused)
        self.assertTrue(reader.poll().error)
        self.assertIsNone(reader.snapshot.total.total_tokens)

    def test_late_worker_cannot_overwrite_new_checkpoint(self):
        reader = self.checkpoint()
        reader.snapshot.total = panel.Usage.parse(usage(9999))
        self.cache.store(self.path, 1, reader)
        restored, reused = self.cache.acquire(self.path, 2, panel.LogTailer)
        self.assertTrue(reused)
        self.assertEqual(restored.snapshot.total.total_tokens, 1100)

    def test_lru_and_large_pending_are_bounded(self):
        for index in range(self.cache.LIMIT + 2):
            path = self.folder / f"{index}.jsonl"
            path.write_bytes(line(event(usage())))
            reader = panel.LogTailer(path)
            reader.poll()
            self.cache.store(path, 1, reader)
        self.assertEqual(self.cache.summary()["entries"], self.cache.LIMIT)
        self.assertNotIn(self.cache.key(self.folder / "0.jsonl"), self.cache.entries)
        reader = panel.LogTailer(self.path)
        reader.poll()
        reader.pending = b"x" * (self.cache.MAX_PENDING + 1)
        self.cache.store(self.path, 1, reader)
        self.assertNotIn(self.cache.key(self.path), self.cache.entries)

    def test_explicit_reload_discards_checkpoint(self):
        self.checkpoint()
        self.cache.invalidate(self.path)
        reader, reused = self.cache.acquire(self.path, 2, panel.LogTailer)
        self.assertFalse(reused)
        self.assertEqual(reader.offset, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

