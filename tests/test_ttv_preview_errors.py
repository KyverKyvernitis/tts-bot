"""Private preview failures preserve settings without dumping worker commands."""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from test_ttv_ui import cog_for, interaction, modules


@pytest.mark.parametrize(('error', 'expected'), [
    (RuntimeError("TimeoutExpired: Command '['/data/data/com.termux/files/usr/bin/python', '/data/data/private/resampler.py']' timed out after 90 seconds"), 'tempo de espera'),
    (subprocess.TimeoutExpired(['/private/python', '/private/resampler.py'], 90), 'tempo de espera'),
    (asyncio.TimeoutError(), 'tempo de espera'),
    (RuntimeError('Pururu Púrpura indisponível: /data/data/private/bank missing'), 'indisponível'),
    (RuntimeError('fonema necessário para esta frase: /private/native.wav'), 'pronunciar'),
    (ValueError('comando do resampler inválido: /private/credentials.env'), 'Não consegui gerar'),
])
def test_preview_reports_safe_actionable_failure_and_keeps_preferences(error, expected, capsys):
    async def run():
        _, actions, _ = modules()
        cog = cog_for({'ttv_voice_id': 'pururu-purpura', 'ttv_language': 'pt-BR'})
        cog._ttv_preview_mp3.side_effect = error
        view = actions.VisaoConfirmacaoTTV(cog, 10, 20, 10, 'Usuário', None)
        event = interaction()
        await view.ouvir_amostra(event)
        payload = event.followup.send.await_args.kwargs
        assert payload['ephemeral'] and expected in payload['content']
        assert 'ajustes continuam salvos' in payload['content']
        assert not any(marker in payload['content'] for marker in ('/private', '/data/data', 'Command', 'resampler', 'TimeoutExpired'))
        assert 'file' not in payload
        cog._set_user_tts_and_refresh.assert_not_awaited()
        assert not view._preview_running
    asyncio.run(run())
    assert '[ttv_preview] geração falhou:' in capsys.readouterr().out


def test_preview_can_retry_after_a_worker_timeout():
    async def run():
        _, actions, _ = modules()
        cog = cog_for({'ttv_voice_id': 'pururu-purpura', 'ttv_language': 'pt-BR'})
        cog._ttv_preview_mp3.side_effect = [RuntimeError('TimeoutExpired: /private/resampler.py'), b'ID3-native-pururu']
        view = actions.VisaoConfirmacaoTTV(cog, 10, 20, 10, 'Usuário', None)
        await view.ouvir_amostra(interaction())
        retry = interaction()
        await view.ouvir_amostra(retry)
        payload = retry.followup.send.await_args.kwargs
        assert payload['file'].filename == 'ttv-amostra.mp3'
        assert payload['file'].fp.read() == b'ID3-native-pururu'
        assert cog._ttv_preview_mp3.await_count == 2
        assert not view._preview_running
    asyncio.run(run())
