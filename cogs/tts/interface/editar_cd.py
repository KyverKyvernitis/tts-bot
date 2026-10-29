"""Owner-only Components V2 editor for the installed CD catalog."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import unicodedata

import discord

from .importar_cd import (_Session, _DISCORD_TIMEOUT, _FORM_TIMEOUT,
    _LEASE_ATTRIBUTE, _VINYL, _defer, _interaction_notice, _owned, _send, _text)


_LOG = logging.getLogger(__name__)
_PAGE_SIZE = 25


def _service():
    from osaka_cd import discord_edit
    return discord_edit


def _row_name(row):
    return str(row.get('name') or row.get('id') or 'CD')


def _reference(row):
    value = row.get('video_url') or row.get('source_url') or ''
    try:
        identity = _service().parse_video_url(value)
    except (ValueError, TypeError):
        return '', ''
    return (f'https://www.youtube.com/watch?v={identity}',
            f'https://i.ytimg.com/vi/{identity}/hqdefault.jpg')


@dataclass
class _EditorSession(_Session):
    rows: tuple = ()
    page: int = 0
    selected: str = ''
    modal_epoch: int = 0
    save_task: object = None

    def current(self):
        return next((row for row in self.rows if row.get('id') == self.selected), None)

    async def show(self, *, note=''):
        if self.finished:
            return False
        previous = self.view
        if previous is not None:
            previous.stop()
        self.waiting, self.submitted = True, False
        self.view = _EditorView(self, note=note)
        if not await self.edit('', '', view=self.view):
            self.release()
            return False
        return True


class _EditorView(discord.ui.LayoutView):
    def __init__(self, session, *, note=''):
        super().__init__(timeout=_FORM_TIMEOUT)
        self.session = session
        count = len(session.rows)
        pages = max(1, (count + _PAGE_SIZE - 1) // _PAGE_SIZE)
        session.page = max(0, min(session.page, pages - 1))
        start = session.page * _PAGE_SIZE
        page = session.rows[start:start + _PAGE_SIZE]
        items = [discord.ui.TextDisplay('## 💿 Editar CDs\n'
            'Escolha um CD para editar o nome, o artista e o vídeo da capa.')]
        if note:
            items.append(discord.ui.TextDisplay(f'-# {note}'))
        selected = session.current()
        if selected:
            url, thumbnail = _reference(selected)
            detail = f'**{_text(_row_name(selected), 100)}**\n{_text(selected.get("artist") or "Artista não identificado")}'
            if url:
                detail += f'\n-# [Vídeo da capa]({url})'
            if thumbnail:
                items.append(discord.ui.Section(discord.ui.TextDisplay(detail),
                    accessory=discord.ui.Thumbnail(thumbnail, description='Capa do CD selecionado')))
            else:
                items.append(discord.ui.TextDisplay(detail))
        self.select = discord.ui.Select(custom_id='cd-editor:select',
            placeholder='Escolha o CD que deseja editar', min_values=1, max_values=1,
            options=[discord.SelectOption(label=_row_name(row)[:100],
                description=str(row.get('artist') or 'Artista não identificado')[:100],
                value=str(start + index), default=row.get('id') == session.selected,
                emoji='💿') for index, row in enumerate(page)])
        self.select.callback = self.choose
        items.append(discord.ui.ActionRow(self.select))
        edit = discord.ui.Button(label='Editar CD', style=discord.ButtonStyle.primary,
                                 custom_id='cd-editor:edit', disabled=selected is None)
        close = discord.ui.Button(label='Fechar', style=discord.ButtonStyle.secondary,
                                  custom_id='cd-editor:close')
        edit.callback, close.callback = self.edit_cd, self.close
        if pages > 1:
            previous = discord.ui.Button(label='Anterior', style=discord.ButtonStyle.secondary,
                custom_id='cd-editor:previous', disabled=session.page == 0)
            following = discord.ui.Button(label='Próxima', style=discord.ButtonStyle.secondary,
                custom_id='cd-editor:next', disabled=session.page == pages - 1)
            previous.callback, following.callback = self.previous, self.following
            items.append(discord.ui.ActionRow(previous, following, edit, close))
            items.append(discord.ui.TextDisplay(f'-# Página {session.page + 1}/{pages} · {count} CDs'))
        else:
            items.append(discord.ui.ActionRow(edit, close))
            items.append(discord.ui.TextDisplay(f'-# {count} CDs · só o dono do bot pode editar'))
        self.add_item(discord.ui.Container(*items, accent_color=discord.Color.blurple()))

    async def active(self, interaction):
        if not await self.session.check(interaction):
            return False
        task = self.session.save_task
        if task is not None and not task.done():
            await _interaction_notice(interaction, 'Salvando o CD', 'Espere a atualização terminar.')
            return False
        if self.session.view is not self:
            await _interaction_notice(interaction, 'Editor atualizado', 'Use a seleção mais recente.')
            return False
        return True

    async def choose(self, interaction):
        if not await self.active(interaction):
            return
        data = getattr(interaction, 'data', None)
        values = data.get('values') if isinstance(data, dict) else self.select.values
        start = self.session.page * _PAGE_SIZE
        allowed = {str(index) for index in range(start, min(start + _PAGE_SIZE, len(self.session.rows)))}
        if (not isinstance(values, (list, tuple)) or len(values) != 1
                or not isinstance(values[0], str) or values[0] not in allowed):
            await _interaction_notice(interaction, 'Escolha um CD', 'Selecione um dos CDs desta página.')
            return
        identity = self.session.rows[int(values[0])]['id']
        await _defer(interaction)
        if self.session.finished or self.session.view is not self:
            return
        self.session.selected = identity
        self.session.modal_epoch += 1
        await self.session.show()

    async def navigate(self, interaction, delta):
        if not await self.active(interaction):
            return
        await _defer(interaction)
        if self.session.finished or self.session.view is not self:
            return
        self.session.page += delta
        await self.session.show()

    async def previous(self, interaction):
        await self.navigate(interaction, -1)

    async def following(self, interaction):
        await self.navigate(interaction, 1)

    async def edit_cd(self, interaction):
        if not await self.active(interaction):
            return
        row = self.session.current()
        if row is None:
            await _interaction_notice(interaction, 'Escolha um CD', 'Selecione um CD antes de editar.')
            return
        self.session.modal_epoch += 1
        modal = _EditModal(self.session, row, self.session.modal_epoch)
        try:
            await asyncio.wait_for(interaction.response.send_modal(modal), timeout=_DISCORD_TIMEOUT)
        except asyncio.CancelledError:
            modal.stop()
            raise
        except Exception as exc:
            modal.stop()
            _LOG.info('[cd_editor_modal] type=%s', type(exc).__name__)

    async def close(self, interaction):
        if not await self.active(interaction):
            return
        self.session.submitted, self.session.waiting = True, False
        try:
            await _defer(interaction)
            await self.session.edit('Editor encerrado', 'Use `_importcd` para editar outro CD.')
        finally:
            self.session.release()

    async def on_timeout(self):
        if self.session.view is self and self.session.waiting and not self.session.finished:
            try:
                await self.session.edit('Editor encerrado', 'Use `_importcd` quando quiser continuar.')
            finally:
                self.session.release()


class _EditModal(discord.ui.Modal):
    def __init__(self, session, row, epoch):
        super().__init__(title='Editar CD', timeout=_FORM_TIMEOUT)
        self.session, self.row, self.epoch = session, dict(row), epoch
        reference, _ = _reference(row)
        self.name = discord.ui.TextInput(default=_row_name(row)[:100], required=True,
                                        max_length=100, custom_id='cd-editor:name')
        self.artist = discord.ui.TextInput(default=str(row.get('artist') or '')[:160], required=False,
                                          max_length=160, custom_id='cd-editor:artist')
        self.video = discord.ui.TextInput(default=reference, required=False, max_length=800,
            placeholder='https://www.youtube.com/watch?v=...', custom_id='cd-editor:video')
        self.add_item(discord.ui.TextDisplay('O vídeo informado será usado exatamente para buscar a capa.'))
        for label, description, component in [
            ('Nome do CD', 'Como o CD aparece no menu.', self.name),
            ('Artista', 'Deixe em branco para manter o artista atual.', self.artist),
            ('Vídeo da capa', 'Cole o link do YouTube. Em branco, mantém a referência atual.', self.video),
        ]:
            self.add_item(discord.ui.Label(text=label, description=description, component=component))

    async def on_submit(self, interaction):
        session = self.session
        if not await session.check(interaction):
            return
        if self.epoch != session.modal_epoch or session.selected != self.row.get('id'):
            await _interaction_notice(interaction, 'Editor atualizado', 'Abra o formulário do CD novamente.')
            return
        title, artist, url = str(self.name.value).strip(), str(self.artist.value).strip(), str(self.video.value).strip()
        try:
            if (not title or len(title) > 100 or len(artist) > 160
                    or any(unicodedata.category(character).startswith('C') for character in title + artist)):
                raise ValueError('Confira o nome e o artista do CD.')
            if url:
                _service().parse_video_url(url)
        except (ValueError, TypeError) as exc:
            await _interaction_notice(interaction, 'Confira os dados',
                getattr(exc, 'public_message', None) or str(exc) or 'Confira os dados informados.')
            return
        session.submitted, session.waiting = True, False
        if session.view is not None:
            session.view.stop()
        session.metadata = {'title': title}
        self.stop()
        try:
            await _defer(interaction)
        except asyncio.CancelledError:
            session.release()
            raise
        scheduler = getattr(session.cog, '_schedule_tts_background', None)
        if not callable(scheduler):
            await session.edit('Editor indisponível', 'Use `_importcd` para tentar novamente.')
            session.release()
            return
        coroutine = _save(session, self.row, title, artist, url)
        try:
            task = scheduler(coroutine)
        except Exception:
            coroutine.close()
            session.release()
            raise
        if task is None:
            coroutine.close()
            await session.edit('Editor indisponível', 'Use `_importcd` para tentar novamente.')
            session.release()
            return
        session.save_task = task
        def finished(done):
            if session.save_task is done:
                session.save_task = None
            if done.cancelled():
                session.release()
            elif done.exception() is not None:
                _LOG.info('[cd_editor_task] type=%s', type(done.exception()).__name__)
                session.release()
        task.add_done_callback(finished)


async def _save(session, row, title, artist, url):
    try:
        await session.start_progress('artwork')
        await session.progress('artwork')
        result = await _service().edit_cd(row['id'], title=title, artist=artist or None,
            video_url=url or None, expected_version=row['version'], progress=session.progress)
        await session.stop_progress()
        try:
            session.rows = tuple(await asyncio.to_thread(_service().list_editable_cds))
        except Exception:
            await session.edit('CD atualizado', f'**{_text(title)}**\nA capa e os dados foram salvos.')
            session.release()
            return
        session.modal_epoch += 1
        note = 'CD atualizado · capa pronta.'
        if result.state != 'activated':
            note = 'CD salvo · atualizando o menu de CDs.'
        await session.show(note=note)
    except asyncio.CancelledError:
        session.release()
        raise
    except Exception as exc:
        from osaka_cd.discord_import import ImportCDError
        if isinstance(exc, ImportCDError):
            message = exc.public_message
        else:
            message = 'Não consegui salvar agora. Tente novamente pelo editor.'
            _LOG.info('[cd_editor_save] type=%s', type(exc).__name__)
        # Refresh stale versions before another form can be opened.
        try:
            session.rows = tuple(await asyncio.to_thread(_service().list_editable_cds))
            session.modal_epoch += 1
            await session.show(note=_text(message, 260))
        except Exception:
            await session.edit('Não foi possível editar', message, color=discord.Color.orange())
            session.release()
    finally:
        await session.stop_progress()


async def handle_edit_cd(cog, message):
    """Open the installed catalog editor; no attachment or new MIDI is required."""
    if not await _owned(cog.bot, message.author):
        await _send(message.channel, 'Acesso restrito', 'Só o dono do bot pode editar CDs.')
        return True
    if getattr(cog.bot, _LEASE_ATTRIBUTE, None) is not None:
        await _send(message.channel, 'Importação em andamento', 'Feche o editor ou espere o CD atual terminar.')
        return True
    session = _EditorSession(cog, message)
    setattr(cog.bot, _LEASE_ATTRIBUTE, session)
    opened = False
    try:
        session.response = await _send(message.channel, f'{_VINYL} Abrindo seus CDs', 'Carregando o catálogo…')
        session.rows = tuple(await asyncio.to_thread(_service().list_editable_cds))
        if not session.rows:
            await session.edit('Seu catálogo está vazio', 'Envie `_importcd` com um MIDI para adicionar um CD.')
            return True
        opened = await session.show()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _LOG.info('[cd_editor_open] type=%s', type(exc).__name__)
        await session.edit('Editor indisponível', 'Não consegui abrir os CDs agora. Tente novamente.')
    finally:
        if not opened:
            session.release()
    return True


start_editor = handle_edit_cd
