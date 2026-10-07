from __future__ import annotations

import hmac
import ipaddress
import math
import json
import logging
import asyncio
import queue
import os
import secrets
import threading
import time
from typing import Any, Callable

from flask import Response, g, jsonify, request


_MODEL_ID = "osaka-auto"
_MODEL_OWNER = "osaka"
_MODEL_CREATED = int(time.time())
_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_chat_provider: Callable[[dict[str, Any]], dict[str, Any]] | None = None
_chat_provider_lock = threading.RLock()
_chat_slots_lock = threading.RLock()
_chat_slots: threading.BoundedSemaphore | None = None
_chat_slots_size = 0

# Diagnóstico estruturado para identificar incompatibilidades com clientes
# OpenAI sem registrar mensagens, cabeçalhos, credenciais ou IPs completos.
_audit_logger = logging.getLogger("utility.openai_compat")
_AUDIT_ROUTES = {"/v1/models": "models", "/v1/chat/completions": "chat"}
_AUDIT_KNOWN_KEYS = frozenset({
    "model", "messages", "stream", "stream_options", "tools", "tool_choice",
    "temperature", "top_p", "top_k", "max_tokens", "max_completion_tokens",
    "n", "stop", "seed", "response_format", "reasoning_effort",
    "parallel_tool_calls", "modalities", "user", "frequency_penalty",
    "presence_penalty", "logit_bias", "logprobs", "top_logprobs",
    "service_tier", "prediction", "metadata", "store", "safety_identifier",
})
_AUDIT_ERROR_CODES = frozenset({
    "tailscale_required", "openai_compat_not_configured", "invalid_api_key",
    "invalid_json", "model_not_found", "invalid_stream", "invalid_stream_options",
    "unsupported_n", "unsupported_tools", "invalid_messages", "too_many_messages",
    "invalid_message", "unsupported_role", "unsupported_content", "context_too_large",
    "user_message_required", "invalid_temperature", "invalid_max_tokens",
    "request_too_large", "chat_provider_unavailable", "server_busy",
    "chat_backend_error", "stream_backend_unavailable", "invalid_backend_response",
    "chat_backend_timeout", "chatbot_unavailable", "discord_loop_unavailable",
    "rate_limit_exceeded", "upstream_timeout", "providers_unconfigured",
    "content_filter", "upstream_error", "other_error",
})


def _audit_enabled() -> bool:
    return str(os.getenv("BOT_OPENAI_AUDIT_ENABLED", "true")).strip().lower() not in {
        "0", "false", "off", "no",
    }


def _audit_code(code: Any) -> str:
    value = str(code or "")
    return value if value in _AUDIT_ERROR_CODES else "other_error"


def _audit_log(event: str, **fields: Any) -> None:
    if not _audit_enabled():
        return
    try:
        _audit_logger.info(
            "[osaka/openai-audit] %s",
            json.dumps({"event": event, **fields}, ensure_ascii=True, separators=(",", ":")),
        )
    except Exception:
        # Diagnóstico jamais deve interromper um pedido de IA.
        pass


def _audit_request_start() -> None:
    route = _AUDIT_ROUTES.get(request.path)
    if route is None or not _audit_enabled():
        return
    g.osaka_audit_id = secrets.token_hex(6)
    g.osaka_audit_started_at = time.monotonic()
    length = request.content_length
    _audit_log(
        "request", request_id=g.osaka_audit_id, route=route, method=request.method,
        is_json=bool(request.is_json),
        body_bytes=length if isinstance(length, int) and length >= 0 else None,
        auth_supplied=bool(request.headers.get("Authorization")),
    )


def _audit_request_finish(response: Response) -> Response:
    request_id = getattr(g, "osaka_audit_id", None)
    if request_id is None:
        return response
    response.headers["X-Osaka-Request-ID"] = request_id
    error_code = None
    if response.is_json and response.status_code >= 400:
        try:
            body = response.get_json(silent=True)
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                error_code = _audit_code(body["error"].get("code"))
        except Exception:
            error_code = "other_error"
    _audit_log(
        "http_response", request_id=request_id,
        route=_AUDIT_ROUTES.get(request.path), status=response.status_code,
        error_code=error_code,
        elapsed_ms=round((time.monotonic() - g.osaka_audit_started_at) * 1000),
        sse_headers=response.mimetype == "text/event-stream",
    )
    return response


