"""Recording choices and animated import status use one owner-bound V2 card."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from discord.http import handle_message_parameters
import pytest

from cogs.tts.interface import importar_cd as ui
from osaka_cd import discord_import as service


def _text(view):
    def walk(items):
        for item in items:
            if 'content' in item:
                yield item['content']
            yield from walk(item.get('components', []))
    return '\n'.join(walk(view.to_components()))


def _interaction(owner=42, values=None):
    fields = dict(user=SimpleNamespace(id=owner), response=SimpleNamespace(
        send_message=AsyncMock(), defer=AsyncMock()))
    if values is not None:
        fields['data'] = {'values': values}
    return SimpleNamespace(**fields)


def _candidates(count=5):
    return [dict(title=f'Song {i}', artist=f'Artist {i}', aliases=[],
                 artist_aliases=[], seed_video_ids=[f'{i:011d}'],
                 search_query=f'Song {i} Artist {i}') for i in range(count)]


def _setup():
    response = SimpleNamespace(edit=AsyncMock())
    source = SimpleNamespace(author=SimpleNamespace(id=42),
        attachments=[SimpleNamespace(filename='The Vampire.mid', size=10,
                                     read=AsyncMock(return_value=b'MThd-midi'))],
        channel=SimpleNamespace(send=AsyncMock(return_value=response)))
    tasks = []
    def schedule(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task
    cog = SimpleNamespace(bot=SimpleNamespace(is_owner=AsyncMock(return_value=True)),
                          _schedule_tts_background=schedule)
    session = ui._Session(cog, source, response=response, metadata={
        'title': 'The Vampire', 'artist': '', 'id': 'the-vampire',
        'aliases': ['Original Alias'], 'start_ms': 900, 'max_duration_ms': 22000},
        overrides={'start_ms': 900, 'max_duration_ms': 22000, 'id': 'the-vampire'}, waiting=True)
    return cog, source, response, tasks, session


async def _until(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(.003)
    raise AssertionError('Expected import UI state was not reached')


@pytest.mark.asyncio
async def test_five_real_choices_serialize_as_v2_select_without_artist_form():
    _, _, _, _, session = _setup()
    view = ui._ChoicesView(session, _candidates(8))
    session.view = view
    assert len(view.candidates) == len(view.select.options) == 5
    assert [option.description for option in view.select.options] == [f'Artist {i}' for i in range(5)]
    with handle_message_parameters(view=view, allowed_mentions=ui.discord.AllowedMentions.none()) as data:
        payload = data.payload
    assert payload['flags'] & (1 << 15)
    assert payload['components'][0]['type'] == 17
    assert payload['allowed_mentions']['parse'] == []
    assert 'Informe o artista' not in _text(view) and 'Completar dados' not in _text(view)
    view.stop()


@pytest.mark.asyncio
async def test_choice_preserves_english_label_identity_and_crop_with_public_aliases():
    _, _, _, _, session = _setup()
    choices = _candidates(1)
    choices[0].update(title='ヴァンパイア', artist='DECO*27', aliases=['Vampire'])
    view = ui._ChoicesView(session, choices)
    session.view = view
    await view.choose(_interaction(values=['0']))
    assert session.overrides['title'] == 'The Vampire'
    assert session.overrides['artist'] == 'DECO*27'
    assert session.overrides['id'] == 'the-vampire'
    assert session.overrides['start_ms'] == 900
    assert session.overrides['max_duration_ms'] == 22000
    assert session.overrides['aliases'] == ['Original Alias', 'Vampire', 'ヴァンパイア']
    assert session.overrides['seed_video_ids'] == ['00000000000']
    assert session.resume.is_set() and session.submitted and not session.waiting
    repeat = _interaction(values=['0'])
    await view.choose(repeat)
    assert 'encerrada' in _text(repeat.response.send_message.await_args.kwargs['view'])


@pytest.mark.asyncio
async def test_nonowner_invalid_and_stale_choices_cannot_resume_import():
    _, _, _, _, session = _setup()
    view = ui._ChoicesView(session, _candidates())
    session.view = view
    intruder = _interaction(999, ['0'])
    await view.choose(intruder)
    assert 'Acesso restrito' in _text(intruder.response.send_message.await_args.kwargs['view'])
    for values in [['99'], ['-1'], [{}], ['0', '1'], []]:
        await view.choose(_interaction(values=values))
        assert not session.resume.is_set() and session.waiting
    current = ui._ChoicesView(session, _candidates(1))
    session.view = current
    await view.choose(_interaction(values=['0']))
    await view.on_timeout()
    assert not session.resume.is_set() and not session.cancelled
    view.stop()
    current.stop()


@pytest.mark.asyncio
async def test_empty_results_offer_retry_without_asking_for_author_and_timeout_cancels():
    _, _, _, _, session = _setup()
    view = ui._ChoicesView(session)
    session.view = view
    assert view.select is None and not view.candidates
    payload = str(view.to_components())
    assert 'Buscar novamente' in payload and 'Cancelar' in payload
    assert 'artista' not in payload.lower()
    await view.on_timeout()
    assert session.cancelled and session.resume.is_set() and view.is_finished()


@pytest.mark.asyncio
async def test_progress_coalesces_phases_and_stops_before_choice_or_final():
    _, _, response, _, session = _setup()
    session.waiting = False
    await session.start_progress()
    await session.progress('discovering')
    await session.progress('importing')
    await session.progress('installing')
    assert response.edit.await_count == 1
    first = _text(response.edit.await_args.kwargs['view'])
    assert ui._VINYL in first and '0:00' in first and '%' not in first
    await session.edit('CD importado', '**The Vampire**')
    count = response.edit.await_count
    await session._render_progress(session.progress_epoch - 1)
    assert response.edit.await_count == count
    assert session.progress_task is None and not session.progress_active
    assert 'CD importado' in _text(response.edit.await_args.kwargs['view'])


@pytest.mark.asyncio
async def test_heartbeat_cannot_overwrite_final_even_when_old_edit_finishes_during_stop():
    _, _, response, _, session = _setup()
    session.waiting = False
    entered, release = asyncio.Event(), asyncio.Event()
    written = []
    async def edit(**kwargs):
        content = _text(kwargs['view'])
        if ui._VINYL in content:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        written.append(content)
    response.edit.side_effect = edit
    with patch.object(ui, '_PROGRESS_INTERVAL', .01):
        await session.start_progress()
        await asyncio.wait_for(entered.wait(), 1)
        final = asyncio.create_task(session.edit('CD importado', 'Pronto.'))
        await asyncio.sleep(.01)
        assert not final.done()
        release.set()
        await asyncio.wait_for(final, 1)
        await asyncio.sleep(.03)
    assert len(written) == 2 and 'CD importado' in written[-1]
    assert session.progress_task is None


@pytest.mark.asyncio
async def test_artwork_failure_with_known_artist_waits_for_selection_then_imports_once():
    cog, source, response, tasks, _ = _setup()
    result = service.ImportResult((dict(id='the-vampire', name='The Vampire',
        artist='DECO*27', notes=93, duration_ms=23049),), applied=True, state='activated')
    error = service.ImportCDError('artwork_unavailable', candidates=_candidates())
    with patch.object(service, 'import_cd', new=AsyncMock(side_effect=[error, result])) as imported, \
         patch.object(service, 'inspect_input', return_value={'title': 'The Vampire', 'artist': 'Known Artist'}):
        await ui.handle_import_cd(cog, source)
        try:
            await _until(lambda: getattr(cog.bot, ui._LEASE_ATTRIBUTE).waiting)
            session = getattr(cog.bot, ui._LEASE_ATTRIBUTE)
            assert isinstance(session.view, ui._ChoicesView) and len(session.view.candidates) == 5
            assert not session.progress_active and session.progress_task is None
            await session.view.choose(_interaction(values=['2']))
            await asyncio.wait_for(tasks[0], 1)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert imported.await_count == 2
    assert imported.await_args.kwargs['overrides']['artist'] == 'Artist 2'
    assert source.channel.send.await_count == 1
    assert 'CD importado' in _text(response.edit.await_args.kwargs['view'])
    assert not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)
