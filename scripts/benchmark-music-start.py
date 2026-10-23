#!/usr/bin/env python3
"""Compara durações locais, grupos e resultados; nunca mistura relógios de máquinas."""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path


CONTEXT_VALUES = {
    "entrypoint": {"command", "selection", "modal", "attachment", "unknown"},
    "input_kind": {"url", "search", "playlist", "album", "attachment", "unknown"},
    "source_mode": {"archive", "online", "unknown"},
    "voice_state": {"disconnected", "connected", "unknown"},
    "resolve_cache": {"hit", "miss", "unknown"},
    "helper_state": {"cold", "warming", "warm", "closed", "unknown"},
    "queue_state": {"immediate", "queued", "unknown"},
}
EVENTS = {"music_first_packet_sent", "music_start_attempt", "music_start_outcome", "music_search_results"}
OUTCOMES = ("success", "confirmed", "timeout", "failed", "cancelled", "unconfirmed", "queued", "pending")


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def metric_summary(metrics):
    return {name: {"n": len(values), "p50_ms": round(percentile(values, .5), 2),
                   "p95_ms": round(percentile(values, .95), 2)}
            for name, values in sorted(metrics.items()) if values}


def context_from(record):
    raw = record.get("start_context")
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, allowed in CONTEXT_VALUES.items()
            if isinstance((value := raw.get(key)), str) and value in allowed}


def timings_from(record, origin):
    timings = record.get(origin + "_timing_ms")
    if not isinstance(timings, dict):
        return {}
    return {name: float(value) for name, value in timings.items()
            if isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
            and isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= 86400000 and math.isfinite(float(value))}


def aggregate(intents):
    duplicates = sum("coalesced" in intent["outcomes"] and not intent["packets"] for intent in intents)
    intents = [intent for intent in intents if "coalesced" not in intent["outcomes"] or intent["packets"]]
    metrics, outcomes, runtime_outcomes, delivery_outcomes = {}, dict.fromkeys(OUTCOMES, 0), {}, {}
    packets = runtime_attempts = controller_attempts = legacy_samples = continuations = 0
    for intent in intents:
        observations = intent["outcomes"]
        # O envio real prevalece sobre resposta HTTP perdida. Confirmação
        # apenas no controller fica explícita e não inventa um pacote UDP.
        outcome = "success" if intent["packets"] else (
            "confirmed" if "success" in observations else
            next((item for item in OUTCOMES[2:] if item in observations), "pending"))
        outcomes[outcome] += 1
        packets += len(intent["packets"])
        legacy_samples += intent["legacy_samples"]
        continuations += intent["continuation_packets"]
        runtime_attempts += len(intent["runtime_attempts"])
        controller_attempts += bool(intent["controller_attempt"])
        for values in intent["runtime_attempts"].values():
            item = "success" if "success" in values else next((name for name in OUTCOMES[2:] if name in values), "pending")
            runtime_outcomes[item] = runtime_outcomes.get(item, 0) + 1
        for item in intent["delivery_outcomes"]:
            delivery_outcomes[item] = delivery_outcomes.get(item, 0) + 1
        for name, observations in intent["metrics"].items():
            metrics.setdefault(name, []).extend(observations.values())
    return {"samples": packets, "attempts": len(intents), "outcomes": outcomes,
            "duplicate_requests": duplicates,
            "continuation_packets": continuations,
            "runtime_attempts": runtime_attempts, "runtime_outcomes": runtime_outcomes,
            "controller_attempts": controller_attempts, "controller_outcomes": delivery_outcomes,
            "legacy_success_samples": legacy_samples,
            "success_rate": round(outcomes["success"] / len(intents), 4) if intents and not legacy_samples else None,
            "metrics": metric_summary(metrics)}