def _audit_request_shape(payload: Any, body_bytes: int) -> None:
    """Somente presença/tipo/contagem; NUNCA valores textuais de mensagens."""
    request_id = getattr(g, "osaka_audit_id", None)
    if request_id is None:
        return
    if not isinstance(payload, dict):
        _audit_log("request_shape", request_id=request_id, json_object=False, body_bytes=body_bytes)
        return
    messages = payload.get("messages")
    roles = {"system": 0, "developer": 0, "user": 0, "assistant": 0, "tool": 0, "other": 0}
    arrays = 0
    media = 0
    non_text = 0
    if isinstance(messages, list):
        for message in messages[:256]:
            if not isinstance(message, dict):
                roles["other"] += 1
                continue
            role = message.get("role")
            roles[role if isinstance(role, str) and role in roles else "other"] += 1
            content = message.get("content")
            if isinstance(content, list):
                arrays += 1
                for block in content[:16]:
                    if not isinstance(block, dict):
                        non_text += 1
                    elif block.get("type") not in ("text", "input_text"):
                        non_text += 1
                        if block.get("type") in ("image_url", "input_image", "input_audio", "audio", "image"):
                            media += 1
            elif not isinstance(content, (str, type(None))):
                non_text += 1
    tools = payload.get("tools")
    stream = payload.get("stream", False)
    _audit_log(
        "request_shape", request_id=request_id, json_object=True, body_bytes=body_bytes,
        model_known=payload.get("model") == _MODEL_ID,
        stream=stream if isinstance(stream, bool) else "invalid",
        message_count=len(messages) if isinstance(messages, list) else None,
        roles=roles, content_arrays=arrays, non_text_blocks=non_text,
        media_blocks=media,
        tools_present=tools not in (None, [], ()),
        tool_count=len(tools) if isinstance(tools, list) else None,
        tool_choice_present=payload.get("tool_choice") is not None,
        response_format_present=payload.get("response_format") is not None,
        stream_options_present=payload.get("stream_options") is not None,
        reasoning_effort_present=payload.get("reasoning_effort") is not None,
        modalities_present=payload.get("modalities") is not None,
        top_p_present=payload.get("top_p") is not None,
        unknown_key_count=sum(key not in _AUDIT_KNOWN_KEYS for key in payload),
    )


class OpenAIChatStream:
    """Ponte limitada entre o event loop do Discord e as threads Waitress.

    Nunca bloqueia o loop Discord com queue.put(); o consumidor HTTP mantém a
    memória em <=64 eventos. close() cancela a requisição upstream quando o
    cliente fecha o stream ou o timeout é atingido.
    """

    def __init__(self, timeout: float):
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=64)
        self.deadline = time.monotonic() + max(1.0, timeout) + 4.0
        self._closed = threading.Event()
        self._future: Any = None
        self._lock = threading.Lock()
        self.fragments_sent = 0

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def start(self, future: Any) -> None:
        with self._lock:
            self._future = future
            if self.closed:
                future.cancel()

    async def _put(self, kind: str, value: Any) -> None:
        while not self.closed:
            try:
                self.events.put_nowait((kind, value))
                return
            except queue.Full:
                if time.monotonic() >= self.deadline:
                    self.close()
                    raise asyncio.CancelledError()
                await asyncio.sleep(0.025)
        raise asyncio.CancelledError()

    async def send(self, delta: str) -> None:
        if delta:
            await self._put("delta", delta)
            self.fragments_sent += 1

    async def finish(self, result: dict[str, Any]) -> None:
        if not self.closed:
            await self._put("done", result)

    def close(self) -> None:
        with self._lock:
            self._closed.set()
            future = self._future
        if future is not None and not future.done():
            future.cancel()



def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError, OverflowError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(minimum, min(maximum, value))


def _error(message: str, status: int, *, error_type: str, code: str, retry_after: float | None = None):
    response = jsonify({
        "error": {
            "message": message,
            "type": error_type,
            "param": None,
            "code": code,
        }
    })
    response.headers["Cache-Control"] = "no-store"
    if status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    if retry_after is not None and retry_after > 0:
        response.headers["Retry-After"] = str(max(1, int(math.ceil(retry_after))))
    return response, status


