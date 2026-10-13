"""Execute the real archive index against temporary SQLite, without SDKs."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "cogs/musica/busca/arquivo.py"


def _index_module():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    # Only the persistence-path/payload import would pull in the player SDK;
    # all archive queries, schema, transitions and readback run unmodified.
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.ImportFrom) and node.module == "memoria"
        and any(alias.name == "_db_path" for alias in node.names)
    )]
    module = ModuleType("offline_music_archive_index")
    module.__file__ = str(SOURCE)
    exec(compile(tree, str(SOURCE), "exec"), module.__dict__)
    return module


class ArchiveRetryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.archive = _index_module()
        self.archive._db_path = lambda: Path(temp.name) / "choices.sqlite3"
        self.clock = [1000.0]
        timer = patch.object(self.archive.time, "time", lambda: self.clock[0])
        timer.start()
        self.addCleanup(timer.stop)
        self.archive.set_channel(1, 2, kind="forum")
        self.reference = {"guild_id": 1, "forum_id": 2, "channel_id": 3,
                          "message_id": 4, "attachment_id": 5}

    def seed(self, video_id="abcdefghijk", *, presentation=7, archived=True):
        track = SimpleNamespace(webpage_url=f"https://www.youtube.com/watch?v={video_id}",
                                original_url="", is_live=False, attachment_ref={}, archive_ref={})
        key = self.archive.media_key(track)
        payload = {"title": "Fixture", "webpage_url": track.webpage_url, "duration": 190}
        encoded = json.dumps(self.reference) if archived else ""
        with self.archive._db() as db:
            db.execute("""INSERT INTO arquivo_musicas
                (chave,track_json,reference_json,aprendida_em,apresentacao,enfileirada_em)
                VALUES (?,?,?,?,?,?)""", (key, json.dumps(payload), encoded, 1, presentation, self.clock[0]))
        return key, track

    def test_permanent_old_post_failure_stops_retry_and_keeps_playable_reference(self):
        terminals = (("unavailable", "metadata_mismatch"), ("unavailable", "no_match"),
                     ("unavailable", "source_mismatch"), ("unavailable", "source_unavailable"),
                     ("too_large", "size_limit"), ("ineligible", "duration_limit"))
        for index, (status, reason) in enumerate(terminals):
            with self.subTest(status=status, reason=reason):
                key, track = self.seed(f"terminal{index:03}")
                self.assertEqual(self.archive.pending()["key"], key)
                self.archive.mark_attempt(key)
                self.archive.mark_result(key, {"status": status, "reason": reason})
                for _ in range(24):
                    self.clock[0] += 5
                    self.assertIsNone(self.archive.pending())
                self.assertEqual(self.archive.archived(track)[0], self.reference)
                with self.archive._db() as db:
                    attempts = db.execute("SELECT falhas FROM arquivo_musicas WHERE chave=?", (key,)).fetchone()[0]
                self.assertEqual(attempts, 1)

    def test_transient_old_post_failure_obeys_backoff_and_preserves_reference(self):
        key, track = self.seed()
        self.archive.mark_attempt(key)
        self.archive.mark_result(key, {"status": "failed", "reason": "download_timeout"})
        self.assertIsNone(self.archive.pending())
        self.assertEqual(self.archive.archived(track)[0], self.reference)
        self.clock[0] += 29
        self.assertIsNone(self.archive.pending())
        self.clock[0] += 1
        pending = self.archive.pending()
        self.assertEqual(pending["key"], key)
        self.assertTrue(pending["retry"])
        self.assertEqual(pending["reference"], self.reference)

    def test_healthy_old_presentation_still_migrates(self):
        key, track = self.seed()
        with self.archive._db() as db:
            db.execute("UPDATE arquivo_musicas SET estado='done' WHERE chave=?", (key,))
        pending = self.archive.pending(prefer_new=False)
        self.assertEqual(pending["key"], key)
        self.assertEqual(pending["reference"], self.reference)
        self.assertEqual(self.archive.archived(track)[0], self.reference)

    def test_current_presentation_does_not_remigrate_and_new_jobs_still_queue(self):
        self.seed(presentation=8)
        self.assertIsNone(self.archive.pending())
        key, _track = self.seed("newnewnew01", archived=False)
        self.assertEqual(self.archive.pending()["key"], key)
        self.archive.mark_result(key, {"status": "unavailable", "reason": "metadata_mismatch"})
        self.assertIsNone(self.archive.pending())

    def test_paused_old_post_cannot_starve_next_healthy_migration(self):
        key, _track = self.seed()
        with self.archive._db() as db:
            db.execute("UPDATE arquivo_musicas SET estado='paused' WHERE chave=?", (key,))
        next_key, _track = self.seed("nextnext001")
        self.assertEqual(self.archive.pending(prefer_new=False)["key"], next_key)


if __name__ == "__main__":
    unittest.main()
