"""Contexto compacto e contagem de uso, sem transformar dados em autoridade."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import math
import re


USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")
RESOURCE_FIELDS = ("neurons",)
CONTEXT_FIELDS = ("system_chars", "message_chars", "history_chars", "current_user_chars",
                  "quoted_context_chars", "retrieved_data_chars", "tool_result_chars",
                  "tool_schema_chars", "image_count")


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
    resources: dict = field(default_factory=dict)
    usage_field_attempts: dict = field(default_factory=dict)
    wasted_usage: dict = field(default_factory=dict)
    successful_usage: dict = field(default_factory=dict)
    wasted_resources: dict = field(default_factory=dict)
    successful_resources: dict = field(default_factory=dict)
    provider_usage: dict = field(default_factory=dict)
    model_usage: dict = field(default_factory=dict)
    context_sent: dict = field(default_factory=dict)
    context_peak: dict = field(default_factory=dict)
    context_calls: int = 0
    stages: dict = field(default_factory=dict)
    repair_attempts: int = 0
    fallback_attempts: int = 0
    failed_generation_attempts: int = 0
    tool_calls_seen: int = 0
    tool_calls_executed: int = 0
    tool_reads_reused: int = 0
    speculative_tools_pruned: int = 0
    tool_selection_metrics: dict = field(default_factory=dict)
    local_savings: dict = field(default_factory=dict)
    complete: bool = True
    requests_known: bool = True

    def _stage(self, name):
        key = str(name or "generation")[:40]
        return self.stages.setdefault(key, {
            "calls": 0, "reported_calls": 0, "usage": {}, "resources": {},
            "context_sent": {}, "context_peak": {}, "context_calls": 0,
        })

    def record_context(self, metrics, *, stage="generation"):
        if not isinstance(metrics, dict):
            return
        clean = {}
        for name in CONTEXT_FIELDS:
            value = metrics.get(name)
            if type(value) is int and 0 <= value <= 10**9:
                clean[name] = value
        if not clean:
            return
        self.context_calls += 1
        stage_bucket = self._stage(stage)
        stage_bucket["calls"] += 1
        stage_bucket["context_calls"] += 1
        for name, value in clean.items():
            self.context_sent[name] = self.context_sent.get(name, 0) + value
            self.context_peak[name] = max(self.context_peak.get(name, 0), value)
            stage_bucket["context_sent"][name] = stage_bucket["context_sent"].get(name, 0) + value
            stage_bucket["context_peak"][name] = max(stage_bucket["context_peak"].get(name, 0), value)

    def record_tool_call(self, *, executed: bool = False, reused_read: bool = False, seen: bool = True):
        """Telemetria local: não altera limites nem semântica das ferramentas."""
        if seen:
            self.tool_calls_seen += 1
        if executed:
            self.tool_calls_executed += 1
        if reused_read:
            self.tool_reads_reused += 1

    def record_local_saving(self, name: str, chars: int) -> None:
        """Conta caracteres evitados localmente, sem estimar tokens do provider."""
        if not isinstance(name, str) or not name or type(chars) is not int or chars <= 0:
            return
        key = name[:48]
        self.local_savings[key] = self.local_savings.get(key, 0) + min(chars, 10**9)

    def record_tool_selection(self, metrics=None, *, pruned: int = 0):
        """Registra economia de schemas sem guardar nomes/argumentos de tools."""
        if isinstance(metrics, dict):
            for name in ("catalog_tools", "catalog_schema_chars"):
                value = metrics.get(name)
                if type(value) is int and 0 <= value <= 10**9:
                    self.tool_selection_metrics.setdefault(name, value)
            for source, initial, current in (
                ("loaded_tools", "initial_loaded_tools", "current_loaded_tools"),
                ("loaded_schema_chars", "initial_loaded_schema_chars", "current_loaded_schema_chars"),
            ):
                value = metrics.get(source)
                if type(value) is int and 0 <= value <= 10**9:
                    self.tool_selection_metrics.setdefault(initial, value)
                    self.tool_selection_metrics[current] = value
            for name in ("explicit_tools", "used_tools", "index_hint_tools"):
                value = metrics.get(name)
                if type(value) is int and 0 <= value <= 10**9:
                    self.tool_selection_metrics[name] = value
        if type(pruned) is int and pruned > 0:
            self.speculative_tools_pruned += pruned

    @staticmethod
    def _sum_usage(target, values):
        if not isinstance(values, dict):
            return
        for name in USAGE_FIELDS:
            value = _count(values.get(name))
            if value is not None:
                target[name] = target.get(name, 0) + value

    @staticmethod
    def _sum_resources(target, values):
        if not isinstance(values, dict):
            return
        for name in RESOURCE_FIELDS:
            value = values.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 10**12:
                target[name] = target.get(name, 0.0) + float(value)

    def record(self, report, *, stage="generation"):
        self.turn_calls += 1
        stage_bucket = self._stage(stage)
        stage_bucket["reported_calls"] += 1
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
            for name in RESOURCE_FIELDS:
                value = values.get(name)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 10**12:
                    self.resources[name] = self.resources.get(name, 0.0) + float(value)
            self._sum_usage(stage_bucket["usage"], values)
            self._sum_resources(stage_bucket["resources"], values)
        attempts_list = report.get("attempts")
        if isinstance(attempts_list, list):
            self.fallback_attempts += max(0, len(attempts_list) - 1)
            for attempt in attempts_list:
                if not isinstance(attempt, dict):
                    continue
                provider = attempt.get("provider") if isinstance(attempt.get("provider"), str) else ""
                model = attempt.get("model") if isinstance(attempt.get("model"), str) else ""
                attempt_usage = attempt.get("usage") if isinstance(attempt.get("usage"), dict) else {}
                if provider:
                    bucket = self.provider_usage.setdefault(provider, {"attempts": 0, "successes": 0, "failed": 0,
                                                                       "usage": {}, "resources": {}})
                    bucket["attempts"] += 1
                    bucket["successes" if attempt.get("kind") == "success" else "failed"] += 1
                    self._sum_usage(bucket["usage"], attempt_usage)
                    self._sum_resources(bucket["resources"], attempt_usage)
                if provider and model:
                    key = f"{provider}/{model}"
                    bucket = self.model_usage.setdefault(key, {"attempts": 0, "successes": 0, "failed": 0,
                                                                "usage": {}, "resources": {}})
                    bucket["attempts"] += 1
                    bucket["successes" if attempt.get("kind") == "success" else "failed"] += 1
                    self._sum_usage(bucket["usage"], attempt_usage)
                    self._sum_resources(bucket["resources"], attempt_usage)
                if attempt.get("repair") is True:
                    self.repair_attempts += 1
                if attempt.get("kind") != "success":
                    self.failed_generation_attempts += 1
                    self._sum_usage(self.wasted_usage, attempt_usage)
                    self._sum_resources(self.wasted_resources, attempt_usage)
                else:
                    self._sum_usage(self.successful_usage, attempt_usage)
                    self._sum_resources(self.successful_resources, attempt_usage)
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
                  "request_count_complete": self.requests_known,
                  "repair_attempts": self.repair_attempts,
                  "fallback_attempts": self.fallback_attempts,
                  "failed_generation_attempts": self.failed_generation_attempts}
        if self.resources:
            result["resources"] = {name: round(value, 6) for name, value in self.resources.items()}
        if self.wasted_usage:
            result["wasted_usage"] = dict(self.wasted_usage)
        if self.successful_usage:
            result["successful_usage"] = dict(self.successful_usage)
        if self.wasted_resources:
            result["wasted_resources"] = {name: round(value, 6) for name, value in self.wasted_resources.items()}
        if self.successful_resources:
            result["successful_resources"] = {name: round(value, 6) for name, value in self.successful_resources.items()}
        if self.provider_usage:
            result["providers"] = self._usage_breakdown(self.provider_usage)
        if self.model_usage:
            result["models"] = self._usage_breakdown(self.model_usage)
        if self.context_calls:
            result["context"] = {"calls": self.context_calls, "sent": dict(self.context_sent),
                                 "peak": dict(self.context_peak)}
        if self.stages:
            result["stages"] = {}
            for name, bucket in self.stages.items():
                item = {"calls": bucket["calls"], "reported_calls": bucket["reported_calls"]}
                if bucket["usage"]:
                    item["usage"] = dict(bucket["usage"])
                if bucket["resources"]:
                    item["resources"] = {key: round(value, 6) for key, value in bucket["resources"].items()}
                if bucket["context_calls"]:
                    item["context"] = {"calls": bucket["context_calls"],
                                       "sent": dict(bucket["context_sent"]),
                                       "peak": dict(bucket["context_peak"])}
                result["stages"][name] = item
        input_tokens = self.usage.get("input_tokens")
        cached_tokens = self.usage.get("cached_tokens")
        if type(input_tokens) is int and input_tokens > 0 and type(cached_tokens) is int:
            result["cache_hit_ratio"] = round(cached_tokens / input_tokens, 4)
        output_tokens = self.usage.get("output_tokens")
        reasoning_tokens = self.usage.get("reasoning_tokens")
        if type(output_tokens) is int and output_tokens > 0 and type(reasoning_tokens) is int:
            result["reasoning_ratio"] = round(reasoning_tokens / output_tokens, 4)
        total_tokens = self.usage.get("total_tokens")
        wasted_tokens = self.wasted_usage.get("total_tokens")
        if type(total_tokens) is int and total_tokens > 0 and type(wasted_tokens) is int:
            result["wasted_token_ratio"] = round(wasted_tokens / total_tokens, 4)
        neurons = self.resources.get("neurons")
        wasted_neurons = self.wasted_resources.get("neurons")
        if isinstance(neurons, (int, float)) and neurons > 0 and isinstance(wasted_neurons, (int, float)):
            result["wasted_neuron_ratio"] = round(wasted_neurons / neurons, 4)
        if self.tool_calls_seen:
            result["tools"] = {"seen": self.tool_calls_seen, "executed": self.tool_calls_executed,
                               "reused_reads": self.tool_reads_reused}
        if self.local_savings:
            result["local_savings_chars"] = dict(self.local_savings)
        if self.tool_selection_metrics or self.speculative_tools_pruned:
            selection = dict(self.tool_selection_metrics)
            if self.speculative_tools_pruned:
                selection["speculative_pruned"] = self.speculative_tools_pruned
            catalog = selection.get("catalog_schema_chars")
            initial = selection.get("initial_loaded_schema_chars")
            current = selection.get("current_loaded_schema_chars")
            if type(catalog) is int and catalog > 0:
                if type(initial) is int:
                    selection["initial_schema_reduction_ratio"] = round(max(0.0, 1.0 - initial / catalog), 4)
                if type(current) is int:
                    selection["current_schema_reduction_ratio"] = round(max(0.0, 1.0 - current / catalog), 4)
            result["tool_selection"] = selection
        if self.requests_known or self.request_count:
            result["request_count"] = self.request_count
        if self.discovery_request_count:
            result["discovery_request_count"] = self.discovery_request_count
        return result

    @staticmethod
    def _usage_breakdown(source):
        result = {}
        for key, bucket in source.items():
            item = {name: bucket[name] for name in ("attempts", "successes", "failed")}
            if bucket.get("usage"):
                item["usage"] = dict(bucket["usage"])
            if bucket.get("resources"):
                item["resources"] = {name: round(value, 6) for name, value in bucket["resources"].items()}
            result[key] = item
        return result


def annotate_delivery(result, *, delivered: bool, response_chars: int = 0, source: str = "", audio: bool = False):
    """Anexa eficiência por resposta entregue sem inventar contagens ausentes."""
    if not isinstance(result, dict):
        return result
    delivery = {"delivered": bool(delivered), "response_chars": max(0, int(response_chars or 0)),
                "audio": bool(audio)}
    if source:
        delivery["source"] = str(source)[:40]
    if delivered:
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        for key in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"):
            value = _count(usage.get(key))
            if value is not None:
                delivery[key + "_per_response"] = value
        for source_key, target_key in (("turn_calls", "model_rounds_per_response"),
                                       ("generation_attempt_count", "generation_attempts_per_response"),
                                       ("request_count", "http_requests_per_response")):
            value = _count(result.get(source_key))
            if value is None or (source_key != "turn_calls" and value <= 0):
                continue
            if source_key == "request_count" and result.get("request_count_complete") is not True:
                continue
            delivery[target_key] = value
        wasted = result.get("wasted_usage") if isinstance(result.get("wasted_usage"), dict) else {}
        wasted_total = _count(wasted.get("total_tokens"))
        if wasted_total is not None:
            delivery["wasted_tokens_per_response"] = wasted_total
        elif result.get("usage_complete") is True and result.get("failed_generation_attempts") == 0:
            delivery["wasted_tokens_per_response"] = 0
        resources = result.get("resources") if isinstance(result.get("resources"), dict) else {}
        neurons = resources.get("neurons")
        if isinstance(neurons, (int, float)) and not isinstance(neurons, bool) and math.isfinite(neurons) and neurons >= 0:
            delivery["neurons_per_response"] = round(float(neurons), 6)
    result["delivery"] = delivery
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
    tools = result.get("tools")
    if isinstance(tools, dict) and not any(tools.values()):
        result.pop("tools", None)
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
    # Provider/model/cota são decisões do host. Expor esse diagnóstico em toda
    # geração só repete tokens e pode induzir o modelo a opinar sobre um
    # fallback que ele não controla. O painel/log preserva o snapshot completo.
    result.pop("providers", None)
    return result


def compact_closing_state(state):
    """Estado mínimo para síntese final, quando novas ferramentas são proibidas.

    Referências, presença de voz e rascunhos só servem para decidir/executar
    chamadas. Depois que o host fecha as tools, repeti-los não pode mudar nada e
    apenas aumenta a entrada. Preferências ainda orientam idioma/formato.
    """
    result = {}
    preferences = state.get("preferences") if isinstance(state, dict) else None
    if isinstance(preferences, dict):
        kept = {}
        for name in ("effective_mode", "mode", "language"):
            value = preferences.get(name)
            if isinstance(value, str) and value:
                kept[name] = value
        if kept:
            result["preferences"] = kept
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
    # Espontâneo usa apenas os provedores primários. Reservas independentes
    # (Mistral/Cloudflare) ficam guardadas para menções e replies; portanto não
    # sortear uma resposta que o router já sabe que não pode atender.
    primary = [item for provider in ("groq", "gemini") for item in groups.get(provider, ())]
    if not primary and any(name in groups for name in ("mistral", "cloudflare")):
        # Reservas protegidas nunca justificam iniciar fala espontânea. Sem um
        # primário configurado, nem vale sortear/gerar um turno que o router
        # recusará por política.
        return 0.0
    if primary and not any(item.get("available") is True for item in primary):
        return 0.0
    known = [item for group in groups.values() for item in group if type(item.get("available")) is bool]
    if known and len(known) == sum(map(len, groups.values())) and not any(item["available"] for item in known):
        return 0.0
    primary_groups = {name: groups[name] for name in ("groq", "gemini") if name in groups}
    limited = sum(all(item.get("available") is False and item.get("cause_kind") == "rate_limit"
                      for item in group) for group in primary_groups.values())
    if limited and limited * 2 >= max(1, len(primary_groups)):
        return 0.25
    if primary_groups and any(not any(item.get("available") is True for item in group)
                              for group in primary_groups.values()):
        return 0.5
    return 1.0