def _remote_is_private_ai_client() -> bool:
    """Aceita apenas localhost ou endereços da tailnet do Tailscale.

    Não confia em X-Forwarded-For: esta API é acessada diretamente pelo IP
    Tailscale da VPS. Assim o webserver principal pode continuar em 0.0.0.0 sem
    tornar a superfície OpenAI utilizável pela interface pública.
    """
    remote = str(request.remote_addr or "").strip()
    if not remote:
        return False
    remote = remote.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(remote)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return address in _TAILSCALE_V4
    return address in _TAILSCALE_V6


def _configured_api_key() -> str:
    return str(os.getenv("BOT_OPENAI_API_KEY") or "").strip()


def _bearer_token() -> str:
    authorization = str(request.headers.get("Authorization") or "").strip()
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    return token.strip()


def _authenticate():
    if not _remote_is_private_ai_client():
        return _error(
            "OpenAI compatibility API is available only from localhost or Tailscale.",
            403,
            error_type="permission_error",
            code="tailscale_required",
        )

    expected = _configured_api_key()
    if not expected:
        return _error(
            "OpenAI compatibility API is not configured.",
            503,
            error_type="server_error",
            code="openai_compat_not_configured",
        )

    supplied = _bearer_token()
    if not supplied or not hmac.compare_digest(supplied, expected):
        return _error(
            "Invalid API key.",
            401,
            error_type="authentication_error",
            code="invalid_api_key",
        )
    return None


def set_openai_chat_provider(provider: Callable[[dict[str, Any]], dict[str, Any]] | None) -> None:
    """Instala a ponte síncrona usada pelas threads do Waitress.

    O provider real pertence ao event loop do Discord; bot.py é responsável por
    atravessar a fronteira de thread com asyncio.run_coroutine_threadsafe().
    """
    global _chat_provider
    with _chat_provider_lock:
        _chat_provider = provider if callable(provider) else None


def _get_chat_provider() -> Callable[[dict[str, Any]], dict[str, Any]] | None:
    with _chat_provider_lock:
        return _chat_provider


def _chat_semaphore() -> threading.BoundedSemaphore:
    global _chat_slots, _chat_slots_size
    wanted = _env_int("BOT_OPENAI_MAX_CONCURRENT", 2, minimum=1, maximum=8)
    with _chat_slots_lock:
        if _chat_slots is None or _chat_slots_size != wanted:
            _chat_slots = threading.BoundedSemaphore(wanted)
            _chat_slots_size = wanted
        return _chat_slots


def _content_text(content: Any) -> tuple[str | None, str | None]:
    if isinstance(content, str):
        return content, None
    if content is None:
        return "", None
    if not isinstance(content, list):
        return None, "Message content must be a string or text-content array."
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            return None, "Only text content blocks are supported in this version."
        kind = str(block.get("type") or "").strip().lower()
        if kind in {"text", "input_text"} and isinstance(block.get("text"), str):
            parts.append(block["text"])
            continue
        return None, "Only text content blocks are supported in this version."
    return "".join(parts), None


