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


def test_aviso_lote_escreve_estado_uma_vez(monkeypatch, tmp_path):
    """O número de gravações independe de quantos servidores recebem aviso."""
    mod = load_module(monkeypatch)
    channels = [Channel(gid, 1000 + gid) for gid in (12, 13, 14)]
    guilds = [Guild(channel) for channel in channels]

    class Bot(mod.AvisosServidoresUpdaterMixin):
        _repo_root = tmp_path
        def __init__(self):
            self.guilds = guilds
        def get_cog(self, key):
            return types.SimpleNamespace(guild_states={
                guild.id: types.SimpleNamespace(last_text_channel_id=guild.channel.id)
                for guild in guilds
            })
        def is_ready(self):
            return True

    writes = []
    save = mod._save_json
    def counting_save(path, data):
        writes.append(path.name)
        return save(path, data)
    monkeypatch.setattr(mod, '_save_json', counting_save)

    result = asyncio.run(Bot()._server_notice_start({'run_id': 'batch'}))
    assert result['sent'] == 3
    assert writes.count('batch.json') == 2  # Criação durável + resultado do lote.
    assert all(len(channel.sent) == 1 for channel in channels)


def test_limpeza_retomada_sem_deletar_aviso_final_duas_vezes(monkeypatch, tmp_path):
    """Se a exclusão do aviso antigo falhar, novo fica marcado como excluído."""
    mod = load_module(monkeypatch)

    class FlakyChannel(Channel):
        fail_start_once = True
        def get_partial_message(self, msg_id):
            async def delete():
                if msg_id == 100 and self.fail_start_once:
                    self.fail_start_once = False
                    raise HTTPException('temporário')
                self.deleted.append(msg_id)
            return types.SimpleNamespace(delete=delete)

    channel = FlakyChannel(12, 230)
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

    async def run():
        bot = Bot()
        assert (await bot._server_notice_start({'run_id': 'retry'}))['sent'] == 1
        root = bot._server_notice_root()
        (root / 'retry.result.json').write_text(json.dumps({'result': 'success'}))
        monkeypatch.setattr(mod, 'TTL_SECONDS', 0)
        await bot._server_notice_reconcile_once()
        assert channel.deleted == [101]
        state = json.loads((root / 'retry.json').read_text())
        assert state['messages'][0]['finish_id_deleted'] is True
        await bot._server_notice_reconcile_once()
        assert channel.deleted == [101, 100]
        assert not (root / 'retry.json').exists()
    asyncio.run(run())


def test_reconciliacao_inativa_dispensa_varredura(monkeypatch, tmp_path):
    """Com nenhuma atualização pendente, o segundo tick só faz uma stat."""
    mod = load_module(monkeypatch)
    class Bot(mod.AvisosServidoresUpdaterMixin):
        _repo_root = tmp_path
        def is_ready(self):
            return True
    async def run():
        bot = Bot()
        root = bot._server_notice_root()
        root.mkdir(parents=True)
        await bot._server_notice_reconcile_once()
        stamp = bot._server_notice_idle_directory_stamp
        assert stamp is not None
        await bot._server_notice_reconcile_once()
        assert bot._server_notice_idle_directory_stamp == stamp
        (root / 'newrun.json').write_text(json.dumps({'created_at': time.time(), 'messages': []}))
        await bot._server_notice_reconcile_once()
        assert not (root / 'newrun.json').exists()
    asyncio.run(run())


def test_update_notices_follow_terminal_deployment_not_transient_health():
    final_script = (ROOT / 'updater' / 'core' / 'finalizacao.sh').read_text()
    recovery_script = (ROOT / 'updater' / 'core' / 'recuperacao.sh').read_text()
    assert 'voice_notices_finish success || true' in final_script
    assert 'voice_notices_finish failure' not in final_script
    post_failure = recovery_script.split('handle_post_deploy_failure() {', 1)[1].split('\non_error() {', 1)[0]
    assert 'voice_notices_finish success || true' in post_failure
    assert 'voice_notices_finish failure' not in post_failure
    # Before deployment is committed, real failures must still alert users.
    assert 'voice_notices_finish failure' in recovery_script.split('\non_error() {', 1)[1]


def test_notices_are_independent_main_stage():
    progress = (ROOT / 'updater' / 'core' / 'progresso.sh').read_text()
    card = (ROOT / 'updater' / 'discord' / 'cartoes.py').read_text()
    core = (ROOT / 'updater' / 'core' / 'atualizar.sh').read_text()
    assert 'ZIP_PROGRESS_MACRO_MAX_INDEX=10' in core
    assert '"Pacote", "Avisando servidores", "Segurança"' in card
    assert 'min(10, int(presentation.get("macro_index")' in card
    assert 'avisando\\ servidores' in progress and 'servidores\\ avisados' in progress
