from __future__ import annotations

import pytest

flask = pytest.importorskip("flask")
Flask = flask.Flask

from utility.openai_compat import OpenAIChatStream, register_openai_compat_routes, set_openai_chat_provider


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


def _props(client, *, remote: str = "100.64.10.20", token: str | None = None):
    return client.get("/props", headers=_headers(token), environ_base={"REMOTE_ADDR": remote})


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


def test_r61_capability_discovery_without_bearer_for_tailscale(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    client = _client()
    models = _get(client)
    props = _props(client)

    assert models.status_code == 200
    assert props.status_code == 200
    assert models.headers["Cache-Control"] == "no-store"
    assert props.headers["Cache-Control"] == "no-store"
    body = models.get_json()
    assert [item["id"] for item in body["data"]] == [
        "osaka-auto", "osaka-fast", "osaka-smart",
    ]
    assert all(item["capabilities"] == ["tools"] for item in body["data"])
    assert all(item["kind"] == "chat" for item in body["data"])
    props_body = props.get_json()
    assert props_body["chat_template_caps"]["supports_tools"] is True
    assert props_body["modalities"]["vision"] is False
    assert props_body["default_generation_settings"]["n_ctx"] == 4096


def test_r61_discovery_blocks_public_address_even_without_bearer(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    client = _client()
    for response in (_get(client, remote="203.0.113.25"),
                     _props(client, remote="203.0.113.25")):
        assert response.status_code == 403
        assert response.get_json()["error"]["code"] == "tailscale_required"


def test_r61_discovery_does_not_trust_forwarded_ip(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    response = _client().get(
        "/props", headers={"X-Forwarded-For": "127.0.0.1"},
        environ_base={"REMOTE_ADDR": "203.0.113.25"},
    )
    assert response.status_code == 403


def test_r61_discovery_rejects_explicit_invalid_authorization(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    client = _client()
    for response in (_get(client, token="invalid"), _props(client, token="invalid")):
        assert response.status_code == 401
        assert response.get_json()["error"]["code"] == "invalid_api_key"
    assert _get(client, token="correct-secret").status_code == 200
    assert _props(client, token="correct-secret").status_code == 200


def test_r61_discovery_remains_disabled_without_server_key(monkeypatch):
    monkeypatch.delenv("BOT_OPENAI_API_KEY", raising=False)
    assert _get(_client()).status_code == 503
    assert _props(_client()).status_code == 503


def test_models_allows_missing_bearer_but_rejects_wrong_bearer(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    client = _client()

    missing = _get(client)
    wrong = _get(client, token="wrong-secret")

    assert missing.status_code == 200  # Read-only discovery is safe without Bearer on tailnet.
    assert wrong.status_code == 401
    assert wrong.get_json()["error"]["code"] == "invalid_api_key"
    assert wrong.headers["WWW-Authenticate"] == "Bearer"


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
    assert [item["id"] for item in body["data"]] == [
        "osaka-auto", "osaka-fast", "osaka-smart",
    ]
    assert all(item["object"] == "model" for item in body["data"])
    assert all(item["owned_by"] == "osaka" for item in body["data"])
    assert all(isinstance(item["created"], int) for item in body["data"])
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
    assert seen["routing_profile"] == "auto"


@pytest.mark.parametrize(("model", "profile"), [
    ("osaka-auto", "auto"),
    ("osaka-fast", "fast"),
    ("osaka-smart", "smart"),
])
def test_r5_virtual_models_select_profile_and_echo_requested_model(monkeypatch, model, profile):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    seen = {}

    def provider(spec):
        seen.update(spec)
        return {"ok": True, "text": "ok", "provider": "groq", "usage": {}}

    set_openai_chat_provider(provider)
    response = _post(_client(), {
        "model": model,
        "messages": [{"role": "user", "content": "teste"}],
        "stream": False,
    })

    assert response.status_code == 200
    assert response.get_json()["model"] == model
    assert seen["model"] == model
    assert seen["routing_profile"] == profile


def test_r5_virtual_profile_cannot_be_overridden_by_untrusted_payload(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    seen = {}

    def provider(spec):
        seen.update(spec)
        return {"ok": True, "text": "ok", "usage": {}}

    set_openai_chat_provider(provider)
    response = _post(_client(), {
        "model": "osaka-fast",
        "routing_profile": "smart",
        "messages": [{"role": "user", "content": "teste"}],
    })
    assert response.status_code == 200
    assert seen["routing_profile"] == "fast"
    assert response.get_json()["model"] == "osaka-fast"


def test_r5_malformed_model_id_is_rejected_not_a_server_error(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    monkeypatch.setenv("BOT_OPENAI_AUDIT_ENABLED", "true")
    response = _post(_client(), {
        "model": ["osaka-auto"],
        "messages": [{"role": "user", "content": "oi"}],
    })
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "model_not_found"


def test_chat_requires_auth_and_tailscale(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    set_openai_chat_provider(lambda spec: {"ok": True, "text": "x", "usage": {}})
    payload = {"model": "osaka-auto", "messages": [{"role": "user", "content": "x"}]}

    missing = _post(_client(), payload, token=None)
    public = _post(_client(), payload, remote="203.0.113.2")

    assert missing.status_code == 401
    assert public.status_code == 403


def test_chat_rejects_invalid_tools_and_unknown_model(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    set_openai_chat_provider(lambda spec: {"ok": True, "text": "x", "usage": {}})
    base = {"model": "osaka-auto", "messages": [{"role": "user", "content": "x"}]}

    tools = _post(_client(), {**base, "tools": [{"type": "function", "function": {"name": "INVALID NAME"}}]})
    unknown = _post(_client(), {**base, "model": "other-model"})

    assert tools.status_code == 400
    assert tools.get_json()["error"]["code"] == "invalid_tools"
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
    assert rules.count("/props") == 1
    assert rules.count("/v1/chat/completions") == 1


def _sse_payloads(response):
    import json
    data = response.get_data(as_text=True)
    return [json.loads(line[6:]) if line[6:] != "[DONE]" else "[DONE]"
            for line in data.splitlines() if line.startswith("data: ")]


def test_streaming_sse_incremental_openai_schema(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    seen = {}

    def provider(spec):
        seen.update(spec)
        stream = OpenAIChatStream(5)
        stream.events.put_nowait(("delta", "Olá"))
        stream.events.put_nowait(("delta", " mundo"))
        stream.events.put_nowait(("done", {
            "ok": True, "usage": {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9}
        }))
        return stream

    set_openai_chat_provider(provider)
    response = _post(_client(), {
        "model": "osaka-auto", "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "Olá"}],
    })
    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    assert response.headers["X-Accel-Buffering"] == "no"
    events = _sse_payloads(response)
    assert events[0]["object"] == "chat.completion.chunk"
    assert events[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert [e["choices"][0]["delta"]["content"] for e in events[1:3]] == ["Olá", " mundo"]
    assert events[3]["choices"][0]["finish_reason"] == "stop"
    assert events[4]["choices"] == []
    assert events[4]["usage"] == {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}
    assert events[5] == "[DONE]"
    assert seen["stream"] is True
    response.close()


@pytest.mark.parametrize("model", ["osaka-auto", "osaka-fast", "osaka-smart"])
def test_r5_streaming_chunks_echo_virtual_model(monkeypatch, model):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")

    def provider(_spec):
        stream = OpenAIChatStream(5)
        stream.events.put_nowait(("delta", "ok"))
        stream.events.put_nowait(("done", {"ok": True, "provider": "groq", "usage": {}}))
        return stream

    set_openai_chat_provider(provider)
    response = _post(_client(), {
        "model": model, "stream": True,
        "messages": [{"role": "user", "content": "x"}],
    })
    assert response.status_code == 200
    events = _sse_payloads(response)
    chunks = [event for event in events if isinstance(event, dict) and event.get("object") == "chat.completion.chunk"]
    assert chunks
    assert all(event["model"] == model for event in chunks)
    response.close()


def test_streaming_backend_error_is_not_a_successful_finish(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")

    def provider(spec):
        stream = OpenAIChatStream(5)
        stream.events.put_nowait(("done", {"ok": False, "code": "upstream_timeout",
                                          "message": "Provider deadline reached."}))
        return stream

    set_openai_chat_provider(provider)
    response = _post(_client(), {"model": "osaka-auto", "stream": True,
                                 "messages": [{"role": "user", "content": "Olá"}]})
    assert response.status_code == 200  # headers SSE já foram enviados
    events = _sse_payloads(response)
    assert events[1]["error"]["code"] == "upstream_timeout"
    assert events[2] == "[DONE]"
    assert not any(isinstance(e, dict) and e.get("choices", [{}])[0].get("finish_reason") == "stop"
                   for e in events if isinstance(e, dict) and e.get("choices"))
    response.close()


def test_streaming_http_disconnect_cancels_source_and_releases_slot(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    monkeypatch.setenv("BOT_OPENAI_MAX_CONCURRENT", "1")
    sources = []

    def provider(spec):
        stream = OpenAIChatStream(10)
        sources.append(stream)
        return stream

    set_openai_chat_provider(provider)
    client = _client()
    payload = {"model": "osaka-auto", "stream": True,
               "messages": [{"role": "user", "content": "Olá"}]}
    response = _post(client, payload)
    assert response.status_code == 200
    first = next(iter(response.response))
    assert b'"role":"assistant"' in first
    competing = _post(client, payload)
    assert competing.status_code == 429
    assert competing.get_json()["error"]["code"] == "server_busy"
    response.close()
    assert sources[0].closed
    followup = _post(client, payload)
    assert followup.status_code == 200
    followup.close()


def test_streaming_invalid_boolean_rejected(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    set_openai_chat_provider(lambda spec: {"ok": True, "text": "x", "usage": {}})
    invalid = _post(_client(), {"model": "osaka-auto", "stream": "yes",
                                "messages": [{"role": "user", "content": "x"}]})
    assert invalid.status_code == 400
    assert invalid.get_json()["error"]["code"] == "invalid_stream"


def _audit_events(caplog):
    import json
    prefix = "[osaka/openai-audit] "
    return [json.loads(message[len(prefix):]) for record in caplog.records
            if (message := record.getMessage()).startswith(prefix)]


def test_r4_audit_logs_request_shape_and_rejection_without_sensitive_data(monkeypatch, caplog):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "very-private-token-never-log")
    monkeypatch.setenv("BOT_OPENAI_AUDIT_ENABLED", "true")
    payload = {
        "model": "osaka-auto", "stream": True, "top_p": 1,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "secret-prompt-never-log"}]}],
        "tools": [{"type": "function", "function": {"name": "private-function-name"}}],
        "tool_choice": "auto", "response_format": {"type": "json_object"},
        "a-secret-custom-param": "secret-param-never-log",
    }
    with caplog.at_level("INFO", logger="utility.openai_compat"):
        response = _post(_client(), payload, token="very-private-token-never-log")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_tools"
    request_id = response.headers.get("X-Osaka-Request-ID")
    assert isinstance(request_id, str) and len(request_id) == 12
    entries = _audit_events(caplog)
    assert [e["event"] for e in entries] == ["request", "request_shape", "http_response"]
    assert all(e["request_id"] == request_id for e in entries)
    assert entries[1]["tools_present"] is True
    assert entries[1]["tool_choice_present"] is True
    assert entries[1]["response_format_present"] is True
    assert entries[1]["stream"] is True
    assert entries[1]["content_arrays"] == 1
    assert entries[1]["unknown_key_count"] == 1
    assert entries[2]["status"] == 400
    assert entries[2]["error_code"] == "invalid_tools"
    for sensitive in ("very-private-token-never-log", "secret-prompt-never-log",
                      "private-function-name", "secret-param-never-log", "a-secret-custom-param"):
        assert sensitive not in caplog.text


def test_r4_audit_captures_auth_rejections_without_parsing_body(monkeypatch, caplog):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "valid-secret")
    monkeypatch.setenv("BOT_OPENAI_AUDIT_ENABLED", "true")
    with caplog.at_level("INFO", logger="utility.openai_compat"):
        result = _post(_client(), {"messages": [{"role": "user", "content": "secret"}]}, token=None)
    assert result.status_code == 401
    events = _audit_events(caplog)
    assert [e["event"] for e in events] == ["request", "http_response"]
    assert events[0]["auth_supplied"] is False
    assert events[1]["error_code"] == "invalid_api_key"


def test_r4_audit_stream_lifecycle_with_sse_success(monkeypatch, caplog):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    monkeypatch.setenv("BOT_OPENAI_AUDIT_ENABLED", "true")

    def provider(_spec):
        stream = OpenAIChatStream(5)
        stream.events.put_nowait(("delta", "Olá"))
        stream.events.put_nowait(("delta", "!"))
        stream.events.put_nowait(("done", {"ok": True, "provider": "groq"}))
        return stream

    set_openai_chat_provider(provider)
    with caplog.at_level("INFO", logger="utility.openai_compat"):
        response = _post(_client(), {"model": "osaka-auto", "stream": True,
                                    "messages": [{"role": "user", "content": "Olá"}]})
        assert response.status_code == 200
        assert _sse_payloads(response)[-1] == "[DONE]"
        response.close()

    entries = _audit_events(caplog)
    assert [e["event"] for e in entries] == ["request", "request_shape", "http_response", "stream_end"]
    assert entries[2]["status"] == 200 and entries[2]["sse_headers"] is True
    assert entries[3]["outcome"] == "complete"
    assert entries[3]["sse_done_generated"] is True
    assert entries[3]["delta_count"] == 2
    assert entries[3]["character_count"] == 4
    assert isinstance(entries[3]["first_delta_ms"], int)
    assert entries[3]["provider"] == "groq"
    assert entries[3]["request_id"] == response.headers["X-Osaka-Request-ID"]


def test_r4_audit_stream_disconnect_is_not_logged_as_success(monkeypatch, caplog):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    monkeypatch.setenv("BOT_OPENAI_AUDIT_ENABLED", "true")
    source = OpenAIChatStream(10)
    set_openai_chat_provider(lambda _spec: source)
    with caplog.at_level("INFO", logger="utility.openai_compat"):
        response = _post(_client(), {"model": "osaka-auto", "stream": True,
                                    "messages": [{"role": "user", "content": "hello"}]})
        next(iter(response.response))
        response.close()
    events = _audit_events(caplog)
    endings = [event for event in events if event["event"] == "stream_end"]
    assert len(endings) == 1
    assert endings[0]["outcome"] == "client_disconnected"
    assert endings[0]["sse_done_generated"] is False
    assert source.closed


def test_r4_audit_can_be_disabled(monkeypatch, caplog):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    monkeypatch.setenv("BOT_OPENAI_AUDIT_ENABLED", "false")
    with caplog.at_level("INFO", logger="utility.openai_compat"):
        response = _get(_client(), token="correct-secret")
    assert response.status_code == 200
    assert "X-Osaka-Request-ID" not in response.headers
    assert not _audit_events(caplog)


def test_r4_audit_stream_backend_error_has_terminal_code_even_on_http_200(monkeypatch, caplog):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    monkeypatch.setenv("BOT_OPENAI_AUDIT_ENABLED", "true")

    def provider(_spec):
        stream = OpenAIChatStream(5)
        stream.events.put_nowait(("done", {"ok": False, "code": "upstream_timeout",
                                          "message": "Backend error text is not part of audit."}))
        return stream

    set_openai_chat_provider(provider)
    with caplog.at_level("INFO", logger="utility.openai_compat"):
        response = _post(_client(), {"model": "osaka-auto", "stream": True,
                                    "messages": [{"role": "user", "content": "Olá"}]})
        events = _sse_payloads(response)
        response.close()
    assert response.status_code == 200
    assert events[1]["error"]["code"] == "upstream_timeout"
    audit = _audit_events(caplog)
    assert audit[-2]["event"] == "http_response" and audit[-2]["status"] == 200
    assert audit[-1]["event"] == "stream_end"
    assert audit[-1]["outcome"] == "backend_error"
    assert audit[-1]["backend_code"] == "upstream_timeout"
    assert audit[-1]["sse_done_generated"] is True
    assert "Backend error text is not part of audit." not in caplog.text


def test_r4_audit_models_exposes_request_id_without_credentials(monkeypatch, caplog):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "secret-should-stay-private")
    monkeypatch.setenv("BOT_OPENAI_AUDIT_ENABLED", "true")
    with caplog.at_level("INFO", logger="utility.openai_compat"):
        response = _get(_client(), token="secret-should-stay-private")
    assert response.status_code == 200
    entries = _audit_events(caplog)
    assert [e["event"] for e in entries] == ["request", "http_response"]
    assert entries[0]["route"] == "models"
    assert entries[1]["status"] == 200
    assert response.headers.get("X-Osaka-Request-ID") == entries[0]["request_id"]
    assert "secret-should-stay-private" not in caplog.text


_R6_CALCULATOR = {"type": "function", "function": {
    "name": "calculator", "description": "Evaluate arithmetic", "parameters": {
        "type": "object", "properties": {"expression": {"type": "string"}},
        "required": ["expression"], "additionalProperties": False,
    },
}}


def _r6_call(call_id="call_123", name="calculator", arguments='{"expression":"2+3"}'):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def test_r6_tool_call_nonstream_returns_only_client_execution_instruction(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    seen = {}
    calls = [_r6_call()]

    def provider(spec):
        seen.update(spec)
        return {"ok": True, "text": "", "tool_calls": calls, "usage": {}}

    set_openai_chat_provider(provider)
    response = _post(_client(), {
        "model": "osaka-auto", "messages": [{"role": "user", "content": "Quanto é 2+3?"}],
        "tools": [_R6_CALCULATOR], "tool_choice": "auto", "stream": False,
    })
    assert response.status_code == 200
    choice = response.get_json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"] == calls
    assert seen["allow_tool_calls"] is True
    assert seen["tools"] == [_R6_CALCULATOR["function"]]


def test_r6_tool_results_history_roundtrip(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    seen = {}
    set_openai_chat_provider(lambda spec: (seen.update(spec) or {"ok": True, "text": "5", "usage": {}}))
    response = _post(_client(), {
        "model": "osaka-auto", "tools": [_R6_CALCULATOR],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Quanto é 2+3?"}]},
            {"role": "assistant", "content": "", "tool_calls": [_r6_call()]},
            {"role": "tool", "tool_call_id": "call_123", "content": [{"type": "text", "text": "5"}]},
        ],
    })
    assert response.status_code == 200
    assert seen["messages"][1]["tool_calls"][0] == {
        "id": "call_123", "name": "calculator", "arguments": {"expression": "2+3"},
    }
    assert seen["messages"][2] == {"role": "tool", "content": "5", "tool_call_id": "call_123", "name": "calculator"}
    assert response.get_json()["choices"][0]["finish_reason"] == "stop"


def test_r6_invalid_tool_result_reference_rejected_before_backend(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    called = []
    set_openai_chat_provider(lambda spec: (called.append(spec) or {"ok": True, "text": "ok"}))
    response = _post(_client(), {"model": "osaka-auto", "tools": [_R6_CALCULATOR],
        "messages": [{"role": "user", "content": "oi"},
                     {"role": "tool", "tool_call_id": "call_fake", "content": "5"}]})
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_tool_history"
    assert not called


def test_r6_tool_choice_none_forbids_new_calls(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    seen = {}
    set_openai_chat_provider(lambda spec: (seen.update(spec) or {"ok": True, "text": "ok"}))
    response = _post(_client(), {"model": "osaka-fast", "tools": [_R6_CALCULATOR],
        "tool_choice": "none", "messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 200
    assert seen["allow_tool_calls"] is False
    assert seen["tools"]
    bad = _post(_client(), {"model": "osaka-fast", "tools": [_R6_CALCULATOR],
        "tool_choice": "required", "messages": [{"role": "user", "content": "hello"}]})
    assert bad.status_code == 400
    assert bad.get_json()["error"]["code"] == "unsupported_tool_choice"


def test_r6_streaming_native_tool_calls_offgrid_shape(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    calls = [_r6_call()]
    def provider(spec):
        source = OpenAIChatStream(4)
        source.events.put_nowait(("done", {"ok": True, "provider": "groq", "tool_calls": calls,
                                           "usage": {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12}}))
        return source
    set_openai_chat_provider(provider)
    response = _post(_client(), {"model": "osaka-smart", "stream": True,
        "messages": [{"role": "user", "content": "2+3"}], "tools": [_R6_CALCULATOR]})
    assert response.status_code == 200
    events = _sse_payloads(response)
    assert events[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert events[1]["choices"][0]["delta"]["tool_calls"] == [{"index": 0, **calls[0]}]
    assert events[2]["choices"][0]["finish_reason"] == "tool_calls"
    assert events[3] == "[DONE]"
    response.close()


def test_r6_backend_cannot_inject_unrequested_function(monkeypatch):
    monkeypatch.setenv("BOT_OPENAI_API_KEY", "correct-secret")
    set_openai_chat_provider(lambda _spec: {"ok": True, "text": "", "tool_calls": [_r6_call(name="admin_ban")], "usage": {}})
    response = _post(_client(), {"model": "osaka-auto", "tools": [_R6_CALCULATOR],
        "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 502
    assert response.get_json()["error"]["code"] == "invalid_backend_tools"