def _normalize_chat_payload(payload: Any) -> tuple[dict[str, Any] | None, tuple[Any, int] | None]:
    if not isinstance(payload, dict):
        return None, _error("Request body must be a JSON object.", 400, error_type="invalid_request_error", code="invalid_json")

    model = str(payload.get("model") or "").strip()
    if model != _MODEL_ID:
        return None, _error("Unknown model.", 404, error_type="invalid_request_error", code="model_not_found")

    stream = payload.get("stream", False)
    if not isinstance(stream, bool):
        return None, _error("stream must be a boolean.", 400, error_type="invalid_request_error", code="invalid_stream")
    options = payload.get("stream_options") or {}
    if not isinstance(options, dict) or not isinstance(options.get("include_usage", False), bool):
        return None, _error("Invalid stream_options.", 400, error_type="invalid_request_error", code="invalid_stream_options")

    n = payload.get("n", 1)
    if isinstance(n, bool) or not isinstance(n, int) or n != 1:
        return None, _error("Only n=1 is supported.", 400, error_type="invalid_request_error", code="unsupported_n")

    tools = payload.get("tools")
    if tools not in (None, [], ()):
        return None, _error(
            "Tool calling is not enabled yet for this endpoint.",
            400,
            error_type="invalid_request_error",
            code="unsupported_tools",
        )

    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        return None, _error("messages must be a non-empty array.", 400, error_type="invalid_request_error", code="invalid_messages")
    max_messages = _env_int("BOT_OPENAI_MAX_MESSAGES", 100, minimum=1, maximum=256)
    if len(raw_messages) > max_messages:
        return None, _error("Too many messages.", 400, error_type="invalid_request_error", code="too_many_messages")

    system_parts: list[str] = []
    messages: list[dict[str, str]] = []
    total_chars = 0
    has_user = False
    max_chars = _env_int("BOT_OPENAI_MAX_TEXT_CHARS", 120_000, minimum=4_096, maximum=800_000)
    for index, raw in enumerate(raw_messages):
        if not isinstance(raw, dict):
            return None, _error(f"messages[{index}] must be an object.", 400, error_type="invalid_request_error", code="invalid_message")
        role = str(raw.get("role") or "").strip().lower()
        if role not in {"system", "developer", "user", "assistant"}:
            return None, _error(
                f"Unsupported message role at messages[{index}].",
                400,
                error_type="invalid_request_error",
                code="unsupported_role",
            )
        text, content_error = _content_text(raw.get("content"))
        if content_error is not None:
            return None, _error(content_error, 400, error_type="invalid_request_error", code="unsupported_content")
        assert text is not None
        total_chars += len(text)
        if total_chars > max_chars:
            return None, _error("Message content is too large.", 413, error_type="invalid_request_error", code="context_too_large")
        if role in {"system", "developer"}:
            if text.strip():
                system_parts.append(text)
            continue
        if role == "user":
            has_user = True
        messages.append({"role": role, "content": text})

    if not has_user:
        return None, _error("At least one user message is required.", 400, error_type="invalid_request_error", code="user_message_required")

    raw_temperature = payload.get("temperature", 0.8)
    if isinstance(raw_temperature, bool):
        return None, _error("temperature must be a number.", 400, error_type="invalid_request_error", code="invalid_temperature")
    try:
        temperature = float(raw_temperature)
    except (TypeError, ValueError, OverflowError):
        return None, _error("temperature must be a number.", 400, error_type="invalid_request_error", code="invalid_temperature")
    if not math.isfinite(temperature):
        return None, _error("temperature must be finite.", 400, error_type="invalid_request_error", code="invalid_temperature")
    temperature = max(0.0, min(1.5, temperature))

    max_output_tokens: int | None = None
    raw_max_tokens = payload.get("max_completion_tokens", payload.get("max_tokens"))
    if raw_max_tokens is not None:
        if isinstance(raw_max_tokens, bool) or not isinstance(raw_max_tokens, int) or raw_max_tokens <= 0:
            return None, _error("max_tokens must be a positive integer.", 400, error_type="invalid_request_error", code="invalid_max_tokens")
        max_output_tokens = min(raw_max_tokens, 4096)

    return {
        "model": _MODEL_ID,
        "stream": stream,
        "include_usage": bool(options.get("include_usage")),
        "system": "\n\n".join(system_parts),
        "messages": messages,
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
        "timeout_seconds": _env_float("BOT_OPENAI_REQUEST_TIMEOUT", 45.0, minimum=5.0, maximum=120.0),
    }, None


def _list_models():
    auth_error = _authenticate()
    if auth_error is not None:
        return auth_error

    response = jsonify({
        "object": "list",
        "data": [
            {
                "id": _MODEL_ID,
                "object": "model",
                "created": _MODEL_CREATED,
                "owned_by": _MODEL_OWNER,
            }
        ],
    })
    response.headers["Cache-Control"] = "no-store"
    return response, 200


