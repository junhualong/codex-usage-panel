"""Bounded in-memory checkpoints for append-only session logs."""
from collections import OrderedDict
from copy import deepcopy
import os
from pathlib import Path
import threading


class SessionReadCache:
    LIMIT = 6
    MAX_PENDING = 256 * 1024

    def __init__(self):
        self.entries = OrderedDict()
        self.lock = threading.Lock()
        self.generation = 0
        self.hits = self.misses = 0

    @staticmethod
    def key(path):
        return os.path.normcase(str(Path(path).absolute()))

    def activate(self, generation):
        with self.lock:
            self.generation = generation

    def acquire(self, path, generation, factory):
        key = self.key(path)
        with self.lock:
            cached = self.entries.get(key)
            reader = deepcopy(cached) if cached is not None else None
        # Validate on the reader thread, never perform log I/O in Tk callbacks.
        if reader is not None and reader.can_resume():
            with self.lock:
                if generation == self.generation:
                    self.hits += 1
                    if key in self.entries:
                        self.entries.move_to_end(key)
            return reader, True
        with self.lock:
            if generation == self.generation:
                self.entries.pop(key, None)
                self.misses += 1
        return factory(path), False

    def store(self, path, generation, reader):
        if reader.snapshot.error or reader.snapshot.loading or len(reader.pending) > self.MAX_PENDING:
            return
        with self.lock:
            # A reader finishing after a chat switch must not overwrite a newer checkpoint.
            if generation != self.generation:
                return
            key = self.key(path)
            self.entries[key] = deepcopy(reader)
            self.entries.move_to_end(key)
            while len(self.entries) > self.LIMIT:
                self.entries.popitem(last=False)

    def invalidate(self, path):
        with self.lock:
            self.entries.pop(self.key(path), None)

    def summary(self):
        with self.lock:
            return {"entries": len(self.entries), "limit": self.LIMIT, "hits": self.hits, "misses": self.misses}

