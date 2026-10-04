"""O painel distingue quota longa, modelo ausente e consumo medido."""
from types import SimpleNamespace

from cogs.chatbot import constants as C
from tests.test_chatbot_config_controls import _cog


def _render(data):
    cog = _cog()
    cog._router = SimpleNamespace(diagnostics=lambda: data)
    return cog._provider_status()


def test_missing_model_does_not_shorten_the_wait_for_a_limited_model():
    limited, missing = C.GEMINI_MODELS[:2]
    result = _render({
        "configured": {"groq": False, "gemini": True},
        "circuits": {
            f"gemini/{limited}": {"available": False, "last_kind": "rate_limit", "last_status": 429,
                                    "cooldown_seconds": 77994},
            f"gemini/{missing}": {"available": False, "last_kind": "model", "last_status": 404,
                                    "cooldown_seconds": 300},
        },
    })
    assert "próxima tentativa em cerca de 21h40min" in result
    assert f"{missing}: modelo indisponível." in result
    assert f"{limited}: limite de uso; espera de cerca de 21h40min." in result
    assert "5min" not in result and "77994s" not in result


def test_discovered_usable_model_prevents_false_provider_unavailable_message():
    missing = C.GEMINI_MODELS[0]
    result = _render({
        "configured": {"gemini": True},
        "models": {"gemini": [missing, "gemini-discovered-flash"]},
        "circuits": {f"gemini/{missing}": {"available": False, "last_kind": "model",
                                              "last_status": 404, "cooldown_seconds": 300}},
    })
    assert "Gemini:** configurado; há modelos em espera" in result
    assert "Gemini:** temporariamente indisponível" not in result


def test_summary_considers_all_cached_models_even_when_details_are_shortened():
    models = [f"gemini-candidate-{index}" for index in range(8)]
    result = _render({
        "configured": {"gemini": True}, "models": {"gemini": models},
        "circuits": {f"gemini/{model}": {"available": False, "last_kind": "rate_limit",
                                             "last_status": 429, "cooldown_seconds": 60}
                     for model in models[:-1]},
    })
    assert "Gemini:** configurado; há modelos em espera" in result
    assert "Gemini:** temporariamente indisponível" not in result
    assert result.count("limite de uso") == 2


def test_panel_uses_only_safe_token_metadata_and_ignores_raw_provider_content():
    result = _render({
        "configured": {"gemini": True}, "circuits": {},
        "models": {"gemini": ["private\nresponse", "gemini-safe-model"]},
        "raw_body": "PRIVATE BODY", "api_key": "PRIVATE KEY",
        "last_request": {"system": "PRIVATE PROMPT", "attempts": [
            {"usage": {"input_tokens": 1250, "output_tokens": 135, "total_tokens": 1385},
             "raw": "PRIVATE AUDIO"},
            {"usage": {"input_tokens": "PRIVATE TOKENS", "output_tokens": True}},
        ]},
    })
    assert "entrada 1250, saída 135" in result
    assert "PRIVATE" not in result and "private" not in result


def test_missing_models_and_rejected_keys_have_no_arbitrary_retry_promise():
    for kind, status in (("model", 404), ("auth", 401)):
        result = _render({
            "configured": {"groq": True}, "circuits": {
                f"groq/{model}": {"available": False, "last_kind": kind,
                                  "last_status": status, "cooldown_seconds": 300}
                for model in C.GROQ_MODELS
            },
        })
        assert "próxima tentativa" not in result
        assert "5min" not in result
