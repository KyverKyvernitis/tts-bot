from __future__ import annotations

import pytest

flask = pytest.importorskip("flask")
Flask = flask.Flask

from utility.openai_compat import register_openai_compat_routes, set_openai_chat_provider


@pytest.fixture(autouse=True)
def _reset_chat_provider():
    set_openai_chat_provider(None)
    yield
    set_openai_chat_provider(None)


def _client():
    app = Flask("openai-compat-test")
    app.testing = True
    register_openai_compat_routes(app)
    return app.test_client()


def _headers(token: str | None):
    return {"Authorization": f"Bearer {token}"} if token is not None else {}


def _get(client, *, remote: str = "100.64.10.20", token: str | None = None):
    return client.get("/v1/models", headers=_headers(token), environ_base={"REMOTE_ADDR": remote})


def _post(client, payload, *, remote: str = "100.64.10.20", token: str | None = "correct-secret"):
    return client.post(
        "/v1/chat/completions",
        json=payload,
        headers=_headers(token),
        environ_base={"REMOTE_ADDR": remote},
    )


def test_models_returns_503_when_server_key_is_not_configured(monkeypatch):
    monkeypatch.delenv("BOT_OPENAI_API_KEY", raising=False)
    response = _get(_client(), token="anything")

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "openai_compat_not_configured"
    assert response.headers["Cache-Control"] == "no-store"


def test_models_rejects_missing_and_wrong_bearer(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    client = _client()

    missing = _get(client)
    wrong = _get(client, token="wrong-secret")

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.get_json()["error"]["code"] == "invalid_api_key"
    assert wrong.get_json()["error"]["code"] == "invalid_api_key"
    assert missing.headers["WWW-Authenticate"] == "Bearer"


def test_models_rejects_non_tailscale_remote_even_with_valid_key(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    response = _get(_client(), remote="203.0.113.50", token="correct-secret")

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "tailscale_required"


@pytest.mark.parametrize("remote", ["127.0.0.1", "::1", "100.64.0.1", "100.127.255.254", "fd7a:115c:a1e0::1234"])
def test_models_accepts_loopback_and_tailscale_addresses(monkeypatch, remote):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    response = _get(_client(), remote=remote, token="correct-secret")

    assert response.status_code == 200
    body = response.get_json()
    assert body["object"] == "list"
    assert body["data"] == [
        {
            "id": "osaka-auto",
            "object": "model",
            "created": body["data"][0]["created"],
            "owned_by": "osaka",
        }
    ]
    assert isinstance(body["data"][0]["created"], int)
    assert response.headers["Cache-Control"] == "no-store"


def test_chat_completion_calls_bridge_and_returns_openai_shape(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    seen = {}

    def provider(spec):
        seen.update(spec)
        return {
            "ok": True,
            "text": "Olá do Osaka.",
            "provider": "groq",
            "provider_model": "example-model",
            "usage": {"input_tokens": 7, "output_tokens": 4, "total_tokens": 11},
        }

    set_openai_chat_provider(provider)
    response = _post(_client(), {
        "model": "osaka-auto",
        "messages": [
            {"role": "system", "content": "Seja breve."},
            {"role": "user", "content": "Olá"},
        ],
        "stream": False,
        "temperature": 0.4,
    })

    assert response.status_code == 200
    body = response.get_json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "osaka-auto"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "Olá do Osaka."}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11}
    assert seen["system"] == "Seja breve."
    assert seen["messages"] == [{"role": "user", "content": "Olá"}]
    assert seen["temperature"] == 0.4


def test_chat_requires_auth_and_tailscale(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    set_openai_chat_provider(lambda spec: {"ok": True, "text": "x", "usage": {}})
    payload = {"model": "osaka-auto", "messages": [{"role": "user", "content": "x"}]}

    missing = _post(_client(), payload, token=None)
    public = _post(_client(), payload, remote="203.0.113.2")

    assert missing.status_code == 401
    assert public.status_code == 403


def test_chat_rejects_streaming_tools_and_unknown_model(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    set_openai_chat_provider(lambda spec: {"ok": True, "text": "x", "usage": {}})
    base = {"model": "osaka-auto", "messages": [{"role": "user", "content": "x"}]}

    streaming = _post(_client(), {**base, "stream": True})
    tools = _post(_client(), {**base, "tools": [{"type": "function", "function": {"name": "x"}}]})
    unknown = _post(_client(), {**base, "model": "other-model"})

    assert streaming.status_code == 400
    assert streaming.get_json()["error"]["code"] == "unsupported_stream"
    assert tools.status_code == 400
    assert tools.get_json()["error"]["code"] == "unsupported_tools"
    assert unknown.status_code == 404
    assert unknown.get_json()["error"]["code"] == "model_not_found"


def test_chat_maps_backend_error_and_retry_after(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    set_openai_chat_provider(lambda spec: {
        "ok": False,
        "status": 429,
        "code": "rate_limit_exceeded",
        "type": "rate_limit_error",
        "message": "Temporarily limited.",
        "retry_after": 2.2,
    })
    response = _post(_client(), {"model": "osaka-auto", "messages": [{"role": "user", "content": "x"}]})

    assert response.status_code == 429
    assert response.get_json()["error"]["code"] == "rate_limit_exceeded"
    assert response.headers["Retry-After"] == "3"


def test_route_registration_is_idempotent():
    app = Flask("openai-compat-idempotent")
    register_openai_compat_routes(app)
    register_openai_compat_routes(app)

    rules = [str(rule) for rule in app.url_map.iter_rules()]
    assert rules.count("/v1/models") == 1
    assert rules.count("/v1/chat/completions") == 1
