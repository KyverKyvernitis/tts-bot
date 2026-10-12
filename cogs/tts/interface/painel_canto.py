"""Painel pessoal de melodias, composto com componentes nativos do Discord.

O catálogo controla as opções. A seleção é temporária até a confirmação e a
amostra usa a rota de canto do cog, sem entrar em call ou na fila de fala.
"""
from __future__ import annotations

import asyncio
import inspect
import io

import discord

import config

from ..failure_notices import _public_notice, failure_code
from ..melodies import DEFAULT_MELODY_ID, get_melody, list_melodies
from ..ttv import DEFAULT_VOICE_ID, language_label, resolve_preferences, voice_label
from .visoes_layout import VisaoLayoutBaseTTS


async def _maybe_await(value):
    return await value if inspect.isawaitable(value) else value


async def _reply(interaction: discord.Interaction, content: str, *, file=None) -> None:
    kwargs = dict(content=content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    if file is not None:
        kwargs["file"] = file
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


class CDPanelView(VisaoLayoutBaseTTS):
    """Um painel de canto por usuário e servidor, sem consultas ao YouTube."""

    def __init__(self, cog, owner_id: int, guild_id: int, *, timeout: float = 300):
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
        self._saved = False
        self._initialized = False
        self._rebuild()

    async def refresh(self) -> None:
        """Carrega ajustes e status já disponíveis, antes de enviar a mensagem."""
        self._settings_available = False
        try:
            db = self.cog._get_db()
            if db is not None:
                settings = await _maybe_await(db.resolve_tts(self.guild_id, self.owner_id))
                settings = settings if isinstance(settings, dict) else {}
                self._preferences = resolve_preferences(settings)
                saved_id = settings.get("ttv_melody_id") or DEFAULT_MELODY_ID
                try:
                    self.active_melody_id = get_melody(saved_id).id
                except ValueError:
                    # Não afirmar que Padrão está salva quando a escolha antiga
                    # saiu do catálogo. O usuário pode confirmar a restauração.
                    self.active_melody_id = saved_id if isinstance(saved_id, str) else "unavailable"
                self._settings_available = True
                if not self._initialized:
                    self.selected_melody_id = self.active_melody_id
                    self._initialized = True
        except Exception:
            self._availability_note = "Seus ajustes estão indisponíveis agora."
        try:
            get_melody(self.selected_melody_id)
        except ValueError:
            self.selected_melody_id = DEFAULT_MELODY_ID
        await self._refresh_availability()
        self._rebuild()

    async def _refresh_availability(self) -> None:
        self._preview_available = False
        if not self._settings_available:
            self._availability_note = "Seus ajustes estão indisponíveis agora."
            return
        checker = getattr(self.cog, "_ttv_voice_status", None)
        online_checker = getattr(self.cog, "_tts_phone_worker_online_for_ui", None)
        try:
            online = bool(await _maybe_await(online_checker())) if callable(online_checker) else None
            status = await _maybe_await(checker(self._preferences["ttv_voice_id"])) if callable(checker) else {}
            status = status if isinstance(status, dict) else {}
            if online is None:
                online = bool(status.get("online"))
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
            if protocol is not None and (type(protocol) is not int or protocol < 2):
                self._availability_note = "Atualize o worker para ouvir a melodia padrão."
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
        melody = get_melody(self.selected_melody_id)
        try:
            active_name = get_melody(self.active_melody_id).name
        except ValueError:
            active_name = "Melodia indisponível"
        voice = voice_label(self._preferences.get("ttv_voice_id", DEFAULT_VOICE_ID))
        language = language_label(self._preferences.get("ttv_language"))
        duration = f"{melody.duration_ms / 1000:g}".replace(".", ",")
        card = discord.ui.TextDisplay(
            f"**{melody.name}**\n{melody.description}\n"
            f"-# {melody.note_count} notas · {duration} s · {voice} · {language}"
        )
        container = discord.ui.Container(
            discord.ui.TextDisplay("## 🎼 Melodias de canto\nEscolha a melodia para cantar suas mensagens."),
            accent_color=discord.Color.blurple(),
        )
        cover = str(getattr(melody, "thumbnail_url", "") or "")
        cover_description = melody.name
        if not cover and melody.id == DEFAULT_MELODY_ID:
            bot_user = getattr(getattr(self.cog, "bot", None), "user", None)
            cover = str(getattr(getattr(bot_user, "display_avatar", None), "url", "") or "")
            cover_description = "Painel de canto"
        if cover.startswith("https://"):
            container.add_item(discord.ui.Section(card, accessory=discord.ui.Thumbnail(cover, description=cover_description)))
        else:
            container.add_item(card)
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        self.melody_select = discord.ui.Select(
            custom_id="ttv:cd:melody",
            placeholder="Escolha uma melodia",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(
                label=item.name[:100], value=item.id,
                description=item.description[:100] or None,
                default=item.id == self.selected_melody_id,
            ) for item in list_melodies()],
            disabled=self._preview_running or self._saving or self._is_expired(),
        )
        self.melody_select.callback = self.select_melody
        container.add_item(discord.ui.ActionRow(self.melody_select))
        self.preview_button = discord.ui.Button(
            custom_id="ttv:cd:preview", label="Gerando amostra…" if self._preview_running else "Ouvir amostra",
            emoji="▶️", style=discord.ButtonStyle.secondary,
            disabled=not self._preview_available or self._preview_running or self._is_expired(),
        )
        self.preview_button.callback = self.preview_melody
        already_active = self.selected_melody_id == self.active_melody_id
        self.apply_button = discord.ui.Button(
            custom_id="ttv:cd:apply", label="Melodia em uso" if already_active else "Usar melodia",
            emoji="✅", style=discord.ButtonStyle.primary,
            disabled=already_active or not self._settings_available or self._saving or self._is_expired(),
        )
        self.apply_button.callback = self.apply_melody
        container.add_item(discord.ui.ActionRow(self.preview_button, self.apply_button))
        footer = f"-# Em uso: {active_name} · Sua escolha neste servidor."
        if self._saved:
            footer = f"-# ✓ {active_name} salva · Sua escolha neste servidor."
        if self._availability_note:
            footer += f"\n-# {self._availability_note}"
        container.add_item(discord.ui.TextDisplay(footer))
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        guild = getattr(interaction, "guild", None)
        if guild is None or int(guild.id) != int(self.guild_id):
            await _reply(interaction, "Esse painel só pode ser usado no servidor em que foi aberto.")
            return False
        if self._is_expired():
            prefix = str(getattr(config, "BOT_PREFIX", getattr(config, "PREFIX", "_")) or "_")
            hint = f"`{prefix}cd`"
            try:
                hint = await _maybe_await(self.cog._get_panel_prefix_hint(self.guild_id, "cd"))
            except Exception:
                pass
            await _reply(interaction, f"Essa interação expirou. Use {hint} novamente para abrir suas melodias.")
            return False
        return await super().interaction_check(interaction)

    async def _edit_panel(self, interaction: discord.Interaction | None = None) -> None:
        self._rebuild()
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())
            return
        message = self.message or getattr(interaction, "message", None)
        if message is not None:
            try:
                await message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                # Excluir a mensagem não cancela uma síntese nem desfaz ajustes.
                pass

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
        self._saved = False
        await self.refresh()
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
            await interaction.response.defer()
            await self.cog._set_user_tts_and_refresh(self.guild_id, self.owner_id, ttv_melody_id=melody_id)
            self.active_melody_id = melody_id
            self._saved = True
            await self.refresh()
            await self._edit_panel(interaction)
        except Exception:
            await _reply(interaction, "Não consegui salvar a melodia agora. Tente novamente.")
        finally:
            self._saving = False
            await self._edit_panel(interaction)

    async def preview_melody(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        if self._preview_running:
            await _reply(interaction, "A amostra anterior ainda está sendo gerada.")
            return
        await self.refresh()
        if self._preview_running:
            await _reply(interaction, "A amostra anterior ainda está sendo gerada.")
            return
        if not self._preview_available:
            await _reply(interaction, self._availability_note or "Não consegui gerar a amostra agora.")
            return
        melody_id = self.selected_melody_id
        preview_voice = voice_label(self._preferences["ttv_voice_id"])
        preview_language = language_label(self._preferences["ttv_language"])
        preview_settings = dict(self._preferences)
        self._preview_running = True
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self._edit_panel(interaction)
            mp3 = await asyncio.wait_for(
                self.cog._cd_preview_mp3(self.guild_id, self.owner_id, melody_id=melody_id, settings=preview_settings), timeout=130,
            )
            if not isinstance(mp3, bytes) or not mp3:
                raise ValueError("empty_audio")
            await _reply(
                interaction,
                f"**{get_melody(melody_id).name} · {preview_voice}**\n{preview_language}",
                file=discord.File(io.BytesIO(mp3), filename="cd-amostra.mp3"),
            )
        except asyncio.TimeoutError:
            await _reply(interaction, _public_notice("ttv_singing_timeout"))
        except Exception as error:
            await _reply(interaction, _public_notice(failure_code("ttv_singing_failed", error)))
        finally:
            self._preview_running = False
            await self.refresh()
            await self._edit_panel(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await _reply(interaction, "Essa interação falhou. Abra seu painel de melodias novamente.")

    async def on_timeout(self) -> None:
        self.expires_at_monotonic = 0
        await self._edit_panel()
