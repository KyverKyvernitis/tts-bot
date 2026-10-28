"""Owner command through both Discord entries, real store and catalog reader."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import zipfile

import discord
from discord.http import handle_message_parameters
import pytest

import config
from cogs.tts.cog import TTSVoice
from cogs.tts.interface import painel_canto as panel
from osaka_cd import discord_import as service, runtime
from osaka_cd import store as store_module
from osaka_cd.store import RuntimeStore
from test_cd_import_routing import _BotEntry


def _package(path, identity):
    score = {
        'id': identity, 'name': 'New Song', 'artist': 'Composer',
        'excerpt': 'Trecho principal', 'source_url': '',
        'source_sha256': hashlib.sha256(identity.encode()).hexdigest(),
        'revision': 1, 'duration_ms': 800,
        'notes': [{'midi_note': 60, 'start_ms': 0, 'duration_ms': 800, 'velocity': 90}],
    }
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('cogs/tts/cd_song_scores.json', json.dumps([score]))
        archive.writestr('cogs/tts/cd_song_videos.json', '{"version":1,"songs":{}}')
    return path.read_bytes()


def _serialize(kwargs):
    with handle_message_parameters(**kwargs) as request:
        payload = request.payload
    assert discord.MessageFlags._from_value(payload['flags']).components_v2
    assert payload['allowed_mentions']['parse'] == []
    assert not payload.get('embeds') and not payload.get('content')
    return json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_owner_zip_command_activates_through_real_watcher_once_and_repeats_safely(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TTS_ENABLED', False)
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    store = RuntimeStore(tmp_path / 'runtime', legacy)
    monkeypatch.setenv('OSAKA_CD_DATA_ROOT', str(tmp_path / 'runtime'))
    first = tmp_path / 'other.zip'
    _package(first, 'other-song')
    receipt = store.receive(first)
    assert store.process(receipt['job_id'])['applied']
    other_version = store.snapshot()['versions']['other-song']

    cards = []
    response = SimpleNamespace(id=800)

    async def edit(**kwargs):
        cards.append(_serialize(kwargs))
        return response

    async def send(**kwargs):
        cards.append(_serialize(kwargs))
        return response

    response.edit = AsyncMock(side_effect=edit)
    channel = SimpleNamespace(id=123, send=AsyncMock(side_effect=send))
    bot = _BotEntry()
    bot.ZIP_UPDATE_CHANNEL_ID = channel.id
    bot.settings_db = SimpleNamespace(get_guild_tts_defaults=lambda _: {'bot_prefix': '_', 'enabled': False})
    bot.user = SimpleNamespace(id=900)
    bot.is_owner = AsyncMock(return_value=True)
    bot.wait_until_ready = AsyncMock()
    bot.is_closed = lambda: False
    bot.process_commands = AsyncMock()
    bot._handle_zip_update_message = AsyncMock()
    cog = TTSVoice(bot)
    bot.get_cog = lambda name: cog if name == 'TTSVoice' else None
    cog._ensure_connected = AsyncMock(side_effect=AssertionError('Import must never join voice'))
    cog._enqueue_tts = AsyncMock(side_effect=AssertionError('Import must never synthesize speech'))
    raw = _package(tmp_path / 'incoming.zip', 'new-song')
    attachment = SimpleNamespace(filename='new-song.zip', size=len(raw), read=AsyncMock(return_value=raw))
    message = SimpleNamespace(id=77, content='_importcd', guild=SimpleNamespace(id=20),
        author=SimpleNamespace(id=10, bot=False), channel=channel, attachments=[attachment])
    manager = SimpleNamespace(schedule=Mock(), close=Mock())

    with patch.object(store_module, 'get_store', return_value=store), \
            patch.object(service, 'get_store', return_value=store), \
            patch.object(panel, '_CD_EMOJIS', manager):
        with runtime.unpin_snapshot():
            runtime.invalidate()
            watcher = asyncio.create_task(panel._watch_catalog(cog))
            try:
                await asyncio.sleep(0)
                await asyncio.gather(bot.on_message(message), cog.on_message(message))
                tasks = tuple(cog._get_tts_background_tasks())
                assert len(tasks) == 1
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
                assert 'CD importado' in cards[-1]
                assert 'metadados e capa' in cards[-1]
                assert set(store.snapshot()['versions']) == {'other-song', 'new-song'}
                assert store.snapshot()['versions']['other-song'] == other_version
                assert store.snapshot()['activated_generation'] == store.snapshot()['generation']
                generation = store.snapshot()['generation']
                assert not hasattr(bot, '_osaka_cd_import_session')

                message.id = 78
                await bot.on_message(message)
                await asyncio.wait_for(asyncio.gather(*tuple(cog._get_tts_background_tasks())), timeout=10)
                assert 'CD já disponível' in cards[-1]
                assert store.snapshot()['generation'] == generation
                assert channel.send.await_count == 2
                assert attachment.read.await_count == 2
                bot._handle_zip_update_message.assert_not_awaited()
                bot.process_commands.assert_not_awaited()
                cog._ensure_connected.assert_not_awaited()
                cog._enqueue_tts.assert_not_awaited()
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                runtime.invalidate()


@pytest.mark.asyncio
async def test_non_owner_zip_command_never_downloads_or_reaches_code_updater(monkeypatch):
    monkeypatch.setattr(config, 'TTS_ENABLED', False)
    bot = _BotEntry()
    bot.ZIP_UPDATE_CHANNEL_ID = 123
    bot.settings_db = SimpleNamespace(get_guild_tts_defaults=lambda _: {'bot_prefix': '_'})
    bot.is_owner = AsyncMock(return_value=False)
    bot.process_commands = AsyncMock()
    bot._handle_zip_update_message = AsyncMock()
    cog = TTSVoice(bot)
    bot.get_cog = lambda _: cog
    sent = SimpleNamespace(id=800)
    channel = SimpleNamespace(id=123, send=AsyncMock(return_value=sent))
    attachment = SimpleNamespace(filename='code.zip', size=100, read=AsyncMock())
    message = SimpleNamespace(id=77, content='_importcd', guild=SimpleNamespace(id=20),
        author=SimpleNamespace(id=10, bot=False, guild_permissions=SimpleNamespace(administrator=True)),
        channel=channel, attachments=[attachment])
    await asyncio.gather(bot.on_message(message), cog.on_message(message))
    assert 'Acesso restrito' in _serialize(channel.send.await_args.kwargs)
    assert channel.send.await_count == 1
    attachment.read.assert_not_awaited()
    bot._handle_zip_update_message.assert_not_awaited()
    bot.process_commands.assert_not_awaited()
