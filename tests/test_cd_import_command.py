"""Owner, attachment, lifecycle and real Discord Components V2 command contract."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from discord.http import handle_message_parameters
import pytest

from cogs.tts.aliases import get_prefixed_aliases
from cogs.tts.prefix import match_prefix_control_command, dispatch_prefix_control_command
from cogs.tts.interface import importar_cd as ui
from osaka_cd import discord_import as service


def _attachment(name='Artist - Song.mid', data=b'MThd-midi', *, size=None):
    return SimpleNamespace(filename=name, size=len(data) if size is None else size,
                           read=AsyncMock(return_value=data))


def _setup(*, owner=True, attachments=None):
    response = SimpleNamespace(id=120, edit=AsyncMock())
    message = SimpleNamespace(id=110, author=SimpleNamespace(id=42), guild=None,
        channel=SimpleNamespace(send=AsyncMock(return_value=response)),
        attachments=[] if attachments is None else attachments,
        reference=SimpleNamespace(resolved=SimpleNamespace(attachments=[_attachment()])))
    tasks = []

    def schedule(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    cog = SimpleNamespace(bot=SimpleNamespace(is_owner=AsyncMock(return_value=owner)),
                          _schedule_tts_background=schedule)
    return cog, message, response, tasks


def _contents(view):
    def walk(nodes):
        for node in nodes:
            if 'content' in node:
                yield node['content']
            yield from walk(node.get('components', []))
    return '\n'.join(walk(view.to_components()))


def _result(**updates):
    values = dict(songs=({'id': 'song', 'name': 'Song', 'artist': 'Artist',
                         'notes': 86, 'duration_ms': 16008, 'parts': 3},),
                  pending=(), already_installed=False, applied=True,
                  generation='g-demo', state='activated', job_id='abc')
    values.update(updates)
    return SimpleNamespace(**values)


def _choices(count=5):
    return [{'title': f'Song {index + 1}', 'artist': f'Artist {index + 1}',
             'aliases': [], 'artist_aliases': [],
             'seed_video_ids': [f'{index:011d}'],
             'search_query': f'Song {index + 1} Artist {index + 1}'}
            for index in range(count)]


def _interaction(user_id=42):
    return SimpleNamespace(user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock(), defer=AsyncMock()))


async def _tick_until(predicate):
    for _ in range(150):
        if predicate():
            return
        await asyncio.sleep(.005)
    raise AssertionError('UI condition was never reached')


@pytest.mark.parametrize('content,prefix', [('_importcd', '_'), (' _IMPORTCD ', '_'), ('!importcd', '!')])
def test_import_command_uses_exact_configured_prefix(content, prefix):
    command = match_prefix_control_command(content, prefix)
    assert command.kind == 'importcd' and command.argument == ''
    assert get_prefixed_aliases(prefix, 'importcd') == (prefix + 'importcd',)


@pytest.mark.parametrize('content', ['_importcdmore', 'importcd', '_impotcd'])
def test_import_command_does_not_consume_other_messages(content):
    assert match_prefix_control_command(content, '_') is None


def test_import_command_accepts_a_song_name_without_a_json():
    command = match_prefix_control_command('_importcd Tetoris', '_')
    assert command.kind == 'importcd' and command.argument == 'Tetoris'


@pytest.mark.asyncio
async def test_dispatch_uses_the_new_handler_once():
    command = match_prefix_control_command('_importcd', '_')
    with patch.object(ui, 'handle_import_cd', new=AsyncMock(return_value=True)) as handler:
        cog, message = object(), object()
        assert await dispatch_prefix_control_command(cog, message, command)
        handler.assert_awaited_once_with(cog, message)


@pytest.mark.asyncio
async def test_owner_check_precedes_attachment_access_or_download():
    cog, message, response, tasks = _setup(owner=False, attachments=[_attachment()])
    attachment = message.attachments[0]
    with patch.object(ui, '_select_attachments', side_effect=AssertionError('unauthorized attachment access')):
        assert await ui.handle_import_cd(cog, message)
    cog.bot.is_owner.assert_awaited_once_with(message.author)
    attachment.read.assert_not_awaited()
    assert not tasks
    assert 'Só o dono do bot' in _contents(message.channel.send.await_args.kwargs['view'])


@pytest.mark.asyncio
async def test_missing_owner_check_fails_closed():
    cog, message, response, tasks = _setup(attachments=[_attachment()])
    del cog.bot.is_owner
    assert await ui.handle_import_cd(cog, message)
    message.attachments[0].read.assert_not_awaited()
    assert not tasks


@pytest.mark.asyncio
async def test_standalone_command_opens_editor_without_importing_replied_attachment():
    cog, message, response, tasks = _setup()
    with patch('cogs.tts.interface.editar_cd.handle_edit_cd', new=AsyncMock(return_value=True)) as editor:
        assert await ui.handle_import_cd(cog, message)
    editor.assert_awaited_once_with(cog, message)
    message.reference.resolved.attachments[0].read.assert_not_awaited()
    assert not tasks and not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)


@pytest.mark.asyncio
async def test_caption_without_attachment_still_does_not_import_a_replied_file():
    cog, message, response, tasks = _setup()
    with patch('cogs.tts.interface.editar_cd.handle_edit_cd', new=AsyncMock()) as editor:
        assert await ui.handle_import_cd(cog, message, title='Example Song')
    editor.assert_not_awaited()
    assert 'junto' in _contents(message.channel.send.await_args.kwargs['view'])
    message.reference.resolved.attachments[0].read.assert_not_awaited()
    assert not tasks and not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)


@pytest.mark.asyncio
@pytest.mark.parametrize('attachments', [
    [_attachment('song.mp3')],
    [_attachment('song.mid'), _attachment('another.mid')],
    [_attachment('song.zip'), _attachment('song.json', b'{}')],
    [_attachment('../song.mid')],
    [_attachment('song.mid', size=service.MAX_MIDI_BYTES + 1)],
])
async def test_invalid_inputs_never_download(attachments):
    cog, message, response, tasks = _setup(attachments=attachments)
    assert await ui.handle_import_cd(cog, message)
    assert not tasks and not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)
    for item in attachments:
        item.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_midi_auto_import_edits_one_real_v2_card_with_no_mentions():
    cog, message, response, tasks = _setup(attachments=[_attachment()])

    async def importing(prepared, **options):
        await options['progress']('importing')
        await options['progress']('installing')
        return _result()

    prepared = SimpleNamespace(kind='midi')
    with patch.object(service, 'prepare_input', return_value=prepared), \
         patch.object(service, 'inspect_input', return_value={'title': 'Song', 'artist': 'Artist'}), \
         patch.object(service, 'import_cd', new=AsyncMock(side_effect=importing)) as imported:
        assert await ui.handle_import_cd(cog, message)
        await tasks[0]
    assert imported.await_count == 1
    assert message.channel.send.await_count == 1
    assert 1 <= response.edit.await_count <= 3
    assert not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)
    payloads = [message.channel.send.await_args.kwargs, *[call.kwargs for call in response.edit.await_args_list]]
    for kwargs in payloads:
        assert set(kwargs) == {'view', 'allowed_mentions'}
        assert isinstance(kwargs['view'], discord.ui.LayoutView)
        with handle_message_parameters(**kwargs) as parameters:
            payload = parameters.payload
        assert discord.MessageFlags._from_value(payload['flags']).components_v2
        assert payload['components'][0]['type'] == 17
        assert not payload.get('content') and not payload.get('embeds')
        assert payload['allowed_mentions']['parse'] == []
    final = _contents(response.edit.await_args.kwargs['view'])
    assert 'CD importado' in final and '86 notas' in final and '16.008 s' in final
    assert 'Vinyl_Music_loading' in _contents(message.channel.send.await_args.kwargs['view'])
    assert 'Vinyl_Music_loading' not in final


@pytest.mark.asyncio
async def test_title_only_attachment_attempts_discovery_without_requiring_a_form_or_json():
    cog, message, response, tasks = _setup(attachments=[_attachment('Tetoris.mid')])
    async def importing(prepared, **options):
        assert prepared.sidecar is None
        await options['progress']('discovering')
        return _result()
    with patch.object(service, 'import_cd', new=AsyncMock(side_effect=importing)) as imported, \
         patch.object(ui, '_DetailsView', side_effect=AssertionError('No form before discovery')):
        assert await ui.handle_import_cd(cog, message)
        await tasks[0]
    imported.assert_awaited_once()
    assert 'CD importado' in _contents(response.edit.await_args.kwargs['view'])
    assert not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)


@pytest.mark.asyncio
async def test_caption_name_ignores_upload_prefix_and_preserves_explicit_sidecar_artist():
    for sidecar, expected_artist in [(None, ''), (b'{"artist":"Known Artist"}', None)]:
        attachments = [_attachment('Unknown Uploader - recording.mid')]
        if sidecar:
            attachments.append(_attachment('recording.json', sidecar))
        cog, message, response, tasks = _setup(attachments=attachments)
        with patch.object(service, 'import_cd', new=AsyncMock(return_value=_result())) as imported:
            await ui.handle_import_cd(cog, message, title='Tetoris')
            await tasks[0]
        overrides = imported.await_args.kwargs['overrides']
        assert overrides['title'] == 'Tetoris'
        assert overrides.get('artist') == expected_artist


@pytest.mark.asyncio
async def test_caption_is_validated_before_attachment_download():
    cog, message, response, tasks = _setup(attachments=[_attachment()])
    assert await ui.handle_import_cd(cog, message, title='x' * 101)
    message.attachments[0].read.assert_not_awaited()
    assert not tasks


@pytest.mark.asyncio
async def test_global_busy_is_reserved_before_downloading_and_released_after_cancel():
    entered, release = asyncio.Event(), asyncio.Event()
    attachment = _attachment()

    async def read():
        entered.set()
        await release.wait()
        return b'MThd-midi'

    attachment.read.side_effect = read
    cog, message, response, tasks = _setup(attachments=[attachment])
    await ui.handle_import_cd(cog, message)
    await entered.wait()
    second = _attachment('Artist - Other.mid')
    _, other, _, _ = _setup(attachments=[second])
    await ui.handle_import_cd(cog, other)
    second.read.assert_not_awaited()
    assert 'andamento' in _contents(other.channel.send.await_args.kwargs['view'])
    tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    assert not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)


@pytest.mark.asyncio
async def test_missing_artist_waits_for_five_owner_choices_and_keeps_crop_then_runs_once():
    cog, message, response, tasks = _setup(attachments=[_attachment('Song.mid'),
        _attachment('Song.json', b'{"start_ms":95600,"max_duration_ms":29500}')])
    with patch.object(service, 'import_cd', new=AsyncMock(side_effect=[
            service.ImportCDError('metadata_ambiguous', candidates=_choices()), _result()])) as imported:
        await ui.handle_import_cd(cog, message)
        await _tick_until(lambda: getattr(cog.bot, ui._LEASE_ATTRIBUTE).waiting)
        session = getattr(cog.bot, ui._LEASE_ATTRIBUTE)
        view = session.view
        assert isinstance(view, ui._ChoicesView)
        assert len(view.select.options) == 5
        imported.assert_awaited_once()
        view.select._values = ['0']
        unauthorized = _interaction(999)
        await view.choose(unauthorized)
        unauthorized.response.send_modal.assert_not_awaited()
        assert session.waiting and not session.submitted
        authorized = _interaction()
        await view.choose(authorized)
        authorized.response.send_modal.assert_not_awaited()
        await tasks[0]
        overrides = imported.await_args.kwargs['overrides']
        assert overrides['title'] == 'Song' and overrides['artist'] == 'Artist 1'
        retried = service.inspect_input(imported.await_args.args[0], overrides)
        assert retried['start_ms'] == 95600 and retried['max_duration_ms'] == 29500
        assert overrides['aliases'] == ['Song 1']
        assert overrides['seed_video_ids'] == ['00000000000']
        assert imported.await_count == 2
        repeat = _interaction()
        await view.choose(repeat)
        assert imported.await_count == 2
        assert 'encerrada' in _contents(repeat.response.send_message.await_args.kwargs['view'])


@pytest.mark.asyncio
async def test_invalid_choice_can_be_fixed_without_publishing():
    cog, message, response, tasks = _setup(attachments=[_attachment('song.mid')])
    with patch.object(service, 'prepare_input', return_value=SimpleNamespace(kind='midi')), \
         patch.object(service, 'inspect_input', return_value={'title': 'Song', 'artist': ''}), \
         patch.object(service, 'import_cd', new=AsyncMock(side_effect=[
             service.ImportCDError('metadata_ambiguous', candidates=_choices(2)), _result()])) as imported:
        await ui.handle_import_cd(cog, message)
        await _tick_until(lambda: getattr(cog.bot, ui._LEASE_ATTRIBUTE).waiting)
        session = getattr(cog.bot, ui._LEASE_ATTRIBUTE)
        view = session.view
        view.select._values = ['99']
        invalid = _interaction()
        await view.choose(invalid)
        imported.assert_awaited_once()
        assert session.waiting and not session.submitted
        assert 'Escolha uma música' in _contents(invalid.response.send_message.await_args.kwargs['view'])
        view.select._values = ['1']
        await view.choose(_interaction())
        await tasks[0]
        assert imported.await_count == 2
        assert imported.await_args.kwargs['overrides']['artist'] == 'Artist 2'
    assert not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)


@pytest.mark.asyncio
async def test_waiting_choices_timeout_releases_global_busy():
    cog, message, response, tasks = _setup(attachments=[_attachment('song.mid')])
    with patch.object(service, 'prepare_input', return_value=SimpleNamespace(kind='midi')), \
         patch.object(service, 'inspect_input', return_value={'title': 'Song', 'artist': ''}), \
         patch.object(service, 'import_cd', new=AsyncMock(side_effect=service.ImportCDError('metadata_unavailable'))) as imported:
        await ui.handle_import_cd(cog, message)
        await _tick_until(lambda: getattr(cog.bot, ui._LEASE_ATTRIBUTE).waiting)
        session = getattr(cog.bot, ui._LEASE_ATTRIBUTE)
        await session.view.on_timeout()
        await tasks[0]
    imported.assert_awaited_once()
    assert session.view.is_finished() and not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)


@pytest.mark.asyncio
async def test_failed_choice_card_releases_busy_instead_of_waiting_for_invisible_select():
    cog, message, response, tasks = _setup(attachments=[_attachment('song.mid')])
    response.edit.side_effect = RuntimeError('simulated unreachable card')
    with patch.object(service, 'prepare_input', return_value=SimpleNamespace(kind='midi')), \
         patch.object(service, 'inspect_input', return_value={'title': 'Song', 'artist': ''}), \
         patch.object(service, 'import_cd', new=AsyncMock(side_effect=service.ImportCDError('metadata_unavailable'))) as imported:
        await ui.handle_import_cd(cog, message)
        await asyncio.wait_for(tasks[0], 1)
    assert imported.await_count == 1
    assert not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)


@pytest.mark.asyncio
async def test_choice_ack_failure_does_not_strand_valid_submission():
    cog, message, response, tasks = _setup(attachments=[_attachment('song.mid')])
    with patch.object(service, 'prepare_input', return_value=SimpleNamespace(kind='midi')), \
         patch.object(service, 'inspect_input', return_value={'title': 'Song', 'artist': ''}), \
         patch.object(service, 'import_cd', new=AsyncMock(side_effect=[
             service.ImportCDError('metadata_ambiguous', candidates=_choices(1)), _result()])) as imported:
        await ui.handle_import_cd(cog, message)
        await _tick_until(lambda: getattr(cog.bot, ui._LEASE_ATTRIBUTE).waiting)
        session = getattr(cog.bot, ui._LEASE_ATTRIBUTE)
        view = session.view
        view.select._values = ['0']
        submit = _interaction()
        submit.response.defer.side_effect = RuntimeError('simulated expired interaction')
        await view.choose(submit)
        await asyncio.wait_for(tasks[0], 1)
        view.stop()
    assert imported.await_count == 2
    assert not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)


@pytest.mark.asyncio
async def test_service_error_is_public_and_unexpected_error_has_no_private_detail():
    for error, expected in [
        (service.ImportCDError('invalid_midi', 'O MIDI está inválido.'), 'O MIDI está inválido.'),
        (RuntimeError('https://secret-url/?token=private'), 'Tente novamente'),
    ]:
        cog, message, response, tasks = _setup(attachments=[_attachment('song.zip')])
        with patch.object(service, 'prepare_input', return_value=SimpleNamespace(kind='zip')), \
             patch.object(service, 'inspect_input', return_value={'kind': 'zip'}), \
             patch.object(service, 'import_cd', new=AsyncMock(side_effect=error)):
            await ui.handle_import_cd(cog, message)
            await tasks[0]
        final = _contents(response.edit.await_args.kwargs['view'])
        assert expected in final and 'secret-url' not in final and 'private' not in final
        assert not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)


@pytest.mark.asyncio
async def test_actual_download_size_is_checked_again():
    attachment = _attachment(data=b'x' * 30, size=10)
    with pytest.raises(service.ImportCDError):
        await ui._read_attachment(attachment, 20)


@pytest.mark.asyncio
async def test_task_cancelled_before_first_instruction_releases_lease():
    cog, message, response, tasks = _setup(attachments=[_attachment()])

    def cancelled_scheduler(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        task.cancel()
        return task

    cog._schedule_tts_background = cancelled_scheduler
    assert await ui.handle_import_cd(cog, message)
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    await asyncio.sleep(0)
    assert not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)
    message.attachments[0].read.assert_not_awaited()


@pytest.mark.asyncio
async def test_discord_progress_failure_does_not_abort_or_contradict_commit():
    cog, message, response, tasks = _setup(attachments=[_attachment()])
    response.edit.side_effect = RuntimeError('simulated unreachable card')
    committed = []

    async def importing(prepared, **options):
        await options['progress']('installing')
        committed.append(True)
        return _result()

    with patch.object(service, 'prepare_input', return_value=SimpleNamespace(kind='midi')), \
         patch.object(service, 'inspect_input', return_value={'title': 'Song', 'artist': 'Artist'}), \
         patch.object(service, 'import_cd', new=AsyncMock(side_effect=importing)):
        await ui.handle_import_cd(cog, message)
        await tasks[0]
    assert committed == [True]
    assert not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)


@pytest.mark.asyncio
async def test_download_timeout_releases_busy_without_running_importer():
    attachment = _attachment()
    attachment.read.side_effect = lambda: asyncio.sleep(60)
    # AsyncMock returns the side-effect's value, so use an actual awaitable body.
    async def read():
        await asyncio.sleep(60)
    attachment.read.side_effect = read
    cog, message, response, tasks = _setup(attachments=[attachment])
    with patch.object(ui, '_DOWNLOAD_TIMEOUT', .01), \
         patch.object(service, 'import_cd', new=AsyncMock()) as imported:
        await ui.handle_import_cd(cog, message)
        await tasks[0]
    imported.assert_not_awaited()
    assert 'demorou' in _contents(response.edit.await_args.kwargs['view'])
    assert not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)


def test_applied_without_activation_is_not_advertised_as_imported():
    title, text = ui._result_text(_result(state='applied', pending=('metadata', 'thumbnail')))
    assert title == 'CD salvo' and 'Aguardando' in text
    assert 'metadados e capa' in text


@pytest.mark.parametrize('value,expected', [('95.6', 95600), ('1:35.6', 95600), ('1:05,125', 65125), ('0', 0), ('', None)])
def test_modal_time_parser(value, expected):
    assert ui._milliseconds(value) == expected


@pytest.mark.parametrize('value', ['-1', '1:61', 'nan', 'inf', '1:02:03', '95s'])
def test_modal_rejects_invalid_times(value):
    with pytest.raises(ValueError):
        ui._milliseconds(value)
