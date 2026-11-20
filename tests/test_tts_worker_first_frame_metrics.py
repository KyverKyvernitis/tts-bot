"""Remote frame timings must remain separate from VPS clock measurements."""
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_tts_helpers import tts_audio


def record(result, engine="edge"):
    samples = []
    probe = SimpleNamespace(_record_latency_sample=lambda name, value: samples.append((name, value)))
    tts_audio.TTSAudioMixin._record_worker_first_frame_timing(probe, result, engine=engine)
    return dict(samples)


def test_remote_observed_frame_is_recorded_without_local_clock_or_playback_time():
    result = record({"ok": True, "first_frame_observed": True, "first_frame_ms": 187.5,
                     "engine": "edge_tts", "native_stream": True, "playback_ms": 4500})
    assert result == {"worker_command_to_first_frame": 187.5,
                      "worker_command_to_first_frame_edge": 187.5,
                      "worker_command_to_first_frame_rust_stream": 187.5}
    assert "message_to_first_frame" not in result
    assert "total_to_first_frame" not in result


def test_nested_worker_contract_and_prebuilt_audio_keep_actual_engine():
    result = record({"ok": True, "worker_result": {"first_frame_observed": True, "first_frame_ms": 0,
                                                    "engine": "teto", "native_stream": False}})
    assert result == {"worker_command_to_first_frame": 0.0, "worker_command_to_first_frame_teto": 0.0}


def test_cache_label_keeps_the_engine_dimension():
    assert record({"first_frame_observed": True, "first_frame_ms": 20, "engine": "gtts-cache"}) == {
        "worker_command_to_first_frame": 20.0, "worker_command_to_first_frame_gtts": 20.0,
    }


@pytest.mark.parametrize("value", [None, -1, math.nan, math.inf, -math.inf, "invalid", True])
def test_missing_or_invalid_remote_duration_never_becomes_a_latency_sample(value):
    assert not record({"ok": True, "first_frame_observed": True, "first_frame_ms": value})


@pytest.mark.parametrize("result", [None, {}, {"first_frame_ms": 5},
                                   {"first_frame_observed": False, "first_frame_ms": 5},
                                   {"ok": False, "first_frame_observed": True, "first_frame_ms": 5},
                                   {"first_frame_observed": False, "worker_result": {
                                       "first_frame_observed": True, "first_frame_ms": 5}}])
def test_failure_or_unobserved_frame_never_looks_like_success(result):
    assert not record(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("observed_frame", [True, False])
async def test_direct_worker_failure_only_falls_back_before_confirmed_partial_playback(observed_frame):
    result = {"ok": False, "worker_result": {"ok": False, "partial_playback": True,
                                               "first_frame_observed": observed_frame}}
    release = AsyncMock()
    probe = SimpleNamespace(
        _worker_voice_direct_tts_available_for=lambda *_: (True, "ready"),
        _worker_voice_direct_tts_payload=lambda *_: {},
        _maybe_attach_prebuilt_direct_tts_audio=AsyncMock(return_value=None),
        _get_voice_client_for_guild=lambda _: None,
        _request_worker_voice_agent_json=AsyncMock(return_value=result),
        _record_worker_voice_session_metric=lambda _: None,
        _worker_voice_direct_tts_disabled_untils=dict,
        _worker_voice_agent_release_transfer=release,
    )
    item = SimpleNamespace(engine="gtts", channel_id=2)
    value = await tts_audio.TTSAudioMixin._try_worker_voice_direct_tts(probe, SimpleNamespace(id=1), item)
    release.assert_awaited_once()
    if observed_frame:
        assert value == {"ok": False, "worker_result": result}
        assert item._tts_remote_active is False
        with pytest.raises(tts_audio.TTSPlaybackError):
            tts_audio._validate_tts_playback_result(value["worker_result"])
    else:
        assert value is None