def _chat_completions():
    auth_error = _authenticate()
    if auth_error is not None:
        return auth_error

    max_body = _env_int("BOT_OPENAI_MAX_BODY_BYTES", 1_048_576, minimum=16_384, maximum=4_194_304)
    if request.content_length is not None and request.content_length > max_body:
        return _error("Request body is too large.", 413, error_type="invalid_request_error", code="request_too_large")
    raw_body = request.get_data(cache=True)
    if len(raw_body) > max_body:
        return _error("Request body is too large.", 413, error_type="invalid_request_error", code="request_too_large")
    payload = request.get_json(silent=True)
    _audit_request_shape(payload, len(raw_body))
    spec, payload_error = _normalize_chat_payload(payload)
    if payload_error is not None:
        return payload_error
    assert spec is not None

    provider = _get_chat_provider()
    if provider is None:
        return _error(
            "Chat provider is not ready.",
            503,
            error_type="server_error",
            code="chat_provider_unavailable",
        )

    slot = _chat_semaphore()
    if not slot.acquire(blocking=False):
        return _error(
            "Too many concurrent requests.",
            429,
            error_type="rate_limit_error",
            code="server_busy",
            retry_after=1.0,
        )
    if spec["stream"]:
        try:
            stream_source = provider(spec)
        except Exception:
            slot.release()
            return _error("The chat backend failed unexpectedly.", 502,
                          error_type="server_error", code="chat_backend_error")
        if not isinstance(stream_source, OpenAIChatStream):
            slot.release()
            return _error("Streaming backend is unavailable.", 503,
                          error_type="server_error", code="stream_backend_unavailable")
        return _streaming_response(stream_source, slot, spec)

    try:
        try:
            result = provider(spec)
        except Exception:
            return _error(
                "The chat backend failed unexpectedly.",
                502,
                error_type="server_error",
                code="chat_backend_error",
            )
    finally:
        slot.release()

    if not isinstance(result, dict) or not result.get("ok"):
        result = result if isinstance(result, dict) else {}
        status = int(result.get("status") or 502)
        status = status if status in {400, 401, 403, 408, 409, 422, 429, 500, 502, 503, 504} else 502
        retry_after = result.get("retry_after")
        try:
            retry_after = float(retry_after) if retry_after is not None else None
        except (TypeError, ValueError, OverflowError):
            retry_after = None
        return _error(
            str(result.get("message") or "The chat backend is unavailable."),
            status,
            error_type=str(result.get("type") or ("rate_limit_error" if status == 429 else "server_error")),
            code=str(result.get("code") or "chat_backend_error"),
            retry_after=retry_after,
        )

    text = result.get("text")
    if not isinstance(text, str):
        return _error("The chat backend returned an invalid response.", 502, error_type="server_error", code="invalid_backend_response")

    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    prompt_tokens = max(0, int(usage.get("input_tokens") or 0))
    completion_tokens = max(0, int(usage.get("output_tokens") or 0))
    total_tokens = max(0, int(usage.get("total_tokens") or (prompt_tokens + completion_tokens)))
    response = jsonify({
        "id": "chatcmpl-" + secrets.token_hex(12),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": _MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    })
    response.headers["Cache-Control"] = "no-store"
    return response, 200


def _sse_data(value: Any) -> str:
    if value == "[DONE]":
        return "data: [DONE]\n\n"
    return "data: " + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def _streaming_response(source: OpenAIChatStream, slot: threading.BoundedSemaphore, spec: dict):
    chat_id = "chatcmpl-" + secrets.token_hex(12)
    created = int(time.time())
    request_id = getattr(g, "osaka_audit_id", None)
    audit_start = getattr(g, "osaka_audit_started_at", time.monotonic())
    stream_created_at = time.monotonic()

    def chunk(delta: dict, *, finish_reason: str | None = None, usage: dict | None = None):
        data = {"id": chat_id, "object": "chat.completion.chunk", "created": created,
                "model": _MODEL_ID, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
        if usage is not None:
            data["usage"] = usage
        return _sse_data(data)

    closed = threading.Event()
    cleanup_lock = threading.Lock()
    stats = {"delta_count": 0, "character_count": 0, "first_delta_ms": None,
             "sse_done_generated": False, "backend_code": None, "provider": None}

    def cleanup(outcome: str = "client_disconnected") -> None:
        with cleanup_lock:
            if closed.is_set():
                return
            closed.set()
        try:
            source.close()
        finally:
            slot.release()
            _audit_log(
                "stream_end", request_id=request_id, outcome=outcome,
                delta_count=stats["delta_count"], character_count=stats["character_count"],
                first_delta_ms=stats["first_delta_ms"],
                sse_done_generated=stats["sse_done_generated"],
                backend_code=stats["backend_code"], provider=stats["provider"],
                elapsed_ms=round((time.monotonic() - audit_start) * 1000),
                stream_elapsed_ms=round((time.monotonic() - stream_created_at) * 1000),
            )

    def generate():
        outcome = "client_disconnected"
        try:
            yield chunk({"role": "assistant"})
            while True:
                remaining = source.deadline - time.monotonic()
                if remaining <= 0:
                    outcome = "timeout"
                    stats["backend_code"] = "chat_backend_timeout"
                    yield _sse_data({"error": {"message": "Chat backend timed out.",
                                                  "type": "server_error", "code": "chat_backend_timeout"}})
                    stats["sse_done_generated"] = True
                    yield _sse_data("[DONE]")
                    break
                try:
                    kind, value = source.events.get(timeout=min(5.0, remaining))
                except queue.Empty:
                    if source.closed:
                        outcome = "upstream_cancelled"
                        break
                    yield ": keep-alive\n\n"
                    continue
                if kind == "delta":
                    stats["delta_count"] += 1
                    stats["character_count"] += len(value) if isinstance(value, str) else 0
                    if stats["first_delta_ms"] is None:
                        stats["first_delta_ms"] = round((time.monotonic() - audit_start) * 1000)
                    yield chunk({"content": value})
                    continue
                if kind == "done":
                    if not isinstance(value, dict) or not value.get("ok"):
                        outcome = "backend_error"
                        error = value if isinstance(value, dict) else {}
                        stats["backend_code"] = _audit_code(error.get("code"))
                        yield _sse_data({"error": {
                            "message": str(error.get("message") or "Chat backend is unavailable."),
                            "type": str(error.get("type") or "server_error"),
                            "code": str(error.get("code") or "chat_backend_error"),
                        }})
                    else:
                        outcome = "complete"
                        provider = str(value.get("provider") or "").lower()
                        stats["provider"] = provider if provider in {"groq", "gemini", "mistral", "cloudflare"} else "other"
                        finish = value.get("finish_reason")
                        yield chunk({}, finish_reason="length" if finish == "length" else "stop")
                        if spec.get("include_usage"):
                            usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
                            counters = ("input_tokens", "output_tokens", "total_tokens")
                            measured = all(type(usage.get(field)) is int and usage[field] >= 0 for field in counters)
                            openai_usage = None
                            if measured:
                                openai_usage = {"prompt_tokens": usage["input_tokens"],
                                                "completion_tokens": usage["output_tokens"],
                                                "total_tokens": usage["total_tokens"]}
                            yield _sse_data({"id": chat_id, "object": "chat.completion.chunk", "created": created,
                                             "model": _MODEL_ID, "choices": [], "usage": openai_usage})
                    stats["sse_done_generated"] = True
                    yield _sse_data("[DONE]")
                    break
        except GeneratorExit:
            outcome = "client_disconnected"
            raise
        except Exception:
            outcome = "stream_internal_error"
            raise
        finally:
            cleanup(outcome)

    response = Response(generate(), content_type="text/event-stream; charset=utf-8")
    response.headers["Cache-Control"] = "no-cache, no-store"
    response.headers["X-Accel-Buffering"] = "no"
    response.call_on_close(cleanup)
    return response


def register_openai_compat_routes(app: Any) -> None:
    """Registra a superfície OpenAI compatível no servidor já existente."""
    # O after_request registra o status inclusive nas recusas de autenticação
    # e validação. Streaming possui, adicionalmente, um evento terminal próprio.
    if _audit_request_start not in app.before_request_funcs.get(None, []):
        app.before_request(_audit_request_start)
    if _audit_request_finish not in app.after_request_funcs.get(None, []):
        app.after_request(_audit_request_finish)
    if "openai_compat_models" not in getattr(app, "view_functions", {}):
        app.add_url_rule(
            "/v1/models",
            endpoint="openai_compat_models",
            view_func=_list_models,
            methods=["GET"],
        )
    if "openai_compat_chat_completions" not in getattr(app, "view_functions", {}):
        app.add_url_rule(
            "/v1/chat/completions",
            endpoint="openai_compat_chat_completions",
            view_func=_chat_completions,
            methods=["POST"],
        )
