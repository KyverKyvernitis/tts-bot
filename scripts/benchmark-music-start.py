#!/usr/bin/env python3
"""Compara durações locais dos traces; não compara timestamps entre máquinas."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize(path: Path, *, include_queued=False):
    metrics = {}
    accepted = 0
    ignored = 0
    seen = set()
    with path.open(encoding="utf-8", errors="replace") as log:
        for line in log:
            if "[music-start] " not in line:
                continue
            try:
                record = json.loads(line.split("[music-start] ", 1)[1])
            except (ValueError, TypeError):
                ignored += 1
                continue
            if not isinstance(record, dict):
                ignored += 1
                continue
            if record.get("event") != "music_first_packet_sent":
                continue
            agent = record.get("agent_timing_ms") or {}
            if not isinstance(agent, dict):
                ignored += 1
                continue
            queue_wait = agent.get("queue_wait_ms", 0)
            if not isinstance(agent, dict) or not isinstance(queue_wait, (float, int)) or not math.isfinite(queue_wait):
                ignored += 1
                continue
            if not include_queued and queue_wait > 100:
                ignored += 1
                continue
            key = tuple(str(record.get(field) or "") for field in ("guild_id", "trace_id", "at"))
            if key in seen:
                continue
            seen.add(key)
            accepted += 1
            for origin in ("controller", "agent"):
                timings = record.get(origin + "_timing_ms")
                if not isinstance(timings, dict):
                    continue
                for name, value in timings.items():
                    if isinstance(value, (float, int)) and math.isfinite(value) and value >= 0:
                        metrics.setdefault(origin + "." + name, []).append(float(value))
    return {"samples": accepted, "ignored": ignored, "metrics": {
        name: {"n": len(values), "p50_ms": round(percentile(values, .5), 2),
               "p95_ms": round(percentile(values, .95), 2)}
        for name, values in sorted(metrics.items())}}


def comparison(before, after):
    return {name: {"p50_reduction_percent": round(100 * (1 - new["p50_ms"] / old["p50_ms"]), 2),
                   "before_n": old["n"], "after_n": new["n"]}
            for name, old in before["metrics"].items()
            if old["p50_ms"] > 0 and (new := after["metrics"].get(name))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--include-queued", action="store_true")
    args = parser.parse_args()
    before = summarize(args.log, include_queued=args.include_queued)
    result = {"before": before}
    if args.compare:
        after = summarize(args.compare, include_queued=args.include_queued)
        result.update(after=after, comparison=comparison(before, after))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
