"""Owner-only attachment import, using the existing CD importer and store."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import re
import json
from pathlib import PurePosixPath

import discord


_LOG = logging.getLogger(__name__)
_FORM_TIMEOUT = 180
_DOWNLOAD_TIMEOUT = 30
_DISCORD_TIMEOUT = 10
_LEASE_ATTRIBUTE = '_osaka_cd_import_session'
_PROGRESS = {
    'discovering': ('Identificando música', 'Buscando o artista e a gravação pelo nome.'),
    'preparing': ('Preparando CD', 'Conferindo as notas e os dados do arquivo.'),
    'importing': ('Preparando CD', 'Buscando os metadados e importando a capa.'),
    'installing': ('Salvando CD', 'Atualizando o catálogo com segurança.'),
    'activating': ('Finalizando CD', 'Atualizando o menu de CDs.'),
}


def _text(value, limit=160):
    text = ''.join(character if ord(character) >= 32 else ' ' for character in str(value or ''))
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text.strip()[:limit]))


def _notice(title, body, *, color=None):
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(f'## {title}\n{body}'),
        accent_color=color or discord.Color.blurple(),
    ))
    return view


async def _send(channel, title, body, *, color=None):
    return await asyncio.wait_for(channel.send(view=_notice(title, body, color=color),
                              allowed_mentions=discord.AllowedMentions.none()), timeout=_DISCORD_TIMEOUT)


async def _owned(bot, user):
    check = getattr(bot, 'is_owner', None)
    if not callable(check):
        return False
    try:
        return bool(await asyncio.wait_for(check(user), timeout=_DISCORD_TIMEOUT))
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


async def _interaction_notice(interaction, title, body):
    try:
        await asyncio.wait_for(interaction.response.send_message(
            view=_notice(title, body), ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none()), timeout=_DISCORD_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _LOG.info('[cd_import_interaction] type=%s', type(exc).__name__)


@dataclass
class _Session:
    cog: object
    source: object
    response: object = None
    view: object = None
    metadata: dict = field(default_factory=dict)
    overrides: dict | None = None
    resume: asyncio.Event = field(default_factory=asyncio.Event)
    waiting: bool = False
    submitted: bool = False
    cancelled: bool = False
    finished: bool = False
    title: str = ''

    async def edit(self, title, body, *, color=None, view=None):
        if self.response is None:
            return False
        try:
            await asyncio.wait_for(self.response.edit(view=view or _notice(title, body, color=color),
                                     allowed_mentions=discord.AllowedMentions.none()), timeout=_DISCORD_TIMEOUT)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A removed card does not roll back an already published CD.
            _LOG.info('[cd_import_card] type=%s', type(exc).__name__)
            return False

    async def check(self, interaction):
        if (int(getattr(interaction.user, 'id', 0)) != int(self.source.author.id)
                or not await _owned(self.cog.bot, interaction.user)):
            await _interaction_notice(interaction, 'Acesso restrito', 'Só o dono do bot pode importar CDs.')
            return False
        if self.finished or not self.waiting or self.submitted:
            await _interaction_notice(interaction, 'Importação encerrada',
                'Envie o comando com o anexo para começar novamente.')
            return False
        return True

    def release(self):
        self.finished = True
        self.waiting = False
        if self.view is not None:
            self.view.stop()
        if getattr(self.cog.bot, _LEASE_ATTRIBUTE, None) is self:
            delattr(self.cog.bot, _LEASE_ATTRIBUTE)


def _milliseconds(value):
    raw = str(value or '').strip().replace(',', '.')
    if not raw:
        return None
    if not re.fullmatch(r'\d{1,5}(?::[0-5]?\d)?(?:\.\d{1,3})?', raw):
        raise ValueError('Use segundos ou minutos:segundos, como 95.6 ou 1:35.6.')
    pieces = raw.split(':')
    seconds = float(pieces[-1]) + (int(pieces[0]) * 60 if len(pieces) == 2 else 0)
    if seconds > 2147483:
        raise ValueError('O tempo informado é muito grande.')
    return round(seconds * 1000)


async def _defer(interaction):
    try:
        await asyncio.wait_for(interaction.response.defer(), timeout=_DISCORD_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _LOG.info('[cd_import_ack] type=%s', type(exc).__name__)


class _DetailsModal(discord.ui.Modal):
    def __init__(self, session):
        super().__init__(title='Dados do CD', timeout=_FORM_TIMEOUT)
        self.session = session
        self.song = discord.ui.TextInput(label='Música', required=True, max_length=100,
                                        default=str(session.metadata.get('title') or '')[:100])
        self.artist = discord.ui.TextInput(label='Artista', required=True, max_length=160,
                                          default=str(session.metadata.get('artist') or '')[:160])
        start_ms = session.metadata.get('start_ms') or 0
        self.start = discord.ui.TextInput(label='Início do trecho (opcional)', required=False,
            max_length=20, placeholder='Ex.: 1:35.6', default=f'{start_ms / 1000:g}' if start_ms else '')
        self.end = discord.ui.TextInput(label='Fim do trecho (opcional)', required=False,
                                       max_length=20, placeholder='Ex.: 2:05.1')
        for item in (self.song, self.artist, self.start, self.end):
            self.add_item(item)

    async def on_submit(self, interaction):
        if not await self.session.check(interaction):
            return
        title, artist = str(self.song.value).strip(), str(self.artist.value).strip()
        try:
            if not title or not artist or any(ord(char) < 32 for char in title + artist):
                raise ValueError('Informe o nome da música e do artista.')
            start, end = _milliseconds(self.start.value), _milliseconds(self.end.value)
            start = start if start is not None else self.session.metadata.get('start_ms') or 0
            if end is not None and not 40 <= end - start <= 60000:
                raise ValueError('Escolha um trecho de até 60 segundos, com o fim depois do início.')
            overrides = {'title': title, 'artist': artist, 'start_ms': start}
            if end is not None:
                overrides['max_duration_ms'] = end - start
        except ValueError as exc:
            await _interaction_notice(interaction, 'Confira os dados', str(exc))
            return
        # No await between the check and this latch: another modal cannot
        # submit the same import a second time.
        self.session.submitted = True
        self.session.overrides = overrides
        self.session.waiting = False
        self.session.resume.set()
        self.stop()
        await _defer(interaction)


class _DetailsView(discord.ui.LayoutView):
    def __init__(self, session):
        super().__init__(timeout=_FORM_TIMEOUT)
        self.session = session
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay('## Complete o CD\n'
                f'**{_text(session.metadata.get("title") or "Novo CD")}**\n'
                'A busca não identificou uma única música. Informe o artista para continuar.'),
            discord.ui.ActionRow(
                discord.ui.Button(label='Completar dados', style=discord.ButtonStyle.primary,
                                  custom_id='cd-import:details'),
                discord.ui.Button(label='Cancelar', style=discord.ButtonStyle.secondary,
                                  custom_id='cd-import:cancel'),
            ), accent_color=discord.Color.blurple(),
        ))
        buttons = self.children[0].children[1].children
        buttons[0].callback = self.details
        buttons[1].callback = self.cancel

    async def details(self, interaction):
        if await self.session.check(interaction):
            modal = _DetailsModal(self.session)
            try:
                await asyncio.wait_for(interaction.response.send_modal(modal), timeout=_DISCORD_TIMEOUT)
            except asyncio.CancelledError:
                modal.stop()
                raise
            except Exception as exc:
                modal.stop()
                _LOG.info('[cd_import_modal] type=%s', type(exc).__name__)

    async def cancel(self, interaction):
        if not await self.session.check(interaction):
            return
        self.session.cancelled = True
        self.session.waiting = False
        self.session.resume.set()
        await _defer(interaction)

    async def on_timeout(self):
        self.session.cancelled = True
        self.session.waiting = False
        self.session.resume.set()


def _select_attachments(attachments):
    from osaka_cd.discord_import import MAX_MIDI_BYTES, MAX_ZIP_BYTES, MAX_SIDECAR_BYTES, ImportCDError
    if not attachments:
        raise ImportCDError('attachment_required', 'Envie o comando junto de um MIDI ou de um pacote de CD.')
    if len(attachments) > 2:
        raise ImportCDError('attachment_count', 'Envie um MIDI com seu JSON opcional, ou um pacote de CD por vez.')
    primary, sidecar = None, None
    for item in attachments:
        name = str(getattr(item, 'filename', '') or '')
        if (not name or len(name) > 240 or '\\' in name or '/' in name
                or any(ord(char) < 32 for char in name)):
            raise ImportCDError('attachment_name', 'O nome do anexo é inválido.')
        suffix = PurePosixPath(name).suffix.casefold()
        limit = {'.mid': MAX_MIDI_BYTES, '.midi': MAX_MIDI_BYTES, '.zip': MAX_ZIP_BYTES,
                 '.json': MAX_SIDECAR_BYTES}.get(suffix)
        if limit is None:
            raise ImportCDError('attachment_type', 'Use um MIDI (.mid ou .midi) ou um pacote de CD (.zip).')
        size = getattr(item, 'size', 0)
        if type(size) is not int or not 0 < size <= limit:
            raise ImportCDError('attachment_size', 'Um dos anexos está vazio ou excede o tamanho permitido.')
        if suffix == '.json':
            if sidecar is not None:
                raise ImportCDError('attachment_count', 'Envie somente um JSON junto do MIDI.')
            sidecar = item
        else:
            if primary is not None:
                raise ImportCDError('attachment_count', 'Importe um CD por vez.')
            primary = item
    if primary is None:
        raise ImportCDError('attachment_type', 'Envie o MIDI junto do JSON.')
    if sidecar is not None and PurePosixPath(primary.filename).suffix.casefold() == '.zip':
        raise ImportCDError('attachment_count', 'O pacote de CD deve ser enviado sozinho.')
    return primary, sidecar


async def _read_attachment(attachment, limit):
    from osaka_cd.discord_import import ImportCDError
    try:
        raw = await asyncio.wait_for(attachment.read(), timeout=_DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        raise ImportCDError('download_timeout', 'O anexo demorou para baixar. Envie novamente.') from None
    except (discord.HTTPException, OSError):
        raise ImportCDError('download_failed', 'Não consegui baixar o anexo. Envie novamente.') from None
    if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
        raise ImportCDError('attachment_size', 'Um dos anexos está vazio ou excede o tamanho permitido.')
    return raw


def _result_text(result):
    songs = tuple(result.songs)
    if result.already_installed:
        title = 'CD já disponível' if len(songs) == 1 else 'CDs já disponíveis'
    elif result.state == 'activated':
        title = 'CD importado' if len(songs) == 1 else 'CDs importados'
    else:
        title = 'CD salvo' if len(songs) == 1 else 'CDs salvos'
    lines = []
    for song in songs[:3]:
        duration = (float(song.get('duration_ms') or 0) / 1000)
        notes = song.get('notes') or 0
        name = _text(song.get('name') or song.get('id') or 'CD')
        lines.append(f'**{name}** · {notes} notas · {duration:g} s')
    if len(songs) > 3:
        lines.append(f'-# Mais {len(songs) - 3} CDs no catálogo.')
    missing = set(result.pending)
    pending = []
    if 'metadata' in missing:
        pending.append('metadados')
    if 'thumbnail' in missing or 'cover' in missing:
        pending.append('capa')
    if pending:
        lines.append('-# Ainda pendente: ' + ' e '.join(pending) + '.')
    if result.state != 'activated' and not result.already_installed:
        lines.append('-# Aguardando a atualização do menu de CDs.')
    return title, '\n'.join(lines) or 'O catálogo de CDs foi atualizado.'


async def _run_import(session, primary, sidecar):
    from osaka_cd.discord_import import (
        MAX_MIDI_BYTES, MAX_ZIP_BYTES, MAX_SIDECAR_BYTES,
        ImportCDError, prepare_input, inspect_input, import_cd,
    )
    try:
        limit = MAX_ZIP_BYTES if PurePosixPath(primary.filename).suffix.casefold() == '.zip' else MAX_MIDI_BYTES
        data = await _read_attachment(primary, limit)
        metadata = await _read_attachment(sidecar, MAX_SIDECAR_BYTES) if sidecar is not None else None
        prepared = await asyncio.to_thread(prepare_input, primary.filename, data, metadata)
        if session.title:
            if prepared.kind != 'midi':
                raise ImportCDError('invalid_metadata', 'O nome informado junto do comando vale para arquivos MIDI.')
            session.overrides = {'title': session.title}
            explicit_artist = json.loads(metadata).get('artist') if metadata else None
            if not explicit_artist:
                # A caption identifies the requested song, not an upload's filename prefix.
                session.overrides['artist'] = ''
        session.metadata = await asyncio.to_thread(inspect_input, prepared, session.overrides)

        async def progress(stage):
            if stage in _PROGRESS:
                await session.edit(*_PROGRESS[stage])

        for attempt in range(2):
            try:
                result = await import_cd(prepared, overrides=session.overrides, progress=progress)
                break
            except ImportCDError as exc:
                if (attempt or prepared.kind != 'midi' or session.metadata.get('artist')
                        or exc.code not in {'metadata_ambiguous', 'metadata_unavailable'}):
                    raise
                session.waiting = True
                session.view = _DetailsView(session)
                if not await session.edit('', '', view=session.view):
                    return
                try:
                    await asyncio.wait_for(session.resume.wait(), timeout=_FORM_TIMEOUT)
                except asyncio.TimeoutError:
                    session.cancelled = True
                session.waiting = False
                session.view.stop()
                if session.cancelled:
                    await session.edit('Importação encerrada', 'Envie o comando com o anexo quando quiser tentar novamente.')
                    return
                session.metadata = await asyncio.to_thread(inspect_input, prepared, session.overrides)
        await session.edit(*_result_text(result), color=discord.Color.green())
    except asyncio.CancelledError:
        raise
    except ImportCDError as exc:
        await session.edit('Não foi possível importar', exc.public_message, color=discord.Color.orange())
    except Exception as exc:
        _LOG.warning('[cd_import_ui] type=%s', type(exc).__name__)
        await session.edit('Não foi possível importar',
            'A importação foi interrompida. Tente novamente com o mesmo anexo.', color=discord.Color.orange())
    finally:
        session.release()


async def handle_import_cd(cog, message, *, title=''):
    """Consume the exact command; only attachments on this message are inputs."""
    bot = cog.bot
    if not await _owned(bot, message.author):
        await _send(message.channel, 'Acesso restrito', 'Só o dono do bot pode importar CDs.')
        return True
    # The bot-wide lease is acquired before the first await after validation.
    # It covers downloading, an optional modal, conversion and the commit.
    if getattr(bot, _LEASE_ATTRIBUTE, None) is not None:
        await _send(message.channel, 'Importação em andamento', 'Espere o CD atual terminar para enviar outro.')
        return True
    from osaka_cd.discord_import import ImportCDError
    try:
        if (not isinstance(title, str) or len(title.strip()) > 100
                or any(ord(char) < 32 for char in title)):
            raise ImportCDError('invalid_metadata', 'Use um nome de música de até 100 caracteres.')
        primary, sidecar = _select_attachments(tuple(getattr(message, 'attachments', ()) or ()))
    except ImportCDError as exc:
        await _send(message.channel, 'Importar CD', exc.public_message)
        return True
    session = _Session(cog, message, title=title.strip())
    setattr(bot, _LEASE_ATTRIBUTE, session)
    scheduled = False
    try:
        session.response = await _send(message.channel, 'Importando CD', 'Recebendo o anexo e conferindo o arquivo.')
        scheduler = getattr(cog, '_schedule_tts_background', None)
        if not callable(scheduler):
            await session.edit('Importação indisponível', 'Tente novamente em alguns instantes.')
            return True
        coroutine = _run_import(session, primary, sidecar)
        task = scheduler(coroutine)
        if task is None:
            coroutine.close()
            await session.edit('Importação indisponível', 'Tente novamente em alguns instantes.')
        else:
            # cancel() before a coroutine's first instruction skips its finally.
            # The done callback owns that pre-start edge as well.
            def finished(done):
                session.release()
                if not done.cancelled():
                    error = done.exception()
                    if error is not None:
                        _LOG.warning('[cd_import_task] type=%s', type(error).__name__)
            task.add_done_callback(finished)
            scheduled = True
    finally:
        if not scheduled:
            session.release()
    return True
