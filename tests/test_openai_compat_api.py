from __future__ import annotations

import pytest

flask = pytest.importorskip("flask")
Flask = flask.Flask

from utility.openai_compat import register_openai_compat_routes


def _client():
    app = Flask("openai-compat-test")
    app.testing = True
    register_openai_compat_routes(app)
    return app.test_client()


def _get(client, *, remote: str = "100.64.10.20", token: str | None = None):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.get("/v1/models", headers=headers, environ_base={"REMOTE_ADDR": remote})


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


def test_route_registration_is_idempotent():
    app = Flask("openai-compat-idempotent")
    register_openai_compat_routes(app)
    register_openai_compat_routes(app)

    rules = [str(rule) for rule in app.url_map.iter_rules()]
    assert rules.count("/v1/models") == 1
