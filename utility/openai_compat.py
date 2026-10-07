from __future__ import annotations

import hmac
import ipaddress
import math
import os
import secrets
import threading
import time
from typing import Any, Callable

from flask import jsonify, request


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

    if payload.get("stream") is True:
        return None, _error(
            "Streaming is not enabled yet for this endpoint.",
            400,
            error_type="invalid_request_error",
            code="unsupported_stream",
        )

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


def register_openai_compat_routes(app: Any) -> None:
    """Registra a superfície OpenAI compatível no servidor já existente."""
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
