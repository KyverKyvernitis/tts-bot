"""Router HTTP leve com fallback real de texto e visão.

Estados são mantidos por provider/modelo, então um modelo removido não derruba
os demais. Há deadline total, respostas vazias são rejeitadas e o fallback
Gemini recebe os bytes das imagens — nunca afirma que viu algo que não recebeu.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field, replace
from copy import deepcopy
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp

from . import constants as C
from .action_protocol import (
    ChatReply, InvalidActionProposal, MAX_PROPOSALS, NativeToolCall, TOOL_NAME, enabled_actions, parse_proposal, parse_proposals,
    private_reply_text, proposal_tool,
)
from .tool_registry import InvalidToolArguments, ToolSpec, validate_tool_arguments
from .media import ImagePreparationError, MediaAttachment, PreparedImage, prepare_image_attachments

log = logging.getLogger(__name__)

# Apenas valores documentados entram no diagnóstico. Esse campo vem da API e
# não deve servir como caminho alternativo para registrar conteúdo da resposta.
_DIAGNOSTIC_FINISH_REASONS = frozenset({
    "stop", "length", "content_filter", "tool_calls", "function_call",
    "STOP", "MAX_TOKENS", "SAFETY", "RECITATION", "LANGUAGE", "OTHER",
    "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "MALFORMED_FUNCTION_CALL",
    "UNEXPECTED_TOOL_CALL", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT",
    "IMAGE_OTHER", "NO_IMAGE", "FINISH_REASON_UNSPECIFIED",
})
_INCOMPLETE_TOOL_FINISH_REASONS = frozenset({
    "length", "MAX_TOKENS", "MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL",
})
_DIAGNOSTIC_CODES = frozenset({
    "type", "enum", "required", "additional_properties", "max_length", "min_length", "null_character",
    "minimum", "maximum", "max_items", "min_items", "any_of", "max_depth", "argument_size", "serialization",
    "tool_name", "invalid_json", "duplicate_key", "invalid_constant", "target_reference", "forbidden_field",
    "max_proposals", "invalid_arguments", "invalid_schema", "invalid_call_id", "duplicate_call_id",
    "batch_limit", "incomplete_calls", "unexpected_calls", "invalid_envelope", "malformed_response",
    "api_tool_validation", "api_tool_schema", "api_tool_history", "api_context_limit",
    "api_parameter", "api_invalid_argument",
})
_ATTEMPT_USAGE: ContextVar[dict | None] = ContextVar("chatbot_provider_usage", default=None)
_ATTEMPT_REQUESTS: ContextVar[dict | None] = ContextVar("chatbot_provider_requests", default=None)
_REQUEST_REPORT: ContextVar[dict | None] = ContextVar("chatbot_provider_report", default=None)


def _record_http_request(*, discovery: bool = False) -> None:
    holder = _ATTEMPT_REQUESTS.get()
    if holder is not None:
        holder["count"] = holder.get("count", 0) + 1
    report = _REQUEST_REPORT.get()
    if report is not None:
        report["request_count"] = report.get("request_count", 0) + 1
        if discovery:
            report["discovery_request_count"] = report.get("discovery_request_count", 0) + 1


def _safe_usage(usage) -> dict:
    result = {}
    for key, value in (usage.items() if isinstance(usage, dict) else []):
        if key in {"input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "cached_tokens"}:
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000_000:
                result[key] = value
        elif key == "neurons" and isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(number) and 0 <= number <= 1_000_000_000:
                result[key] = number
    return result


def _record_usage(data, provider: str, model: str) -> dict:
    compatible = provider in {"groq", "cloudflare", "mistral"}
    source = data.get("usage" if compatible else "usageMetadata", {}) if isinstance(data, dict) else {}
    mapping = ({"prompt_tokens": "input_tokens", "completion_tokens": "output_tokens", "total_tokens": "total_tokens"}
               if compatible else {"promptTokenCount": "input_tokens", "candidatesTokenCount": "output_tokens",
                                          "totalTokenCount": "total_tokens", "thoughtsTokenCount": "reasoning_tokens",
                                          "cachedContentTokenCount": "cached_tokens"})
    raw_usage = {target: source[key] for key, target in mapping.items() if isinstance(source, dict) and key in source}
    if provider == "cloudflare" and isinstance(source, dict) and "neurons" in source:
        raw_usage["neurons"] = source["neurons"]
    usage = _safe_usage(raw_usage)
    if not compatible and "output_tokens" in usage and "reasoning_tokens" in usage:
        # Gemini separa candidates de thoughts; OpenAI inclui reasoning na
        # completion. Normalizar a saída total, mantendo total remoto intacto.
        usage["output_tokens"] += usage["reasoning_tokens"]
    if compatible and isinstance(source, dict):
        for nested, key, target in (("prompt_tokens_details", "cached_tokens", "cached_tokens"),
                                    ("completion_tokens_details", "reasoning_tokens", "reasoning_tokens")):
            detail = source.get(nested)
            if isinstance(detail, dict) and key in detail:
                usage.update(_safe_usage({target: detail[key]}))
    # Cache/raciocínio são partes dos totais informados, não novos tokens.
    subdivisions = (("cached_tokens", "input_tokens"), ("reasoning_tokens", "output_tokens")) if compatible else (("cached_tokens", "input_tokens"),)
    for detail, parent in subdivisions:
        if detail in usage and parent in usage and usage[detail] > usage[parent]:
            usage.pop(detail)
    holder = _ATTEMPT_USAGE.get()
    if holder is not None:
        holder.update(usage)
    if usage:
        log.info("chatbot: usage provider=%s model=%s input_tokens=%s output_tokens=%s total_tokens=%s reasoning_tokens=%s cached_tokens=%s neurons=%s",
                 provider, model, usage.get("input_tokens"), usage.get("output_tokens"), usage.get("total_tokens"),
                 usage.get("reasoning_tokens"), usage.get("cached_tokens"), usage.get("neurons"))
    return usage


def _safe_schema_path(path, schema) -> str:
    if path == "$":
        return "$"
    if not isinstance(path, str) or not path.startswith("$/") or len(path) > 200:
        return "$"
    node = schema
    for encoded in path[2:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict):
            return "$"
        if token == "*" and isinstance(node.get("items"), dict):
            node = node["items"]
        elif token in node.get("properties", {}):
            node = node["properties"][token]
        else:
            return "$"
    return path


def _diagnostic_finish_reason(reason: Optional[str]) -> str:
    if reason is None:
        return "none"
    return reason if isinstance(reason, str) and reason in _DIAGNOSTIC_FINISH_REASONS else "other"


class ProviderError(Exception):
    def __init__(
        self, message: str, *, status: Optional[int] = None,
        retry_after: Optional[float] = None, kind: Optional[str] = None,
        stage: str = "api", finish_reason: Optional[str] = None,
        quota_scope: str = "model", cause_kind: Optional[str] = None,
        diagnostic_code: str = "", diagnostic_path: str = "$", diagnostic_tool: str = "",
        diagnostic_index: Optional[int] = None, usage: Optional[dict] = None,
        causes: tuple[dict, ...] = (), earliest_retry_seconds: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = _bounded_retry(retry_after)
        self.stage = stage
        self.finish_reason = finish_reason
        self.kind = kind or (
            "auth" if status in (401, 403) else
            "rate_limit" if status == 429 else
            "model" if status in (400, 404, 422) else "network"
        )
        self.quota_scope = "account" if quota_scope == "account" else "model"
        self.cause_kind = cause_kind or self.kind
        self.diagnostic_code = diagnostic_code if diagnostic_code in _DIAGNOSTIC_CODES else ""
        self.diagnostic_path = diagnostic_path
        self.diagnostic_tool = diagnostic_tool
        self.diagnostic_index = diagnostic_index if isinstance(diagnostic_index, int) and 0 <= diagnostic_index <= 8 else None
        self.usage = _safe_usage(usage)
        self.causes = causes
        self.earliest_retry_seconds = earliest_retry_seconds


class RateLimitError(ProviderError):
    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("kind", "rate_limit")
        kwargs.setdefault("status", 429)
        super().__init__(message, **kwargs)


class AllProvidersExhausted(ProviderError):
    pass


@dataclass
class ChatMessage:
    role: str
    content: str
    image_urls: list[str] = field(default_factory=list)
    images: list[PreparedImage] = field(default_factory=list)
    tool_calls: tuple[NativeToolCall, ...] = ()
    tool_call_id: str = ""
    name: str = ""

    def to_openai_payload(self) -> dict:
        if self.role == "tool":
            payload = {"role": "tool", "content": self.content, "tool_call_id": self.tool_call_id}
            if self.name:
                payload["name"] = self.name
            return payload
        if self.tool_calls:
            return {"role": self.role, "content": self.content or None, "tool_calls": [
                {"id": call.id, "type": "function", "function": {"name": call.name,
                 "arguments": json.dumps(call.arguments, ensure_ascii=False, allow_nan=False)}}
                for call in self.tool_calls
            ]}
        if not self.images and not self.image_urls:
            return {"role": self.role, "content": self.content}
        blocks: list[dict] = [{"type": "text", "text": self.content}]
        if self.images:
            urls = [
                f"data:{image.mime_type};base64,{base64.b64encode(image.data).decode('ascii')}"
                for image in self.images[:C.MAX_IMAGES_PER_MESSAGE]
            ]
        else:
            urls = self.image_urls[:C.MAX_IMAGES_PER_MESSAGE]
        for url in urls:
            blocks.append({"type": "image_url", "image_url": {"url": url}})
        return {"role": self.role, "content": blocks}


@dataclass
class _ProviderState:
    next_allowed_monotonic: float = 0.0
    consecutive_failures: int = 0
    last_status: int = 0
    last_kind: str = ""
    last_stage: str = "api"
    quota_scope: str = "model"
    failure_generation: int = 0

    def is_available(self) -> bool:
        return time.monotonic() >= self.next_allowed_monotonic

    def mark_success(self, *, expected_generation: Optional[int] = None) -> bool:
        # Respostas simultâneas podem chegar fora de ordem. Um sucesso iniciado
        # antes de uma quota/auth/rede mais recente não reabre seu circuito.
        if expected_generation is not None and expected_generation != self.failure_generation:
            return False
        self.consecutive_failures = 0
        self.next_allowed_monotonic = 0.0
        self.last_status = 0
        self.last_kind = ""
        self.last_stage = "api"
        self.quota_scope = "model"
        return True

    def mark_failure(
        self, cooldown_seconds: float, *, status: int = 0,
        kind: str = "network", stage: str = "api", quota_scope: str = "model",
        explicit_retry: bool = False,
    ) -> None:
        self.consecutive_failures += 1
        self.failure_generation += 1
        factor = 1 if explicit_retry else 2 ** min(self.consecutive_failures - 1, 4)
        bound = _MAX_RETRY_SECONDS if explicit_retry else 900.0
        self.next_allowed_monotonic = time.monotonic() + min(bound, cooldown_seconds * factor)
        self.last_status = int(status)
        self.last_kind, self.last_stage = kind, stage
        self.quota_scope = quota_scope


# A API pode devolver um número arbitrário. Nunca agendar espera infinita ou
# negativa; um dia permite representar quotas diárias sem multiplicar RetryInfo.
_MAX_RETRY_SECONDS = 86400.0


def _bounded_retry(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(1.0, min(_MAX_RETRY_SECONDS, seconds)) if math.isfinite(seconds) and seconds >= 0 else None


def _retry_after(resp: aiohttp.ClientResponse) -> Optional[float]:
    raw = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
    seconds = _bounded_retry(raw)
    if seconds is not None:
        return seconds
    try:
        return _bounded_retry(parsedate_to_datetime(raw).timestamp() - time.time()) if raw else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderError("deadline total dos providers esgotado", kind="deadline", stage="routing")
    return min(C.PROVIDER_TIMEOUT_SECONDS, remaining)


async def _read_limited_bytes(
    response: aiohttp.ClientResponse, *, limit: int,
) -> bytes:
    raw_length = response.headers.get("Content-Length")
    try:
        declared = int(raw_length) if raw_length else 0
    except (TypeError, ValueError):
        declared = 0
    if declared > limit:
        raise ProviderError("resposta do provider excede o limite", kind="invalid_response", stage="output")
    body = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        body.extend(chunk)
        if len(body) > limit:
            raise ProviderError("resposta do provider excede o limite", kind="invalid_response", stage="output")
    return bytes(body)


async def _read_json_limited(response: aiohttp.ClientResponse):
    body = await _read_limited_bytes(
        response, limit=C.MAX_PROVIDER_RESPONSE_BYTES,
    )
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProviderError("provider retornou JSON inválido", kind="invalid_response", stage="output") from exc


def _retry_duration(value) -> Optional[float]:
    if isinstance(value, dict):
        if not any(key in value for key in ("seconds", "nanos")):
            return None
        try:
            return _bounded_retry(float(value.get("seconds", 0)) + float(value.get("nanos", 0)) / 1e9)
        except (TypeError, ValueError, OverflowError):
            return None
    if not isinstance(value, str) or len(value) > 80:
        return _bounded_retry(value)
    # RetryInfo usa protobuf Duration; Groq também publica resets como 1m2.5s.
    if not re.fullmatch(r"(?:\d+(?:\.\d+)?[hms])+", value):
        return None
    return _bounded_retry(sum(float(number) * {"h": 3600, "m": 60, "s": 1}[unit]
                              for number, unit in re.findall(r"(\d+(?:\.\d+)?)([hms])", value)))


def _quota_scope(error: dict) -> str:
    """Só evidência estruturada explícita pode abrir o circuito da conta."""
    details = error.get("details")
    details = details[:20] if isinstance(details, list) else []
    scope, code = error.get("quota_scope"), error.get("code")
    account = isinstance(scope, str) and scope in {"account", "organization", "project"}
    account |= isinstance(code, str) and code in {"account_quota_exceeded", "organization_quota_exceeded", "billing_hard_limit_reached"}
    for detail in details:
        if not isinstance(detail, dict) or not str(detail.get("@type", "")).endswith(".QuotaFailure"):
            continue
        violations = detail.get("violations")
        for violation in violations[:20] if isinstance(violations, list) else []:
            if not isinstance(violation, dict):
                continue
            dimensions = violation.get("quotaDimensions")
            dimensions = dimensions if isinstance(dimensions, dict) else {}
            quota_id = str(violation.get("quotaId", "")).lower()
            metric = str(violation.get("quotaMetric", "")).lower()
            if any("model" in str(key).lower() for key in dimensions) or "permodel" in quota_id or "per_model" in metric:
                continue
            # Um QuotaFailure identifica a dimensão efetivamente limitada.
            # Nome do projeto numa mensagem comum não prova alcance global.
            if (any(str(key).lower() in {"project", "organization", "account"} for key in dimensions)
                    or quota_id.endswith(("perproject", "perorganization", "peraccount"))):
                account = True
    return "account" if account else "model"


async def _http_error(response: aiohttp.ClientResponse, *, provider: str = "") -> ProviderError:
    """Extrai apenas categorias e esperas; nunca preserva corpo/identificadores."""
    body = bytearray()
    async for chunk in response.content.iter_chunked(1024):
        body.extend(chunk[:max(0, 8192 - len(body))])
        if len(body) >= 8192:
            break
    decoded = bytes(body).decode("utf-8", errors="replace")
    excerpt = decoded.lower()
    try:
        data = json.loads(decoded)
    except (ValueError, RecursionError):
        data = {}
    error = data.get("error", {}) if isinstance(data, dict) else {}
    error = error if isinstance(error, dict) else {}
    cloudflare_errors = data.get("errors") if provider == "cloudflare" and isinstance(data, dict) else None
    cloudflare_errors = [item for item in cloudflare_errors[:20] if isinstance(item, dict)] if isinstance(cloudflare_errors, list) else []
    if not error and cloudflare_errors:
        error = cloudflare_errors[0]
    status = response.status
    # Failed generations may contain arbitrary conversation text. They never
    # participate in error classification or get copied into repair feedback.
    if error:
        excerpt = " ".join(value.lower() for key in ("message", "code", "status")
                           if isinstance((value := error.get(key)), str))
    # A franquia de neurons é diária e compartilhada pela conta. Sem essa
    # evidência explícita, um 429 comum continua sendo uma espera temporária.
    daily_neurons = provider == "cloudflare" and any(
        (item.get("quota_period") in ("day", "daily") and item.get("quota_unit") == "neurons") or
        (isinstance(item.get("message"), str) and "daily" in item["message"].lower()
         and "neuron" in item["message"].lower()
         and any(word in item["message"].lower() for word in ("limit", "exceed", "reached")))
        for item in [error, *cloudflare_errors]
    )
    if status == 429 or daily_neurons:
        waits = [_retry_after(response), _bounded_retry(error.get("retry_after"))]
        details = error.get("details")
        for detail in details[:20] if isinstance(details, list) else []:
            if isinstance(detail, dict) and str(detail.get("@type", "")).endswith(".RetryInfo"):
                waits.append(_retry_duration(detail.get("retryDelay")))
        message = error.get("message")
        if isinstance(message, str):
            hint = re.search(r"try again in\s+((?:\d+(?:\.\d+)?[hms])+)", message[:4096], re.I)
            if hint:
                waits.append(_retry_duration(hint.group(1)))
        explicit_waits = [wait for wait in waits if wait is not None]
        if daily_neurons and not explicit_waits:
            waits.append(_bounded_retry(86400 - (time.time() % 86400)))
            explicit_waits = [wait for wait in waits if wait is not None]
        if not explicit_waits:
            # Um bucket saudável pode ter reset longo. Somente o bucket
            # comprovadamente esgotado é uma pista útil, e nunca prevalece
            # sobre RetryAfter/RetryInfo ou a espera explícita da API.
            for bucket in ("requests", "tokens"):
                raw = response.headers.get(f"x-ratelimit-remaining-{bucket}")
                try:
                    remaining = float(raw)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(remaining) and remaining == 0:
                    waits.append(_retry_duration(response.headers.get(f"x-ratelimit-reset-{bucket}")))
        return RateLimitError(
            "provider atingiu um limite temporário", status=status, retry_after=max((wait for wait in waits if wait is not None), default=None),
            quota_scope="account" if daily_neurons else _quota_scope(error),
        )
    auth_parts = [error.get("message"), error.get("code"), error.get("status")]
    details = error.get("details")
    for detail in details[:20] if isinstance(details, list) else []:
        if isinstance(detail, dict) and str(detail.get("@type", "")).endswith(".ErrorInfo"):
            auth_parts.append(detail.get("reason"))
    auth_excerpt = " ".join(part.lower() for part in auth_parts if isinstance(part, str))
    if not error:
        auth_excerpt = excerpt
    if status == 401 or (status in (400, 403) and any(marker in auth_excerpt for marker in (
        "invalid_api_key", "api_key_invalid", "invalid api key", "api key not valid",
        "api key expired", "authentication_error",
    ))):
        kind = "auth"
    elif status in (400, 422) and error.get("code") == "tool_use_failed":
        # Groq rejects a generated tool batch before returning choices. The
        # existing one-repair budget applies; no rejected call is executed.
        return ProviderError("provider gerou chamadas inválidas", status=status,
                             kind="invalid_response", stage="output",
                             diagnostic_code="api_tool_validation")
    elif status >= 500:
        kind = "network"
    elif any(marker in excerpt for marker in ("content_filter", "safety blocked", "prohibited_content")):
        kind = "blocked"
    elif status in (400, 422) and any(marker in excerpt for marker in (
        "does not support tool", "tools are not supported", "tool calling is not supported",
        "function calling is not supported", "tool use is not supported",
        "does not support function calling", "does not support function declarations",
    )):
        kind = "tools_unsupported"
    elif status == 404 or any(marker in excerpt for marker in (
        "model_not_found", "model_decommissioned", "model has been decommissioned",
        "not a valid model", "model is not supported", "unknown model",
        "does not support image", "does not support vision",
        "model_permission_denied", "model_access_denied", "model_access_restricted",
        "does not have access to model", "does not have access to the model",
        "not allowed to access model",
    )):
        kind = "model"
    elif status == 403:
        kind = "auth"
    else:
        kind = "request"
    diagnostic = ""
    if kind == "request" and status in (400, 422):
        if any(marker in excerpt for marker in (
            "tool_call_id", "tool call id", "tool responses", "tool response",
            "function response", "functionresponse", "function_response", "function call turn",
            "function call and function response", "missing tool result",
            "thought signature", "thought_signature", "thoughtsignature",
        )):
            diagnostic = "api_tool_history"
        elif any(marker in excerpt for marker in (
            "context_length_exceeded", "maximum context", "context window",
            "context length", "too many tokens", "input token limit", "input token count exceeds",
        )):
            diagnostic = "api_context_limit"
        elif any(marker in excerpt for marker in (
            "function_declarations", "functiondeclarations", "parameters.properties",
            "properties: should be non-empty", "properties must be non-empty",
            "invalid schema", "invalid json schema", "parameters schema",
        )):
            diagnostic = "api_tool_schema"
        elif any(marker in excerpt for marker in (
            "unsupported parameter", "unknown parameter", "unrecognized parameter",
            "unknown name", "reasoning_effort", "include_reasoning", "thinkingbudget",
            "thinking_budget", "max_completion_tokens", "maxoutputtokens",
        )):
            diagnostic = "api_parameter"
        else:
            diagnostic = "api_invalid_argument"
    return ProviderError("provider rejeitou a requisição", status=status, kind=kind,
                         diagnostic_code=diagnostic)


def _output_tokens(
    messages: list[ChatMessage], *, actions: tuple[str, ...] = (),
    has_tools: bool = False, allow_tool_calls: bool = True,
) -> int:
    has_images = any(message.images or message.image_urls for message in messages)
    if has_images:
        tokens = getattr(C, "MAX_VISION_RESPONSE_TOKENS", C.MAX_RESPONSE_TOKENS)
    else:
        # A maioria das conversas do Discord é curta. Reservar sempre o teto
        # máximo aumenta saída potencial e, no Workers AI, neurons de reserva.
        latest = next((message.content for message in reversed(messages)
                       if message.role == "user" and isinstance(message.content, str)), "")
        visible = len(latest.strip())
        if visible <= 180:
            tokens = getattr(C, "MIN_RESPONSE_TOKENS", 220)
        elif visible <= 700:
            tokens = getattr(C, "SHORT_RESPONSE_TOKENS", 320)
        else:
            tokens = C.MAX_RESPONSE_TOKENS
    # Propostas de ação preservam o teto antigo porque podem conter várias
    # etapas. Ferramentas de leitura precisam de espaço para JSON nativo; no
    # fechamento sem novas tools, 500 tokens bastam sem reabrir o teto de 768.
    if TOOL_NAME in actions or enabled_actions(actions):
        return max(tokens, C.MAX_ACTION_RESPONSE_TOKENS)
    if has_tools:
        return max(tokens, C.MAX_TOOL_RESPONSE_TOKENS if allow_tool_calls else C.MAX_RESPONSE_TOKENS)
    return tokens


def _action_reply(
    text: str, calls: list[tuple[object, object]], actions: tuple[str, ...],
    *, provider: str, model: str, finish_reason: Optional[str],
) -> ChatReply:
    if calls and finish_reason in _INCOMPLETE_TOOL_FINISH_REASONS:
        # Um prefixo JSON válido não prova que toda a sequência foi recebida.
        # Nunca preparar uma cadeia parcial porque a API truncou sua saída.
        raise ProviderError(
            "provider truncou as propostas", kind="invalid_response", stage="output",
            finish_reason=finish_reason,
            diagnostic_code="incomplete_calls",
        )
    try:
        proposals = parse_proposals(calls, actions)
    except InvalidActionProposal as exc:
        # Um lote inválido pode misturar pedido privado com outra chamada
        # malformada. Descartar também o texto público, que pode antecipar a fala.
        raise ProviderError(
            "provider retornou proposta inválida", kind="invalid_response",
            stage="output", finish_reason=finish_reason,
            diagnostic_code=getattr(exc, "code", "invalid_arguments"),
            diagnostic_path=_safe_schema_path(getattr(exc, "path", "$"), proposal_tool(actions)["parameters"]),
            diagnostic_tool=TOOL_NAME if any(name == TOOL_NAME for name, _arguments in calls) else "",
        ) from exc
    if not text and not proposals:
        raise ProviderError(
            "provider retornou resposta vazia", kind="empty", stage="output",
            finish_reason=finish_reason,
        )
    return ChatReply(private_reply_text(text, proposals), proposals, provider, model)


def _native_reply(text, raw_calls, specs, actions, *, provider, model, finish_reason):
    """Um lote estritamente validado; nenhuma ferramenta é executada aqui."""
    if len(raw_calls) > getattr(C, "MAX_TOOL_CALLS", 8) or (raw_calls and finish_reason in _INCOMPLETE_TOOL_FINISH_REASONS):
        raise ProviderError("provider retornou ferramentas incompletas", kind="invalid_response", stage="output", finish_reason=finish_reason,
                            diagnostic_code="batch_limit" if len(raw_calls) > getattr(C, "MAX_TOOL_CALLS", 8) else "incomplete_calls")
    by_name = {spec.name: spec for spec in specs}
    calls, proposals, ids = [], [], set()
    index, name, schema = 0, "", {}
    try:
        for index, (call_id, name, arguments, opaque) in enumerate(raw_calls):
            spec = by_name.get(name) if isinstance(name, str) else None
            if spec is None:
                raise ProviderError("provider retornou ferramenta não declarada", kind="invalid_response", stage="output",
                                    finish_reason=finish_reason, diagnostic_code="tool_name" if isinstance(name, str) else "invalid_envelope", diagnostic_index=index)
            schema = spec.parameters
            if isinstance(arguments, str):
                if len(arguments.encode("utf-8")) > 8192:
                    raise ProviderError("argumentos excedem o contrato", kind="invalid_response", stage="output", finish_reason=finish_reason,
                                        diagnostic_code="argument_size", diagnostic_tool=name, diagnostic_index=index)
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ProviderError("argumentos JSON inválidos", kind="invalid_response", stage="output", finish_reason=finish_reason,
                                                diagnostic_code="duplicate_key", diagnostic_tool=name, diagnostic_index=index)
                        result[key] = value
                    return result
                def constant(value):
                    raise ProviderError("argumentos JSON inválidos", kind="invalid_response", stage="output", finish_reason=finish_reason,
                                        diagnostic_code="invalid_constant", diagnostic_tool=name, diagnostic_index=index)
                arguments = json.loads(arguments, object_pairs_hook=unique, parse_constant=constant)
            if name == TOOL_NAME:
                # Validação do envelope inteiro antes de separar campos. O
                # schema legado aceita ask_permission, mas o host o ignora.
                schema = deepcopy(spec.parameters)
                schema.setdefault("properties", {})["ask_permission"] = {"type": "boolean"}
                arguments = validate_tool_arguments(arguments, schema)
                allowed = tuple(spec.parameters.get("properties", {}).get("action", {}).get("enum", ())) or actions
                proposal = parse_proposal(name, arguments, allowed)
                proposals.append(proposal)
                arguments.pop("ask_permission", None)
                if len(proposals) > MAX_PROPOSALS:
                    raise ProviderError("lote excede o contrato", kind="invalid_response", stage="output", finish_reason=finish_reason,
                                        diagnostic_code="max_proposals", diagnostic_tool=name, diagnostic_index=index)
            else:
                arguments = validate_tool_arguments(arguments, spec.parameters)
            call_id = f"call_{uuid4().hex}" if call_id is None else call_id
            if not isinstance(call_id, str) or not call_id or len(call_id) > 200 or call_id in ids:
                raise ProviderError("identificação da chamada inválida", kind="invalid_response", stage="output", finish_reason=finish_reason,
                                    diagnostic_code="duplicate_call_id" if isinstance(call_id, str) and call_id in ids else "invalid_call_id",
                                    diagnostic_tool=name, diagnostic_index=index)
            ids.add(call_id)
            calls.append(NativeToolCall(call_id, name, arguments, deepcopy(opaque)))
    except (InvalidToolArguments, InvalidActionProposal, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        code = "invalid_json" if isinstance(exc, json.JSONDecodeError) else getattr(exc, "code", "invalid_arguments")
        tool = name if isinstance(name, str) and name in by_name else ""
        raise ProviderError("provider retornou ferramentas inválidas", kind="invalid_response", stage="output", finish_reason=finish_reason,
                            diagnostic_code=code, diagnostic_path=_safe_schema_path(getattr(exc, "path", "$"), schema),
                            diagnostic_tool=tool, diagnostic_index=index) from exc
    if not text and not calls:
        raise ProviderError("provider retornou resposta vazia", kind="empty", stage="output", finish_reason=finish_reason)
    return ChatReply(private_reply_text(text, tuple(proposals)), tuple(proposals), provider, model, tuple(calls))


def _tool_declarations(actions, target_refs, tool_specs):
    specs = tuple(spec for spec in tool_specs if spec.available)
    declarations = [spec.native_declaration() for spec in specs]
    if actions and not any(spec.name == TOOL_NAME for spec in specs):
        declaration = proposal_tool(actions, target_refs)
        declarations.append(declaration)
        specs += (ToolSpec(declaration["name"], declaration["description"], declaration["parameters"], permission="staff/automatic"),)
    return specs, declarations


def _gemini_schema(schema: dict) -> dict:
    """Exporta o Schema OpenAPI da API; o original continua validando no host."""
    supported = {"type", "format", "title", "description", "nullable", "enum", "items", "minItems", "maxItems",
                 "properties", "required", "minProperties", "maxProperties", "minimum", "maximum", "anyOf",
                 "propertyOrdering", "default", "example"}
    result = {key: deepcopy(value) for key, value in schema.items() if key in supported}
    bounds = []
    for key, label in (("minLength", "mínimo de caracteres"), ("maxLength", "máximo de caracteres")):
        if key in schema:
            bounds.append(f"{label}: {schema[key]}")
    if bounds:
        result["description"] = (str(result.get("description", "")) + " " + "; ".join(bounds) + ".").strip()
    if isinstance(result.get("properties"), dict):
        result["properties"] = {name: _gemini_schema(child) for name, child in result["properties"].items()}
    if isinstance(result.get("items"), dict):
        result["items"] = _gemini_schema(result["items"])
    if isinstance(result.get("anyOf"), list):
        result["anyOf"] = [_gemini_schema(child) for child in result["anyOf"]]
    return result


class _GroqClient:
    BASE_URL = "https://api.groq.com/openai/v1/chat/completions"
    PROVIDER = "groq"
    LABEL = "Groq"
    MAX_TOKENS_KEY = "max_completion_tokens"

    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self._session = session
        self._api_key = api_key

    def _validate_request(self, model, messages) -> None:
        pass

    def _prepare_payload(self, payload: dict) -> None:
        pass

    def _reserve_request(self, payload: dict):
        return None

    def _settle_request(self, reservation, usage: dict) -> None:
        pass

    def _visible_content(self, content):
        return content

    async def chat(
        self, *, system: str, messages: list[ChatMessage], temperature: float,
        model: str, timeout_seconds: float, actions: tuple[str, ...] = (),
        target_refs: tuple[str, ...] = (),
        tool_specs: tuple[ToolSpec, ...] = (),
        allow_tool_calls: bool = True,
    ) -> str | ChatReply:
        self._validate_request(model, messages)
        actions = enabled_actions(actions)
        specs, declarations = _tool_declarations(actions, target_refs, tool_specs)
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}]
            + [message.to_openai_payload() for message in messages],
            "temperature": max(C.MIN_TEMPERATURE, min(C.MAX_TEMPERATURE, temperature)),
            self.MAX_TOKENS_KEY: _output_tokens(
                messages, actions=actions, has_tools=bool(specs), allow_tool_calls=allow_tool_calls,
            ),
            "stream": False,
        }
        if declarations:
            payload["tools"] = [{"type": "function", "function": declaration} for declaration in declarations]
            payload["tool_choice"] = "auto" if allow_tool_calls else "none"
        # Evita gastar tokens de raciocínio oculto em conversa casual.
        if self.PROVIDER == "groq" and model in {"openai/gpt-oss-120b", "openai/gpt-oss-20b"}:
            payload.update({"reasoning_effort": "low", "include_reasoning": False})
        elif self.PROVIDER == "groq" and model == "qwen/qwen3.8-27b":
            # Valores documentados pelo Groq para este modelo específico.
            payload.update({"reasoning_effort": "none", "include_reasoning": False})
        self._prepare_payload(payload)
        reservation = self._reserve_request(payload)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=max(0.001, timeout_seconds))
        try:
            _record_http_request()
            async with self._session.post(
                self.BASE_URL, json=payload, headers=headers, timeout=timeout,
            ) as resp:
                if resp.status >= 400:
                    raise await _http_error(resp, provider=self.PROVIDER)
                data = await _read_json_limited(resp)
        except asyncio.TimeoutError as exc:
            raise ProviderError(f"{self.LABEL} timeout", kind="timeout") from exc
        except aiohttp.ClientError as exc:
            raise ProviderError(f"{self.LABEL} erro de rede", kind="network") from exc
        usage = _record_usage(data, self.PROVIDER, model)
        self._settle_request(reservation, usage)
        try:
            choice = data["choices"][0]
            finish_reason = choice.get("finish_reason")
            message = choice["message"]
            if finish_reason == "content_filter":
                raise ProviderError(
                    "resposta bloqueada pelo provider", kind="blocked", stage="output",
                    finish_reason=finish_reason,
                )
            content = message.get("content")
            refusal_present = bool(message.get("refusal"))
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") for part in content
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                )
            # Alguns endpoints colocam a recusa textual num campo separado.
            # Ela continua sendo uma resposta válida, sem troca para outro modelo.
            if not (isinstance(content, str) and content.strip()):
                refusal = message.get("refusal")
                if isinstance(refusal, str) and refusal.strip():
                    content = refusal
                elif refusal:
                    raise ProviderError("resposta bloqueada pelo provider", kind="blocked", stage="output")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError(f"{self.LABEL} resposta malformada", kind="invalid_response", stage="output") from exc
        content = self._visible_content(content)
        reply = content.strip() if isinstance(content, str) else ""
        if not allow_tool_calls and message.get("tool_calls"):
            raise ProviderError("provider ignorou o fechamento sem ferramentas", kind="invalid_response", stage="output", finish_reason=finish_reason,
                                diagnostic_code="unexpected_calls")
        if tool_specs:
            native_calls = []
            raw_calls = message.get("tool_calls") or []
            if not isinstance(raw_calls, list):
                raw_calls = [None]
            for call in raw_calls if not refusal_present else []:
                function = call.get("function") if isinstance(call, dict) else None
                if not isinstance(function, dict) or call.get("type", "function") != "function":
                    native_calls.append((None, None, None, {}))
                else:
                    native_calls.append((call.get("id"), function.get("name"), function.get("arguments"), {}))
                if len(native_calls) > getattr(C, "MAX_TOOL_CALLS", 8):
                    break
            return _native_reply(reply, native_calls, specs, actions, provider=self.PROVIDER, model=model, finish_reason=finish_reason)
        if actions:
            calls = []
            raw_calls = message.get("tool_calls") or []
            # Uma recusa explícita é terminal e não carrega ações anexas.
            if not refusal_present:
                if not isinstance(raw_calls, list):
                    raw_calls = [None]
                for call in raw_calls:
                    function = call.get("function") if isinstance(call, dict) else None
                    if not isinstance(function, dict) or call.get("type", "function") != "function":
                        calls.append((None, None))
                    else:
                        calls.append((function.get("name"), function.get("arguments")))
                    if len(calls) > MAX_PROPOSALS:
                        break
            return _action_reply(
                reply, calls, actions, provider=self.PROVIDER, model=model, finish_reason=finish_reason,
            )
        if not reply:
            raise ProviderError(
                f"{self.LABEL} retornou resposta vazia", kind="empty", stage="output",
                finish_reason=finish_reason,
            )
        return reply


class _MistralClient(_GroqClient):
    """Chat Completions oficial da Mistral, texto + ferramentas nativas."""

    BASE_URL = "https://api.mistral.ai/v1/chat/completions"
    PROVIDER = "mistral"
    LABEL = "Mistral"
    MAX_TOKENS_KEY = "max_tokens"

    def _validate_request(self, model, messages) -> None:
        if model not in C.MISTRAL_MODELS:
            raise ProviderError("modelo Mistral não permitido", kind="model", stage="routing")
        if any(message.images or message.image_urls for message in messages):
            raise ProviderError("reserva Mistral aceita apenas texto", kind="model", stage="routing")

    def _prepare_payload(self, payload: dict) -> None:
        # Mistral Small 4 permite desligar a exposição/uso de raciocínio
        # desnecessário para conversa cotidiana e tool routing simples.
        payload["reasoning_effort"] = "none"
        # A chave não contém IDs nem conteúdo do usuário; deriva apenas do
        # prefixo system exato para favorecer o prompt cache do provedor.
        system = str(payload.get("messages", [{}])[0].get("content", ""))
        digest = hashlib.sha256(system.encode("utf-8", "surrogatepass")).hexdigest()[:24]
        payload["prompt_cache_key"] = "chatbot-system-" + digest


class _CloudflareClient(_GroqClient):
    """Workers AI direto, apenas Qwen de texto e sem rotas pagas adicionais."""

    PROVIDER = "cloudflare"
    LABEL = "Cloudflare"
    MAX_TOKENS_KEY = "max_tokens"
    _INPUT_NEURONS_PER_TOKEN = 0.004625
    _OUTPUT_NEURONS_PER_TOKEN = 0.030475

    def __init__(self, session, api_key: str, account_id: str):
        if not isinstance(account_id, str) or not re.fullmatch(r"[A-Fa-f0-9]{32}", account_id):
            raise ValueError("Cloudflare Account ID inválido")
        super().__init__(session, api_key)
        self.BASE_URL = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions"
        self._budget_day = int(time.time() // 86400)
        self._budget_spent = 0.0

    def _validate_request(self, model, messages) -> None:
        if model not in C.CLOUDFLARE_MODELS:
            raise ProviderError("modelo Cloudflare não permitido", kind="model", stage="routing")
        if any(message.images or message.image_urls for message in messages):
            raise ProviderError("Cloudflare Qwen aceita apenas texto", kind="model", stage="routing")

    def _prepare_payload(self, payload: dict) -> None:
        # /no_think é a instrução leve documentada pelo Qwen. O endpoint não
        # documenta um hard switch: não enviar enable_thinking/extra_body.
        payload["messages"][0]["content"] += "\n/no_think"

    def _rotate_budget(self) -> None:
        day = int(time.time() // 86400)
        if day != self._budget_day:
            self._budget_day, self._budget_spent = day, 0.0

    def budget_diagnostics(self) -> dict:
        self._rotate_budget()
        return {"limit_neurons": C.CLOUDFLARE_DAILY_NEURON_BUDGET,
                "reserved_or_used_neurons": round(self._budget_spent, 3),
                "remaining_neurons": round(max(0.0, C.CLOUDFLARE_DAILY_NEURON_BUDGET - self._budget_spent), 3),
                "scope": "process_daily", "estimated": True,
                "reset_seconds": max(1.0, 86400 - (time.time() % 86400))}

    def _reserve_request(self, payload: dict):
        self._rotate_budget()
        # Reserva conservadora por bytes UTF-8 + overhead do template. É uma
        # proteção local por processo, não uma leitura da franquia da conta.
        serialized = json.dumps({"messages": payload["messages"], "tools": payload.get("tools", [])},
                                ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        input_upper = len(serialized) + 512 + 64 * (len(payload["messages"]) + len(payload.get("tools", [])))
        reservation = (input_upper * self._INPUT_NEURONS_PER_TOKEN
                       + payload["max_tokens"] * self._OUTPUT_NEURONS_PER_TOKEN)
        if self._budget_spent + reservation > C.CLOUDFLARE_DAILY_NEURON_BUDGET:
            raise RateLimitError("orçamento local diário Cloudflare esgotado", stage="routing", quota_scope="account",
                                 retry_after=86400 - (time.time() % 86400))
        # Sem awaits entre conferir e reservar: duas conversas não passam
        # simultaneamente pela mesma franquia disponível deste processo.
        self._budget_spent += reservation
        return self._budget_day, reservation

    def _settle_request(self, reservation, usage: dict) -> None:
        if not reservation or reservation[0] != self._budget_day:
            return
        reported = usage.get("neurons") if isinstance(usage, dict) else None
        if isinstance(reported, (int, float)) and not isinstance(reported, bool) and math.isfinite(reported) and reported >= 0:
            actual = float(reported)
        elif {"input_tokens", "output_tokens"} <= usage.keys():
            actual = (usage["input_tokens"] * self._INPUT_NEURONS_PER_TOKEN
                      + usage["output_tokens"] * self._OUTPUT_NEURONS_PER_TOKEN)
        else:
            return  # Sem medição, manter reserva; timeout pode ter consumido.
        self._budget_spent = max(0.0, self._budget_spent - reservation[1] + actual)

    def _visible_content(self, content):
        if not isinstance(content, str) or not content.lstrip().startswith("<think>"):
            return content
        content = content.lstrip()
        end = content.find("</think>")
        # Raciocínio incompleto nunca vira texto público nem histórico.
        return content[end + len("</think>"):].lstrip() if end >= 0 else ""


class _GeminiClient:
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    CATALOG_URL = "https://generativelanguage.googleapis.com/v1beta/models"
    _ALLOWED_IMAGE_HOST_SUFFIXES = (
        ".discordapp.com", ".discordapp.net", ".discord.com",
    )

    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self._session = session
        self._api_key = api_key
        self._catalog: tuple[tuple[str, bool], ...] = ()
        self._catalog_expires = 0.0
        self._catalog_task: asyncio.Task | None = None
        self._catalog_status: dict = {}

    def catalog_models(self, *, has_images: bool = False) -> tuple[str, ...]:
        if time.monotonic() >= self._catalog_expires:
            return ()
        return tuple(name for name, vision in self._catalog if vision or not has_images)[:8]

    def discovery_diagnostics(self) -> dict:
        return {**self._catalog_status, "available_count": len(self.catalog_models()),
                "cache_seconds": max(0.0, self._catalog_expires - time.monotonic())}

    @staticmethod
    def _catalog_entry(record) -> tuple[str, bool] | None:
        if not isinstance(record, dict) or not isinstance(record.get("supportedGenerationMethods"), list) or "generateContent" not in record["supportedGenerationMethods"]:
            return None
        name = record.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"models/(gemini-[A-Za-z0-9._-]{1,100})", name, re.ASCII):
            return None
        model = name[7:]
        if re.search(r"(?:^|-)(?:image|tts|audio|live|embedding|deep-research|computer-use|robotics)(?:-|$)", model, re.I):
            return None
        # listModels não declara suporte a ferramentas. Isso será verificado
        # pela própria requisição nativa, nunca presumido a partir do nome.
        modalities = record.get("inputModalities")
        if isinstance(modalities, list):
            vision = "IMAGE" in modalities
            if "TEXT" not in modalities:
                return None
        else:
            vision = bool(re.match(r"gemini-(?:1\.5|[2-9](?:[.-]|$))", model))
        return model, vision

    async def _fetch_catalog(self, timeout_seconds: float) -> tuple[tuple[str, bool], ...]:
        entries = {}
        seen, pages, truncated = 0, 0, False
        started = time.monotonic()
        deadline = time.monotonic() + min(3.0, timeout_seconds)
        async def read_pages():
            nonlocal seen, pages, truncated
            getter = getattr(self._session, "get", None)
            if not callable(getter):
                raise ProviderError("descoberta não disponível", kind="discovery_unavailable", stage="discovery")
            token, tokens = "", set()
            for _page in range(2):
                params = {"pageSize": 50}
                if token:
                    params["pageToken"] = token
                _record_http_request(discovery=True)
                async with getter(self.CATALOG_URL, params=params, headers={"x-goog-api-key": self._api_key},
                                  timeout=aiohttp.ClientTimeout(total=_remaining(deadline))) as response:
                    if response.status >= 400:
                        raise await _http_error(response)
                    data = await _read_json_limited(response)
                rows = data.get("models") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    raise ProviderError("catálogo inválido", kind="invalid_response", stage="discovery")
                seen += min(50, len(rows))
                pages += 1
                for row in rows[:50]:
                    entry = self._catalog_entry(row)
                    if entry is not None:
                        entries[entry[0]] = entry
                token = data.get("nextPageToken")
                valid_token = isinstance(token, str) and 0 < len(token) <= 512 and token.isascii() and all(32 <= ord(char) < 127 for char in token)
                truncated = len(rows) > 50 or bool(token)
                if not valid_token or token in tokens:
                    break
                tokens.add(token)
        try:
            await asyncio.wait_for(read_pages(), timeout=max(.001, deadline - time.monotonic()))
            delay, kind, status = 600.0, "success", 200
            retry = None
            scope = "model"
        except Exception as exc:
            kind = (exc.kind if isinstance(exc, ProviderError) else "timeout" if isinstance(exc, asyncio.TimeoutError)
                    else "network" if isinstance(exc, aiohttp.ClientError) else "invalid_response")
            retry = exc.retry_after if isinstance(exc, ProviderError) else None
            delay = retry or (900.0 if kind == "auth" else 60.0)
            status = int(getattr(exc, "status", 0) or 0)
            scope = exc.quota_scope if isinstance(exc, ProviderError) else "model"
            if kind == "auth":
                entries.clear()
        def rank(entry):
            name = entry[0]
            version = re.match(r"gemini-(\d+)(?:\.(\d+))?", name)
            major, minor = (int(version[1]), int(version[2] or 0)) if version else (0, 0)
            family = 1 if "flash-lite" in name else 0 if "flash" in name else 2 if "pro" in name else 3
            return family, bool(re.search(r"(?:preview|exp)(?:-|$)", name)), -major, -minor, name
        ordered = sorted(entries.values(), key=rank)
        # Não expulsar modelos com imagem só porque a família Flash text-only
        # veio primeiro. Cada capacidade conserva até oito candidatos seguros.
        selected = {entry[0] for entry in ordered[:8]} | {entry[0] for entry in [item for item in ordered if item[1]][:8]}
        self._catalog = tuple(entry for entry in ordered if entry[0] in selected)
        self._catalog_expires = time.monotonic() + delay
        self._catalog_status = {"kind": kind, "status": status, "retry_after": retry,
                                "quota_scope": scope, "pages": pages, "rows_seen": seen, "truncated": truncated}
        log.info("chatbot: model_discovery provider=gemini kind=%s status=%s pages=%d candidates=%d truncated=%s elapsed_ms=%d quota_scope=%s retry_after=%s",
                 kind, status, pages, len(self._catalog), truncated, max(0, int((time.monotonic() - started) * 1000)), scope, retry)
        return self._catalog

    async def discover_models(self, *, timeout_seconds: float, has_images: bool = False) -> tuple[str, ...]:
        if time.monotonic() < self._catalog_expires:
            return self.catalog_models(has_images=has_images)
        if timeout_seconds <= 0:
            return ()
        if self._catalog_task is None or self._catalog_task.done():
            self._catalog_task = asyncio.create_task(self._fetch_catalog(min(3.0, timeout_seconds)))
        try:
            await asyncio.wait_for(asyncio.shield(self._catalog_task), timeout=min(3.0, timeout_seconds))
        except asyncio.TimeoutError:
            return ()
        return self.catalog_models(has_images=has_images)

    async def _download_inline_image(self, url: str, deadline: float) -> dict:
        """Compatibilidade para chamadores antigos; falha do CDN nunca é credencial."""
        filename = urlsplit(url).path.rsplit("/", 1)[-1] or "image"
        try:
            images = await prepare_image_attachments(
                self._session, [MediaAttachment(url, filename, "", 0, "image")],
                timeout_seconds=max(.01, deadline - time.monotonic()),
            )
        except ImagePreparationError as exc:
            raise ProviderError(str(exc), kind=exc.kind, stage=exc.stage, status=exc.status) from exc
        return self._image_part(images[0])

    @staticmethod
    def _image_part(image: PreparedImage) -> dict:
        return {"inlineData": {
            "mimeType": image.mime_type,
            "data": base64.b64encode(image.data).decode("ascii"),
        }}

    async def chat(
        self, *, system: str, messages: list[ChatMessage], temperature: float,
        model: str, timeout_seconds: float, actions: tuple[str, ...] = (),
        target_refs: tuple[str, ...] = (),
        tool_specs: tuple[ToolSpec, ...] = (),
        allow_tool_calls: bool = True,
    ) -> str | ChatReply:
        actions = enabled_actions(actions)
        specs, declarations = _tool_declarations(actions, target_refs, tool_specs)
        deadline = time.monotonic() + max(0.001, timeout_seconds)
        contents: list[dict] = []
        prior_calls = {call.id: call for message in messages for call in message.tool_calls}
        for message in messages:
            if message.role == "tool":
                try:
                    result = json.loads(message.content)
                except (ValueError, TypeError):
                    result = {"result": message.content}
                if not isinstance(result, dict):
                    result = {"result": result}
                part = {"functionResponse": {"name": message.name, "response": result}}
                prior = prior_calls.get(message.tool_call_id)
                native_id = (prior.provider_data.get("part", {}).get("functionCall", {}).get("id") if prior else None)
                if native_id:
                    part["functionResponse"]["id"] = native_id
                if contents and contents[-1]["role"] == "user":
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
                continue
            parts: list[dict] = [{"text": message.content}]
            if message.tool_calls:
                parts = ([{"text": message.content}] if message.content else [])
                for call in message.tool_calls:
                    parts.append(call.provider_data.get("part") or {"functionCall": {"name": call.name, "args": call.arguments}})
            if message.images:
                parts.extend(self._image_part(image) for image in message.images[:C.MAX_IMAGES_PER_MESSAGE])
            else:
                for url in message.image_urls[:C.MAX_IMAGES_PER_MESSAGE]:
                    parts.append(await self._download_inline_image(url, deadline))
            role = "user" if message.role == "user" else "model"
            # Contexto citado e mensagem atual podem ser blocos user seguidos.
            # Preservar cada parte na ordem original, sem atravessar uma resposta.
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": parts})
        payload = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {
                "temperature": max(C.MIN_TEMPERATURE, min(C.MAX_TEMPERATURE, temperature)),
                "maxOutputTokens": _output_tokens(
                    messages, actions=actions, has_tools=bool(specs), allow_tool_calls=allow_tool_calls,
                ),
            },
        }
        if declarations:
            for tool in declarations:
                schema = tool["parameters"]
                # FunctionDeclaration.parameters is optional. Omit it for a
                # function with no arguments instead of sending an empty
                # OpenAPI OBJECT; the original strict schema stays on host.
                if schema.get("type") == "object" and not schema.get("properties") and not schema.get("required"):
                    tool.pop("parameters")
                else:
                    tool["parameters"] = _gemini_schema(schema)
            payload["tools"] = [{"functionDeclarations": declarations}]
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO" if allow_tool_calls else "NONE"}}
        # Flash/Lite 2.5 aceitam desativar pensamento para conversa curta.
        # Sem isso o orçamento de saída pode acabar antes do texto visível.
        if model.startswith(("gemini-2.5-flash", "gemini-2.5-flash-lite")):
            payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
        headers = {"Content-Type": "application/json", "x-goog-api-key": self._api_key}
        timeout = aiohttp.ClientTimeout(total=_remaining(deadline))
        try:
            _record_http_request()
            async with self._session.post(
                self.BASE_URL.format(model=model), json=payload,
                headers=headers, timeout=timeout,
            ) as resp:
                if resp.status >= 400:
                    raise await _http_error(resp)
                data = await _read_json_limited(resp)
        except asyncio.TimeoutError as exc:
            raise ProviderError("Gemini timeout", kind="timeout") from exc
        except aiohttp.ClientError as exc:
            raise ProviderError("Gemini erro de rede", kind="network") from exc
        _record_usage(data, "gemini", model)
        if not isinstance(data, dict):
            raise ProviderError("Gemini resposta malformada", kind="invalid_response", stage="output")
        feedback = data.get("promptFeedback") or {}
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise ProviderError("pedido bloqueado pelo provider", kind="blocked", stage="output")
        try:
            candidate = data["candidates"][0]
            finish_reason = candidate.get("finishReason")
            if finish_reason in {
                "SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY",
            }:
                raise ProviderError(
                    "resposta bloqueada pelo provider", kind="blocked", stage="output",
                    finish_reason=finish_reason,
                )
            parts = candidate.get("content", {}).get("parts", [])
            reply = "".join(
                part.get("text", "") for part in parts
                if isinstance(part, dict) and not part.get("thought") and isinstance(part.get("text"), str)
            ).strip()
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError("Gemini resposta malformada", kind="invalid_response", stage="output") from exc
        if not allow_tool_calls and any(isinstance(part, dict) and "functionCall" in part for part in parts):
            raise ProviderError("provider ignorou o fechamento sem ferramentas", kind="invalid_response", stage="output", finish_reason=finish_reason,
                                diagnostic_code="unexpected_calls")
        if tool_specs:
            native_calls = []
            for part in parts:
                if not isinstance(part, dict) or part.get("thought") or "functionCall" not in part:
                    continue
                function = part["functionCall"]
                if not isinstance(function, dict):
                    native_calls.append((None, None, None, {}))
                else:
                    native_calls.append((function.get("id"), function.get("name"), function.get("args"), {"part": part}))
                if len(native_calls) > getattr(C, "MAX_TOOL_CALLS", 8):
                    break
            return _native_reply(reply, native_calls, specs, actions, provider="gemini", model=model, finish_reason=finish_reason)
        if actions:
            calls = []
            for part in parts:
                if not isinstance(part, dict) or part.get("thought") or "functionCall" not in part:
                    continue
                function = part["functionCall"]
                if not isinstance(function, dict):
                    calls.append((None, None))
                else:
                    calls.append((function.get("name"), function.get("args")))
                if len(calls) > MAX_PROPOSALS:
                    break
            return _action_reply(
                reply, calls, actions, provider="gemini", model=model, finish_reason=finish_reason,
            )
        if not reply:
            raise ProviderError(
                "Gemini retornou resposta vazia", kind="empty", stage="output",
                finish_reason=finish_reason,
            )
        return reply


class ProviderRouter:
    def __init__(
        self, session: aiohttp.ClientSession, *, groq_key: Optional[str] = None,
        gemini_key: Optional[str] = None, mistral_key: Optional[str] = None,
        mistral_enabled: bool = False,
        cloudflare_account_id: Optional[str] = None, cloudflare_key: Optional[str] = None,
        cloudflare_enabled: bool = False,
    ) -> None:
        self._session = session
        self._groq = _GroqClient(session, groq_key) if groq_key else None
        self._gemini = _GeminiClient(session, gemini_key) if gemini_key else None
        self._mistral = _MistralClient(session, mistral_key) if mistral_enabled is True and mistral_key else None
        self._mistral_setup = {"enabled": mistral_enabled is True, "api_key_configured": bool(mistral_key)}
        # Credenciais podem pertencer ao gerador de imagens. Ativar o chat é
        # uma escolha separada e explícita, nunca efeito de encontrar a chave.
        valid_account = isinstance(cloudflare_account_id, str) and bool(re.fullmatch(r"[A-Fa-f0-9]{32}", cloudflare_account_id))
        self._cloudflare_setup = {"enabled": cloudflare_enabled is True,
                                  "account_id_configured": valid_account,
                                  "api_token_configured": bool(cloudflare_key)}
        self._cloudflare = (_CloudflareClient(session, cloudflare_key, cloudflare_account_id)
                           if cloudflare_enabled is True and valid_account and cloudflare_key else None)
        self._states: dict[tuple[str, str], _ProviderState] = {}
        self._account_states: dict[str, _ProviderState] = {}
        self._unsupported_tool_models: set[tuple[str, str]] = set()
        self._last_request: dict = {}
        self._request_report: ContextVar[dict | None] = ContextVar("chatbot_router_task_report", default=None)
        log.info("chatbot: configuration groq_configured=%s gemini_configured=%s mistral_configured=%s mistral_enabled=%s cloudflare_configured=%s cloudflare_enabled=%s",
                 bool(self._groq), bool(self._gemini), bool(self._mistral), mistral_enabled is True,
                 bool(self._cloudflare), cloudflare_enabled is True)
        if not self._groq and not self._gemini and not self._mistral and not self._cloudflare:
            log.warning("ProviderRouter: nenhuma API key configurada")

    def get_request_report(self) -> dict:
        """Relatório desta tarefa; não confunde turnos concorrentes do router."""
        return deepcopy(self._request_report.get() or {})

    def _state(self, provider: str, model: str) -> _ProviderState:
        state = self._states.setdefault((provider, model), _ProviderState())
        account = self._account_states.get(provider)
        if account is not None and not account.is_available() and state.next_allowed_monotonic < account.next_allowed_monotonic:
            state.failure_generation += 1
            state.next_allowed_monotonic = account.next_allowed_monotonic
            state.last_kind, state.last_stage = account.last_kind, account.last_stage
            state.last_status, state.quota_scope = account.last_status, account.quota_scope
            state.consecutive_failures = max(1, state.consecutive_failures)
        return state

    def _catalog_models(self, *, has_images=False) -> tuple[str, ...]:
        getter = getattr(self._gemini, "catalog_models", None)
        return tuple(getter(has_images=has_images)) if callable(getter) else ()

    def _all_models(self, provider: str, models: tuple[str, ...]) -> tuple[str, ...]:
        configured = (C.CLOUDFLARE_MODELS if provider == "cloudflare" else
                      C.MISTRAL_MODELS if provider == "mistral" else
                      (*C.GROQ_MODELS, *C.GROQ_VISION_MODELS) if provider == "groq" else
                      (*C.GEMINI_MODELS, *getattr(C, "GEMINI_VISION_MODELS", C.GEMINI_MODELS)))
        discovered = (*self._catalog_models(), *self._catalog_models(has_images=True)) if provider == "gemini" else ()
        known = tuple(model for candidate, model in self._states if candidate == provider)
        return tuple(dict.fromkeys((*models, *configured, *discovered, *known)))

    def _mark_provider_failure(
        self, provider: str, models: tuple[str, ...], cooldown_seconds: float, *, status: int,
        kind: str = "network", stage: str = "api", quota_scope: str = "account", explicit_retry: bool = False,
    ) -> None:
        if kind == "auth" or (kind == "rate_limit" and quota_scope == "account"):
            account = self._account_states.setdefault(provider, _ProviderState())
            account.mark_failure(cooldown_seconds, status=status, kind=kind, stage=stage,
                                 quota_scope=quota_scope, explicit_retry=explicit_retry)
        for candidate in models:
            self._states.setdefault((provider, candidate), _ProviderState()).mark_failure(
                cooldown_seconds, status=status, kind=kind, stage=stage,
                quota_scope=quota_scope, explicit_retry=explicit_retry,
            )

    def _record_failure(
        self, provider: str, model: str, models: tuple[str, ...], error: ProviderError,
        *, expected_generation: Optional[int] = None,
    ) -> tuple[bool, ProviderError]:
        """Circuito e causa efetiva, preservando falhas simultâneas mais novas."""
        state = self._state(provider, model)
        if expected_generation is not None and expected_generation != state.failure_generation:
            if not state.is_available() and state.last_kind:
                effective = ProviderError(
                    "circuito mais recente preservado", kind=state.last_kind, stage=state.last_stage,
                    status=state.last_status or None, quota_scope=state.quota_scope,
                    retry_after=max(0.0, state.next_allowed_monotonic - time.monotonic()),
                )
                stop = state.quota_scope == "account" or state.last_kind in {"auth", "network", "timeout"}
                return stop, effective
            return False, error
        status = int(error.status or 0)
        metadata = dict(status=status, kind=error.kind, stage=error.stage, quota_scope=error.quota_scope)
        if error.kind == "rate_limit":
            metadata["explicit_retry"] = error.retry_after is not None
            cooldown = error.retry_after or 30.0
            if error.quota_scope == "account":
                self._mark_provider_failure(provider, self._all_models(provider, models), cooldown, **metadata)
                return True, error
            state.mark_failure(cooldown, **metadata)
        elif error.kind == "auth":
            self._mark_provider_failure(provider, self._all_models(provider, models), 900.0, **metadata)
            return True, error
        elif error.kind in {"network", "timeout"}:
            self._mark_provider_failure(provider, models, 20.0, **metadata)
            return True, error
        elif error.kind == "model":
            state.mark_failure(300.0, **metadata)
        elif error.kind in {"empty", "invalid_response"} and not error.diagnostic_code and error.finish_reason not in {"length", "MAX_TOKENS"}:
            state.mark_failure(20.0, **metadata)
        return error.kind == "deadline", error

    def snapshot(self) -> dict[str, dict]:
        now = time.monotonic()
        return {
            f"{provider}/{model}": {
                "available": now >= state.next_allowed_monotonic,
                "cooldown_seconds": max(0.0, state.next_allowed_monotonic - now),
                "failures": state.consecutive_failures,
                "last_status": state.last_status,
                "last_kind": state.last_kind,
                "quota_scope": state.quota_scope,
            }
            for (provider, model), state in self._states.items()
        }

    def diagnostics(self) -> dict:
        """Metadados operacionais; nenhuma chave, mensagem ou corpo HTTP."""
        circuits = self.snapshot()
        discovery = getattr(self._gemini, "discovery_diagnostics", None)
        discovery_state = discovery() if callable(discovery) else {}
        cloudflare_budget = getattr(self._cloudflare, "budget_diagnostics", None)
        gemini_text = tuple(dict.fromkeys((*C.GEMINI_MODELS, *self._catalog_models())))
        groq_text = tuple(C.GROQ_MODELS)
        availability = []
        for provider, text, vision, configured in (
            ("groq", groq_text, tuple(C.GROQ_VISION_MODELS), self._groq is not None),
            ("gemini", gemini_text, tuple(dict.fromkeys((*getattr(C, "GEMINI_VISION_MODELS", C.GEMINI_MODELS), *self._catalog_models(has_images=True)))), self._gemini is not None),
            ("mistral", tuple(C.MISTRAL_MODELS), (), self._mistral is not None),
            ("cloudflare", tuple(C.CLOUDFLARE_MODELS), (), self._cloudflare is not None),
        ):
            for model in dict.fromkeys((*text, *vision)):
                state = self._states.get((provider, model))
                account = self._account_states.get(provider)
                candidates = [item for item in (state, account) if item is not None and not item.is_available()]
                blocker = max(candidates, key=lambda item: item.next_allowed_monotonic) if candidates else None
                availability.append({"provider": provider, "model": model, "configured": configured,
                    "exposed": False if state is not None and state.last_status == 404 and state.last_kind == "model"
                               else True if provider == "gemini" and model in (*self._catalog_models(), *self._catalog_models(has_images=True)) else None,
                    "available": configured and blocker is None, "cooldown_seconds": max(0.0, blocker.next_allowed_monotonic - time.monotonic()) if blocker else 0.0,
                    "cause_kind": blocker.last_kind if blocker else "", "status": blocker.last_status if blocker else 0,
                    "quota_scope": blocker.quota_scope if blocker else "model",
                    "modes": tuple(mode for mode, names in (("text", text), ("vision", vision)) if model in names),
                    "tools_support": "unsupported" if (provider, model) in self._unsupported_tool_models else "unknown"})
        mode = "vision" if self._last_request.get("mode") == "vision" else "text"
        waits = [item["cooldown_seconds"] for item in availability
                 if item["configured"] and mode in item["modes"] and not item["available"]
                 and item["cause_kind"] not in {"auth", "model"} and item["cooldown_seconds"] > 0]
        if discovery_state.get("kind") == "rate_limit" and discovery_state.get("cache_seconds", 0) > 0:
            waits.append(discovery_state["cache_seconds"])
        eligible = any(item["available"] and mode in item["modes"]
                       and not (self._last_request.get("tools_requested") and item["tools_support"] == "unsupported")
                       for item in availability)
        return {
            "configured": {"groq": self._groq is not None, "gemini": self._gemini is not None,
                           "mistral": self._mistral is not None, "cloudflare": self._cloudflare is not None},
            "circuits": circuits,
            "earliest_retry_seconds": 0.0 if eligible else min(waits) if waits else None,
            "last_request": deepcopy(self._last_request),
            "models": {"groq": groq_text, "gemini": gemini_text, "mistral": tuple(C.MISTRAL_MODELS),
                       "cloudflare": tuple(C.CLOUDFLARE_MODELS)},
            "availability": availability,
            "model_discovery": discovery_state,
            "mistral_setup": dict(self._mistral_setup),
            "cloudflare_setup": {**self._cloudflare_setup,
                                  "billing_verified": False,
                                  "budget": cloudflare_budget() if callable(cloudflare_budget) else {}},
        }

    def _available(self, provider: str, model: str) -> bool:
        account = self._account_states.get(provider)
        if account is not None and not account.is_available():
            return False
        state = self._states.get((provider, model))
        return state is None or state.is_available()

    def _retry_context(self, attempts) -> tuple[Optional[float], Optional[str], Optional[int]]:
        now = time.monotonic()
        waiting = [state for provider, _, models in attempts for model in models
                   if (state := self._states.get((provider, model))) is not None and not state.is_available()]
        if not waiting:
            return None, None, None
        viable = [state for state in waiting if state.last_kind not in {"auth", "model"}]
        earliest = min(viable or waiting, key=lambda state: state.next_allowed_monotonic)
        return max(0.0, earliest.next_allowed_monotonic - now), earliest.last_kind or None, earliest.last_status or None

    @staticmethod
    def _attempt_timeout(deadline: float, *, siblings: int = 0) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("prazo da tentativa esgotado", kind="deadline", stage="routing")
        # Reservar até 3s por irmão. Com pouco prazo, dividir o que existe;
        # não inventar um timeout mínimo que ultrapasse o deadline do turno.
        reserve = min(3.0 * siblings, remaining * .5) if siblings else 0.0
        return min(C.PROVIDER_TIMEOUT_SECONDS, remaining - reserve)

    async def _call(self, client, kwargs) -> str | ChatReply:
        try:
            return await asyncio.wait_for(client.chat(**kwargs), timeout=kwargs["timeout_seconds"])
        except asyncio.TimeoutError as exc:
            raise ProviderError("tempo da tentativa esgotado", kind="timeout") from exc

    async def chat(
        self, *, system: str, messages: list[ChatMessage],
        temperature: float = C.DEFAULT_TEMPERATURE, actions: tuple[str, ...] = (),
        target_refs: tuple[str, ...] = (), tool_specs: tuple[ToolSpec, ...] = (),
        text_provider_order: tuple[str, ...] | None = None,
        budget_seconds: float | None = None, allow_tool_calls: bool = True,
        repair_state: dict | None = None,
        request_report: dict | None = None,
    ) -> str | ChatReply:
        # O contador HTTP é herdado por wait_for e por descoberta single-flight,
        # mas não pode permanecer ativo para outra operação após este await.
        report = request_report if isinstance(request_report, dict) else {}
        token = _REQUEST_REPORT.set(report)
        self._request_report.set(report)
        try:
            return await self._chat(system=system, messages=messages, temperature=temperature, actions=actions,
                                    target_refs=target_refs, tool_specs=tool_specs,
                                    text_provider_order=text_provider_order, budget_seconds=budget_seconds,
                                    allow_tool_calls=allow_tool_calls, repair_state=repair_state, request_report=report)
        finally:
            _REQUEST_REPORT.reset(token)

    async def _chat(
        self, *, system: str, messages: list[ChatMessage],
        temperature: float = C.DEFAULT_TEMPERATURE, actions: tuple[str, ...] = (),
        target_refs: tuple[str, ...] = (), tool_specs: tuple[ToolSpec, ...] = (),
        text_provider_order: tuple[str, ...] | None = None,
        budget_seconds: float | None = None, allow_tool_calls: bool = True,
        repair_state: dict | None = None, request_report: dict | None = None,
    ) -> str | ChatReply:
        actions = enabled_actions(actions)
        started = time.monotonic()
        has_images = any(message.images or message.image_urls for message in messages)
        mode = "vision" if has_images else "text"
        attempts: list[tuple[str, object, tuple[str, ...]]] = []
        if self._groq:
            attempts.append(("groq", self._groq, C.GROQ_VISION_MODELS if has_images else C.GROQ_MODELS))
        if self._gemini:
            configured = getattr(C, "GEMINI_VISION_MODELS", C.GEMINI_MODELS) if has_images else C.GEMINI_MODELS
            cached = tuple(model for model in self._catalog_models(has_images=has_images)
                           if model not in configured and self._available("gemini", model))[:2]
            attempts.append(("gemini", self._gemini, (*configured, *cached)))
        if self._mistral and not has_images:
            attempts.append(("mistral", self._mistral, C.MISTRAL_MODELS))
        if self._cloudflare and not has_images:
            attempts.append(("cloudflare", self._cloudflare, C.CLOUDFLARE_MODELS))
        if not has_images:
            by_provider = {attempt[0]: attempt for attempt in attempts}
            priority = text_provider_order if text_provider_order is not None else ("groq", "gemini")
            primary = tuple(name for name in dict.fromkeys(priority) if name in {"groq", "gemini"})
            # Mistral usa uma cota independente antes da reserva diária de
            # neurons da Cloudflare, protegendo Qwen para indisponibilidade real.
            names = tuple(dict.fromkeys((*primary, "mistral", "cloudflare", *by_provider)))
            attempts = [by_provider[name] for name in names if name in by_provider]
        report = request_report if isinstance(request_report, dict) else {}
        report.clear()
        report.update(outcome="pending", mode=mode, attempts=[], skips=[], attempt_count=0,
                      request_count=0, discovery_request_count=0)
        self._last_request = report

        def finish(kind: str, *, cause_kind=None, retry_after=None, outcome="failed"):
            report.update(outcome=outcome, kind=kind, cause_kind=cause_kind or kind,
                          retry_after=retry_after, attempt_count=len(report["attempts"]),
                          generation_attempt_count=len(report["attempts"]),
                          elapsed_ms=max(0, int((time.monotonic() - started) * 1000)))
            measured = [item["usage"] for item in report["attempts"] if item.get("usage")]
            report["usage"] = {key: sum(item.get(key, 0) for item in measured) for key in {key for item in measured for key in item}}
            report["usage_field_attempts"] = {key: sum(key in item for item in measured) for key in report["usage"]}
            report["usage_attempt_count"] = len(measured)
            report["usage_scope"] = "reported_attempts"
            report["usage_complete"] = bool(measured) and len(measured) == len(report["attempts"]) and all(
                {"input_tokens", "output_tokens", "total_tokens"} <= set(item) for item in measured)

        def skip(provider: str, model: str, reason: str, *, retry_after=None):
            report["skips"].append({"provider": provider, "model": model, "reason": reason, "retry_after": retry_after})
            log.info("chatbot: skip provider=%s model=%s mode=%s reason=%s retry_after=%s", provider, model, mode, reason, retry_after)

        if not attempts:
            finish("unconfigured")
            raise AllProvidersExhausted("nenhum provider configurado", kind="unconfigured", stage="routing")
        budget = C.PROVIDER_ROUTER_TIMEOUT_SECONDS
        # Um budget menor que 1s é válido e mantém o prazo exato do host.
        if budget_seconds is not None:
            try:
                budget = float(budget_seconds)
            except (TypeError, ValueError, OverflowError):
                budget = 0.0
            if not math.isfinite(budget):
                budget = 0.0
        deadline = started + min(C.PROVIDER_ROUTER_TIMEOUT_SECONDS, max(0.0, budget))
        normalized: list[ChatMessage] = []
        try:
            for message in messages:
                if message.images or not message.image_urls:
                    normalized.append(message)
                    continue
                attachments = [MediaAttachment(url, urlsplit(url).path.rsplit("/", 1)[-1], "", 0, "image")
                               for url in message.image_urls[:C.MAX_IMAGES_PER_MESSAGE]]
                images = await prepare_image_attachments(self._session, attachments, timeout_seconds=_remaining(deadline))
                normalized.append(replace(message, image_urls=[], images=images))
        except ImagePreparationError as exc:
            finish(exc.kind)
            raise AllProvidersExhausted(str(exc), kind=exc.kind, stage=exc.stage, status=exc.status) from exc
        except ProviderError as exc:
            finish(exc.kind)
            raise AllProvidersExhausted("prazo dos providers esgotado", kind=exc.kind, stage=exc.stage) from exc
        messages = normalized
        last_error: Optional[ProviderError] = None
        errors: list[ProviderError] = []
        text_fallbacks = []
        wants_tools = bool(actions or tool_specs)
        report["tools_requested"] = wants_tools
        has_native_history = any(message.tool_calls or message.role == "tool" for message in messages)
        latest_user = next((message.content for message in reversed(messages)
                            if message.role == "user" and isinstance(message.content, str)), "")
        economy_route = (not has_images and not wants_tools and not has_native_history
                         and len(latest_user.strip()) <= 700)
        if economy_route:
            preferred = {
                "groq": ("openai/gpt-oss-20b", "openai/gpt-oss-120b"),
                "gemini": ("gemini-2.5-flash-lite", "gemini-2.5-flash"),
            }
            reordered = []
            for provider, client, models in attempts:
                wanted = preferred.get(provider, ())
                ordered = tuple(dict.fromkeys((*[name for name in wanted if name in models], *models)))
                reordered.append((provider, client, ordered))
            attempts = reordered
        report["routing_profile"] = "economy" if economy_route else "full"
        repair_used = bool(repair_state and repair_state.get("used"))
        discovery_used = False
        native_schemas = {spec.name: spec.parameters for spec in tool_specs if spec.available}
        if actions:
            declaration = proposal_tool(actions, target_refs)
            native_schemas.setdefault(TOOL_NAME, declaration["parameters"])

        async def attempt(provider: str, client, model: str, timeout: float, *, text_only=False, repair=False, feedback=""):
            began = time.monotonic()
            state = self._state(provider, model)
            generation = state.failure_generation
            entry = {"provider": provider, "model": model, "budget_ms": max(0, int(timeout * 1000)), "text_only": text_only, "repair": repair}
            report["attempts"].append(entry)
            kwargs = dict(system=system, messages=messages, temperature=temperature, model=model, timeout_seconds=timeout)
            if feedback:
                kwargs["system"] += "\n\n" + feedback
            if text_only:
                kwargs["system"] += "\n\nNeste turno as ferramentas estão indisponíveis. Responda apenas em texto; não crie pedidos, não afirme executar ações e não peça códigos internos ao usuário."
            else:
                # Chamadores antigos recebem exatamente os kwargs anteriores.
                if actions:
                    kwargs["actions"] = actions
                    if target_refs:
                        kwargs["target_refs"] = target_refs
                if tool_specs:
                    kwargs["tool_specs"] = tool_specs
                if not allow_tool_calls:
                    kwargs["allow_tool_calls"] = False
            usage = {}
            usage_token = _ATTEMPT_USAGE.set(usage)
            requests = {"count": 0}
            requests_token = _ATTEMPT_REQUESTS.set(requests)
            try:
                reply = await self._call(client, kwargs)
                if not allow_tool_calls and isinstance(reply, ChatReply) and (reply.tool_calls or reply.proposals):
                    raise ProviderError("provider ignorou o fechamento sem ferramentas", kind="invalid_response", stage="output")
            except ProviderError as exc:
                usage.update(exc.usage)
                entry["usage"] = _safe_usage(usage)
                diagnostic_tool = exc.diagnostic_tool if exc.diagnostic_tool in native_schemas else ""
                diagnostic_path = _safe_schema_path(exc.diagnostic_path, native_schemas.get(diagnostic_tool, {}))
                entry.update(kind=exc.kind, stage=exc.stage, status=int(exc.status or 0),
                             retry_after=exc.retry_after, quota_scope=exc.quota_scope,
                             diagnostic_code=exc.diagnostic_code, diagnostic_path=diagnostic_path,
                             diagnostic_tool=diagnostic_tool, diagnostic_index=exc.diagnostic_index,
                             elapsed_ms=max(0, int((time.monotonic() - began) * 1000)))
                exc.diagnostic_tool, exc.diagnostic_path = diagnostic_tool, diagnostic_path
                errors.append(exc)
                log.warning(
                    "chatbot: provider=%s model=%s mode=%s stage=%s kind=%s status=%s finish_reason=%s elapsed_ms=%d budget_ms=%d quota_scope=%s retry_after=%s diagnostic_code=%s diagnostic_path=%s diagnostic_tool=%s diagnostic_index=%s repair=%s",
                    provider, model, mode, exc.stage, exc.kind, int(exc.status or 0),
                    _diagnostic_finish_reason(exc.finish_reason), entry["elapsed_ms"], entry["budget_ms"], exc.quota_scope, exc.retry_after,
                    exc.diagnostic_code or "none", diagnostic_path, diagnostic_tool or "none", exc.diagnostic_index, repair,
                )
                raise
            finally:
                _ATTEMPT_USAGE.reset(usage_token)
                _ATTEMPT_REQUESTS.reset(requests_token)
                entry["request_count"] = requests["count"]
            entry["usage"] = _safe_usage(usage)
            entry.update(kind="success", status=200, elapsed_ms=max(0, int((time.monotonic() - began) * 1000)))
            state.mark_success(expected_generation=generation)
            finish("success", outcome="success")
            log.info("chatbot: result=success provider=%s model=%s mode=%s elapsed_ms=%d message_count=%d",
                     provider, model, mode, report["elapsed_ms"], len(messages))
            if text_only or (wants_tools and isinstance(reply, str)):
                return ChatReply(reply, provider=provider, model=model)
            return reply

        def terminal(error: ProviderError):
            if error.stage == "attachment" or error.kind == "blocked":
                finish(error.kind, retry_after=error.retry_after)
                raise AllProvidersExhausted("pedido não pôde ser processado", kind=error.kind, stage=error.stage,
                                            status=error.status, finish_reason=error.finish_reason) from error

        async def discover(client, models, provider_deadline):
            nonlocal discovery_used
            account = self._account_states.get("gemini")
            if discovery_used or (account is not None and not account.is_available()):
                return
            lookup = getattr(client, "discover_models", None)
            remaining = provider_deadline - time.monotonic()
            if not callable(lookup) or remaining <= 0:
                return
            discovery_used = True
            timeout = min(3.0, remaining / 3)
            try:
                found = await asyncio.wait_for(lookup(timeout_seconds=timeout, has_images=has_images), timeout=timeout)
            except (ProviderError, asyncio.TimeoutError):
                found = ()
            getter = getattr(client, "discovery_diagnostics", None)
            status = getter() if callable(getter) else {}
            report["model_discovery"] = status
            if status.get("kind") in {"auth", "rate_limit"}:
                error = ProviderError("a descoberta de modelos está indisponível", kind=status["kind"], stage="discovery",
                                      status=status.get("status"), retry_after=status.get("retry_after"),
                                      quota_scope="account" if status["kind"] == "auth" else status.get("quota_scope", "model"))
                errors.append(error)
                if error.kind == "auth" or error.quota_scope == "account":
                    self._mark_provider_failure("gemini", self._all_models("gemini", tuple(models)),
                                                900.0 if error.kind == "auth" else error.retry_after or 30.0,
                                                status=error.status or 0, kind=error.kind, stage="discovery",
                                                quota_scope=error.quota_scope, explicit_retry=error.retry_after is not None)
            configured = set(getattr(C, "GEMINI_VISION_MODELS", C.GEMINI_MODELS) if has_images else C.GEMINI_MODELS)
            added = sum(model not in configured for model in models)
            for candidate in found:
                if added >= 2:
                    break
                if (isinstance(candidate, str) and re.fullmatch(r"gemini-[A-Za-z0-9._-]{1,100}", candidate, re.ASCII)
                        and candidate not in models and self._available("gemini", candidate)):
                    models.append(candidate)
                    added += 1

        for index, (provider, client, models) in enumerate(attempts):
            models = list(models)
            attempts[index] = (provider, client, models)
            remaining = max(0.0, deadline - time.monotonic())
            later = [item for item in attempts[index + 1:] if any(self._available(item[0], model) for model in item[2])]
            # O prazo deste provider é fixo durante sua cadeia. Recalcular a
            # reserva a cada modelo permitiria consumir aos poucos todo o fallback.
            reserve = min(10.0, remaining * .5) if later else 0.0
            provider_deadline = deadline - reserve
            if provider == "gemini" and any(
                (state := self._states.get((provider, model))) is not None
                and state.last_kind == "model" and state.last_status == 404 for model in models
            ):
                await discover(client, models, provider_deadline)
            for model_index, model in enumerate(models):
                state = self._state(provider, model)
                generation = state.failure_generation
                if not state.is_available():
                    skip(provider, model, "cooldown", retry_after=max(0.0, state.next_allowed_monotonic - time.monotonic()))
                    continue
                if wants_tools and (provider, model) in self._unsupported_tool_models:
                    text_fallbacks.append((provider, client, model))
                    skip(provider, model, "tools_unsupported")
                    continue
                if time.monotonic() >= provider_deadline:
                    skip(provider, model, "fallback_reserved" if later else "deadline")
                    continue
                siblings = sum(self._available(provider, candidate) and not (wants_tools and (provider, candidate) in self._unsupported_tool_models)
                               for candidate in models[model_index + 1:])
                try:
                    timeout = self._attempt_timeout(provider_deadline, siblings=siblings)
                    return await attempt(provider, client, model, timeout)
                except ProviderError as exc:
                    last_error = exc
                    terminal(exc)
                    repairable = (wants_tools and allow_tool_calls and not repair_used and exc.kind == "invalid_response"
                                  and exc.stage == "output" and exc.diagnostic_code
                                  and exc.diagnostic_code not in {"incomplete_calls", "unexpected_calls", "malformed_response"}
                                  and exc.finish_reason not in _INCOMPLETE_TOOL_FINISH_REASONS
                                  and state.is_available() and state.failure_generation == generation
                                  and provider_deadline - time.monotonic() > .05)
                    if repairable:
                        repair_used = True
                        if repair_state is not None:
                            repair_state["used"] = True
                        report["repair_used"] = True
                        feedback = (
                            "O lote anterior deste pedido foi rejeitado. Nenhuma ferramenta desse lote foi executada. "
                            "Corrija uma única vez o contrato das chamadas usando as declarações atuais. "
                            "Não invente valores que faltam; pergunte quando o pedido não contiver a informação. "
                            "Não repita um efeito confirmado no histórico. Não revele texto privado de áudio. "
                            "Diagnóstico seguro: " + json.dumps({"code": exc.diagnostic_code, "path": exc.diagnostic_path,
                                                                "tool": exc.diagnostic_tool, "index": exc.diagnostic_index}, ensure_ascii=False)
                        )
                        try:
                            generation = state.failure_generation
                            return await attempt(provider, client, model, self._attempt_timeout(provider_deadline, siblings=siblings),
                                                 repair=True, feedback=feedback)
                        except ProviderError as repaired:
                            exc, last_error = repaired, repaired
                            terminal(repaired)
                    if exc.kind == "tools_unsupported" and wants_tools:
                        self._unsupported_tool_models.add((provider, model))
                        text_fallbacks.append((provider, client, model))
                        continue
                    stop, last_error = self._record_failure(provider, model, models, exc, expected_generation=generation)
                    if last_error is not exc:
                        report["concurrent_failure_preserved"] = True
                    if provider == "gemini" and exc.kind == "model" and exc.status == 404:
                        await discover(client, models, provider_deadline)
                    if stop:
                        for candidate in models[model_index + 1:]:
                            skip(provider, candidate, "account_cooldown" if last_error.quota_scope == "account" or last_error.kind == "auth" else "provider_unavailable")
                        break
            if time.monotonic() >= deadline:
                break

        # Apenas depois dos modelos nativos, degradar para conversa sem ações.
        for index, (provider, client, model) in enumerate(text_fallbacks):
            if has_native_history:
                # Um modelo sem suporte a ferramentas não recebe envelopes
                # nativos anteriores nem uma narrativa inventada de efeitos.
                skip(provider, model, "native_history_requires_tools")
                continue
            if not self._available(provider, model):
                skip(provider, model, "cooldown")
                continue
            if time.monotonic() >= deadline:
                skip(provider, model, "deadline")
                break
            try:
                generation = self._state(provider, model).failure_generation
                timeout = self._attempt_timeout(deadline, siblings=len(text_fallbacks) - index - 1)
                return await attempt(provider, client, model, timeout, text_only=True)
            except ProviderError as exc:
                last_error = exc
                terminal(exc)
                models = next(item[2] for item in attempts if item[0] == provider)
                _, last_error = self._record_failure(provider, model, models, exc, expected_generation=generation)
                if last_error is not exc:
                    report["concurrent_failure_preserved"] = True

        retry_after, cooldown_kind, cooldown_status = self._retry_context(attempts)
        if last_error is not None and last_error not in errors:
            errors.append(last_error)
        causes = tuple({
            "kind": error.kind, "stage": error.stage, "status": error.status,
            "retry_after": error.retry_after, "quota_scope": error.quota_scope,
            "diagnostic_code": error.diagnostic_code, "diagnostic_path": error.diagnostic_path,
            "diagnostic_tool": error.diagnostic_tool, "diagnostic_index": error.diagnostic_index,
        } for error in errors)
        report["causes"] = causes
        viable_waits = [
            max(0.0, state.next_allowed_monotonic - time.monotonic())
            for provider, _, models in attempts for model in models
            if (state := self._states.get((provider, model))) is not None and not state.is_available()
            and state.last_kind not in {"auth", "model"}
        ]
        catalog_status = report.get("model_discovery", {})
        if catalog_status.get("kind") == "rate_limit" and catalog_status.get("cache_seconds", 0) > 0:
            viable_waits.append(catalog_status["cache_seconds"])
        earliest_retry = min(viable_waits) if viable_waits else None
        if any(self._available(provider, model) and not (wants_tools and (provider, model) in self._unsupported_tool_models)
               for provider, _, models in attempts for model in models):
            earliest_retry = 0.0
        report["earliest_retry_seconds"] = earliest_retry
        # Remoção/404 de um último modelo não apaga os limites e contratos que
        # efetivamente impediram as alternativas saudáveis deste turno.
        def importance(error):
            if error.kind == "request" and error.diagnostic_code.startswith("api_"):
                # Waiting for the primary quota cannot fix a request rejected
                # by available fallbacks. Preserve that actionable cause.
                return 6
            if error.kind == "invalid_response" and error.diagnostic_code:
                return 5
            return {"rate_limit": 4, "network": 3, "timeout": 3, "invalid_response": 3, "empty": 3,
                    "tools_unsupported": 2, "auth": 2, "request": 1, "model": 0}.get(error.kind, 1)
        representative = max(reversed(errors), key=importance) if errors else None
        if report.get("concurrent_failure_preserved") and last_error is not None:
            representative = last_error
        attempted = len(report["attempts"])
        if time.monotonic() >= deadline:
            kind, stage = "deadline", "routing"
        elif attempted == 0 and retry_after is not None:
            kind, stage = "cooldown", "routing"
        elif attempted == 0 and has_native_history and text_fallbacks:
            kind, stage = "tools_unsupported", "routing"
        elif attempted == 0:
            kind, stage = "cooldown", "routing"
        else:
            kind = representative.kind if representative else "network"
            stage = representative.stage if representative else "api"
        cause_kind = cooldown_kind if kind == "cooldown" else representative.kind if representative else kind
        if kind == "rate_limit":
            retry_after = earliest_retry if earliest_retry is not None else representative.retry_after if representative else None
        elif kind not in {"cooldown"}:
            retry_after = representative.retry_after if representative else None
        finish(kind, cause_kind=cause_kind, retry_after=retry_after)
        log.info("chatbot: exhausted mode=%s kind=%s cause_kind=%s attempts=%d elapsed_ms=%d earliest_retry=%s",
                 mode, kind, cause_kind, attempted, report["elapsed_ms"], retry_after)
        raise AllProvidersExhausted(
            "todos os providers estão temporariamente indisponíveis" if kind == "cooldown" else "todos os providers falharam",
            kind=kind, cause_kind=cause_kind, stage=stage, retry_after=retry_after,
            status=(cooldown_status if kind == "cooldown" else representative.status if representative else None),
            quota_scope=representative.quota_scope if representative else "model",
            finish_reason=representative.finish_reason if representative else None,
            diagnostic_code=representative.diagnostic_code if representative else "",
            diagnostic_path=representative.diagnostic_path if representative else "$",
            diagnostic_tool=representative.diagnostic_tool if representative else "",
            diagnostic_index=representative.diagnostic_index if representative else None,
            usage=report.get("usage"), causes=causes, earliest_retry_seconds=earliest_retry,
        )
