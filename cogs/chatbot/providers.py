"""Router HTTP leve com fallback real de texto e visão.

Estados são mantidos por provider/modelo, então um modelo removido não derruba
os demais. Há deadline total, respostas vazias são rejeitadas e o fallback
Gemini recebe os bytes das imagens — nunca afirma que viu algo que não recebeu.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field, replace
from copy import deepcopy
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


async def _http_error(response: aiohttp.ClientResponse) -> ProviderError:
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
    status = response.status
    if status == 429:
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
            "provider atingiu um limite temporário", retry_after=max((wait for wait in waits if wait is not None), default=None),
            quota_scope=_quota_scope(error),
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
    return ProviderError("provider rejeitou a requisição", status=status, kind=kind)


def _output_tokens(messages: list[ChatMessage], *, actions: tuple[str, ...] = ()) -> int:
    tokens = (
        getattr(C, "MAX_VISION_RESPONSE_TOKENS", C.MAX_RESPONSE_TOKENS)
        if any(message.images or message.image_urls for message in messages)
        else C.MAX_RESPONSE_TOKENS
    )
    # Quatro argumentos de ferramentas precisam de espaço além da resposta
    # curta habitual. Sem ferramentas, preservamos o orçamento anterior.
    return max(tokens, C.MAX_ACTION_RESPONSE_TOKENS) if actions else tokens


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
        )
    try:
        proposals = parse_proposals(calls, actions)
    except InvalidActionProposal as exc:
        # Um lote inválido pode misturar pedido privado com outra chamada
        # malformada. Descartar também o texto público, que pode antecipar a fala.
        raise ProviderError(
            "provider retornou proposta inválida", kind="invalid_response",
            stage="output", finish_reason=finish_reason,
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
        raise ProviderError("provider retornou ferramentas incompletas", kind="invalid_response", stage="output", finish_reason=finish_reason)
    by_name = {spec.name: spec for spec in specs}
    calls, proposals, ids = [], [], set()
    try:
        for call_id, name, arguments, opaque in raw_calls:
            spec = by_name.get(name)
            if spec is None:
                raise InvalidToolArguments("Ferramenta inválida.")
            if isinstance(arguments, str):
                if len(arguments.encode("utf-8")) > 8192:
                    raise InvalidToolArguments("Argumentos inválidos.")
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise InvalidToolArguments("Argumentos inválidos.")
                        result[key] = value
                    return result
                def constant(value):
                    raise InvalidToolArguments("Argumentos inválidos.")
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
                    raise InvalidToolArguments("Propostas inválidas.")
            else:
                arguments = validate_tool_arguments(arguments, spec.parameters)
            call_id = f"call_{uuid4().hex}" if call_id is None else call_id
            if not isinstance(call_id, str) or not call_id or len(call_id) > 200 or call_id in ids:
                raise InvalidToolArguments("Chamada inválida.")
            ids.add(call_id)
            calls.append(NativeToolCall(call_id, name, arguments, deepcopy(opaque)))
    except (InvalidToolArguments, InvalidActionProposal, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ProviderError("provider retornou ferramentas inválidas", kind="invalid_response", stage="output", finish_reason=finish_reason) from exc
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

    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self._session = session
        self._api_key = api_key

    async def chat(
        self, *, system: str, messages: list[ChatMessage], temperature: float,
        model: str, timeout_seconds: float, actions: tuple[str, ...] = (),
        target_refs: tuple[str, ...] = (),
        tool_specs: tuple[ToolSpec, ...] = (),
        allow_tool_calls: bool = True,
    ) -> str | ChatReply:
        actions = enabled_actions(actions)
        specs, declarations = _tool_declarations(actions, target_refs, tool_specs)
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}]
            + [message.to_openai_payload() for message in messages],
            "temperature": max(C.MIN_TEMPERATURE, min(C.MAX_TEMPERATURE, temperature)),
            "max_completion_tokens": _output_tokens(messages, actions=(actions or tuple(spec.name for spec in specs)) if allow_tool_calls else ()),
            "stream": False,
        }
        if declarations:
            payload["tools"] = [{"type": "function", "function": declaration} for declaration in declarations]
            payload["tool_choice"] = "auto" if allow_tool_calls else "none"
        # Evita gastar tokens de raciocínio oculto em conversa casual.
        if model.startswith("openai/gpt-oss"):
            payload.update({"reasoning_effort": "low", "include_reasoning": False})
        elif model == "qwen/qwen3.8-27b":
            # Valores documentados pelo Groq para este modelo específico.
            payload.update({"reasoning_effort": "none", "include_reasoning": False})
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=max(0.001, timeout_seconds))
        try:
            async with self._session.post(
                self.BASE_URL, json=payload, headers=headers, timeout=timeout,
            ) as resp:
                if resp.status >= 400:
                    raise await _http_error(resp)
                data = await _read_json_limited(resp)
        except asyncio.TimeoutError as exc:
            raise ProviderError("Groq timeout", kind="timeout") from exc
        except aiohttp.ClientError as exc:
            raise ProviderError("Groq erro de rede", kind="network") from exc
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
            raise ProviderError("Groq resposta malformada", kind="invalid_response", stage="output") from exc
        reply = content.strip() if isinstance(content, str) else ""
        if not allow_tool_calls and message.get("tool_calls"):
            raise ProviderError("provider ignorou o fechamento sem ferramentas", kind="invalid_response", stage="output", finish_reason=finish_reason)
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
            return _native_reply(reply, native_calls, specs, actions, provider="groq", model=model, finish_reason=finish_reason)
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
                reply, calls, actions, provider="groq", model=model, finish_reason=finish_reason,
            )
        if not reply:
            raise ProviderError(
                "Groq retornou resposta vazia", kind="empty", stage="output",
                finish_reason=finish_reason,
            )
        return reply


class _GeminiClient:
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    _ALLOWED_IMAGE_HOST_SUFFIXES = (
        ".discordapp.com", ".discordapp.net", ".discord.com",
    )

    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self._session = session
        self._api_key = api_key

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
                "maxOutputTokens": _output_tokens(messages, actions=(actions or tuple(spec.name for spec in specs)) if allow_tool_calls else ()),
            },
        }
        if declarations:
            for tool in declarations:
                tool["parameters"] = _gemini_schema(tool["parameters"])
            payload["tools"] = [{"functionDeclarations": declarations}]
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO" if allow_tool_calls else "NONE"}}
        # Flash/Lite 2.5 aceitam desativar pensamento para conversa curta.
        # Sem isso o orçamento de saída pode acabar antes do texto visível.
        if model.startswith(("gemini-2.5-flash", "gemini-2.5-flash-lite")):
            payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
        headers = {"Content-Type": "application/json", "x-goog-api-key": self._api_key}
        timeout = aiohttp.ClientTimeout(total=_remaining(deadline))
        try:
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
            raise ProviderError("provider ignorou o fechamento sem ferramentas", kind="invalid_response", stage="output", finish_reason=finish_reason)
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
        gemini_key: Optional[str] = None,
    ) -> None:
        self._session = session
        self._groq = _GroqClient(session, groq_key) if groq_key else None
        self._gemini = _GeminiClient(session, gemini_key) if gemini_key else None
        self._states: dict[tuple[str, str], _ProviderState] = {}
        self._unsupported_tool_models: set[tuple[str, str]] = set()
        self._last_request: dict = {}
        log.info("chatbot: configuration groq_configured=%s gemini_configured=%s", bool(self._groq), bool(self._gemini))
        if not self._groq and not self._gemini:
            log.warning("ProviderRouter: nenhuma API key configurada")

    def _state(self, provider: str, model: str) -> _ProviderState:
        return self._states.setdefault((provider, model), _ProviderState())

    @staticmethod
    def _all_models(provider: str, models: tuple[str, ...]) -> tuple[str, ...]:
        configured = ((*C.GROQ_MODELS, *C.GROQ_VISION_MODELS) if provider == "groq" else
                      (*C.GEMINI_MODELS, *getattr(C, "GEMINI_VISION_MODELS", C.GEMINI_MODELS)))
        return tuple(dict.fromkeys((*models, *configured)))

    def _mark_provider_failure(
        self, provider: str, models: tuple[str, ...], cooldown_seconds: float, *, status: int,
        kind: str = "network", stage: str = "api", quota_scope: str = "account", explicit_retry: bool = False,
    ) -> None:
        for candidate in models:
            self._state(provider, candidate).mark_failure(
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
        elif error.kind in {"empty", "invalid_response"} and error.finish_reason not in {"length", "MAX_TOKENS"}:
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
        waits = [item["cooldown_seconds"] for item in circuits.values() if not item["available"]]
        return {
            "configured": {"groq": self._groq is not None, "gemini": self._gemini is not None},
            "circuits": circuits,
            "earliest_retry_seconds": min(waits) if waits else None,
            "last_request": deepcopy(self._last_request),
        }

    def _available(self, provider: str, model: str) -> bool:
        state = self._states.get((provider, model))
        return state is None or state.is_available()

    def _retry_context(self, attempts) -> tuple[Optional[float], Optional[str], Optional[int]]:
        now = time.monotonic()
        waiting = [state for provider, _, models in attempts for model in models
                   if (state := self._states.get((provider, model))) is not None and not state.is_available()]
        if not waiting:
            return None, None, None
        earliest = min(waiting, key=lambda state: state.next_allowed_monotonic)
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
    ) -> str | ChatReply:
        actions = enabled_actions(actions)
        started = time.monotonic()
        has_images = any(message.images or message.image_urls for message in messages)
        mode = "vision" if has_images else "text"
        attempts: list[tuple[str, object, tuple[str, ...]]] = []
        if self._groq:
            attempts.append(("groq", self._groq, C.GROQ_VISION_MODELS if has_images else C.GROQ_MODELS))
        if self._gemini:
            attempts.append(("gemini", self._gemini,
                             getattr(C, "GEMINI_VISION_MODELS", C.GEMINI_MODELS) if has_images else C.GEMINI_MODELS))
        if not has_images:
            by_provider = {attempt[0]: attempt for attempt in attempts}
            priority = text_provider_order if text_provider_order is not None else ("groq", "gemini")
            attempts = [by_provider[name] for name in dict.fromkeys((*priority, *by_provider)) if name in by_provider]
        self._last_request = {"outcome": "pending", "mode": mode, "attempts": [], "skips": [], "attempt_count": 0}
        report = self._last_request

        def finish(kind: str, *, cause_kind=None, retry_after=None, outcome="failed"):
            report.update(outcome=outcome, kind=kind, cause_kind=cause_kind or kind,
                          retry_after=retry_after, attempt_count=len(report["attempts"]),
                          elapsed_ms=max(0, int((time.monotonic() - started) * 1000)))

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
        text_fallbacks = []
        wants_tools = bool(actions or tool_specs)
        has_native_history = any(message.tool_calls or message.role == "tool" for message in messages)

        async def attempt(provider: str, client, model: str, timeout: float, *, text_only=False):
            began = time.monotonic()
            state = self._state(provider, model)
            generation = state.failure_generation
            entry = {"provider": provider, "model": model, "budget_ms": max(0, int(timeout * 1000)), "text_only": text_only}
            report["attempts"].append(entry)
            kwargs = dict(system=system, messages=messages, temperature=temperature, model=model, timeout_seconds=timeout)
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
            try:
                reply = await self._call(client, kwargs)
                if not allow_tool_calls and isinstance(reply, ChatReply) and (reply.tool_calls or reply.proposals):
                    raise ProviderError("provider ignorou o fechamento sem ferramentas", kind="invalid_response", stage="output")
            except ProviderError as exc:
                entry.update(kind=exc.kind, stage=exc.stage, status=int(exc.status or 0),
                             retry_after=exc.retry_after, quota_scope=exc.quota_scope,
                             elapsed_ms=max(0, int((time.monotonic() - began) * 1000)))
                log.warning(
                    "chatbot: provider=%s model=%s mode=%s stage=%s kind=%s status=%s finish_reason=%s elapsed_ms=%d budget_ms=%d quota_scope=%s retry_after=%s",
                    provider, model, mode, exc.stage, exc.kind, int(exc.status or 0),
                    _diagnostic_finish_reason(exc.finish_reason), entry["elapsed_ms"], entry["budget_ms"], exc.quota_scope, exc.retry_after,
                )
                raise
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

        for index, (provider, client, models) in enumerate(attempts):
            remaining = max(0.0, deadline - time.monotonic())
            later = [item for item in attempts[index + 1:] if any(self._available(item[0], model) for model in item[2])]
            # O prazo deste provider é fixo durante sua cadeia. Recalcular a
            # reserva a cada modelo permitiria consumir aos poucos todo o fallback.
            reserve = min(10.0, remaining * .5) if later else 0.0
            provider_deadline = deadline - reserve
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
                    if exc.kind == "tools_unsupported" and wants_tools:
                        self._unsupported_tool_models.add((provider, model))
                        text_fallbacks.append((provider, client, model))
                        continue
                    stop, last_error = self._record_failure(provider, model, models, exc, expected_generation=generation)
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

        retry_after, cooldown_kind, cooldown_status = self._retry_context(attempts)
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
            kind = last_error.kind if last_error else "network"
            stage = last_error.stage if last_error else "api"
        cause_kind = cooldown_kind if kind == "cooldown" else last_error.kind if last_error else kind
        if kind not in {"cooldown", "rate_limit"}:
            retry_after = last_error.retry_after if last_error else None
        finish(kind, cause_kind=cause_kind, retry_after=retry_after)
        log.info("chatbot: exhausted mode=%s kind=%s cause_kind=%s attempts=%d elapsed_ms=%d earliest_retry=%s",
                 mode, kind, cause_kind, attempted, report["elapsed_ms"], retry_after)
        raise AllProvidersExhausted(
            "todos os providers estão temporariamente indisponíveis" if kind == "cooldown" else "todos os providers falharam",
            kind=kind, cause_kind=cause_kind, stage=stage, retry_after=retry_after,
            status=(cooldown_status if kind == "cooldown" else last_error.status if last_error else None),
            quota_scope=last_error.quota_scope if last_error else "model",
            finish_reason=last_error.finish_reason if last_error else None,
        )
