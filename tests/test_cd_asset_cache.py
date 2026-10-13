"""Real bytes, hashes and file replacements exercise bounded cache invalidation."""
from pathlib import Path
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from cogs.tts.cd_assets import CheckedFileCache


class AssetCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'asset'
        self.data = b'verified bytes'
        self.path.write_bytes(self.data)
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.cache = CheckedFileCache(max_bytes=100, max_entries=4)

    def test_warm_read_and_revision_do_not_reread_or_validate(self):
        calls = []
        def validate(data): calls.append(data); return True
        self.assertEqual(self.cache.read(self.path, self.digest, 100, 'test', validate), self.data)
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('warm hit must not read')):
            self.assertEqual(self.cache.read(self.path, self.digest, 100, 'test', validate), self.data)
            self.assertEqual(self.cache.revision(self.path, self.digest, 100, 'test', validate), self.digest)
        self.assertEqual(len(calls), 1)

    def test_rewrite_invalidates_cached_hash_even_if_size_and_mtime_match(self):
        self.cache.read(self.path, self.digest, 100, 'test', lambda _: True)
        previous = self.path.stat()
        self.path.write_bytes(b'bad bytes here')
        import os
        os.utime(self.path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        self.assertIsNone(self.cache.read(self.path, self.digest, 100, 'test', lambda _: True))
        self.assertEqual(self.cache.revision(self.path, self.digest, 100, 'test', lambda _: True), '')

    def test_symlink_and_missing_asset_never_reuse_old_bytes(self):
        self.cache.read(self.path, self.digest, 100, 'test', lambda _: True)
        self.path.unlink()
        target = self.root / 'outside'
        target.write_bytes(self.data)
        self.path.symlink_to(target)
        self.assertIsNone(self.cache.read(self.path, self.digest, 100, 'test', lambda _: True))
        self.path.unlink()
        self.assertIsNone(self.cache.read(self.path, self.digest, 100, 'test', lambda _: True))

    def test_revision_remains_cheap_when_byte_budget_evicted_an_asset(self):
        cache = CheckedFileCache(max_bytes=4, max_entries=4)
        files = []
        for number in range(3):
            path = self.root / str(number)
            data = str(number).encode() * 3
            path.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            cache.read(path, digest, 10, 'test', lambda _: True)
            files.append((path, digest))
        self.assertLessEqual(cache._size, 4)
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('approval must survive byte eviction')):
            self.assertEqual(cache.revision(*files[0], 10, 'test', lambda _: True), files[0][1])

    def test_replacement_during_read_is_not_approved(self):
        def racing_read(path):
            data = self.data
            path.write_bytes(b'replaced')
            return data
        with patch.object(Path, 'read_bytes', racing_read):
            self.assertIsNone(self.cache.read(self.path, self.digest, 100, 'test', lambda _: True))

    def test_transient_read_error_does_not_poison_unchanged_file(self):
        with patch.object(Path, 'read_bytes', side_effect=OSError('temporary read failure')):
            self.assertIsNone(self.cache.read(self.path, self.digest, 100, 'test', lambda _: True))
        self.assertEqual(self.cache.read(self.path, self.digest, 100, 'test', lambda _: True), self.data)
        self.assertEqual(self.cache.revision(self.path, self.digest, 100, 'test', lambda _: True), self.digest)

    def test_entry_eviction_is_bounded_and_old_valid_cover_can_return(self):
        cache=CheckedFileCache(max_bytes=100,max_entries=1)
        other=self.root/'other'
        other.write_bytes(b'other valid data')
        cache.read(self.path,self.digest,100,'test',lambda _:True)
        cache.read(other,hashlib.sha256(other.read_bytes()).hexdigest(),100,'test',lambda _:True)
        self.assertEqual(len(cache._checked),1)
        self.assertEqual(len(cache._bytes),1)
        self.assertEqual(cache.revision(self.path,self.digest,100,'test',lambda _:True),self.digest)


if __name__ == '__main__': unittest.main()
