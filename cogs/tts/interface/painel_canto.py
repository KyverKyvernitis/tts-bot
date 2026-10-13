"""Painel pessoal de melodias, composto com componentes nativos do Discord.

O catálogo controla as opções. A seleção é temporária até a confirmação e a
amostra usa a rota de canto do cog, sem entrar em call ou na fila de fala.
"""
from __future__ import annotations

import asyncio
import inspect
import io
import hashlib
import logging
import math
import time
import weakref

import discord


from ..failure_notices import _public_notice
from ..singing_errors import singing_failure_code
from ..cd_covers import cached_cover_file, cover_attachment_url, remote_thumbnail_url, _cover_bytes, cover_revision, cover_identity
from ..cd_colors import cover_accent, DEFAULT_COLOR
from ..cd_previews import prepared_sample
from ..melodies import DEFAULT_MELODY_ID, catalog_signature, format_score_notes, get_melody, list_melodies
from .application_emojis import ApplicationSelectEmojis
from ..ttv import DEFAULT_VOICE_ID, language_label, resolve_preferences, voice_label
from .visoes_layout import VisaoLayoutBaseTTS

log = logging.getLogger(__name__)


def _avatar_asset_id(bot) -> str | None:
    """The bot avatar's revision key, not a per-guild or per-user image."""
    user = getattr(bot, "user", None)
    avatar = getattr(user, "display_avatar", None)
    if avatar is None:
        return None
    key = getattr(avatar, "key", None)
    # A default Discord avatar also has a stable URL even without a custom key.
    identity = str(key or getattr(avatar, "url", "") or "")
    if not identity:
        return None
    return "bot_avatar_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


_AVATAR_COLORS: dict[str, int] = {}


async def _avatar_png(bot, asset_id: str) -> bytes | None:
    if asset_id != _avatar_asset_id(bot):
        return None
    avatar = getattr(getattr(bot, "user", None), "display_avatar", None)
    reader = getattr(avatar, "read", None)
    if not callable(reader):
        return None
    data = await reader()
    _AVATAR_COLORS[asset_id] = cover_accent(data)
    return data


def _all_emoji_ids(bot):
    avatar_id = _avatar_asset_id(bot)
    return (avatar_id,) if avatar_id is not None else ()


_CD_EMOJIS = ApplicationSelectEmojis(
    "osakacd", _cover_bytes,
    lambda: (melody.id for melody in list_melodies() if melody.id != DEFAULT_MELODY_ID),
    dynamic_ids=_all_emoji_ids,
    remote_assets=_avatar_png,
    version_for_id=lambda asset_id: "" if asset_id.startswith("bot_avatar_") else cover_identity(asset_id),
)


_APPLY_EMOJI = discord.PartialEmoji.from_str("<a:circulando:1551635281738858670>")
_ACTIVE_EMOJI = discord.PartialEmoji.from_str("<a:Vinyl_Music_loading:1559374199133315122>")


async def _maybe_await(value):
    return await value if inspect.isawaitable(value) else value


