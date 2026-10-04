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


@pytest.mark.parametrize("blocked,eligible,expected", [(2, 0, 0), (1, 1, .25), (0, 2, 1)])
def test_spontaneous_participation_tracks_known_quota_pressure(blocked, eligible, expected):
    records = [{"provider": f"provider-{index}", "configured": True, "modes": ["text"],
                "available": False, "cause_kind": "rate_limit"} for index in range(blocked)]
    records += [{"provider": f"eligible-{index}", "configured": True, "modes": ["text"],
                 "available": True} for index in range(eligible)]
    assert spontaneous_quota_factor({"availability": records}) == expected


def test_missing_or_vision_only_quota_state_does_not_suppress_chat():
    assert spontaneous_quota_factor({}) == 1
    assert spontaneous_quota_factor({"availability": [{"configured": True, "provider": "test",
                                                        "available": False, "modes": ["vision"]}]}) == 1
