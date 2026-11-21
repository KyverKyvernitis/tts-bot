"""First PCM and transport measurements must retain separate meanings."""
from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from cogs.musica.runtime_telefone.agente import tts


class Decoder:
    def __init__(self, *chunks):
        self.chunks = iter(chunks)
        self.closed = False

    def is_opus(self):
        return False

    def read(self):
        return next(self.chunks, b"")

    def cleanup(self):
        self.closed = True


def test_signal_onset_and_actual_udp_success_are_distinct():
    silence = bytes(3840)
    signal = b"\xff\x03" * 1920
    decoder = Decoder(silence, signal, signal)
    source = tts._TimedTTSSource(decoder, started=time.monotonic())
    packets = []

    def send(packet):
        if packet == b"failure":
            raise OSError("socket send failed")
        packets.append(packet)

    connection = SimpleNamespace(send_packet=send)
    client = SimpleNamespace(source=source, _connection=connection)
    with tts._trace_direct_tts_packet(client, source):
        assert source.read() == silence
        connection.send_packet(b"silence")
        assert source.first_frame_ms is not None
        assert set(source.startup_timing()) == {"first_pcm_ready"}
        assert source.read() == signal
        with pytest.raises(OSError):
            connection.send_packet(b"failure")
        assert "first_voice_packet" not in source.startup_timing()
        assert source.read() == signal
        connection.send_packet(b"first signal")
    assert connection.send_packet is send
    assert packets == [b"silence", b"first signal"]
    report = source.startup_timing()
    assert report["first_pcm_ready"] <= report["first_pcm_signal"] <= report["first_voice_packet"]


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_during_preparation", [False, True])
async def test_strict_command_never_joins_or_plays_after_lease_loss(lost_during_preparation):
    voice = SimpleNamespace(is_playing=lambda: False, is_paused=lambda: False)
    voice.play = lambda *_args, **_kwargs: pytest.fail("voz sem confirmação tocou")
    decoder = Decoder(b"\xff\x03" * 1920)
    source = tts._TimedTTSSource(decoder, started=time.monotonic())

    class Owner(tts.TTSMixin):
        valid = lost_during_preparation
        states = {}
        _tts_direct_locks = {}
        _tts_direct_lock_users = {}

        @asynccontextmanager
        async def _registry_lock(self, *_args):
            yield

        def _tts_existing_voice_client(self, _body):
            return voice if self.valid else None

        async def _ensure_direct_voice_client(self, _guild):
            pytest.fail("tentativa de criar ou mover conexão no telefone")

        async def _prepare_tts_source(self, *_args, **_kwargs):
            assert lost_during_preparation
            self.valid = False
            return source, "gtts"

        def _update_auto_leave_from_body(self, *_args): pass
        def _cancel_idle_disconnect(self, *_args): pass
        def _cancel_voice_presence_disconnect(self, *_args): pass
        def _set_voice_session_mode(self, state, mode, **_kwargs): state.voice_session_mode = mode
        def _set_status(self, state, status, **_kwargs): state.status = status
        def log(self, *_args, **_kwargs): pass

    body = {"guild_id": 1, "voice_channel_id": 2, "require_existing_voice": True,
            "expected_voice_session": "a" * 32, "engine": "gtts", "text": "olá", "tts_request_id": "test01"}
    result = await Owner()._run_tts_request(body, direct=True)
    assert result == {"ok": False, "error": "existing_voice_unavailable", "first_frame_observed": False,
                      "tts_request_id": "test01"}
    assert decoder.closed is lost_during_preparation


def load_compare():
    script = Path(__file__).resolve().parents[1] / "scripts/compare-tts-discord-start.py"
    spec = importlib.util.spec_from_file_location("compare_tts_discord_start", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_receipt_comparison_counts_failed_attempts_and_rejects_other_clock():
    compare = load_compare().compare
    cohort = {"engine": "gtts", "route": "vps", "voice": "cold", "cache": "miss", "length": "long"}
    plan = [dict(cohort, trial=f"{pair}_{version}", pair=pair, variant=version)
            for pair in ("test1", "test2") for version in ("baseline", "candidate")]
    events = []
    for row in plan:
        trial, version = row["trial"], row["variant"]
        events.append({"trial": trial, "stage": "sent", "clock": "a" * 64, "at_ns": 100_000_000})
        if trial != "test2_candidate":
            events.append({"trial": trial, "stage": "heard", "clock": "a" * 64,
                           "at_ns": 500_000_000 if version == "baseline" else 400_000_000})
    result = compare(plan, events, min_pairs=1)["cohorts"][0]
    assert result["baseline"]["p95_ms"] == 400
    assert result["candidate"]["p95_ms"] == 300
    assert result["candidate_failures"] == 1
    assert result["gate_passed"] is False
    events[0]["clock"] = "b" * 64
    with pytest.raises(ValueError, match="mesmo relógio"):
        compare(plan, events, min_pairs=1)
