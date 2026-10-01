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
    assert result["ignored"] == 3
    assert result["metrics"]["agent.agent_to_first_packet_ms"] == {"n": 4, "p50_ms": 250, "p95_ms": 385}
    assert "controller.invalid" not in result["metrics"]
    assert not any("sum" in key or "end_to_end" in key for key in result["metrics"])
    assert bench.comparison(result, result)["agent.agent_to_first_packet_ms"]["p50_reduction_percent"] == 0