async def _reply(interaction: discord.Interaction, content: str, *, file=None) -> None:
    kwargs = dict(content=content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    if file is not None:
        kwargs["file"] = file
    if interaction.response.is_done():
        await asyncio.wait_for(interaction.followup.send(**kwargs), timeout=10.0)
    else:
        await asyncio.wait_for(interaction.response.send_message(**kwargs), timeout=10.0)


_ACTIVE_PANELS = weakref.WeakSet()
_CATALOG_TASK: asyncio.Task | None = None
_CATALOG_COG = None


def start_catalog_watch(cog):
    global _CATALOG_TASK, _CATALOG_COG
    if _CATALOG_COG is not cog and _CATALOG_TASK is not None:
        _CATALOG_TASK.cancel()
    if _CATALOG_COG is not cog or _CATALOG_TASK is None or _CATALOG_TASK.done():
        _CATALOG_COG = cog
        _CATALOG_TASK = asyncio.create_task(_watch_catalog(cog))


def stop_catalog_watch(cog):
    for panel in tuple(_ACTIVE_PANELS):
        if panel.cog is cog:
            panel.expires_at_monotonic = 0
            panel.stop()
    if _CATALOG_COG is cog:
        if _CATALOG_TASK is not None:
            _CATALOG_TASK.cancel()
        _CD_EMOJIS.close()


async def _watch_catalog(cog=None):
    """Refresh open panels only when a published catalog revision changes."""
    signature = catalog_signature()
    bot = getattr(cog, 'bot', None)
    try:
        ready = getattr(bot, 'wait_until_ready', None)
        if callable(ready):
            await ready()
        _CD_EMOJIS.schedule(bot)
        while True:
            await asyncio.sleep(15)
            active = [panel for panel in _ACTIVE_PANELS if not panel._is_expired()]
            if not active and bot is None:
                return
            closed = getattr(bot, 'is_closed', None)
            if callable(closed) and closed():
                return
            # New catalogue assets and avatars are synchronized even when no
            # user currently has an open panel.
            _CD_EMOJIS.schedule(bot)
            current = catalog_signature()
            if current == signature:
                continue
            signature = current
            for panel in active:
                try:
                    # DB/Discord calls for earlier panels may outlive this one.
                    if panel._is_expired() or panel.is_finished():
                        continue
                    if not any(item.id == panel.selected_melody_id for item in list_melodies()):
                        panel.selected_melody_id = DEFAULT_MELODY_ID
                        panel._page = 0
                    await panel.refresh(rebuild=False)
                    if not panel._is_expired() and not panel.is_finished() and panel.message is not None:
                        await panel._edit_panel()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Failed edits do not abort other panels or the watcher.
                    pass
    finally:
        global _CATALOG_TASK, _CATALOG_COG
        if _CATALOG_TASK is asyncio.current_task():
            _CATALOG_TASK = None
            _CATALOG_COG = None


class CDPanelView(VisaoLayoutBaseTTS):
    """Um painel de canto por usuário e servidor, sem consultas ao YouTube."""

    def __init__(self, cog, owner_id: int, guild_id: int, *, timeout: float = 86400):
        super().__init__(cog, owner_id, guild_id, timeout=timeout)
        self.panel_kind = "cd"
        self.active_melody_id = DEFAULT_MELODY_ID
        self.selected_melody_id = DEFAULT_MELODY_ID
        self._preferences = resolve_preferences({})
        self._settings_available = False
        self._preview_available = False
        self._availability_note = ""
        self._preview_running = False
        self._saving = False
        self._initialized = False
        self._settings_at = 0.0
        self._settings_lock = asyncio.Lock()
        self._cover_attachment_key = None
        self._page = 0
        self._page_locked = False
        self._activity_timeout = max(1.0, float(timeout or 86400))
        _ACTIVE_PANELS.add(self)
        self._rebuild()

    async def refresh(self, *, force: bool = False, rebuild: bool = True) -> None:
        try:
            await asyncio.wait_for(self._refresh_current(force=force, rebuild=rebuild), timeout=5.0)
        except asyncio.TimeoutError:
            self._settings_available = self._preview_available = False
            self._settings_at = 0.0
            self._availability_note = 'Seus ajustes estão indisponíveis agora. Tente novamente.'
            log.warning('[cd_panel] stage=settings_refresh type=TimeoutError guild=%s user=%s',
                        self.guild_id, self.owner_id)
            if rebuild:
                self._rebuild()

    async def _refresh_current(self, *, force: bool = False, rebuild: bool = True) -> None:
        """Reuse recent settings; publication refreshes never extend expiry."""
        if not self._is_expired() and not self.is_finished():
            start_catalog_watch(self.cog)
        async with self._settings_lock:
            if force or time.monotonic() >= self._settings_at:
                self._settings_available = False
                self._settings_at = time.monotonic() + 1
                try:
                    db = self.cog._get_db()
                    if db is not None:
                        settings = await _maybe_await(db.resolve_tts(self.guild_id, self.owner_id))
                        settings = settings if isinstance(settings, dict) else {}
                        self._preferences = resolve_preferences(settings)
                        saved_id = settings.get('ttv_melody_id') or DEFAULT_MELODY_ID
                        try:
                            self.active_melody_id = get_melody(saved_id).id
                        except ValueError:
                            self.active_melody_id = saved_id if isinstance(saved_id, str) else 'unavailable'
                        self._settings_available = True
                        self._settings_at = time.monotonic() + 3
                        if not self._initialized:
                            self.selected_melody_id = self.active_melody_id
                            self._initialized = True
                except Exception:
                    self._availability_note = 'Seus ajustes estão indisponíveis agora.'
        try:
            get_melody(self.selected_melody_id)
        except ValueError:
            self.selected_melody_id = DEFAULT_MELODY_ID
        await self._refresh_availability()
        if rebuild:
            self._rebuild()
        if not self._is_expired() and not self.is_finished():
            _CD_EMOJIS.schedule(getattr(self.cog, 'bot', None), self)

    async def _refresh_availability(self) -> None:
        # Prepared UTAU samples are playable even if the phone is currently offline.
        self._preview_available = prepared_sample(self.selected_melody_id, self._preferences) is not None
        if not self._settings_available:
            self._availability_note = "Seus ajustes estão indisponíveis agora."
            return
        if self._preview_available:
            # A validated installed MP3 needs neither worker discovery nor synthesis.
            self._availability_note = ""
            return
        checker = getattr(self.cog, "_ttv_voice_status", None)
        online_checker = getattr(self.cog, "_tts_phone_worker_online_for_ui", None)
        try:
            status = await _maybe_await(checker(self._preferences["ttv_voice_id"])) if callable(checker) else {}
            status = status if isinstance(status, dict) else {}
            online = status.get('online')
            if online is None:
                online = bool(await _maybe_await(online_checker())) if callable(online_checker) else False
            if not online:
                self._availability_note = "Worker offline · você pode escolher e salvar."
                return
            if not status.get("ready") or status.get("available") is False:
                self._availability_note = "A voz está indisponível para gerar amostras agora."
                return
            if status.get("singing_available") is False:
                self._availability_note = "O canto precisa do banco English-CVVC dessa voz."
                return
            protocol = status.get("performance_protocol")
            if protocol is not None and (type(protocol) is not int or protocol < 1):
                self._availability_note = "Atualize o worker para ouvir amostras cantadas."
                return
            languages = status.get("languages")
            if isinstance(languages, (tuple, list)) and self._preferences["ttv_language"] not in languages:
                self._availability_note = "O idioma selecionado está indisponível nessa voz."
                return
            self._preview_available = True
            self._availability_note = ""
        except Exception:
            self._availability_note = "Amostra indisponível no momento · sua seleção pode ser salva."

    def _rebuild(self) -> None:
        self.clear_items()
        catalog = list_melodies()
        if self.selected_melody_id not in {entry.id for entry in catalog}:
            self.selected_melody_id = DEFAULT_MELODY_ID
        melody = get_melody(self.selected_melody_id)
        selected_index = next(i for i, entry in enumerate(catalog) if entry.id == melody.id)
        pages = math.ceil(len(catalog) / 25)
        if pages > 1 and (selected_index // 25 != self._page and not self._page_locked):
            self._page = selected_index // 25
        self._page = min(max(0, self._page), pages - 1)
        visible_catalog = catalog[self._page * 25:(self._page + 1) * 25]
        duration = f"{melody.duration_ms / 1000:g}".replace(".", ",")
        card = discord.ui.TextDisplay(
            f"**{melody.name}**\n-# {melody.note_count} notas · {duration} s\n"
            f"{format_score_notes(melody)}"
        )
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                "## <a:Vinyl_Music_loading:1559374199133315122> Menu de CDs\n"
                "-# Escolha seu CD, o bot vai ler e no modo de canto vai cantar no ritmo dele."
            ),
            accent_color=discord.Color(cover_accent(
                _cover_bytes(melody.id),
                fallback=_AVATAR_COLORS.get(_avatar_asset_id(getattr(self.cog, 'bot', None)) or '', DEFAULT_COLOR)
                if melody.id == DEFAULT_MELODY_ID else DEFAULT_COLOR)),
        )
        cover = (cover_attachment_url(melody.id) or remote_thumbnail_url(melody.id)
                 or str(getattr(melody, "thumbnail_url", "") or ""))
        cover_description = melody.name
        if not cover and melody.id == DEFAULT_MELODY_ID:
            bot_user = getattr(getattr(self.cog, "bot", None), "user", None)
            cover = str(getattr(getattr(bot_user, "display_avatar", None), "url", "") or "")
            cover_description = "Painel de canto"
        if cover.startswith(("https://", "attachment://")):
            container.add_item(discord.ui.Section(card, accessory=discord.ui.Thumbnail(cover, description=cover_description)))
        else:
            container.add_item(card)
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        self.melody_select = discord.ui.Select(
            custom_id="ttv:cd:melody",
            placeholder="Escolha um CD",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(
                label=item.name[:100], value=item.id,
                emoji=_CD_EMOJIS.emoji(
                    _avatar_asset_id(getattr(self.cog, "bot", None)) if item.id == DEFAULT_MELODY_ID else item.id
                ),
                default=item.id == self.selected_melody_id,
            ) for item in visible_catalog],
            disabled=self._preview_running or self._saving or self._is_expired(),
        )
        self.melody_select.callback = self.select_melody
        container.add_item(discord.ui.ActionRow(self.melody_select))
        if pages > 1:
            self.previous_button = discord.ui.Button(
                custom_id="ttv:cd:previous", label="Anterior", emoji="◀️",
                style=discord.ButtonStyle.secondary, disabled=self._page == 0 or self._is_expired() or self._preview_running or self._saving)
            self.next_button = discord.ui.Button(
                custom_id="ttv:cd:next", label="Próxima", emoji="▶️",
                style=discord.ButtonStyle.secondary, disabled=self._page >= pages - 1 or self._is_expired() or self._preview_running or self._saving)
            self.previous_button.callback = self.previous_page
            self.next_button.callback = self.next_page
            container.add_item(discord.ui.ActionRow(self.previous_button, self.next_button))
        self.preview_button = discord.ui.Button(
            custom_id="ttv:cd:preview", label="Gerando amostra…" if self._preview_running else "Ouvir amostra",
            emoji="▶️", style=discord.ButtonStyle.secondary,
            disabled=not self._preview_available or self._preview_running or self._is_expired(),
        )
        self.preview_button.callback = self.preview_melody
        already_active = self.selected_melody_id == self.active_melody_id
        self.apply_button = discord.ui.Button(
            custom_id="ttv:cd:apply", label="CD já em uso" if already_active else "Usar esse CD",
            emoji=_ACTIVE_EMOJI if already_active else _APPLY_EMOJI,
            style=discord.ButtonStyle.primary,
            disabled=already_active or not self._settings_available or self._saving or self._is_expired(),
        )
        self.apply_button.callback = self.apply_melody
        container.add_item(discord.ui.ActionRow(self.preview_button, self.apply_button))
        # A linha permanente "Em uso" é redundante com o botão de estado.
        # Avisos de indisponibilidade continuam visíveis quando relevantes.
        note = self._availability_note
        if self.active_melody_id not in {item.id for item in list_melodies()}:
            note = "Melodia indisponível: a escolha anteriormente salva saiu do catálogo. Escolha outra."
        if note:
            container.add_item(discord.ui.TextDisplay(f"-# {note}"))
        self.add_item(container)

    def panel_files(self) -> list[discord.File]:
        """Local cached attachments; selecting songs never queries YouTube."""
        cover = cached_cover_file(self.selected_melody_id)
        return [cover] if cover is not None else []

    def remember_cover(self) -> None:
        self._cover_attachment_key = (self.selected_melody_id, cover_revision(self.selected_melody_id))

    @property
    def emoji_generation(self) -> int:
        return _CD_EMOJIS.generation

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        guild = getattr(interaction, "guild", None)
        if guild is None or int(guild.id) != int(self.guild_id):
            await _reply(interaction, "Esse painel só pode ser usado no servidor em que foi aberto.")
            return False
        allowed = await super().interaction_check(interaction)
        if allowed:
            # LayoutView extends its own timeout on interaction; keep logical expiry in sync.
            self.expires_at_monotonic = time.monotonic() + self._activity_timeout
        return allowed

    async def _edit_panel(self, interaction: discord.Interaction | None = None) -> bool:
        try:
            return await asyncio.wait_for(self._edit_panel_current(interaction), timeout=10.0)
        except asyncio.TimeoutError:
            log.warning('[cd_panel] stage=panel_edit type=TimeoutError guild=%s user=%s',
                        self.guild_id, self.owner_id)
            return False

    async def _edit_panel_current(self, interaction: discord.Interaction | None = None) -> bool:
        self._rebuild()
        message = self.message or getattr(interaction, "message", None)
        if message is None and (interaction is None or interaction.response.is_done()):
            return False
        key = (self.selected_melody_id, cover_revision(self.selected_melody_id))
        existing = list(getattr(message, 'attachments', ()) or ())
        filename = f'ttv-cd-{self.selected_melody_id}.jpg'
        reuse = bool(key[1] and key == self._cover_attachment_key
                     and any(getattr(attachment, 'filename', None) == filename for attachment in existing))
        files = [] if reuse else self.panel_files()
        attachments = existing if reuse else files
        try:
            if interaction is not None and not interaction.response.is_done():
                edited = await interaction.response.edit_message(view=self, attachments=attachments,
                                                                allowed_mentions=discord.AllowedMentions.none())
                resource = getattr(edited, 'resource', None)
                if resource is not None and type(getattr(resource, 'id', None)) is int:
                    self.message = resource
            elif message is not None:
                try:
                    edited = await message.edit(view=self, attachments=attachments, allowed_mentions=discord.AllowedMentions.none())
                    if edited is not None:
                        self.message = edited
                except discord.HTTPException:
                    # Excluir a mensagem não cancela uma síntese nem desfaz ajustes.
                    return False
            self._cover_attachment_key = key
            return True
        finally:
            for file in files:
                file.close()

    async def select_melody(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        if self._preview_running or self._saving:
            await _reply(interaction, "Espere a operação atual terminar para trocar a melodia.")
            return
        values = self.melody_select.values
        try:
            melody = get_melody(values[0]) if len(values) == 1 else None
        except (ValueError, TypeError):
            melody = None
        if melody is None:
            await _reply(interaction, "Essa melodia não está disponível. Abra o painel novamente.")
            return
        self.selected_melody_id = melody.id
        self._page_locked = False
        await asyncio.wait_for(interaction.response.defer(), timeout=3.0)
        await self.refresh(rebuild=False)
        await self._edit_panel(interaction)

    async def previous_page(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        if self._preview_running or self._saving:
            await _reply(interaction, "Espere a operação atual terminar para trocar a página.")
            return
        self._page = max(0, self._page - 1)
        self._page_locked = True
        await self._edit_panel(interaction)

    async def next_page(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        if self._preview_running or self._saving:
            await _reply(interaction, "Espere a operação atual terminar para trocar a página.")
            return
        self._page = min((len(list_melodies()) - 1) // 25, self._page + 1)
        self._page_locked = True
        await self._edit_panel(interaction)

    async def apply_melody(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        if self._saving:
            await _reply(interaction, "Sua seleção já está sendo salva.")
            return
        if not self._settings_available:
            await _reply(interaction, "Não consegui acessar seus ajustes agora. Tente novamente.")
            return
        if self.selected_melody_id == self.active_melody_id:
            await _reply(interaction, "Essa melodia já está em uso.")
            return
        melody_id = get_melody(self.selected_melody_id).id
        self._saving = True
        try:
            await asyncio.wait_for(interaction.response.defer(), timeout=3.0)
            await asyncio.wait_for(self.cog._set_user_tts_and_refresh(
                self.guild_id, self.owner_id, ttv_melody_id=melody_id), timeout=12.0)
            self.active_melody_id = melody_id
        except asyncio.TimeoutError:
            await _reply(interaction, "A confirmação demorou. Abra o painel novamente para conferir sua seleção.")
        except Exception:
            await _reply(interaction, "Não consegui salvar a melodia agora. Tente novamente.")
        finally:
            self._saving = False
            await self.refresh(force=True, rebuild=False)
            await self._edit_panel(interaction)

    async def preview_melody(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        if self._preview_running:
            await _reply(interaction, "A amostra anterior ainda está sendo gerada.")
            return
        self._preview_running = True
        try:
            await asyncio.wait_for(interaction.response.defer(ephemeral=True, thinking=True), timeout=3.0)
            await self.refresh(force=True, rebuild=False)
            if not self._preview_available:
                await _reply(interaction, self._availability_note or "Não consegui gerar a amostra agora.")
                return
            melody_id = self.selected_melody_id
            melody_name = get_melody(melody_id).name
            preview_voice = voice_label(self._preferences["ttv_voice_id"])
            preview_language = language_label(self._preferences["ttv_language"])
            preview_settings = dict(self._preferences)
            await self._edit_panel(interaction)
            mp3 = prepared_sample(melody_id, preview_settings)
            if mp3 is None:
                mp3 = await asyncio.wait_for(
                    self.cog._cd_preview_mp3(self.guild_id, self.owner_id, melody_id=melody_id, settings=preview_settings), timeout=130,
                )
            if not isinstance(mp3, bytes) or not mp3:
                raise ValueError("empty_audio")
            await _reply(
                interaction,
                f"**{melody_name} · {preview_voice}**\n{preview_language}",
                file=discord.File(io.BytesIO(mp3), filename="cd-amostra.mp3"),
            )
        except asyncio.TimeoutError:
            await _reply(interaction, "-# " + _public_notice("ttv_singing_timeout"))
        except Exception as error:
            code = singing_failure_code(error)
            log.warning('[cd_panel] stage=preview type=%s code=%s guild=%s user=%s',
                        type(error).__name__, code, self.guild_id, self.owner_id)
            await _reply(interaction, "-# " + _public_notice(code))
        finally:
            self._preview_running = False
            await self.refresh(rebuild=False)
            await self._edit_panel(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await super().on_error(interaction, error, item)

    async def on_timeout(self) -> None:
        self.expires_at_monotonic = 0
        _ACTIVE_PANELS.discard(self)
        await self._edit_panel()

    def stop(self) -> None:
        _ACTIVE_PANELS.discard(self)
        super().stop()
