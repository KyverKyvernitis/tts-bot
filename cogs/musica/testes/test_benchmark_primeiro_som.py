import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/benchmark-music-start.py"
spec = importlib.util.spec_from_file_location("benchmark_music_start", SCRIPT)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def test_summary_ignores_queue_malformed_duplicates_and_infinite_values(tmp_path):
    records = []
    for index, ms in enumerate([100, 200, 300, 400]):
        records.append({"event": "music_first_packet_sent", "trace_id": str(index), "at": str(index),
                        "agent_timing_ms": {"agent_to_first_packet_ms": ms, "queue_wait_ms": 0},
                        "controller_timing_ms": {"command_to_dispatch_ms": ms / 2, "invalid": float("inf")}})
    records += [records[0], {"event": "music_first_packet_sent", "agent_timing_ms": {"queue_wait_ms": 1000}},
                {"event": "music_first_packet_sent", "agent_timing_ms": "bad"}]
    path = tmp_path / "trace.log"
    path.write_text("noise\n[music-start] broken\n" + "\n".join("[music-start] " + json.dumps(item) for item in records))
    result = bench.summarize(path)
    assert result["samples"] == 4
    assert result["success_rate"] is None  # Logs antigos não têm denominador de tentativas.
    assert result["ignored"] == 3
    assert result["metrics"]["agent.agent_to_first_packet_ms"] == {"n": 4, "p50_ms": 250, "p95_ms": 385}
    assert "controller.invalid" not in result["metrics"]
    assert not any("sum" in key or "end_to_end" in key for key in result["metrics"])
    assert bench.comparison(result, result)["agent.agent_to_first_packet_ms"]["p50_reduction_percent"] == 0


def log_records(tmp_path, records):
    path = tmp_path / "new-trace.log"
    path.write_text("\n".join("[music-start] " + json.dumps(record) for record in records))
    return path


def event(name, trace="one", origin="agent", **kwargs):
    return {"event": name, "trace_id": trace, "guild_id": 1, "origin": origin, **kwargs}


def test_queued_controller_attempt_is_excluded_only_after_terminal_is_merged(tmp_path):
    path = log_records(tmp_path, [
        event("music_start_attempt", origin="controller"),
        event("music_start_outcome", origin="controller", outcome="queued", start_context={"queue_state": "queued"}),
    ])
    result = bench.summarize(path)
    assert result["attempts"] == 0
    assert result["outcomes"]["pending"] == 0
    included = bench.summarize(path, include_queued=True)
    assert included["attempts"] == included["outcomes"]["queued"] == 1


def test_first_music_waiting_more_than_100ms_and_failed_intents_are_counted(tmp_path):
    records = [event("music_first_packet_sent", attempt_id="track:1",
                     start_context={"queue_state": "immediate", "source_mode": "archive"},
                     agent_timing_ms={"queue_wait_ms": 1400, "agent_to_first_packet_ms": 2500})]
    for index, outcome in enumerate(("failed", "timeout", "cancelled")):
        records.extend([event("music_start_attempt", trace=str(index), origin="controller"),
                        event("music_start_outcome", trace=str(index), origin="controller", outcome=outcome)])
    result = bench.summarize(log_records(tmp_path, records))
    assert result["samples"] == 1 and result["attempts"] == 4
    assert result["success_rate"] == .25
    assert all(result["outcomes"][name] == 1 for name in ("success", "failed", "timeout", "cancelled"))
    assert result["groups"]["source_mode"]["archive"]["metrics"]["agent.queue_wait_ms"]["p95_ms"] == 1400


def test_runtime_retries_and_lost_http_response_keep_one_cold_intent(tmp_path):
    cold = {"helper_state": "cold", "voice_state": "disconnected", "queue_state": "immediate"}
    warm = dict(cold, helper_state="warm", voice_state="connected", resolve_cache="hit")
    records = [
        event("music_start_attempt", origin="controller", controller_timing_ms={"command_to_dispatch_ms": 100}),
        event("music_start_attempt", attempt_id="track:1", start_context=cold),
        event("music_start_outcome", attempt_id="track:1", outcome="failed", start_context=cold),
        event("music_start_attempt", attempt_id="track:2", start_context=warm),
        event("music_first_packet_sent", attempt_id="track:2", start_context=warm,
              controller_timing_ms={"command_to_dispatch_ms": 100},
              agent_timing_ms={"agent_to_first_packet_ms": 900}),
        event("music_start_outcome", attempt_id="track:2", outcome="success", start_context=warm),
        event("music_start_outcome", origin="controller", outcome="timeout"),
    ]
    result = bench.summarize(log_records(tmp_path, records + records))
    assert result["samples"] == result["attempts"] == result["controller_attempts"] == 1
    assert result["runtime_attempts"] == 2
    assert result["runtime_outcomes"] == {"failed": 1, "success": 1}
    assert result["controller_outcomes"] == {"timeout": 1}
    assert result["outcomes"]["success"] == 1
    assert "warm" not in result["groups"]["helper_state"]
    assert result["groups"]["voice_state"]["disconnected"]["attempts"] == 1
    assert result["metrics"]["controller.command_to_dispatch_ms"]["n"] == 1
    assert bench.comparison(result, result)["agent.agent_to_first_packet_ms"]["p95_reduction_percent"] == 0