def summarize(path: Path, *, include_queued=False):
    intents, seen, ignored, search_metrics = {}, set(), 0, {}
    with path.open(encoding="utf-8", errors="replace") as log:
        for line_index, line in enumerate(log):
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
            event = record.get("event")
            if not isinstance(event, str) or event not in EVENTS:
                continue
            context = context_from(record)
            agent = record.get("agent_timing_ms") or {}
            if not isinstance(agent, dict):
                ignored += 1
                continue
            queue_wait = agent.get("queue_wait_ms", 0)
            if not isinstance(queue_wait, (int, float)) or isinstance(queue_wait, bool) or not 0 <= queue_wait <= 86400000:
                ignored += 1
                continue
            # Só logs antigos mantêm o corte histórico de 100ms. O estado
            # explícito evita esconder uma primeira música nova que foi lenta.
            legacy = not record.get("origin") and not record.get("attempt_id") and not context
            queued = context.get("queue_state") == "queued" or record.get("outcome") == "queued"
            if not include_queued and legacy and queue_wait > 100:
                ignored += 1
                continue
            trace = str(record.get("trace_id") or "")
            origin = record.get("origin", "agent")
            origin = origin if isinstance(origin, str) and origin in {"controller", "agent"} else "unknown"
            attempt_id = str(record.get("attempt_id") or "")
            item_id = str(record.get("item_id") or attempt_id)
            identity = (str(record.get("guild_id") or ""), trace)
            if legacy or not trace:
                identity += (str(record.get("at") or line_index),)
            outcome = record.get("outcome")
            outcome = outcome if isinstance(outcome, str) and outcome in (*OUTCOMES, "coalesced") else ""
            key = (identity, origin, attempt_id, event, outcome)
            if key in seen:
                continue
            seen.add(key)
            if event == "music_search_results":
                for name, value in timings_from(record, "controller").items():
                    search_metrics.setdefault("controller." + name, []).append(value)
                continue
            intent = intents.setdefault(identity, {
                "context": {}, "context_origin": {}, "outcomes": set(), "packets": set(), "metrics": {},
                "runtime_attempts": {}, "controller_attempt": False,
                "delivery_outcomes": set(), "legacy_samples": 0, "queue_states": set(),
                "successful_items": set(), "continuation_packets": 0})
            if queued:
                intent["queue_states"].add("queued")
            elif context.get("queue_state") == "immediate" or (origin == "controller" and outcome == "success"):
                intent["queue_states"].add("immediate")
            if queued and not include_queued:
                ignored += 1
                continue
            # Loops e recuperação de stream mantêm o mesmo trace. Depois do
            # primeiro UDP não são novos starts da intenção. A opção de fila
            # admite a primeira reprodução de cada outro item explicitamente
            # enfileirado, mas continua excluindo replay do mesmo item.
            if origin == "agent" and intent["packets"] and (
                    not include_queued or not queued or item_id in intent["successful_items"]):
                intent["continuation_packets"] += int(event == "music_first_packet_sent")
                ignored += 1
                continue
            for name, value in context.items():
                # A tentativa inicial define frio/quente e voz inicial; um
                # retry aquecido não pode mover uma intenção fria para outro
                # grupo. Cache descreve o resultado observado da resolução.
                replace = name == "resolve_cache" or name not in intent["context"] or (
                    origin == "agent" and intent["context_origin"].get(name) != "agent")
                if value != "unknown" and replace:
                    intent["context"][name] = value
                    intent["context_origin"][name] = origin
            if event == "music_first_packet_sent":
                intent["packets"].add(item_id or identity)
                intent["successful_items"].add(item_id)
                intent["legacy_samples"] += int(legacy)
                if attempt_id:
                    intent["runtime_attempts"].setdefault(attempt_id, set()).add("success")
            if event in {"music_start_attempt", "music_start_outcome"}:
                if origin == "agent" and attempt_id:
                    observed = intent["runtime_attempts"].setdefault(attempt_id, set())
                    if outcome:
                        observed.add(outcome)
                if origin == "controller":
                    intent["controller_attempt"] = True
                    if outcome:
                        intent["delivery_outcomes"].add(outcome)
                if outcome:
                    intent["outcomes"].add(outcome)
            if event == "music_first_packet_sent" or (origin == "controller" and outcome == "success"):
                for metric_origin in ("controller", "agent"):
                    if origin == "controller" and metric_origin == "agent":
                        continue
                    for name, value in timings_from(record, metric_origin).items():
                        observation = identity if metric_origin == "controller" else (identity, attempt_id)
                        intent["metrics"].setdefault(metric_origin + "." + name, {})[observation] = value
    if not include_queued:
        intents = {key: intent for key, intent in intents.items() if intent["queue_states"] != {"queued"}}
    result = aggregate(list(intents.values()))
    result.update(ignored=ignored, search_metrics=metric_summary(search_metrics))
    result["groups"] = {dimension: {
        value: aggregate([intent for intent in intents.values() if intent["context"].get(dimension, "unknown") == value])
        for value in sorted({intent["context"].get(dimension, "unknown") for intent in intents.values()})
    } for dimension in CONTEXT_VALUES}
    result["denominator_note"] = "Uma intenção por trace; logs legados só documentam sucessos observados. Confirmação do controller não substitui pacote UDP."
    return result


def comparison(before, after):
    result = {}
    for name, old in before["metrics"].items():
        new = after["metrics"].get(name)
        if old["p50_ms"] <= 0 or not new:
            continue
        result[name] = {"p50_reduction_percent": round(100 * (1 - new["p50_ms"] / old["p50_ms"]), 2),
                        "p95_reduction_percent": round(100 * (1 - new["p95_ms"] / old["p95_ms"]), 2) if old["p95_ms"] > 0 else None,
                        "before_n": old["n"], "after_n": new["n"]}
    return result


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
        groups = {name: {group: comparison(old, after["groups"].get(name, {}).get(group, {"metrics": {}}))
                         for group, old in values.items()} for name, values in before["groups"].items()}
        result.update(after=after, comparison=comparison(before, after), group_comparison=groups)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
