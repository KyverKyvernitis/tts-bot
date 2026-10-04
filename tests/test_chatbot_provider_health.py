"""O painel mostra circuitos reais sem credenciais ou conteúdo de conversas."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot.providers import ProviderRouter
from tests.test_chatbot_config_controls import _cog, _interaction


def test_configured_key_is_not_a_claim_of_remote_service_health():
    cog = _cog()
    cog._router = ProviderRouter(object(), groq_key="secret-groq-key")
    rendered = cog._provider_status()
    assert "Groq" in rendered and "configurado" in rendered
    assert "Gemini:** chave não configurada" in rendered
    assert "secret-groq-key" not in rendered


def test_one_limited_model_does_not_make_the_panel_claim_entire_provider_unavailable():
    cog = _cog()
    cog._router = ProviderRouter(object(), groq_key="secret", gemini_key="secret")
    cog._router._state("groq", C.GROQ_MODELS[0]).mark_failure(60, status=429)
    assert len(C.GROQ_MODELS) > 1
    rendered = cog._provider_status()
    assert "Groq:** configurado; há modelos em espera" in rendered
    assert "Gemini:** configurado; sem bloqueio local" in rendered


@pytest.mark.asyncio
async def test_config_panel_reads_live_provider_circuits_without_network_requests():
    cog = _cog()
    cog._router = ProviderRouter(object(), groq_key="secret", gemini_key="secret")
    for model in C.GEMINI_MODELS:
        cog._router._state("gemini", model).mark_failure(60, status=429)
    interaction = _interaction()
    with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=True)):
        await cog._do_configurar(interaction)
    content = interaction.followup.send.await_args.args[0]
    assert "Groq → Gemini" in content
    assert "Gemini:** temporariamente indisponível" in content
    assert "próxima tentativa" in content
    assert "secret" not in content
    for model in C.GEMINI_MODELS:
        cog._router._state("gemini", model).mark_success()
    another = _interaction()
    with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=True)):
        await cog._do_configurar(another)
    assert "Gemini:** configurado; sem bloqueio local" in another.followup.send.await_args.args[0]


def test_provider_panel_distinguishes_rejected_credentials_and_never_renders_raw_diagnostics():
    cog = _cog()
    cog._router = SimpleNamespace(diagnostics=lambda: {
        "configured": {"groq": True, "gemini": True},
        "circuits": {
            f"groq/{model}": {"available": False, "cooldown_seconds": 900, "last_kind": "auth", "last_status": 400}
            for model in C.GROQ_MODELS
        },
        "last_request": {"message": "PRIVATE PROMPT", "api_key": "PRIVATE KEY"},
        "raw_body": "PRIVATE BODY",
    })
    rendered = cog._provider_status()
    assert "Groq:** credencial recusada" in rendered
    assert "PRIVATE" not in rendered


@pytest.mark.asyncio
async def test_provider_diagnostics_are_not_read_before_configuration_authorization():
    cog = _cog()
    diagnostics = AsyncMock()
    cog._router = SimpleNamespace(diagnostics=diagnostics)
    with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=False)):
        await cog._do_configurar(_interaction())
    diagnostics.assert_not_called()
