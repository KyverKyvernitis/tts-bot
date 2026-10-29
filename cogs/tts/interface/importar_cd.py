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
_PROGRESS_INTERVAL = 4.0
_VINYL = '<a:Vinyl_Music_loading:1559374199133315122>'
_LEASE_ATTRIBUTE = '_osaka_cd_import_session'
_PROGRESS = {
    'downloading': ('Recebendo o arquivo', 'Baixando seu MIDI ou pacote de CD.', 0),
    'preparing': ('Conferindo a melodia', 'Lendo as notas e o trecho escolhido.', 1),
    'discovering': ('Encontrando a música', 'Buscando o artista e a gravação.', 2),
    'importing': ('Preparando a capa', 'Importando os dados e a imagem do CD.', 3),
    'artwork': ('Preparando a capa', 'Convertendo a imagem para o menu de CDs.', 3),
    'installing': ('Salvando seu CD', 'Guardando a melodia e a capa.', 4),
    'activating': ('Quase pronto', 'Atualizando o menu de CDs.', 5),
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
    edit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    progress_task: object = None
    progress_stage: str = 'downloading'
    progress_started: float = 0
    progress_last_edit: float = 0
    progress_epoch: int = 0
    progress_active: bool = False
    work_task: object = None

    async def stop_progress(self):
        self.progress_active = False
        self.progress_epoch += 1
        task, self.progress_task = self.progress_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def start_progress(self, stage='downloading'):
        await self.stop_progress()
        if self.finished:
            return
        self.progress_stage = stage
        loop = asyncio.get_running_loop()
        if not self.progress_started:
            self.progress_started = loop.time()
        self.progress_active = True
        epoch = self.progress_epoch
        self.progress_task = asyncio.create_task(self._animate_progress(epoch))

    async def progress(self, stage):
        if stage not in _PROGRESS or self.finished or not self.progress_active:
            return
        self.progress_stage = stage
        await self._render_progress(self.progress_epoch)

    async def _animate_progress(self, epoch):
        try:
            while self.progress_active and epoch == self.progress_epoch:
                await asyncio.sleep(_PROGRESS_INTERVAL)
                await self._render_progress(epoch)
        except asyncio.CancelledError:
            return

    async def _render_progress(self, epoch):
        # Network writes share one lock. Stopping the heartbeat then editing a
        # choice/final card cannot be followed by an older progress response.
        async with self.edit_lock:
            if (not self.progress_active or epoch != self.progress_epoch
                    or self.waiting or self.finished or self.response is None):
                return
            now = asyncio.get_running_loop().time()
            if now - self.progress_last_edit < _PROGRESS_INTERVAL:
                return
            self.progress_last_edit = now
            title, detail, position = _PROGRESS[self.progress_stage]
            elapsed = max(0, int(now - self.progress_started))
            timer = f'{elapsed // 60}:{elapsed % 60:02d}'
            steps = ' '.join('●' if index < position else '◉' if index == position else '○'
                             for index in range(6))
            attachments = getattr(self.source, 'attachments', ()) or ()
            filename = str(getattr(attachments[0], 'filename', '')) if attachments else ''
            name = self.metadata.get('title') or self.title or PurePosixPath(filename).stem or 'Seu CD'
            body = f'**{_text(name, 100)}**\n{detail}\n-# {steps} · {timer}'
            try:
                await asyncio.wait_for(self.response.edit(
                    view=_notice(f'{_VINYL} {title}', body),
                    allowed_mentions=discord.AllowedMentions.none()), timeout=_DISCORD_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOG.info('[cd_import_progress] type=%s', type(exc).__name__)

    async def edit(self, title, body, *, color=None, view=None):
        await self.stop_progress()
        if self.response is None:
            return False
        async with self.edit_lock:
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
        self.progress_active = False
        self.progress_epoch += 1
        if self.progress_task is not None:
            self.progress_task.cancel()
            self.progress_task = None
        if self.view is not None:
            self.view.stop()
        if getattr(self.cog.bot, _LEASE_ATTRIBUTE, None) is self:
            delattr(self.cog.bot, _LEASE_ATTRIBUTE)


def _lease_has_active_task(session):
    for name in ('work_task', 'save_task'):
        task = getattr(session, name, None)
        if task is not None and callable(getattr(task, 'done', None)) and not task.done():
            return True
    return False


async def _accept_cd_command(cog, message):
    """Reuse an idle editor, while real imports/saves retain the shared lease.

    Callers must check bot ownership before calling this helper. Retirement is
    synchronous, so a new authorized command cannot race an old modal into a
    second publication while its previous view is being closed.
    """
    session = getattr(cog.bot, _LEASE_ATTRIBUTE, None)
    if session is None:
        return True
    active = _lease_has_active_task(session)
    replace = getattr(session, 'can_replace_for_command', None)
    if not active and callable(replace) and replace():
        session.retire_for_command()
        return True
    if not active and getattr(session, 'finished', False):
        release = getattr(session, 'release', None)
        if callable(release):
            release()
        if getattr(cog.bot, _LEASE_ATTRIBUTE, None) is session:
            delattr(cog.bot, _LEASE_ATTRIBUTE)
        return True
    if callable(replace):
        await _send(message.channel, 'Salvando CD' if active or session.submitted else 'Abrindo CDs',
                    'Espere esta atualização terminar para continuar.')
    else:
        await _send(message.channel, 'Importação em andamento', 'Espere o CD atual terminar para enviar outro.')
    return False


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


class _ChoicesView(discord.ui.LayoutView):
    """Choose public recording evidence without asking the owner to type credits."""

    def __init__(self, session, candidates=(), *, artwork=False):
        super().__init__(timeout=_FORM_TIMEOUT)
        self.session = session
        # Values identify these server-side rows, never client-supplied metadata.
        self.candidates = tuple(dict(row) for row in tuple(candidates)[:5]
                                if isinstance(row, dict) and row.get('title') and row.get('artist'))
        name = _text(session.metadata.get('title') or 'Seu CD', 100)
        if self.candidates:
            heading = 'Escolha a música'
            detail = 'Selecione a gravação certa para buscar os dados e a capa.'
            if artwork:
                detail = 'A capa não ficou pronta. Escolha outra gravação ou tente novamente.'
        else:
            heading = 'Vamos tentar outra busca'
            detail = 'A busca não retornou opções agora. Seu arquivo continua aqui.'
        rows = [discord.ui.TextDisplay(f'## 💿 {heading}\n**{name}**\n{detail}')]
        self.select = None
        if self.candidates:
            self.select = discord.ui.Select(
                placeholder='Escolha uma das músicas encontradas', min_values=1, max_values=1,
                custom_id='cd-import:choice', options=[
                    discord.SelectOption(label=str(row['title'])[:100],
                        description=str(row['artist'])[:100], value=str(index), emoji='💿')
                    for index, row in enumerate(self.candidates)])
            self.select.callback = self.choose
            rows.append(discord.ui.ActionRow(self.select))
        retry = discord.ui.Button(label='Buscar novamente', style=discord.ButtonStyle.secondary,
                                  custom_id='cd-import:retry')
        cancel = discord.ui.Button(label='Cancelar', style=discord.ButtonStyle.secondary,
                                   custom_id='cd-import:cancel')
        retry.callback, cancel.callback = self.retry, self.cancel
        rows.append(discord.ui.ActionRow(retry, cancel))
        self.add_item(discord.ui.Container(*rows, accent_color=discord.Color.blurple()))

    async def _active(self, interaction):
        if not await self.session.check(interaction):
            return False
        if self.session.view is not self:
            await _interaction_notice(interaction, 'Lista atualizada', 'Escolha uma opção na lista mais recente.')
            return False
        return True

    async def choose(self, interaction):
        if not await self._active(interaction):
            return
        data = getattr(interaction, 'data', None)
        values = data.get('values') if isinstance(data, dict) else (
            self.select.values if self.select is not None else ())
        if (not isinstance(values, (list, tuple)) or len(values) != 1
                or not isinstance(values[0], str)
                or values[0] not in {str(index) for index in range(len(self.candidates))}):
            await _interaction_notice(interaction, 'Escolha uma música', 'Selecione uma das opções da lista.')
            return
        choice = self.candidates[int(values[0])]
        metadata = self.session.metadata
        overrides = dict(self.session.overrides or {})
        # Keep an English upload name and its musical crop. A translated public
        # title is an alias for matching, not a new catalog identity.
        title = metadata.get('title') or choice['title']
        overrides.update(title=title, artist=choice['artist'])
        for field in ('aliases', 'artist_aliases'):
            values = [*metadata.get(field, []), *choice.get(field, [])]
            if field == 'aliases' and choice['title'] != title:
                values.append(choice['title'])
            overrides[field] = list(dict.fromkeys(values))[:12]
        overrides['seed_video_ids'] = list(choice.get('seed_video_ids', []))[:12]
        if choice.get('search_query'):
            overrides['search_query'] = choice['search_query']
        self.session.overrides = overrides
        self._resume()
        await _defer(interaction)

    def _resume(self):
        # No await between the permission/state check and this latch: duplicate
        # interaction deliveries cannot schedule a second publication.
        self.session.submitted = True
        self.session.waiting = False
        self.session.resume.set()
        self.stop()

    async def retry(self, interaction):
        if not await self._active(interaction):
            return
        self._resume()
        await _defer(interaction)

    async def cancel(self, interaction):
        if not await self._active(interaction):
            return
        self.session.cancelled = True
        self._resume()
        await _defer(interaction)

    async def on_timeout(self):
        if self.session.view is self and self.session.waiting and not self.session.submitted:
            self.session.cancelled = True
            self._resume()


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
        artist = str(song.get('artist') or '').strip()
        credit = f' · {_text(artist)}' if artist and artist.casefold() not in {
            'artista não identificado', 'unknown', 'unknown artist'} else ''
        lines.append(f'**{name}**{credit}\n-# {notes} notas · {duration:g} s')
    if len(songs) > 3:
        lines.append(f'-# Mais {len(songs) - 3} CDs no catálogo.')
    missing = set(result.pending)
    pending = []
    if 'metadata' in missing:
        pending.append('metadados')
    if 'thumbnail' in missing or 'cover' in missing:
        pending.append('capa')
    if pending:
        lines.append('-# Falta concluir: ' + ' e '.join(pending) + '.')
    else:
        lines.append('-# Capa pronta · no menu de CDs.' if result.state == 'activated'
                     else '-# Capa pronta.')
    if result.state != 'activated' and not result.already_installed:
        lines.append('-# Aguardando a atualização do menu de CDs.')
    return title, '\n'.join(lines) or 'O catálogo de CDs foi atualizado.'


async def _run_import(session, primary, sidecar):
    from osaka_cd.discord_import import (
        MAX_MIDI_BYTES, MAX_ZIP_BYTES, MAX_SIDECAR_BYTES,
        ImportCDError, prepare_input, inspect_input, import_cd,
    )
    try:
        await session.start_progress()
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
                await session.progress(stage)

        while True:
            try:
                result = await import_cd(prepared, overrides=session.overrides, progress=progress)
                break
            except ImportCDError as exc:
                if (prepared.kind != 'midi' or exc.code not in {
                        'metadata_ambiguous', 'metadata_unavailable', 'artwork_unavailable'}):
                    raise
                await session.stop_progress()
                session.resume.clear()
                session.submitted = False
                session.cancelled = False
                session.waiting = True
                session.view = _ChoicesView(session, getattr(exc, 'candidates', ()),
                                           artwork=exc.code == 'artwork_unavailable')
                if not await session.edit('', '', view=session.view):
                    return
                try:
                    await asyncio.wait_for(session.resume.wait(), timeout=_FORM_TIMEOUT)
                except asyncio.TimeoutError:
                    session.cancelled = True
                session.waiting = False
                session.view.stop()
                if session.cancelled:
                    await session.edit('Importação encerrada', 'Você pode enviar o MIDI novamente quando quiser.')
                    return
                session.metadata = await asyncio.to_thread(inspect_input, prepared, session.overrides)
                await session.start_progress('discovering')
        await session.edit(*_result_text(result), color=discord.Color.orange() if result.pending else discord.Color.green())
    except asyncio.CancelledError:
        raise
    except ImportCDError as exc:
        await session.edit('Não foi possível importar', exc.public_message, color=discord.Color.orange())
    except Exception as exc:
        _LOG.warning('[cd_import_ui] type=%s', type(exc).__name__)
        await session.edit('Não foi possível importar',
            'A importação foi interrompida. Tente novamente com o mesmo anexo.', color=discord.Color.orange())
    finally:
        await session.stop_progress()
        session.release()


async def handle_import_cd(cog, message, *, title=''):
    """Edit installed CDs alone, or import attachments on this exact message."""
    bot = cog.bot
    if not await _owned(bot, message.author):
        await _send(message.channel, 'Acesso restrito', 'Só o dono do bot pode importar CDs.')
        return True
    # The bot-wide lease is acquired before the first await after validation.
    # It covers downloading, recording selection, conversion and the commit.
    if not await _accept_cd_command(cog, message):
        return True
    from osaka_cd.discord_import import ImportCDError
    try:
        if (not isinstance(title, str) or len(title.strip()) > 100
                or any(ord(char) < 32 for char in title)):
            raise ImportCDError('invalid_metadata', 'Use um nome de música de até 100 caracteres.')
        attachments = tuple(getattr(message, 'attachments', ()) or ())
        if not attachments and not title.strip():
            from .editar_cd import handle_edit_cd
            return await handle_edit_cd(cog, message)
        primary, sidecar = _select_attachments(attachments)
    except ImportCDError as exc:
        await _send(message.channel, 'Importar CD', exc.public_message)
        return True
    session = _Session(cog, message, title=title.strip())
    setattr(bot, _LEASE_ATTRIBUTE, session)
    scheduled = False
    try:
        session.response = await _send(message.channel, f'{_VINYL} Importando CD',
            f'**{_text(title or PurePosixPath(primary.filename).stem, 100)}**\nRecebendo seu arquivo…')
        session.progress_last_edit = asyncio.get_running_loop().time()
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
            session.work_task = task
            # cancel() before a coroutine's first instruction skips its finally.
            # The done callback owns that pre-start edge as well.
            def finished(done):
                if session.work_task is done:
                    session.work_task = None
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