def test_later_playlist_packets_do_not_pollute_first_track_metrics_or_group(tmp_path):
    records = [
        event("music_start_attempt", origin="controller"),
        event("music_first_packet_sent", attempt_id="first:1", start_context={"queue_state": "immediate"},
              agent_timing_ms={"agent_to_first_packet_ms": 100}),
        event("music_start_attempt", attempt_id="later:2", start_context={"queue_state": "queued"}),
        event("music_first_packet_sent", attempt_id="later:2", start_context={"queue_state": "queued"},
              agent_timing_ms={"agent_to_first_packet_ms": 50000}),
    ]
    path = log_records(tmp_path, records)
    result = bench.summarize(path)
    assert result["attempts"] == result["samples"] == 1
    assert result["metrics"]["agent.agent_to_first_packet_ms"]["p95_ms"] == 100
    assert result["groups"]["queue_state"]["immediate"]["attempts"] == 1
    assert bench.summarize(path, include_queued=True)["samples"] == 2


def test_controller_confirmation_is_explicit_upper_bound_not_synthetic_packet(tmp_path):
    records = [event("music_start_attempt", origin="controller"),
               event("music_start_outcome", origin="controller", outcome="success",
                     controller_timing_ms={"command_to_confirmation_ms": 900, "huge": 10 ** 400})]
    result = bench.summarize(log_records(tmp_path, records))
    assert result["samples"] == 0 and result["outcomes"]["confirmed"] == 1
    assert result["metrics"]["controller.command_to_confirmation_ms"]["p50_ms"] == 900
    assert "controller.huge" not in result["metrics"]


def test_search_results_are_separate_from_playback_denominator_and_bad_context_is_ignored(tmp_path):
    records = [event("music_search_results", origin="controller", start_context={"entrypoint": "https://private.invalid/token"},
                     controller_timing_ms={"search_to_results_ms": 123}),
               event("music_start_outcome", trace="failed", origin="controller", outcome="failed",
                     start_context={"entrypoint": ["bad"], "voice_state": {"url": "private"}})]
    result = bench.summarize(log_records(tmp_path, records))
    assert result["attempts"] == 1 and result["outcomes"]["failed"] == 1
    assert result["search_metrics"]["controller.search_to_results_ms"]["p50_ms"] == 123
    assert set(result["groups"]["entrypoint"]) == {"unknown"}


def test_coalesced_repeat_is_reported_without_inventing_another_pending_play(tmp_path):
    records = [event("music_start_attempt", origin="controller"),
               event("music_start_attempt", trace="repeat", origin="controller"),
               event("music_start_outcome", trace="repeat", origin="controller", outcome="coalesced", related_trace_id="one"),
               event("music_first_packet_sent", attempt_id="first:1", start_context={"queue_state": "immediate"})]
    result = bench.summarize(log_records(tmp_path, records))
    assert result["attempts"] == result["samples"] == result["duplicate_requests"] == 1
    assert result["outcomes"]["pending"] == 0


def test_loop_and_stream_recovery_do_not_add_first_audio_samples_or_attempts(tmp_path):
    immediate = {"queue_state": "immediate"}
    records = [
        event("music_start_attempt", attempt_id="first:1", item_id="a" * 24, start_context=immediate),
        event("music_first_packet_sent", attempt_id="first:1", item_id="a" * 24, start_context=immediate,
              agent_timing_ms={"agent_to_first_packet_ms": 400}),
        event("music_start_attempt", attempt_id="loop:2", item_id="a" * 24, start_context=immediate),
        event("music_first_packet_sent", attempt_id="loop:2", item_id="a" * 24, start_context=immediate,
              agent_timing_ms={"agent_to_first_packet_ms": 180400}),
        event("music_first_packet_sent", item_id="a" * 24, start_context=immediate,
              agent_timing_ms={"agent_to_first_packet_ms": 190400}),
    ]
    path = log_records(tmp_path, records)
    for include_queued in (False, True):
        result = bench.summarize(path, include_queued=include_queued)
        assert result["attempts"] == result["samples"] == result["runtime_attempts"] == 1
        assert result["continuation_packets"] == 2
        assert result["metrics"]["agent.agent_to_first_packet_ms"] == {"n": 1, "p50_ms": 400, "p95_ms": 400}


def test_include_queued_accepts_other_items_once_and_excludes_their_replays(tmp_path):
    queued = {"queue_state": "queued"}
    records = [
        event("music_first_packet_sent", item_id="a" * 24, attempt_id="first:1", start_context=queued,
              agent_timing_ms={"agent_to_first_packet_ms": 400}),
        event("music_first_packet_sent", item_id="b" * 24, attempt_id="second:2", start_context=queued,
              agent_timing_ms={"agent_to_first_packet_ms": 500}),
        event("music_first_packet_sent", item_id="b" * 24, attempt_id="loop:3", start_context=queued,
              agent_timing_ms={"agent_to_first_packet_ms": 180500}),
    ]
    result = bench.summarize(log_records(tmp_path, records), include_queued=True)
    assert result["samples"] == 2 and result["runtime_attempts"] == 2
    assert result["metrics"]["agent.agent_to_first_packet_ms"]["p95_ms"] == 495
    assert result["continuation_packets"] == 1
