"""Reserva Qwen e contagens agregadas no painel sem credenciais ou texto privado."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot.commands import ChatbotCommandsMixin
from cogs.chatbot.cog import ChatbotCog
from cogs.chatbot.config import GuildChatbotConfig
from cogs.chatbot.views import ProviderConfigModal
from tests.test_chatbot_config_controls import _cog, _interaction


def _render(data, turn=None):
    cog = _cog()
    cog._router = SimpleNamespace(diagnostics=lambda: data)
    if turn is not None:
        cog._last_turn_usage = turn
    return cog._provider_status()


@pytest.mark.parametrize("order", [("groq", "gemini"), ("gemini", "groq")])
def test_qwen_is_displayed_after_both_saved_provider_orders(order):
    config = GuildChatbotConfig(guild_id=10, text_provider_order=order)
    rendered = ChatbotCommandsMixin._format_config(config)
    expected = "Groq → Gemini" if order[0] == "groq" else "Gemini → Groq"
    assert expected + " → Cloudflare (texto)" in rendered
    assert config.text_provider_order == order




def test_mistral_reserve_appears_in_order_only_when_enabled(monkeypatch):
    config = GuildChatbotConfig(guild_id=10, text_provider_order=("groq", "gemini"))
    monkeypatch.setattr(C, "MISTRAL_ENABLED", False)
    assert "Mistral" not in ChatbotCommandsMixin._format_config(config)
    monkeypatch.setattr(C, "MISTRAL_ENABLED", True)
    rendered = ChatbotCommandsMixin._format_config(config)
    assert "Groq → Gemini → Mistral (texto) → Cloudflare (texto)" in rendered


def test_mistral_status_never_prints_key_and_explains_opt_in():
    rendered = _render({
        "configured": {"mistral": False}, "circuits": {},
        "mistral_setup": {"enabled": False, "api_key_configured": True, "api_key": "PRIVATE_KEY"},
    })
    assert "Mistral · Ministral adaptativo:** desativada" in rendered
    assert "CHATBOT_MISTRAL_ENABLED=true" in rendered
    assert "PRIVATE_KEY" not in rendered


@pytest.mark.parametrize("account,token,missing", [
    (False, False, "ID da conta e token da API"),
    (True, False, "token da API"),
    (False, True, "ID da conta"),
])
def test_qwen_setup_explains_missing_identity_without_printing_secrets(account, token, missing):
    rendered = _render({
        "configured": {"cloudflare": False}, "circuits": {},
        "cloudflare_setup": {"account_id_configured": account, "api_token_configured": token,
                             "account_id": "PRIVATE ACCOUNT", "api_token": "PRIVATE TOKEN"},
    })
    assert f"Cloudflare · Qwen:** falta {missing}; reserva de texto." in rendered
    assert "PRIVATE" not in rendered


def test_qwen_daily_quota_wait_is_shown_for_the_fixed_model():
    model = C.CLOUDFLARE_MODELS[0]
    rendered = _render({
        "configured": {"cloudflare": True}, "circuits": {
            f"cloudflare/{model}": {"available": False, "last_kind": "rate_limit",
                                    "last_status": 429, "cooldown_seconds": 7200},
        }, "models": {"cloudflare": [model, "PRIVATE MODEL", "@cf/other/arbitrary"]},
    })
    assert "Cloudflare · Qwen:** temporariamente indisponível; próxima tentativa em cerca de 2h00min" in rendered
    assert f"{model}: limite de uso; espera de cerca de 2h00min" in rendered
    assert "PRIVATE" not in rendered and "arbitrary" not in rendered


def test_qwen_local_budget_separates_measured_and_uncertain_without_secrets():
    rendered = _render({
        "configured": {"cloudflare": True}, "circuits": {},
        "cloudflare_setup": {"budget": {"limit_neurons": 10000, "measured_neurons": 12.5,
                                          "uncertain_reserved_neurons": 1.25,
                                          "api_token": "PRIVATE"}},
    })
    assert "Reserva local Cloudflare:** 12.500 + 1.250 reservados/incertos / 10000 neurons" in rendered
    assert "0.1% contabilizado neste processo" in rendered
    assert "PRIVATE" not in rendered


def test_existing_image_credentials_do_not_imply_chat_activation():
    rendered = _render({
        "configured": {"cloudflare": False}, "circuits": {},
        "cloudflare_setup": {"enabled": False, "account_id_configured": True,
                             "api_token_configured": True},
    })
    assert "desativada" in rendered
    assert "Workers Free" in rendered and "CHATBOT_CLOUDFLARE_ENABLED=true" in rendered
    assert "falta ID" not in rendered


def test_whole_turn_usage_includes_cache_and_reasoning_without_double_counting():
    rendered = _render({
        "configured": {}, "circuits": {},
        "last_request": {"attempts": [{"usage": {"input_tokens": 9, "output_tokens": 9}}]},
    }, {
        "usage": {"input_tokens": 3000, "output_tokens": 150, "total_tokens": 3150,
                  "cached_tokens": 2300, "reasoning_tokens": 25,
                  "prompt": "PRIVATE PROMPT"},
        "request_count": 3, "usage_complete": True,
        "wasted_usage": {"total_tokens": 315}, "wasted_token_ratio": .1,
        "providers": {"groq": {"usage": {"total_tokens": 2000}},
                      "mistral": {"usage": {"total_tokens": 1150}}},
        "tools": {"seen": 3, "executed": 2, "reused_reads": 1},
        "delivery": {"delivered": True, "model_rounds_per_response": 2,
                     "generation_attempts_per_response": 3, "total_tokens_per_response": 3150,
                     "neurons_per_response": 0.375},
        "stages": {"initial": {"calls": 1, "usage": {"total_tokens": 2100}},
                   "closing": {"calls": 1, "usage": {"total_tokens": 1050}}},
    })
    assert "Tokens do último turno medido:** entrada 3000, saída 150" in rendered
    assert "cache 2300 (incluído na entrada)" in rendered
    assert "raciocínio 25 (incluído na saída)" in rendered
    assert "3 tentativas" in rendered
    assert "descartados 315 (10.0%)" in rendered
    assert "Uso por provedor:** Groq 2000, Mistral 1150 tokens" in rendered
    assert "Ferramentas no último turno:** 2 execuções; 1 leitura(s) repetida(s) reaproveitada(s)" in rendered
    assert "Eficiência da resposta entregue:** 2 rodada(s) de modelo, 3 tentativa(s), 3150 tokens/resposta, 0.375 neurons/resposta" in rendered
    assert "Rodadas:** inicial 1x/2100 tok, fechamento 1x/1050 tok" in rendered
    assert "contagem parcial" not in rendered and "última tentativa" not in rendered
    assert "PRIVATE" not in rendered


def test_partially_reported_turn_counts_remain_partial():
    rendered = _render({"configured": {}, "circuits": {}}, {
        "usage": {"input_tokens": 200, "output_tokens": 10}, "usage_complete": False,
        "request_count": 2,
    })
    assert "contagem parcial" in rendered
    assert "cache" not in rendered and "raciocínio" not in rendered


def test_invalid_turn_metadata_falls_back_to_last_safe_attempt():
    rendered = _render({
        "configured": {}, "circuits": {},
        "last_request": {"attempts": [{"usage": {"input_tokens": 200, "output_tokens": 10}}]},
    }, {"usage": {"input_tokens": True, "output_tokens": "PRIVATE"}, "request_count": "PRIVATE"})
    assert "Tokens da última tentativa medida:** entrada 200, saída 10" in rendered
    assert "PRIVATE" not in rendered


@pytest.mark.asyncio
async def test_qwen_is_not_a_new_primary_or_model_selector_in_the_modal():
    initial = GuildChatbotConfig(guild_id=10)
    save = AsyncMock()
    modal = ProviderConfigModal(requester_id=40, current_config=initial,
                                on_submit_config=save, check_authorized=AsyncMock(return_value=True))
    assert [option.value for option in modal.provider_group.options] == ["groq", "gemini"]
    modal.provider_group._value = "gemini"
    await modal.on_submit(_interaction())
    assert save.await_args.args[1] == replace(initial, text_provider_order=("gemini", "groq"))


@pytest.mark.asyncio
async def test_provider_diagnostics_are_not_read_before_staff_authorization():
    cog = _cog()
    diagnostics = AsyncMock(side_effect=AssertionError("must not be called"))
    cog._router = SimpleNamespace(diagnostics=diagnostics)
    with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=False)):
        await cog._do_configurar(_interaction())
    diagnostics.assert_not_called()


@pytest.mark.asyncio
async def test_knowledge_publication_requires_staff_and_uses_real_channel_scope():
    from cogs.chatbot.memory import MemoryEpoch

    cog = _cog()
    cog._memory = SimpleNamespace(capture_epoch=AsyncMock(return_value=MemoryEpoch()))
    cog._knowledge = SimpleNamespace(publish=AsyncMock(return_value={"ref": "k-123456abcdef"}))
    interaction = _interaction()
    interaction.channel = interaction.guild.get_channel(20)
    with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=False)):
        await cog._do_conhecimento(interaction, acao="salvar", titulo="Evento", conteudo="A reunião é amanhã.")
    cog._knowledge.publish.assert_not_awaited()
    cog._memory.capture_epoch.assert_not_awaited()

    with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=True)):
        await cog._do_conhecimento(interaction, acao="salvar", titulo="Evento", conteudo="A reunião é amanhã.")
    cog._knowledge.publish.assert_awaited_once_with(
        10, 20, "channel:20", MemoryEpoch(), title="Evento", content="A reunião é amanhã.",
        tags="", guild_public=False, ref="",
    )


@pytest.mark.parametrize("value,expected", [(None, False), ("false", False), ("true", True),
                                             ("invalid", False), ("1", False)])
def test_cloudflare_flag_import_defaults_off_and_requires_explicit_true(value, expected):
    # Fresh module in a child process: importing constants in another pytest
    # test never changes the shared module's environment-backed options.
    code = (
        "import importlib.util, json; "
        f"spec = importlib.util.spec_from_file_location('startup_constants', {str(Path(C.__file__))!r}); "
        "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "print(json.dumps(module.CLOUDFLARE_ENABLED))"
    )
    environment = {} if value is None else {"CHATBOT_CLOUDFLARE_ENABLED": value}
    result = subprocess.run([sys.executable, "-c", code], env=environment,
                            check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_cog_startup_and_shutdown_wire_explicit_cloudflare_flag_without_network(monkeypatch, enabled):
    import asyncio
    from cogs.chatbot import cog as module

    # Real startup reaches the actual ProviderRouter constructor, which used
    # to fail on the missing constant before any fallback could be prepared.
    monkeypatch.setattr(C, "CLOUDFLARE_ENABLED", enabled)
    for key in ("GROQ_API_KEY", "GEMINI_API_KEY", "CLOUDFLARE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "offline-test-token")
    collection = object()
    indexes, migrations = AsyncMock(), AsyncMock()
    monkeypatch.setattr("cogs.chatbot.db.get_chatbot_collection", lambda _: collection)
    monkeypatch.setattr("cogs.chatbot.db.ensure_indexes", indexes)
    monkeypatch.setattr("cogs.chatbot.migrations.run_migrations", migrations)
    session = SimpleNamespace(close=AsyncMock(), post=Mock(side_effect=AssertionError("No HTTP allowed")))
    monkeypatch.setattr(module.aiohttp, "TCPConnector", lambda **_: object())
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda **_: session)
    actions = SimpleNamespace(initialize=AsyncMock(), shutdown=Mock(), ready=True)
    monkeypatch.setattr(module, "ActionService", lambda *_: actions)

    async def maintenance(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(ChatbotCog, "_cooldown_cleanup_loop", maintenance)
    cog = ChatbotCog(SimpleNamespace(settings_db=object()))
    try:
        await cog.cog_load()
        indexes.assert_awaited_once_with(collection)
        migrations.assert_awaited_once_with(collection)
        assert cog._router is not None and cog._cleanup_task is not None
        diagnostics = cog._router.diagnostics()
        assert diagnostics["cloudflare_setup"]["enabled"] is enabled
        assert diagnostics["configured"]["cloudflare"] is enabled
        assert (cog._router._cloudflare is not None) is enabled
        assert "offline-test-token" not in str(diagnostics)
        session.post.assert_not_called()
        actions.initialize.assert_awaited_once()
    finally:
        await cog.cog_unload()
    actions.shutdown.assert_called_once()
    session.close.assert_awaited_once()
