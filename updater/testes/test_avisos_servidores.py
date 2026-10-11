"""Regressões do fluxo de avisos temporários, sem dependência de rede/discord.py."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / 'updater' / 'discord' / 'avisos_servidores.py'


class HTTPException(Exception):
    pass


class NotFound(HTTPException):
    pass


fake_discord = types.SimpleNamespace(
    HTTPException=HTTPException,
    NotFound=NotFound,
    AllowedMentions=types.SimpleNamespace(none=lambda: None),
)


def load_module(monkeypatch):
    monkeypatch.setitem(sys.modules, 'discord', fake_discord)
    spec = importlib.util.spec_from_file_location('standalone_voice_notices', MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Channel:
    def __init__(self, guild_id, channel_id):
        self.guild_id = guild_id
        self.id = channel_id
        self.sent = []
        self.deleted = []

    def permissions_for(self, member):
        return types.SimpleNamespace(view_channel=True, send_messages=True)

    async def send(self, message, **kwargs):
        msgid = 100 + len(self.sent)
        self.sent.append((msgid, message, kwargs))
        return types.SimpleNamespace(id=msgid)

    def get_partial_message(self, msg_id):
        async def delete():
            self.deleted.append(msg_id)
        return types.SimpleNamespace(delete=delete)


class Guild:
    def __init__(self, channel, *, humans=True):
        self.id = channel.guild_id
        self.channel = channel
        self.me = object()
        voice_members = [types.SimpleNamespace(bot=False)] if humans else [types.SimpleNamespace(bot=True)]
        voice_channel = types.SimpleNamespace(members=voice_members)
        self.voice_client = types.SimpleNamespace(is_connected=lambda: True, channel=voice_channel)

    def get_channel(self, cid):
        return self.channel if cid == self.channel.id else None


async def case(monkeypatch, tmp_path):
    mod = load_module(monkeypatch)
    channel = Channel(12, 230)
    guild = Guild(channel)
    class Bot(mod.AvisosServidoresUpdaterMixin):
        _repo_root = tmp_path
        guilds = [guild]
        def get_cog(self, key):
            return types.SimpleNamespace(guild_states={12: types.SimpleNamespace(last_text_channel_id=230)})
        def get_channel(self, cid):
            return channel if cid == 230 else None
        def is_ready(self):
            return True

    bot = Bot()
    expected_start = mod.START_MESSAGE
    expected_success = mod.SUCCESS_MESSAGE
    expected_failure = mod.FAILURE_MESSAGE
    assert expected_start == '-# Vou atualizar rapidinho, talvez eu pare de responder por alguns segundos <:speed:1534287805424074772>'
    assert expected_success == '-# Volteii, tô normal agr <:osaka:1539137127852539944>'
    assert expected_failure == '-# Atualização deu ruim, talvez tenha outra a caminho logo logo'
    result = await bot._server_notice_start({'run_id': 'valid-run-101'})
    assert result == {'ok': True, 'sent': 1}
    assert len(channel.sent) == 1 and channel.sent[0][1] == expected_start
    assert channel.sent[0][2]['allowed_mentions'] is None
    assert (await bot._server_notice_start({'run_id': 'valid-run-101'}))['duplicate'] is True
    assert len(channel.sent) == 1
    root = bot._server_notice_root()
    (root / 'valid-run-101.result.json').write_text(json.dumps({'result': 'success'}))
    mod.TTL_SECONDS = 0
    await bot._server_notice_reconcile_once()
    assert [item[1] for item in channel.sent] == [expected_start, expected_success]
    assert channel.deleted == [101, 100], channel.deleted
    assert not (root / 'valid-run-101.json').exists()
    assert not (root / 'valid-run-101.result.json').exists()

    # Após um reinício, o índice de canal salvo ainda deve funcionar.
    bot.record_tts_channel_for_updates(12, 230)
    reboot = Bot()
    reboot.get_cog = lambda name: types.SimpleNamespace(guild_states={})
    await reboot._server_notice_start({'run_id': 'after-reboot'})
    assert len(channel.sent) == 3
    (root / 'after-reboot.result.json').write_text(json.dumps({'result': 'failure'}))
    await reboot._server_notice_reconcile_once()
    assert channel.sent[-1][1] == expected_failure
    assert channel.deleted[-2:] == [103, 102]

    # Não avisar guilds sem pessoas ou sem canal TTS.
    reboot.guilds = [Guild(channel, humans=False)]
    assert (await reboot._server_notice_start({'run_id': 'no-humans'}))['sent'] == 0
    assert (await reboot._server_notice_start({'run_id': '../../unsafe'}))['ok'] is False
    assert not list(root.glob('no-humans.json'))


def test_avisos_falha_reinicio_duplicidade_e_limpeza(monkeypatch, tmp_path):
    asyncio.run(case(monkeypatch, tmp_path))


def test_integracao_updater_e_fechamento_nas_rotas():
    core = ROOT / 'updater' / 'core'
    source = (core / 'candidato.sh').read_text()
    assert 'zip_progress_publish "Avisando servidores"' in source
    assert 'voice_notices_start' in source
    assert 'voice_notices_finish failure' in (core / 'fila.sh').read_text()
    assert 'voice_notices_finish failure' in (core / 'validacao.sh').read_text()
    assert 'voice_notices_finish success' in (core / 'finalizacao.sh').read_text()
    assert 'internal_update_voice_notices' in (ROOT / 'webserver.py').read_text()
    assert 'record_tts_channel_for_updates' in (ROOT / 'cogs/tts/mensagens/despacho.py').read_text()
