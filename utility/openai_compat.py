from __future__ import annotations

import hmac
import ipaddress
import os
import time
from typing import Any

from flask import jsonify, request


_MODEL_ID = "osaka-auto"
_MODEL_OWNER = "osaka"
_MODEL_CREATED = int(time.time())
_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def _error(message: str, status: int, *, error_type: str, code: str):
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
    return response, status


def _remote_is_private_ai_client() -> bool:
    """Aceita apenas localhost ou endereços da tailnet do Tailscale.

    Não confia em X-Forwarded-For: a primeira versão é intencionalmente direta,
    acessada pelo IP Tailscale da VPS. Isso evita expor a API OpenAI compatível
    pela interface pública apenas porque o webserver principal escuta em 0.0.0.0.
    """
    remote = str(request.remote_addr or "").strip()
    if not remote:
        return False
    # IPv6 link-local pode carregar zone id (ex.: "%tailscale0").
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


def register_openai_compat_routes(app: Any) -> None:
    """Registra a superfície OpenAI mínima sem iniciar outro servidor/processo."""
    if "openai_compat_models" in getattr(app, "view_functions", {}):
        return
    app.add_url_rule(
        "/v1/models",
        endpoint="openai_compat_models",
        view_func=_list_models,
        methods=["GET"],
    )
