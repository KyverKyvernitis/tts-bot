"""Contexto compacto e contagem de uso, sem transformar dados em autoridade."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import re


USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")


def _count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


@dataclass
class TurnUsage:
    """Soma relatórios distintos; cache/raciocínio nunca são somados ao total."""

    turn_calls: int = 0
    reports: int = 0
    request_count: int = 0
    generation_attempt_count: int = 0
    discovery_request_count: int = 0
    measured_attempts: int = 0
    usage: dict = field(default_factory=dict)
    usage_field_attempts: dict = field(default_factory=dict)
    complete: bool = True
    requests_known: bool = True

    def record(self, report):
        self.turn_calls += 1
        if not isinstance(report, dict) or report.get("outcome") not in {"success", "failed"}:
            self.complete = self.requests_known = False
            return
        self.reports += 1
        count = _count(report.get("request_count"))
        if count is None:
            self.requests_known = False
        else:
            self.request_count += count
        attempts = _count(report.get("generation_attempt_count", report.get("attempt_count")))
        if attempts is not None:
            self.generation_attempt_count += attempts
        self.discovery_request_count += _count(report.get("discovery_request_count")) or 0
        self.measured_attempts += _count(report.get("usage_attempt_count")) or 0
        values = report.get("usage")
        measured = {}
        if isinstance(values, dict):
            measured = {name: count for name in USAGE_FIELDS
                        if (count := _count(values.get(name))) is not None}
            for name, value in measured.items():
                self.usage[name] = self.usage.get(name, 0) + value
        coverage = report.get("usage_field_attempts")
        if isinstance(coverage, dict):
            for name in USAGE_FIELDS:
                if (count := _count(coverage.get(name))) is not None:
                    self.usage_field_attempts[name] = self.usage_field_attempts.get(name, 0) + count
        self.complete = (self.complete and report.get("usage_complete") is True
                         and {"input_tokens", "output_tokens", "total_tokens"} <= measured.keys())

    def result(self):
        result = {"turn_calls": self.turn_calls, "reported_calls": self.reports,
                  "generation_attempt_count": self.generation_attempt_count,
                  "measured_attempts": self.measured_attempts,
                  "usage": dict(self.usage), "usage_scope": "reported_attempts",
                  "usage_field_attempts": dict(self.usage_field_attempts),
                  "usage_complete": bool(self.turn_calls and self.complete),
                  "request_count_complete": self.requests_known}
        if self.requests_known or self.request_count:
            result["request_count"] = self.request_count
        if self.discovery_request_count:
            result["discovery_request_count"] = self.discovery_request_count
        return result


def deduplicate_reply_context(reply_context, history):
    """Troca apenas uma cópia textual exata por um vínculo à mensagem mantida."""
    if not isinstance(reply_context, str):
        return reply_context
    quote = re.fullmatch(r'respondendo a (.+?): "(.*)"', reply_context, re.DOTALL)
    if quote is None:
        return reply_context
    name, text = quote.groups()
    if not text:
        return reply_context
    for index in range(len(history) - 1, -1, -1):
        message = history[index]
        if getattr(message, "role", None) in {"user", "assistant"} and getattr(message, "content", None) == text:
            return f"Respondendo a {name}: mensagem {index + 1} do histórico acima (texto já fornecido)."
    return reply_context


def compact_operational_state(state):
    """Remove cópias de IDs/detalhes já mantidos no host; conserva estados reais."""
    result = deepcopy(state)
    for field in ("guild_id", "channel_id", "user_id", "bot_id"):
        result.pop(field, None)
    # A presença completa já descreve os dois lados e a call do bot.
    result.pop("voice_connected", None)
    result.pop("voice_channel_id", None)
    for field in ("action_draft", "action_draft_error"):
        if not result.get(field):
            result.pop(field, None)
    preferences = result.get("preferences")
    if isinstance(preferences, dict):
        for name in ("voice", "language"):
            if not preferences.get(name):
                preferences.pop(name, None)
    references = result.get("references")
    if isinstance(references, dict):
        for kind in ("members", "resources"):
            for record in (references.get(kind) or {}).values():
                if isinstance(record, dict):
                    record.pop("id", None)
    draft = result.get("action_draft")
    if isinstance(draft, dict):
        for name in ("target_id", "draft_id", "revision", "expires_at"):
            draft.pop(name, None)
    providers = result.get("providers")
    if isinstance(providers, dict):
        # Métricas ficam no painel/log; números do pedido anterior não ajudam
        # a decidir uma ação nem representam uma cota remota restante.
        providers.pop("last_request", None)
        records = []
        for item in providers.get("availability", ()):
            if not isinstance(item, dict) or item.get("configured") is False:
                continue
            compact = {key: item[key] for key in ("provider", "model", "available", "modes") if key in item}
            if item.get("available") is not True:
                compact.update({key: item[key] for key in ("cause_kind", "cooldown_seconds", "tools_support") if item.get(key)})
            records.append(compact)
        providers["availability"] = records
    return result


def spontaneous_quota_factor(diagnostics):
    """Poupa cotas compartilhadas usando só elegibilidade observada localmente."""
    if not isinstance(diagnostics, dict):
        return 1.0
    availability = diagnostics.get("availability")
    if not isinstance(availability, (tuple, list)):
        return 1.0
    groups = {}
    for item in availability:
        if (not isinstance(item, dict) or item.get("configured") is not True
                or "text" not in (item.get("modes") or ())):
            continue
        groups.setdefault(item.get("provider"), []).append(item)
    if not groups:
        return 1.0
    known = [item for group in groups.values() for item in group if type(item.get("available")) is bool]
    if known and len(known) == sum(map(len, groups.values())) and not any(item["available"] for item in known):
        return 0.0
    limited = sum(all(item.get("available") is False and item.get("cause_kind") == "rate_limit"
                      for item in group) for group in groups.values())
    return 0.25 if limited and limited * 2 >= len(groups) else 1.0
