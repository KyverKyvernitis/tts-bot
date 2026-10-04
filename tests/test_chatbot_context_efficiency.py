"""Contexto econômico mantém autoria, informações úteis e medidas honestas."""
from copy import deepcopy

import pytest

from cogs.chatbot.context_efficiency import (
    TurnUsage, compact_operational_state, deduplicate_reply_context, spontaneous_quota_factor,
)
from cogs.chatbot.providers import ChatMessage


def test_exact_quoted_reply_reuses_history_without_repeating_long_text():
    text = "A resposta explicada, com todos os detalhes. " * 15
    history = [ChatMessage("user", "Explique"), ChatMessage("assistant", text)]
    reference = deduplicate_reply_context(f'respondendo a Osaka: "{text}"', history)
    assert "Osaka" in reference and "mensagem 2" in reference
    assert text not in reference
    assert history[1].content == text
    assert len(reference) < len(text) // 3


@pytest.mark.parametrize("quoted", [
    'respondendo a Osaka: "Texto corrigido"',
    "respondendo a Osaka: [anexo de áudio]", "Mensagem desconhecida", None,
])
def test_unmatched_quote_or_attachment_is_preserved(quoted):
    assert deduplicate_reply_context(quoted, [ChatMessage("assistant", "Texto original")]) == quoted


def test_compaction_keeps_authority_in_host_and_live_voice_preferences_in_prompt():
    host = {
        "guild_id": 1, "channel_id": 2, "user_id": 3, "bot_id": 4,
        "voice_connected": True, "voice_channel_id": 5,
        "voice_state": {"author": {"channel_id": "5"}, "bot": {"connected": True}},
        "preferences": {"mode": "audio", "voice": "pt-BR-FranciscaNeural", "language": "pt-BR"},
        "providers": {"availability": [{"provider": "groq", "model": "x", "available": True}]},
        "references": {"members": {"m1": {"id": "123456789012345678", "name": "Flora"}}},
        "action_draft": {"target_id": "123456789012345678", "target_ref": "m1", "action": "timeout_member",
                         "missing_fields": ["duration_seconds"], "draft_id": "internal-draft", "revision": 2},
    }
    original = deepcopy(host)
    reduced = compact_operational_state(host)
    assert host == original
    assert reduced["voice_state"] == host["voice_state"]
    assert reduced["preferences"] == host["preferences"]
    assert reduced["references"]["members"]["m1"] == {"name": "Flora"}
    assert reduced["action_draft"] == {"target_ref": "m1", "action": "timeout_member",
                                      "missing_fields": ["duration_seconds"]}
    assert "guild_id" not in reduced and "voice_connected" not in reduced
    assert "providers" not in reduced


def test_measured_cache_is_subset_and_missing_attempt_marks_turn_partial():
    usage = TurnUsage()
    usage.record({"outcome": "success", "request_count": 2, "generation_attempt_count": 2,
                  "usage_attempt_count": 1, "usage_complete": False,
                  "usage": {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100,
                            "cached_tokens": 800, "reasoning_tokens": 20},
                  "usage_field_attempts": {"input_tokens": 1, "cached_tokens": 1}})
    result = usage.result()
    assert result["usage"]["total_tokens"] == 1100
    assert result["request_count"] == 2 and result["usage_complete"] is False
    assert result["usage_field_attempts"]["cached_tokens"] == 1
    usage.record({})
    assert usage.result()["request_count_complete"] is False


def test_turn_telemetry_separates_wasted_usage_neurons_and_context_without_content():
    usage = TurnUsage()
    usage.record_context({"system_chars": 1000, "history_chars": 800, "tool_schema_chars": 400})
    usage.record({"outcome": "success", "request_count": 2, "generation_attempt_count": 2,
                  "usage_attempt_count": 2, "usage_complete": True,
                  "usage": {"input_tokens": 300, "output_tokens": 40, "total_tokens": 340,
                            "cached_tokens": 150, "neurons": 1.25},
                  "usage_field_attempts": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 2},
                  "attempts": [
                      {"provider": "cloudflare", "model": "qwen", "kind": "rate_limit",
                       "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "neurons": .4}},
                      {"provider": "mistral", "model": "small", "kind": "success",
                       "usage": {"input_tokens": 200, "output_tokens": 30, "total_tokens": 230, "neurons": .85}},
                  ]})
    usage.record_tool_call()
    usage.record_tool_call(executed=True, seen=False)
    usage.record_tool_call()
    usage.record_tool_call(reused_read=True, seen=False)
    result = usage.result()
    assert result["resources"]["neurons"] == 1.25
    assert result["wasted_usage"]["total_tokens"] == 110
    assert result["successful_usage"]["total_tokens"] == 230
    assert result["wasted_resources"]["neurons"] == .4
    assert result["successful_resources"]["neurons"] == .85
    assert result["fallback_attempts"] == 1
    assert result["cache_hit_ratio"] == .5
    assert result["context"]["peak"]["history_chars"] == 800
    assert result["wasted_token_ratio"] == pytest.approx(110 / 340, abs=1e-4)
    assert result["wasted_neuron_ratio"] == .32
    assert result["providers"]["cloudflare"]["failed"] == 1
    assert result["models"]["mistral/small"]["successes"] == 1
    assert result["tools"] == {"seen": 2, "executed": 1, "reused_reads": 1}


@pytest.mark.parametrize("records,expected", [
    ([{"provider": "groq", "configured": True, "modes": ["text"], "available": False, "cause_kind": "rate_limit"},
      {"provider": "gemini", "configured": True, "modes": ["text"], "available": False, "cause_kind": "rate_limit"},
      {"provider": "mistral", "configured": True, "modes": ["text"], "available": True}], 0),
    ([{"provider": "groq", "configured": True, "modes": ["text"], "available": False, "cause_kind": "rate_limit"},
      {"provider": "gemini", "configured": True, "modes": ["text"], "available": True}], .25),
    ([{"provider": "groq", "configured": True, "modes": ["text"], "available": False, "cause_kind": "network"},
      {"provider": "gemini", "configured": True, "modes": ["text"], "available": True}], .5),
    ([{"provider": "groq", "configured": True, "modes": ["text"], "available": True},
      {"provider": "gemini", "configured": True, "modes": ["text"], "available": True}], 1),
    ([{"provider": "mistral", "configured": True, "modes": ["text"], "available": True},
      {"provider": "cloudflare", "configured": True, "modes": ["text"], "available": True}], 0),
])
def test_spontaneous_participation_tracks_primary_quota_pressure(records, expected):
    assert spontaneous_quota_factor({"availability": records}) == expected


def test_missing_or_vision_only_quota_state_does_not_suppress_chat():
    assert spontaneous_quota_factor({}) == 1
    assert spontaneous_quota_factor({"availability": [{"configured": True, "provider": "test",
                                                        "available": False, "modes": ["vision"]}]}) == 1
