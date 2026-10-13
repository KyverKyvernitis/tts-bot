from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cd_metadata_test_target", ROOT / "deploy/ttv-cd/cd_metadata.py")
metadata = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metadata)


def candidate(video_id, title, *, channel="wowaka", views=123, duration=197):
    return {"video_id": video_id, "title": title, "channel": channel,
            "view_count": views, "duration": duration}


class CdMetadataTests(unittest.TestCase):
    def test_original_alias_and_japanese_survive_matching(self):
        selected = metadata.search_metadata("Rolling Girl", "wowaka", ["Rollin Girl", "ローリンガール"],
            candidates=[candidate("vnw8zURAxkU", "wowaka 『ローリンガール』feat. 初音ミク / Rollin Girl (Official Video)",
                                  channel="ヒトリエ / wowaka", views=44_107_888)])
        self.assertEqual(selected["selected"]["video_id"], "vnw8zURAxkU")
        self.assertIn("ローリンガール", metadata.unicode_key("『ローリンガール』"))
        self.assertFalse(selected["audit"]["global_youtube_maximum_claimed"])

    def test_japanese_only_title_and_artist(self):
        selected = metadata.search_metadata("ローリンガール", "ヒトリエ", candidates=[
            candidate("vnw8zURAxkU", "『ローリンガール』feat. 初音ミク", channel="ヒトリエ / wowaka")])
        self.assertEqual(selected["audit"]["accepted_candidates"], 1)

    def test_highest_views_among_identified_recording_versions(self):
        candidates = [candidate("vnw8zURAxkU", "wowaka Rolling Girl Official Video", views=700),
                      candidate("22222222222", "wowaka Rolling Girl Official Audio", views=800),
                      candidate("33333333333", "Rolling Girl wowaka cover", views=100000),
                      candidate("44444444444", "Other Song wowaka", views=200000),
                      candidate("55555555555", "Rolling Girl", channel="unrelated artist", views=300000)]
        selected = metadata.search_metadata("Rolling Girl", "wowaka", candidates=candidates)
        self.assertEqual(selected["selected"]["video_id"], "22222222222")
        self.assertEqual(selected["audit"]["rejected_candidates"], 3)

    def test_artist_alias_is_explicit_evidence(self):
        selected = metadata.search_metadata("Rolling Girl", "wowaka", artist_aliases=["ヒトリエ"],
            candidates=[candidate("vnw8zURAxkU", "Rolling Girl", channel="ヒトリエ")])
        self.assertEqual(selected["audit"]["accepted_candidates"], 1)

    def test_missing_artist_is_ambiguous(self):
        with self.assertRaises(metadata.MetadataNoMatchError) as raised:
            metadata.search_metadata("Rolling Girl", "", candidates=[candidate("vnw8zURAxkU", "Rolling Girl")])
        self.assertIn("artist_unspecified", raised.exception.audit["candidates"][0]["rejection_reasons"])

    def test_japanese_covers_and_english_covered_rejected(self):
        for suffix in ("歌ってみた", "Covered by Example", "カバー", "ライブ", "piano version"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(metadata.MetadataNoMatchError):
                    metadata.search_metadata("Rolling Girl", "wowaka", candidates=[
                        candidate("vnw8zURAxkU", "wowaka Rolling Girl " + suffix)])

    def test_requested_variant_allowed(self):
        selected = metadata.search_metadata("Rolling Girl", "wowaka", allow_versions=["cover"],
            candidates=[candidate("vnw8zURAxkU", "wowaka Rolling Girl cover")])
        self.assertEqual(selected["audit"]["accepted_candidates"], 1)

    def test_live_tribute_previously_selected_for_rolling_girl_is_rejected(self):
        selected = metadata.search_metadata("Rolling Girl", "wowaka", aliases=["Rollin Girl", "ローリンガール"],
            candidates=[candidate("xPhPHO-sKlY", "[Rolling girl ローリンガール] Wowaka Tribute - Hatsune Miku Magical Mirai 2019", views=142072),
                        candidate("vnw8zURAxkU", "wowaka - Rollin Girl (Official Video)", views=44_107_888)])
        rejected = selected["audit"]["candidates"][0]
        self.assertIn("unrequested_version_live", rejected["rejection_reasons"])
        self.assertIn("unrequested_version_tribute", rejected["rejection_reasons"])
        self.assertEqual(selected["selected"]["video_id"], "vnw8zURAxkU")

    def test_lyrics_is_presentation_and_player_accent_normalization_reused(self):
        selected = metadata.search_metadata("Canção", "Artísta", candidates=[
            candidate("vnw8zURAxkU", "Artista Cancao [Lyrics]")])
        self.assertEqual(selected["audit"]["accepted_candidates"], 1)

    def test_unknown_and_boolean_views_cannot_win(self):
        rows = [candidate("vnw8zURAxkU", "wowaka Rolling Girl", views=None),
                candidate("22222222222", "wowaka Rolling Girl", views=True)]
        with self.assertRaises(metadata.MetadataNoMatchError) as raised:
            metadata.search_metadata("Rolling Girl", "wowaka", candidates=rows)
        self.assertEqual(raised.exception.audit["inspected_candidates"], 2)

    def test_queries_cover_aliases_and_deduplicate_same_video(self):
        calls = []
        def backend(query, **kwargs):
            calls.append((query, kwargs))
            return [candidate("vnw8zURAxkU", "wowaka ローリンガール", views=700)]
        selected = metadata.search_metadata("Rolling Girl", "wowaka", aliases=["ローリンガール"],
                                            limit=25, search_backend=backend)
        self.assertEqual(len(calls), 2)
        self.assertEqual(selected["audit"]["inspected_candidates"], 1)
        self.assertEqual(selected["audit"]["queries"][0]["limit"], 25)

    def test_provider_errors_do_not_include_credentials(self):
        def backend(query, **kwargs):
            raise RuntimeError("https://provider.invalid?key=SUPER_SECRET")
        with self.assertRaises(metadata.MetadataSearchError) as raised:
            metadata.search_metadata("Rolling Girl", "wowaka", search_backend=backend)
        self.assertNotIn("SUPER_SECRET", json.dumps(raised.exception.audit))
        self.assertEqual(raised.exception.audit["queries"][0]["error"], "RuntimeError")

    def test_api_reuses_player_and_fetches_statistics_at_requested_limit(self):
        calls = []
        class Response:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return json.dumps(self.body).encode()
        def fetch(request, timeout):
            parsed = urlparse(request.full_url)
            params = parse_qs(parsed.query)
            calls.append((parsed.path, params))
            if parsed.path.endswith("search"):
                return Response({"items": [{"id": {"videoId": "vnw8zURAxkU"}, "snippet": {
                    "title": "wowaka Rolling Girl", "channelTitle": "wowaka", "thumbnails": {}}}]})
            return Response({"items": [{"id": "vnw8zURAxkU", "statistics": {"viewCount": "800"},
                                       "contentDetails": {"duration": "PT3M17S"}, "status": {}}]})
        with patch.object(metadata, "urlopen", fetch):
            result = metadata.search_metadata("Rolling Girl", "wowaka", youtube_api_key="SUPER_SECRET", limit=25)
        self.assertEqual(result["selected"]["view_count"], 800)
        self.assertEqual(calls[0][1]["maxResults"], ["25"])
        self.assertEqual(calls[0][1]["order"], ["viewCount"])
        self.assertIn("statistics", calls[1][1]["part"][0])
        self.assertNotIn("SUPER_SECRET", json.dumps(result))
        self.assertFalse(any(name.startswith("cogs.musica.configuracao") for name in sys.modules))

    def test_standalone_bundle_loads_only_copied_sources(self):
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            shutil.copyfile(ROOT / "deploy/ttv-cd/cd_metadata.py", target / "cd_metadata.py")
            for name in metadata.PLAYER_SOURCES:
                path = target / "player_metadata" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                source = ROOT / "cogs/musica"
                if not (source / name).is_file():
                    source = ROOT / "deploy/ttv-cd/player_metadata"
                shutil.copyfile(source / name, path)
            spec = importlib.util.spec_from_file_location("isolated_cd_metadata", target / "cd_metadata.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            result = module.search_metadata("Rolling Girl", "wowaka", candidates=[
                candidate("vnw8zURAxkU", "wowaka Rolling Girl")])
            self.assertEqual(result["selected"]["video_id"], "vnw8zURAxkU")


if __name__ == "__main__":
    unittest.main()
