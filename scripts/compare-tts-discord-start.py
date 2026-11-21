#!/usr/bin/env python3
"""Join authorized same-host Discord sender and receiver timing receipts.

Import capture_event in each local test client and write its JSON to a JSONL
file. Capture `sent` just before sending the test message and `heard` on
the receiver's first attributable speech PCM. `error` marks a failed trial.
The harness handles timing only. The clients own voice access and capture.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import time


COHORTS = {
    "engine": {"edge", "gtts"},
    "route": {"vps", "phone", "overlay"},
    "voice": {"hot", "cold"},
    "cache": {"hit", "miss"},
    "length": {"short", "long"},
}
EVENT_STAGES = {"sent", "heard", "error"}


def clock_domain() -> str:
    """A hashed Linux boot ID limits comparisons to one monotonic clock."""
    return hashlib.sha256(Path("/proc/sys/kernel/random/boot_id").read_bytes().strip()).hexdigest()


def capture_event(trial: str, stage: str) -> dict:
    timestamp = time.monotonic_ns()
    if stage not in EVENT_STAGES:
        raise ValueError("estágio inválido")
    return {"trial": trial, "stage": stage, "clock": clock_domain(), "at_ns": timestamp}


def records(path: Path):
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            if len(line) > 65536:
                raise ValueError(f"linha excede o limite em {path}:{number}")
            if line.strip():
                yield json.loads(line)


def distribution(values):
    if not values:
        return {"n": 0, "p50_ms": None, "p95_ms": None}
    ordered = sorted(values)

    def percentile(fraction):
        position = (len(ordered) - 1) * fraction
        start = math.floor(position)
        end = math.ceil(position)
        return ordered[start] + (ordered[end] - ordered[start]) * (position - start)

    return {"n": len(values), "p50_ms": round(percentile(.5), 2), "p95_ms": round(percentile(.95), 2)}


def compare(manifest, events, *, min_pairs=100, timeout_ms=30000):
    """Score ordered pairs. Every planned attempt contributes to failure counts."""
    by_trial, pairs, groups = {}, {}, {}
    for row in manifest:
        trial, pair, variant = row["trial"], row["pair"], row["variant"]
        if (not isinstance(trial, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", trial)
                or not isinstance(pair, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", pair)
                or trial in by_trial or variant not in {"baseline", "candidate"}):
            raise ValueError("identificadores ou variante inválidos no plano de ensaio")
        cohort = tuple(row[key] for key in COHORTS)
        if any(value not in allowed for value, allowed in zip(cohort, COHORTS.values())):
            raise ValueError("coorte inválida no plano de ensaio")
        if pair not in pairs:
            pairs[pair] = cohort, {}
        prior_cohort, trials = pairs[pair]
        if prior_cohort != cohort or variant in trials:
            raise ValueError("cada par deve manter a coorte e conter uma tentativa de cada versão")
        trials[variant] = trial
        by_trial[trial] = {}

    clocks = set()
    for event in events:
        trial, stage, domain, tick = event["trial"], event["stage"], event["clock"], event["at_ns"]
        if (trial not in by_trial or stage not in EVENT_STAGES or stage in by_trial[trial]
                or not isinstance(domain, str) or not re.fullmatch(r"[0-9a-f]{64}", domain)
                or type(tick) is not int or tick < 0):
            raise ValueError("recibo inválido, repetido ou fora do plano de ensaio")
        clocks.add(domain)
        by_trial[trial][stage] = tick
    if len(clocks) > 1:
        raise ValueError("os recibos precisam vir do mesmo relógio monotônico")

    for cohort, trials in pairs.values():
        if len(trials) != 2:
            raise ValueError("par incompleto no plano de ensaio")
        group = groups.setdefault(cohort, {"pairs": 0, "baseline": [], "candidate": [],
                                          "baseline_failures": 0, "candidate_failures": 0})
        group["pairs"] += 1
        elapsed = {}
        for variant, trial in trials.items():
            receipt = by_trial[trial]
            if "sent" in receipt and "heard" in receipt:
                duration = (receipt["heard"] - receipt["sent"]) / 1_000_000
                if duration < 0:
                    raise ValueError("recepção anterior ao envio no mesmo par")
                if "error" not in receipt and duration <= timeout_ms:
                    elapsed[variant] = duration
                    continue
            group[f"{variant}_failures"] += 1
        if len(elapsed) == 2:
            for variant, duration in elapsed.items():
                group[variant].append(duration)

    output = []
    for cohort, group in sorted(groups.items()):
        score = dict(zip(COHORTS, cohort))
        score.update(pairs=group["pairs"], baseline_failures=group["baseline_failures"],
                     candidate_failures=group["candidate_failures"])
        before, after = distribution(group["baseline"]), distribution(group["candidate"])
        score.update(baseline=before, candidate=after)
        change = None
        if before["p95_ms"]:
            change = round(100 * (after["p95_ms"] / before["p95_ms"] - 1), 2)
        score["p95_change_percent"] = change
        score["gate_passed"] = (before["n"] >= min_pairs and change is not None
                                and change <= (-20 if score["cache"] == "miss" else 5)
                                and score["candidate_failures"] <= score["baseline_failures"])
        output.append(score)
    return {"schema": "tts-discord-start-ab-v1", "metric": "message_send_to_receiver_speech_pcm",
            "min_pairs_per_cohort": min_pairs, "timeout_ms": timeout_ms,
            "gate_passed": bool(output) and all(row["gate_passed"] for row in output), "cohorts": output}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    mark = sub.add_parser("mark", help="emite um recibo com o relógio local para integração")
    mark.add_argument("trial")
    mark.add_argument("stage", choices=sorted(EVENT_STAGES))
    collect = sub.add_parser("compare", help="cruza o plano e os recibos e calcula p50, p95 e falhas")
    collect.add_argument("--manifest", type=Path, required=True, help="JSONL dos pares e suas coortes")
    collect.add_argument("--events", type=Path, nargs="+", required=True, help="JSONL do emissor e do receptor")
    collect.add_argument("--min-pairs", type=int, default=100)
    collect.add_argument("--timeout-ms", type=float, default=30000)
    args = parser.parse_args()
    if args.action == "mark":
        print(json.dumps(capture_event(args.trial, args.stage)))
        return 0
    if args.min_pairs < 1 or not math.isfinite(args.timeout_ms) or args.timeout_ms <= 0:
        parser.error("use limites positivos e finitos")
    try:
        result = compare(records(args.manifest), (event for path in args.events for event in records(path)),
                         min_pairs=args.min_pairs, timeout_ms=args.timeout_ms)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
