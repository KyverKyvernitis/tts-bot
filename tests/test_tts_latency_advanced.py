"""Local regressions for DSP, short streaming and startup latency settings."""
from __future__ import annotations

import asyncio
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cogs.tts import audio
from cogs.tts.utils.ffmpeg import compose_audio_filters
from cogs.tts.prepared import PreparedOpusCache
from cogs.musica.runtime_telefone.agente.efeitos import filtros_tts


def make_item(**updates):
    fields = dict(guild_id=1, channel_id=2, author_id=3, text="Olá!",
                  engine="edge", voice="pt-BR-FranciscaNeural", language="pt",
                  rate="+0%", pitch="+0Hz")
    fields.update(updates)
    return audio.QueueItem(**fields)


class Probe(audio.TTSAudioMixin):
    def __init__(self):
        self.guild_states = {}
        self.edge_voice_names = {"pt-BR-FranciscaNeural"}
        self.bot = SimpleNamespace(audio_router=None, get_guild=lambda _: None)


class AdvancedLatencyTests(unittest.TestCase):
    def test_fallback_preserves_frozen_effects_without_reusing_edge_cache_key(self):
        probe = Probe()
        original = make_item(advanced_slowed_level=3, advanced_reverb_level=2,
                             received_at_monotonic=10.0)
        original_key = probe._cache_key(original)
        fallback = probe._build_edge_gtts_fallback_item(original)
        self.assertEqual((fallback.advanced_nightcore_level, fallback.advanced_slowed_level,
                          fallback.advanced_reverb_level), (0, 3, 2))
        self.assertEqual(fallback.received_at_monotonic, 10.0)
        self.assertEqual(probe._tts_effect_filter(original), probe._tts_effect_filter(fallback))
        self.assertNotEqual(probe._cache_key(fallback), original_key)
        self.assertEqual(probe._cache_key(fallback), probe._cache_key(make_item(engine="gtts")))

    def test_slowed_and_edge_rate_increase_timeout_but_faster_speech_keeps_margin(self):
        probe = Probe()
        plain = make_item(text="a" * 200)
        baseline = probe._estimate_playback_timeout(plain)
        slowed = make_item(text=plain.text, advanced_slowed_level=3, advanced_reverb_level=3)
        self.assertGreater(probe._estimate_playback_timeout(slowed), baseline)
        slowed.rate = "-50%"
        self.assertGreater(probe._estimate_playback_timeout(slowed), baseline * 1.5)
        fast = make_item(text=plain.text, rate="+50%", advanced_nightcore_level=3)
        self.assertEqual(probe._estimate_playback_timeout(fast), baseline)
        slowed._tts_actual_engine = "gtts"
        self.assertLess(probe._estimate_playback_timeout(slowed), baseline * 1.5)
        self.assertLessEqual(probe._estimate_playback_timeout(make_item(text="a" * 1600, rate="-100%")),
                             audio.TTS_PLAYBACK_TIMEOUT_MAX_SECONDS)

    def test_custom_filter_and_effects_share_one_graph_and_keep_quoted_expression(self):
        custom = '-vn -loglevel error -filter:a "volume=\'if(lt(t,1),0.5,1)\'"'
        effect = filtros_tts(engine="edge", nightcore_level=2, reverb_level=1)
        options, graph = compose_audio_filters(custom, effect)
        tokens = shlex.split(options)
        self.assertEqual(tokens.count("-af"), 1)
        self.assertNotIn("-filter:a", tokens)
        self.assertEqual(tokens[tokens.index("-af") + 1], graph)
        self.assertEqual(graph, "volume='if(lt(t,1),0.5,1)'," + effect)
        self.assertEqual(compose_audio_filters(custom, ""), (custom, "volume='if(lt(t,1),0.5,1)'"))

    def test_custom_audio_filter_prevents_opus_streamcopy_even_without_advanced(self):
        probe = Probe()
        with patch.object(audio, "TTS_FFMPEG_OPTIONS", "-vn -af volume=0.5"):
            _, graph = probe._tts_ffmpeg_options_for_item(make_item())
        self.assertEqual(graph, "volume=0.5")

    def test_short_gtts_can_overlap_and_explicit_old_threshold_still_works(self):
        probe = Probe()
        speech = make_item(engine="gtts")
        with patch.object(audio, "TTS_GTTS_STREAM_MIN_CHARS", 1), patch.object(audio, "TTS_GTTS_STREAMING_ENABLED", True):
            allowed, _ = probe._gtts_streaming_allowed_for(speech)
            self.assertTrue(allowed)
        with patch.object(audio, "TTS_GTTS_STREAM_MIN_CHARS", 101):
            self.assertEqual(probe._gtts_streaming_allowed_for(speech), (False, "gtts_single_request"))

    def test_stream_probe_is_configurable_and_preserves_explicit_ffmpeg_options(self):
        probe = Probe()
        probe._edge_stream_handle_for_path = lambda _: SimpleNamespace(engine="gtts")
        with patch.object(audio, "TTS_STREAM_FFMPEG_PROBESIZE_BYTES", 512), patch.object(audio, "TTS_FFMPEG_BEFORE_OPTIONS", "-nostdin"):
            args = shlex.split(probe._tts_ffmpeg_before_options("audio.fifo"))
            self.assertEqual(args[args.index("-probesize") + 1], "512")
        with patch.object(audio, "TTS_FFMPEG_BEFORE_OPTIONS", "-nostdin -f mp3 -probesize 4096 -analyzeduration 100"):
            args = shlex.split(probe._tts_ffmpeg_before_options("audio.fifo"))
            self.assertEqual(args.count("-probesize"), 1)
            self.assertEqual(args[args.index("-probesize") + 1], "4096")
            self.assertEqual(args[args.index("-analyzeduration") + 1], "100")

    def test_effect_variants_share_synthesis_cache_but_have_distinct_playback_graphs(self):
        probe = Probe()
        plain, effect = make_item(), make_item(advanced_nightcore_level=3, advanced_reverb_level=3)
        self.assertEqual(probe._cache_key(plain), probe._cache_key(effect))
        self.assertNotEqual(probe._tts_ffmpeg_options_for_item(plain), probe._tts_ffmpeg_options_for_item(effect))

    def test_cached_effect_variant_returns_independent_opus_cursors_without_ffmpeg(self):
        probe = Probe()
        with tempfile.TemporaryDirectory(prefix="tts-prepared-hit-") as directory:
            path = Path(directory) / "voice.mp3"
            path.write_bytes(b"synthesis cache")
            item = make_item(advanced_slowed_level=2, advanced_reverb_level=3)
            cache = probe._tts_prepared_opus = PreparedOpusCache()
            options, _ = probe._tts_ffmpeg_options_for_item(item)
            key = cache.key(path, options, before_options=probe._tts_ffmpeg_before_options(str(path)))
            cache.put(key, [b"first opus frame", b"second opus frame"])
            with patch.object(audio, "_CACHE_DIR", directory), patch.object(audio.config, "TTS_PREPARED_OPUS_CACHE_ENABLED", True):
                first, kind = probe._make_discord_tts_source(str(path), item=item)
                second, second_kind = probe._make_discord_tts_source(str(path), item=item)
            self.assertEqual((kind, second_kind), ("prepared_opus", "prepared_opus"))
            self.assertTrue(first.is_opus())
            self.assertEqual(first.read(), b"first opus frame")
            self.assertEqual(first.read(), b"second opus frame")
            self.assertEqual(second.read(), b"first opus frame")
            first.cleanup()
            self.assertEqual(second.read(), b"second opus frame")
            self.assertIsNone(cache.get(cache.key(path, "-vn", before_options="-nostdin")))
            cache.close()

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required")
    def test_all_28_effect_variants_decode_real_mp3_with_expected_duration(self):
        with tempfile.TemporaryDirectory(prefix="tts-effects-") as directory:
            path = Path(directory) / "voice.mp3"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=0.3", "-ar", "24000", "-ac", "1",
                            "-c:a", "libmp3lame", str(path)], check=True, capture_output=True, timeout=5)
            combinations = [(night, slow, reverb) for night in range(4) for slow in range(4)
                            if not (night and slow) for reverb in range(4)]
            self.assertEqual(len(combinations), 28)
            for night, slow, reverb in combinations:
                with self.subTest(night=night, slowed=slow, reverb=reverb):
                    graph = filtros_tts(engine="edge", nightcore_level=night,
                                        slowed_level=slow, reverb_level=reverb)
                    args = ["ffmpeg", "-v", "error", "-i", str(path)]
                    if graph:
                        args += ["-af", graph]
                    args += ["-ar", "48000", "-ac", "2", "-f", "s16le", "pipe:1"]
                    pcm = subprocess.run(args, check=True, capture_output=True, timeout=5).stdout
                    self.assertTrue(any(pcm))
                    speed = (1.0, 1.1, 1.2, 1.3)[night] if night else (1.0, .92, .84, .76)[slow]
                    expected_seconds = .3 / speed + (.25 if reverb else 0)
                    self.assertAlmostEqual(len(pcm) / 192000.0, expected_seconds, delta=.035)


class FallbackOverlapTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_arrival_timestamp_survives_chunk_expansion(self):
        from cogs.tts.mensagens.despacho import despachar_mensagem_tts
        from dataclasses import replace
        probe = Probe()
        captured = []

        async def build(*args, **kwargs):
            return SimpleNamespace(queue_item=make_item())

        async def enqueue(guild_id, items):
            captured.extend(items)
            return True, 0, False

        probe._expand_tts_queue_item = lambda item: [replace(item, text="parte um"), replace(item, text="parte dois")]
        probe._enqueue_tts_items = enqueue
        message = SimpleNamespace(id=5, guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=7))
        result = await despachar_mensagem_tts(
            probe, message, guild_defaults={}, active_prefix=",", forced_engine="edge",
            construir_payload=build, received_at_monotonic=10.0,
        )
        self.assertTrue(result.enqueued)
        self.assertEqual(len(captured), 2)
        self.assertEqual([item.received_at_monotonic for item in captured], [10.0, 10.0])
        self.assertTrue(all(item.enqueued_at_monotonic > item.received_at_monotonic for item in captured))

    async def test_progressive_fallback_decoder_receives_personal_effects(self):
        probe = Probe()
        original = make_item(advanced_nightcore_level=3, advanced_reverb_level=2)
        captured = []

        async def prepare(state, fallback, *, store_in_cache):
            captured.append(probe._tts_ffmpeg_options_for_item(fallback))
            return SimpleNamespace(fifo_path="fallback.fifo")

        probe._prepare_gtts_stream = prepare
        probe._try_get_cached_path = lambda *_: None
        probe._gtts_streaming_allowed_for = lambda _: (True, "allowed")
        path, cleanup = await probe._resolve_edge_gtts_fallback(
            probe._get_state(1), original, allow_stream=True,
        )
        self.assertEqual((path, cleanup), ("fallback.fifo", True))
        self.assertEqual(captured, [probe._tts_ffmpeg_options_for_item(original)])
        self.assertEqual(original._tts_actual_engine, "gtts")


if __name__ == "__main__":
    unittest.main()
