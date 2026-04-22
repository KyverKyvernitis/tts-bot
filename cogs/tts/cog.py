import inspect
import contextlib
import asyncio
import time
import os
import re
import weakref
import unicodedata
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

try:
    from google.cloud import texttospeech_v1 as google_texttospeech
except Exception:  # pragma: no cover - dependência opcional em tempo de import
    google_texttospeech = None

import config
from .audio import GuildTTSState, QueueItem, TTSAudioMixin, TTS_BOOT_WARMUP_ENABLED
from .common import (
    _guild_scoped,
    _shorten,
    _replace_custom_emojis_for_tts,
    _normalize_spaces,
    _speech_name,
    _looks_pronounceable_for_tts,
    _extract_primary_domain,
    _expand_abbreviations_for_tts,
    USER_MENTION_PATTERN,
    ROLE_MENTION_PATTERN,
    CHANNEL_MENTION_PATTERN,
    URL_PATTERN,
    DISCORD_CHANNEL_URL_PATTERN,
    _ATTACHMENT_IMAGE_EXTENSIONS,
    _ATTACHMENT_VIDEO_EXTENSIONS,
    get_gtts_languages,
    build_gtts_language_aliases,
    validate_mode,
)
from .utils.embed import (
    make_embed,
    build_expired_panel_embed,
    build_toggle_embed,
    build_status_embed,
    build_settings_embed,
    status_voice_channel_text,
    spoken_name_status_text,
)
from .prefix import dispatch_prefix_control_command
from .utils.message_render import render_message_tts_text, append_tts_descriptions
from .utils.message_gate import analyze_message_for_tts
from .utils.message_dispatch import dispatch_message_tts
from .utils.resolution import (
    gcloud_language_priority,
    build_gcloud_language_options_from_catalog,
    gcloud_voice_priority,
    split_gcloud_voice_name,
    describe_gcloud_voice,
    build_gcloud_voice_options_from_catalog,
    gcloud_voice_matches_language,
    pick_first_gcloud_voice_for_language,
    normalize_rate_value,
    normalize_pitch_value,
    normalize_language_query,
    resolve_gtts_language_input,
    validate_gcloud_language_input,
    validate_gcloud_voice_input,
    normalize_gcloud_rate_value,
    normalize_gcloud_pitch_value,
)
from .ui import (
    _BaseTTSView,
    _SimpleSelectView,
    ModeSelect,
    LanguageSelect,
    SpeedSelect,
    PitchSelect,
    GCloudSpeedSelect,
    GCloudPitchSelect,
    GCloudLanguageSelect,
    GCloudVoiceSelect,
    VoiceRegionSelect,
    VoiceSelect,
    ToggleSelect,
    LanguageCodeModal,
    LanguageHelpView,
    BotPrefixModal,
    GTTSPrefixModal,
    EdgePrefixModal,
    GCloudPrefixModal,
    GCloudLanguageModal,
    GCloudVoiceModal,
    SpokenNameModal,
    IgnoreRoleConfigView,
    TTSMainPanelView,
    TTSStatusView,
    TTSTogglePanelView,
)

from .utils.panel_apply import (
    _apply_server_prefix_from_modal as apply_server_prefix_from_modal,
    _apply_mode_from_panel as apply_mode_from_panel,
    _apply_voice_from_panel as apply_voice_from_panel,
    _apply_language_from_panel as apply_language_from_panel,
    _apply_speed_from_panel as apply_speed_from_panel,
    _apply_pitch_from_panel as apply_pitch_from_panel,
    _apply_gcloud_language_from_modal as apply_gcloud_language_from_modal,
    _apply_gcloud_voice_from_modal as apply_gcloud_voice_from_modal,
    _apply_gcloud_language_from_panel as apply_gcloud_language_from_panel,
    _apply_gcloud_voice_from_panel as apply_gcloud_voice_from_panel,
    _apply_gcloud_speed_from_panel as apply_gcloud_speed_from_panel,
    _apply_gcloud_pitch_from_panel as apply_gcloud_pitch_from_panel,
    _apply_spoken_name_from_modal as apply_spoken_name_from_modal,
    _apply_announce_author_from_panel as apply_announce_author_from_panel,
    _apply_auto_leave_from_panel as apply_auto_leave_from_panel,
)

USER_CONFIG_ACTION_CHOICES = [
    app_commands.Choice(name="Abrir painel pessoal do usuário", value="panel"),
    app_commands.Choice(name="Alterar apelido falado do usuário", value="spoken_name"),
    app_commands.Choice(name="Resetar configurações do usuário para as do servidor", value="reset"),
]

STATUS_ACTION_CHOICES = [
    app_commands.Choice(name="Ver o meu status", value="self"),
    app_commands.Choice(name="Mostrar o status de outro usuário no chat", value="show_other"),
    app_commands.Choice(name="Copiar as configurações de outro usuário", value="copy_other"),
]


class TTSVoice(TTSAudioMixin, commands.GroupCog, group_name="tts", group_description="Comandos de texto para fala"):
    server = app_commands.Group(name="server", description="Configurações padrão do servidor")
    voices = app_commands.Group(name="voices", description="Listas de vozes e idiomas")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_states: dict[int, GuildTTSState] = {}
        self.edge_voice_cache: list[str] = []
        self.edge_voice_names: set[str] = set()
        self.gtts_languages: dict[str, str] = get_gtts_languages()
        self.gtts_language_aliases: dict[str, str] = build_gtts_language_aliases(self.gtts_languages)
        self._recent_tts_message_ids: dict[int, float] = {}
        self._voice_connect_locks: dict[int, asyncio.Lock] = {}
        self._prefix_panel_cooldowns: dict[tuple[int, int, str], float] = {}
        self._active_prefix_panels: dict[tuple[int, int, str], tuple[discord.Message, discord.ui.View]] = {}
        self._public_panel_states: dict[int, dict] = {}
        self._status_views_by_target: dict[tuple[int, int], weakref.WeakSet[TTSStatusView]] = {}
        self._status_refresh_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._last_announced_author_by_guild: dict[int, int] = {}
        self._gcloud_voices_cache: list[dict[str, object]] = []
        self._gcloud_voices_cache_loaded_at: float = 0.0
        self._gcloud_voices_cache_lock = asyncio.Lock()
        self._app_command_id_cache: dict[object, tuple[float, dict[str, int]]] = {}
        self._voice_restore_task: asyncio.Task | None = None
        self._runtime_voice_restore_tasks: dict[int, asyncio.Task] = {}
        self._runtime_voice_restore_failures: dict[int, int] = {}
        self._runtime_voice_restore_next_allowed_at: dict[int, float] = {}
        self._runtime_voice_restore_suppressed_until: dict[int, float] = {}
        self._expected_voice_channel_ids: dict[int, int] = {}
        self._manual_voice_disconnect_until: dict[int, float] = {}

    async def cog_load(self):
        self._prime_tts_runtime()
        await self._load_edge_voices()
        if TTS_BOOT_WARMUP_ENABLED:
            asyncio.create_task(self._boot_warmup())
        self._voice_restore_task = asyncio.create_task(self._restore_voice_sessions_after_ready())

    async def _get_root_command_ids_cached(self, guild: discord.Guild | None = None, *, ttl_seconds: float = 600.0) -> dict[str, int]:
        return await fetch_root_command_ids_cached(
            self.bot,
            self._app_command_id_cache,
            guild,
            ttl_seconds=ttl_seconds,
            include_global_fallback=False,
        )

    def _get_db(self):
        return getattr(self.bot, "settings_db", None)


    async def _set_remembered_voice_channel(self, guild_id: int, channel_id: int | None) -> None:
        db = self._get_db()
        if db is None or not hasattr(db, "set_tts_voice_channel_id"):
            return
        try:
            await self._maybe_await(db.set_tts_voice_channel_id(guild_id, channel_id))
        except Exception as e:
            print(f"[tts_voice] erro ao salvar canal de voz lembrado da guild {guild_id}: {e}")

    async def _clear_remembered_voice_channel(self, guild_id: int) -> None:
        await self._set_remembered_voice_channel(guild_id, None)

    async def _get_remembered_voice_channel_id(self, guild_id: int) -> int:
        db = self._get_db()
        if db is None or not hasattr(db, "get_tts_voice_channel_id"):
            return 0
        try:
            value = db.get_tts_voice_channel_id(guild_id)
            value = await self._maybe_await(value)
            return max(0, int(value or 0))
        except Exception as e:
            print(f"[tts_voice] erro ao ler canal de voz lembrado da guild {guild_id}: {e}")
            return 0

    def _get_bot_voice_state_channel(self, guild: discord.Guild | None):
        if guild is None:
            return None
        me = getattr(guild, "me", None)
        me_voice = getattr(me, "voice", None)
        return getattr(me_voice, "channel", None)

    def _is_voice_client_stale(self, guild: discord.Guild, vc: discord.VoiceClient | None) -> bool:
        actual_channel = self._get_bot_voice_state_channel(guild)
        if vc is None:
            return actual_channel is not None

        try:
            connected = bool(vc.is_connected())
        except Exception:
            connected = False

        vc_channel = getattr(vc, "channel", None)
        if not connected:
            return vc_channel is not None or actual_channel is not None

        if actual_channel is not None and vc_channel is not None:
            return getattr(vc_channel, "id", None) != getattr(actual_channel, "id", None)

        return False

    async def _clear_ghost_voice_state(self, guild: discord.Guild, *, reason: str) -> None:
        actual_channel = self._get_bot_voice_state_channel(guild)
        if actual_channel is None:
            return
        try:
            await guild.change_voice_state(channel=None)
            await asyncio.sleep(0.5)
            print(f"[tts_voice] estado fantasma limpo | guild={guild.id} reason={reason} channel={getattr(actual_channel, 'id', None)}")
        except Exception as e:
            print(f"[tts_voice] falha ao limpar estado fantasma | guild={guild.id} reason={reason} error={e}")

    async def _recover_stale_voice_client(self, guild: discord.Guild, *, reason: str) -> None:
        vc = self._get_voice_client_for_guild(guild)
        stale = self._is_voice_client_stale(guild, vc)
        if not stale:
            return
        try:
            if vc is not None:
                try:
                    if vc.is_playing() or vc.is_paused():
                        vc.stop()
                except Exception:
                    pass
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
        finally:
            await self._clear_ghost_voice_state(guild, reason=reason)

    async def _restore_voice_sessions_after_ready(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(6.0)
        db = self._get_db()
        if db is None or not hasattr(db, "iter_tts_voice_channel_ids"):
            return

        try:
            remembered = db.iter_tts_voice_channel_ids()
            remembered = await self._maybe_await(remembered)
        except Exception as e:
            print(f"[tts_voice] erro ao listar canais de voz lembrados: {e}")
            return

        pending = {int(gid): int(cid) for gid, cid in dict(remembered or {}).items() if int(cid or 0) > 0}
        if not pending:
            return

        for attempt in range(4):
            if not pending or self.bot.is_closed():
                break

            remaining: dict[int, int] = {}
            for guild_id, channel_id in list(pending.items()):
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    continue

                channel = guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
                if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                    await self._clear_remembered_voice_channel(guild_id)
                    continue

                auto_leave_enabled = True
                try:
                    auto_leave_enabled = bool(await self._get_guild_toggle_value(
                        guild_id,
                        public_key="auto_leave",
                        raw_key="auto_leave_enabled",
                        default=True,
                    ))
                except Exception:
                    auto_leave_enabled = True

                humans_present = any(not getattr(member, "bot", False) for member in getattr(channel, "members", []))
                if auto_leave_enabled and not humans_present:
                    remaining[guild_id] = channel_id
                    continue

                current_vc = self._get_voice_client_for_guild(guild)
                if current_vc is not None and not self._is_voice_client_stale(guild, current_vc):
                    current_channel = getattr(current_vc, "channel", None)
                    if getattr(current_channel, "id", None) == channel_id:
                        continue

                try:
                    await self._recover_stale_voice_client(guild, reason="startup_restore")
                    vc = await self._ensure_connected(guild, channel)
                    if vc is not None and getattr(vc, "is_connected", lambda: False)():
                        self._remember_expected_voice_channel(guild_id, channel_id)
                        self._runtime_voice_restore_failures[guild_id] = 0
                        self._runtime_voice_restore_next_allowed_at[guild_id] = 0.0
                        print(f"[tts_voice] call restaurada após boot | guild={guild_id} channel={channel_id}")
                        continue
                except Exception as e:
                    print(f"[tts_voice] falha ao restaurar call após boot | guild={guild_id} channel={channel_id} error={e}")

                remaining[guild_id] = channel_id

            pending = remaining
            if pending:
                await asyncio.sleep(8.0 + (attempt * 4.0))

    async def _get_voice_moderation_settings(self, guild_id: int) -> dict:
        db = self._get_db()
        if db is None or not hasattr(db, "get_voice_moderation_settings"):
            return {}
        try:
            settings = db.get_voice_moderation_settings(guild_id)
            settings = await self._maybe_await(settings)
            return dict(settings or {})
        except Exception as e:
            print(f"[tts_voice] erro ao consultar moderação de voz da guild {guild_id}: {e}")
            return {}

    async def _voice_should_self_deaf(self, guild_id: int) -> bool:
        settings = await self._get_voice_moderation_settings(guild_id)
        return not bool(settings.get("enabled", False))

    async def _should_use_receive_voice_client(self, guild_id: int) -> bool:
        settings = await self._get_voice_moderation_settings(guild_id)
        return bool(settings.get("enabled", False))

    async def _notify_voice_moderation_ready(self, guild: discord.Guild, vc: discord.VoiceClient | None = None) -> None:
        cog = self.bot.get_cog("VoiceModeration")
        if cog is None or not hasattr(cog, "handle_voice_client_ready"):
            return
        try:
            await cog.handle_voice_client_ready(guild, vc or self._get_voice_client_for_guild(guild))
        except Exception as e:
            print(f"[tts_voice] erro ao notificar moderação de voz na guild {guild.id}: {e}")
    async def _notify_voice_moderation_playback_start(self, guild: discord.Guild, vc: discord.VoiceClient | None = None) -> None:
        cog = self.bot.get_cog("VoiceModeration")
        if cog is None or not hasattr(cog, "pause_for_tts_playback"):
            return
        try:
            await cog.pause_for_tts_playback(guild, vc or self._get_voice_client_for_guild(guild))
        except Exception as e:
            print(f"[tts_voice] erro ao pausar moderação de voz na guild {guild.id}: {e}")

    async def _notify_voice_moderation_playback_end(self, guild: discord.Guild, vc: discord.VoiceClient | None = None) -> None:
        cog = self.bot.get_cog("VoiceModeration")
        if cog is None or not hasattr(cog, "resume_after_tts_playback"):
            return
        try:
            await cog.resume_after_tts_playback(guild, vc or self._get_voice_client_for_guild(guild))
        except Exception as e:
            print(f"[tts_voice] erro ao retomar moderação de voz na guild {guild.id}: {e}")


    async def _set_user_tts_and_refresh(self, guild_id: int, user_id: int, *, history_entry: str | None = None, **kwargs):
        db = self._get_db()
        if db is None:
            raise RuntimeError("settings db unavailable")
        result = await self._maybe_await(db.set_user_tts(guild_id, user_id, **kwargs))
        if history_entry and hasattr(db, "set_user_panel_last_change"):
            await self._maybe_await(db.set_user_panel_last_change(guild_id, user_id, history_entry))
        await self._notify_status_views_changed(guild_id, user_id)
        return result

    async def _reset_user_tts_and_refresh(self, guild_id: int, user_id: int, *, history_entry: str | None = None):
        db = self._get_db()
        if db is None:
            raise RuntimeError("settings db unavailable")
        result = await self._maybe_await(db.reset_user_tts(guild_id, user_id))
        if history_entry and hasattr(db, "set_user_panel_last_change"):
            await self._maybe_await(db.set_user_panel_last_change(guild_id, user_id, history_entry))
        await self._notify_status_views_changed(guild_id, user_id)
        return result

    def _register_status_view(self, view: TTSStatusView) -> None:
        if view.message is None:
            return
        target_user_id = int(view.target_user_id or view.owner_id or 0)
        if not target_user_id:
            return
        key = (int(view.guild_id), target_user_id)
        views = self._status_views_by_target.get(key)
        if views is None:
            views = weakref.WeakSet()
            self._status_views_by_target[key] = views
        views.add(view)

    def _unregister_status_view(self, view: TTSStatusView) -> None:
        target_user_id = int(view.target_user_id or view.owner_id or 0)
        if not target_user_id:
            return
        key = (int(view.guild_id), target_user_id)
        views = self._status_views_by_target.get(key)
        if not views:
            return
        views.discard(view)
        if not list(views):
            self._status_views_by_target.pop(key, None)
            self._status_refresh_locks.pop(key, None)

    async def _notify_status_views_changed(self, guild_id: int, user_id: int) -> None:
        key = (int(guild_id), int(user_id))
        views = self._status_views_by_target.get(key)
        if not views:
            return
        active_views = [view for view in list(views) if getattr(view, "message", None) is not None and not view.is_finished()]
        if not active_views:
            self._status_views_by_target.pop(key, None)
            self._status_refresh_locks.pop(key, None)
            return
        lock = self._status_refresh_locks.setdefault(key, asyncio.Lock())
        async with lock:
            for view in list(active_views):
                await view.refresh_from_config_change()

    def _cancel_runtime_voice_restore(self, guild_id: int) -> None:
        task = self._runtime_voice_restore_tasks.pop(int(guild_id), None)
        if task is not None and not task.done():
            task.cancel()

    def _suppress_runtime_voice_restore(self, guild_id: int, *, seconds: float = 15.0) -> None:
        self._runtime_voice_restore_suppressed_until[int(guild_id)] = time.monotonic() + max(0.0, float(seconds))

    def _runtime_voice_restore_is_suppressed(self, guild_id: int) -> bool:
        until = float(self._runtime_voice_restore_suppressed_until.get(int(guild_id), 0.0) or 0.0)
        return until > time.monotonic()

    async def _runtime_should_restore_voice(self, guild_id: int) -> bool:
        try:
            auto_leave_enabled = bool(await self._get_guild_toggle_value(
                guild_id,
                public_key="auto_leave",
                raw_key="auto_leave_enabled",
                default=True,
            ))
        except Exception:
            auto_leave_enabled = True
        return not auto_leave_enabled

    def _remember_expected_voice_channel(self, guild_id: int, channel_id: int | None) -> None:
        try:
            parsed = max(0, int(channel_id or 0))
        except Exception:
            parsed = 0
        if parsed > 0:
            self._expected_voice_channel_ids[int(guild_id)] = parsed
        else:
            self._expected_voice_channel_ids.pop(int(guild_id), None)

    def _mark_manual_voice_disconnect(self, guild_id: int, *, seconds: float = 45.0) -> None:
        self._manual_voice_disconnect_until[int(guild_id)] = time.monotonic() + max(5.0, float(seconds))

    def _clear_manual_voice_disconnect(self, guild_id: int) -> None:
        self._manual_voice_disconnect_until.pop(int(guild_id), None)

    def _is_manual_voice_disconnect_recent(self, guild_id: int) -> bool:
        until = float(self._manual_voice_disconnect_until.get(int(guild_id), 0.0) or 0.0)
        if until <= time.monotonic():
            self._manual_voice_disconnect_until.pop(int(guild_id), None)
            return False
        return True

    async def suppress_runtime_voice_restore(self, guild_id: int, *, seconds: float = 20.0, expected_channel_id: int | None = None) -> None:
        guild_id = int(guild_id)
        self._suppress_runtime_voice_restore(guild_id, seconds=seconds)
        self._cancel_runtime_voice_restore(guild_id)
        if expected_channel_id is not None:
            self._remember_expected_voice_channel(guild_id, expected_channel_id)
            with contextlib.suppress(Exception):
                await self._set_remembered_voice_channel(guild_id, expected_channel_id)


    async def _schedule_runtime_voice_restore(
        self,
        guild: discord.Guild,
        *,
        channel_id: int | None = None,
        reason: str,
        initial_delay: float = 4.0,
    ) -> None:
        guild_id = int(guild.id)
        if self.bot.is_closed() or self._runtime_voice_restore_is_suppressed(guild_id):
            return
        if not await self._runtime_should_restore_voice(guild_id):
            return

        target_channel_id = 0
        try:
            target_channel_id = max(0, int(channel_id or 0))
        except Exception:
            target_channel_id = 0
        if target_channel_id <= 0:
            target_channel_id = int(self._expected_voice_channel_ids.get(guild_id, 0) or 0)
        if target_channel_id <= 0:
            target_channel_id = await self._get_remembered_voice_channel_id(guild_id)
        if target_channel_id <= 0:
            return

        self._expected_voice_channel_ids[guild_id] = target_channel_id
        current_task = self._runtime_voice_restore_tasks.get(guild_id)
        if current_task is not None and not current_task.done():
            return

        async def _runner() -> None:
            delay = max(1.5, float(initial_delay))
            attempt = 0
            try:
                while not self.bot.is_closed():
                    if self._runtime_voice_restore_is_suppressed(guild_id):
                        return

                    now = time.monotonic()
                    next_allowed_at = float(self._runtime_voice_restore_next_allowed_at.get(guild_id, 0.0) or 0.0)
                    wait_for = max(delay, max(0.0, next_allowed_at - now))
                    if wait_for > 0.0:
                        await asyncio.sleep(wait_for)

                    if self._runtime_voice_restore_is_suppressed(guild_id):
                        return
                    if not await self._runtime_should_restore_voice(guild_id):
                        return

                    current_guild = self.bot.get_guild(guild_id) or guild
                    if current_guild is None:
                        return

                    desired_channel_id = int(self._expected_voice_channel_ids.get(guild_id, 0) or 0)
                    if desired_channel_id <= 0:
                        desired_channel_id = await self._get_remembered_voice_channel_id(guild_id)
                    if desired_channel_id <= 0:
                        return

                    current_vc = self._get_voice_client_for_guild(current_guild)
                    if current_vc is not None and not self._is_voice_client_stale(current_guild, current_vc):
                        current_channel = getattr(current_vc, "channel", None)
                        if getattr(current_channel, "id", None) == desired_channel_id:
                            self._runtime_voice_restore_failures[guild_id] = 0
                            self._runtime_voice_restore_next_allowed_at[guild_id] = 0.0
                            return

                    desired_channel = current_guild.get_channel(desired_channel_id) or self.bot.get_channel(desired_channel_id)
                    if not isinstance(desired_channel, (discord.VoiceChannel, discord.StageChannel)):
                        await self._clear_remembered_voice_channel(guild_id)
                        self._expected_voice_channel_ids.pop(guild_id, None)
                        return

                    try:
                        await self._recover_stale_voice_client(current_guild, reason=f"runtime_restore:{reason}:{attempt}")
                        vc = await self._ensure_connected(current_guild, desired_channel)
                        if vc is not None and getattr(vc, "is_connected", lambda: False)() and getattr(getattr(vc, "channel", None), "id", None) == desired_channel_id:
                            self._runtime_voice_restore_failures[guild_id] = 0
                            self._runtime_voice_restore_next_allowed_at[guild_id] = time.monotonic() + 10.0
                            print(f"[tts_voice] call restaurada em runtime | guild={guild_id} channel={desired_channel_id} reason={reason}")
                            return
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        print(f"[tts_voice] falha ao restaurar call em runtime | guild={guild_id} channel={desired_channel_id} reason={reason} error={e}")

                    attempt += 1
                    self._runtime_voice_restore_failures[guild_id] = attempt
                    delay = min(45.0, 4.0 + (attempt * 5.0))
                    self._runtime_voice_restore_next_allowed_at[guild_id] = time.monotonic() + delay
            finally:
                task = self._runtime_voice_restore_tasks.get(guild_id)
                if task is not None and task.done():
                    self._runtime_voice_restore_tasks.pop(guild_id, None)

        self._runtime_voice_restore_tasks[guild_id] = asyncio.create_task(_runner())

    def _cleanup_guild_runtime_state(self, guild_id: int) -> None:
        self._last_announced_author_by_guild.pop(int(guild_id), None)
        self._cancel_runtime_voice_restore(guild_id)
        self._runtime_voice_restore_failures.pop(int(guild_id), None)
        self._runtime_voice_restore_next_allowed_at.pop(int(guild_id), None)
        self._runtime_voice_restore_suppressed_until.pop(int(guild_id), None)
        self._expected_voice_channel_ids.pop(int(guild_id), None)
        self._manual_voice_disconnect_until.pop(int(guild_id), None)

    def _guild_announce_author_enabled(self, guild_defaults: dict | None) -> bool:
        return bool((guild_defaults or {}).get("announce_author", False))

    def _get_ignored_tts_role_id(self, guild_id: int, *, guild_defaults: dict | None = None) -> int:
        if guild_defaults is not None:
            try:
                return max(0, int((guild_defaults or {}).get("ignored_tts_role_id", 0) or 0))
            except Exception:
                return 0

        db = self._get_db()
        if db is not None and hasattr(db, "get_ignored_tts_role_id"):
            try:
                value = db.get_ignored_tts_role_id(guild_id)
                return max(0, int(value or 0))
            except Exception:
                pass
        if db is not None and hasattr(db, "get_guild_tts_defaults"):
            try:
                defaults = db.get_guild_tts_defaults(guild_id)
                return max(0, int((defaults or {}).get("ignored_tts_role_id", 0) or 0))
            except Exception:
                pass
        return 0

    def _get_ignored_tts_role(self, guild: discord.Guild | None, *, guild_defaults: dict | None = None) -> discord.Role | None:
        if guild is None:
            return None
        role_id = self._get_ignored_tts_role_id(guild.id, guild_defaults=guild_defaults)
        if role_id <= 0:
            return None
        return guild.get_role(role_id)

    def _ignored_tts_role_text(self, guild_id: int, *, guild_defaults: dict | None = None) -> str:
        role_id = self._get_ignored_tts_role_id(guild_id, guild_defaults=guild_defaults)
        if role_id <= 0:
            return "`Nenhum`"
        guild = self.bot.get_guild(guild_id)
        role = guild.get_role(role_id) if guild is not None else None
        if role is not None:
            return role.mention
        return f"`{role_id}` (cargo não encontrado)"

    def _member_has_ignored_tts_role(self, member: discord.Member | None, *, guild_defaults: dict | None = None) -> bool:
        if member is None or member.guild is None:
            return False
        ignored_role_id = self._get_ignored_tts_role_id(member.guild.id, guild_defaults=guild_defaults)
        if ignored_role_id <= 0:
            return False
        return any(int(getattr(role, "id", 0) or 0) == ignored_role_id for role in getattr(member, "roles", []))

    def _spoken_name_suffix(self, member: discord.Member | None, *, guild_defaults: dict | None = None) -> str:
        if member is None:
            return ""

        is_muted = False
        voice_state = getattr(member, "voice", None)
        if voice_state is not None:
            try:
                is_muted = bool(getattr(voice_state, "mute", False))
            except Exception:
                is_muted = False

        ignores_tts = self._member_has_ignored_tts_role(member, guild_defaults=guild_defaults)

        if is_muted and ignores_tts:
            return " [ultra-censurado]"
        if is_muted:
            return " [censurado]"
        if ignores_tts:
            return " [bot ignora]"
        return ""

    def _apply_author_prefix_if_needed(self, guild_id: int, author: discord.abc.User | None, text: str, *, enabled: bool) -> str:
        text = str(text or "").strip()
        if not enabled or not text:
            return text
        author_id = int(getattr(author, "id", 0) or 0)
        if not author_id:
            return text
        last_author_id = int(self._last_announced_author_by_guild.get(int(guild_id), 0) or 0)
        self._last_announced_author_by_guild[int(guild_id)] = author_id
        if last_author_id == author_id:
            return text
        speaker = self._tts_user_reference(author, guild_id=guild_id)
        return f"{speaker} disse, {text}" if speaker else text


    def _panel_actor_name(self, interaction: discord.Interaction) -> str:
        member = getattr(interaction, "user", None)
        return self._member_actor_name(member)

    def _member_actor_name(self, member) -> str:
        if member is None:
            return "@usuário"

        name = getattr(member, "name", None) or getattr(member, "display_name", None) or "usuário"
        if not str(name).startswith("@"):
            return f"@{name}"
        return str(name)

    def _encode_public_owner_history(self, owner_id: int, actor_name: str, action_text: str) -> str:
        safe_actor = str(actor_name or "@usuário").replace("|", "/")
        safe_action = str(action_text or "").replace("|", "/")
        return f"__PUBLIC_OWNER_SELF__|{int(owner_id)}|{safe_actor}|{safe_action}"

    def _decode_public_owner_history(self, entry: str) -> tuple[int, str, str] | None:
        raw = str(entry or "")
        prefix = "__PUBLIC_OWNER_SELF__|"
        if not raw.startswith(prefix):
            return None
        try:
            _, owner_id, actor_name, action_text = raw.split("|", 3)
            return int(owner_id), actor_name, action_text
        except (TypeError, ValueError):
            return None

    def _render_history_entry(self, entry: str, *, viewer_user_id: int | None = None, message_id: int | None = None) -> str:
        decoded = self._decode_public_owner_history(entry)
        if not decoded:
            return str(entry or "")

        owner_id, actor_name, action_text = decoded
        state = self._public_panel_states.get(message_id or 0, {}) if message_id else {}
        is_public_user_panel = bool(state and state.get("panel_kind") == "user")
        public_panel_owner_id = int(state.get("owner_id", 0) or 0) if state else 0

        if viewer_user_id == owner_id:
            if is_public_user_panel:
                if public_panel_owner_id == owner_id:
                    return f"Você ({actor_name}) {action_text}"
                return f"{actor_name} {action_text}"
            return f"Você {action_text}"

        return f"{actor_name} {action_text}"

    def _quote_value(self, value: str) -> str:
        return f'"{value}"'

    def _format_history_entries(self, entries: list[str], *, viewer_user_id: int | None = None, message_id: int | None = None) -> str:
        entries = [str(x) for x in (entries or []) if str(x or "").strip()]
        if not entries:
            return ""
        lines = []
        for idx, entry in enumerate(entries):
            rendered = self._render_history_entry(entry, viewer_user_id=viewer_user_id, message_id=message_id)
            safe = rendered.replace("`", "'")
            line = f"`{safe}`"
            if idx == len(entries) - 1:
                line = f"**{line}**"
            lines.append(line)
        return "\n".join(lines)

    def _format_status_history_entries(self, entries: list[str], *, viewer_user_id: int | None = None) -> str:
        entries = [str(x) for x in (entries or []) if str(x or "").strip()]
        if not entries:
            return ""
        lines = []
        recent_entries = entries[-2:]
        for idx, entry in enumerate(recent_entries):
            rendered = self._render_history_entry(entry, viewer_user_id=viewer_user_id, message_id=None)
            safe = rendered.replace("`", "'")
            line = f"• {safe}"
            if idx == len(recent_entries) - 1:
                line = f"**{line}**"
            lines.append(line)
        return "\n".join(lines)

    def _get_public_panel_history(self, message_id: int | None) -> list[str]:
        if not message_id:
            return []
        state = self._public_panel_states.get(message_id, {}) or {}
        return [str(x) for x in (state.get("history", []) or []) if str(x or "").strip()]

    def _merge_history_entries(self, *groups: list[str] | tuple[str, ...]) -> list[str]:
        merged: list[str] = []
        for group in groups:
            for entry in (group or []):
                clean = str(entry or "").strip()
                if not clean:
                    continue
                if merged and merged[-1] == clean:
                    continue
                merged.append(clean)
        return merged[-3:]

    def _append_public_panel_history(self, message_id: int | None, text: str):
        if not message_id:
            return
        state = self._public_panel_states.get(message_id)
        if state is None:
            state = {"history": []}
            self._public_panel_states[message_id] = state
        history = self._merge_history_entries(state.get("history", []) or [], [text] if text else [])
        state["history"] = history

    def _resolve_last_changes(self, *, stored_changes: list[str] | None = None, message_id: int | None = None) -> list[str]:
        stored = [str(x) for x in (stored_changes or []) if str(x or "").strip()]
        if not message_id or message_id not in self._public_panel_states:
            return stored
        public_history = self._get_public_panel_history(message_id)
        return self._merge_history_entries(stored, public_history)

    def _resolve_public_panel_message(self, interaction: discord.Interaction, source_panel_message: discord.Message | None = None) -> tuple[discord.Message | None, int | None]:
        direct_message = getattr(interaction, "message", None)
        direct_id = getattr(direct_message, "id", None)
        if direct_id in self._public_panel_states:
            return direct_message, direct_id

        source_id = getattr(source_panel_message, "id", None)
        if source_id in self._public_panel_states:
            return source_panel_message, source_id

        if source_panel_message is not None:
            return source_panel_message, source_id

        return direct_message, direct_id

    async def _maybe_await(self, value):
        if inspect.isawaitable(value):
            return await value
        return value

    def _get_voice_connect_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._voice_connect_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._voice_connect_locks[guild_id] = lock
        return lock


    def _prefix_panel_key(self, guild_id: int, user_id: int, panel_kind: str) -> tuple[int, int, str]:
        return (guild_id, user_id, panel_kind)

    async def _delete_prefix_panel(self, guild_id: int, user_id: int, panel_kind: str):
        key = self._prefix_panel_key(guild_id, user_id, panel_kind)
        message = self._active_prefix_panels.pop(key, None)
        if not message:
            return
        self._public_panel_states.pop(getattr(message, "id", None), None)
        try:
            await message.delete()
        except Exception:
            pass

    async def _check_prefix_panel_cooldown(self, message: discord.Message, panel_kind: str) -> bool:
        if not message.guild:
            return False

        now = time.monotonic()
        key = self._prefix_panel_key(message.guild.id, message.author.id, panel_kind)
        expires_at = self._prefix_panel_cooldowns.get(key, 0.0)

        if expires_at > now:
            remaining = max(1, int(expires_at - now + 0.999))
            embed = discord.Embed(
                title="Calma aí",
                description=f"Você precisa esperar **{remaining}s** para usar esse comando de painel novamente",
                color=discord.Color.red(),
            )
            await message.channel.send(embed=embed)
            return True

        self._prefix_panel_cooldowns[key] = now + 5.0

        stale = [k for k, ts in self._prefix_panel_cooldowns.items() if ts < now - 60.0]
        for stale_key in stale:
            self._prefix_panel_cooldowns.pop(stale_key, None)

        return False

    async def _send_prefix_panel(
        self,
        message: discord.Message,
        *,
        panel_kind: str,
        embed: discord.Embed,
        view: discord.ui.View,
    ):
        if not message.guild:
            return

        if await self._check_prefix_panel_cooldown(message, panel_kind):
            return

        await self._delete_prefix_panel(message.guild.id, message.author.id, panel_kind)

        sent = await message.channel.send(embed=embed, view=view)
        view.message = sent
        db = self._get_db()
        initial_history: list[str] = []
        if db and hasattr(db, "get_panel_history"):
            panel_history = await self._maybe_await(db.get_panel_history(message.guild.id, message.author.id))
            if panel_kind == "server":
                initial_history = list((panel_history or {}).get("server_last_changes", []) or [])
            elif panel_kind == "toggle":
                initial_history = list((panel_history or {}).get("toggle_last_changes", []) or [])
            else:
                initial_history = list((panel_history or {}).get("user_last_changes", []) or [])
        self._public_panel_states[sent.id] = {"panel_kind": panel_kind, "history": self._merge_history_entries(initial_history), "owner_id": message.author.id}
        self._active_prefix_panels[self._prefix_panel_key(message.guild.id, message.author.id, panel_kind)] = sent

    def _mark_tts_message_seen(self, message_id: int) -> None:
        now = time.monotonic()
        self._recent_tts_message_ids[message_id] = now
        cutoff = now - 30.0
        stale = [mid for mid, ts in self._recent_tts_message_ids.items() if ts < cutoff]
        for mid in stale:
            self._recent_tts_message_ids.pop(mid, None)

    def _was_tts_message_seen(self, message_id: int) -> bool:
        ts = self._recent_tts_message_ids.get(message_id)
        if ts is None:
            return False
        if time.monotonic() - ts > 30.0:
            self._recent_tts_message_ids.pop(message_id, None)
            return False
        return True

    async def _load_edge_voices(self):
        try:
            import edge_tts
            voices = await edge_tts.list_voices()
            names = sorted({v["ShortName"] for v in voices if "ShortName" in v})
            self.edge_voice_cache = names
            self.edge_voice_names = set(names)
            print(f"[tts_voice] {len(names)} vozes edge carregadas.")
        except Exception as e:
            print(f"[tts_voice] Falha ao carregar vozes edge: {e}")
            self.edge_voice_cache = []
            self.edge_voice_names = set()

    async def _load_gcloud_voices(self, *, force: bool = False) -> list[dict[str, object]]:
        if google_texttospeech is None:
            return []
        now = time.monotonic()
        if not force and self._gcloud_voices_cache and (now - self._gcloud_voices_cache_loaded_at) < 1800:
            return list(self._gcloud_voices_cache)
        async with self._gcloud_voices_cache_lock:
            now = time.monotonic()
            if not force and self._gcloud_voices_cache and (now - self._gcloud_voices_cache_loaded_at) < 1800:
                return list(self._gcloud_voices_cache)

            def _worker() -> list[dict[str, object]]:
                self._ensure_google_credentials_file()
                client = google_texttospeech.TextToSpeechClient()
                try:
                    response = client.list_voices(request={})
                    voices: list[dict[str, object]] = []
                    for voice in list(getattr(response, 'voices', []) or []):
                        name = str(getattr(voice, 'name', '') or '')
                        language_codes = [str(code) for code in list(getattr(voice, 'language_codes', []) or []) if str(code or '').strip()]
                        if not name or not language_codes:
                            continue
                        voices.append({
                            'name': name,
                            'language_codes': language_codes,
                            'ssml_gender': int(getattr(voice, 'ssml_gender', 0) or 0),
                        })
                    return voices
                finally:
                    with contextlib.suppress(Exception):
                        client.transport.close()

            try:
                voices = await asyncio.to_thread(_worker)
            except Exception as e:
                print(f"[tts_voice] Falha ao carregar vozes do Google Cloud: {e!r}")
                return list(self._gcloud_voices_cache)

            self._gcloud_voices_cache = list(voices)
            self._gcloud_voices_cache_loaded_at = time.monotonic()
            return list(self._gcloud_voices_cache)

    def _gcloud_language_priority(self, code: str) -> tuple[int, str]:
        return gcloud_language_priority(code)

    def _build_gcloud_language_options_from_catalog(self, catalog: list[dict[str, object]], current_value: str | None = None) -> list[discord.SelectOption]:
        return build_gcloud_language_options_from_catalog(
            catalog,
            current_value=current_value,
            default_language=str(getattr(config, 'GOOGLE_CLOUD_TTS_LANGUAGE_CODE', 'pt-BR') or 'pt-BR'),
        )

    def _gcloud_voice_priority(self, voice_name: str) -> tuple[int, str]:
        return gcloud_voice_priority(voice_name)

    def _split_gcloud_voice_name(self, voice_name: str) -> tuple[str, str]:
        return split_gcloud_voice_name(voice_name)

    def _describe_gcloud_voice(self, voice_name: str) -> str:
        return describe_gcloud_voice(voice_name)

    def _build_gcloud_voice_options_from_catalog(self, catalog: list[dict[str, object]], language_code: str, current_value: str | None = None) -> list[discord.SelectOption]:
        return build_gcloud_voice_options_from_catalog(
            catalog,
            language_code,
            current_value=current_value,
            default_language=str(getattr(config, 'GOOGLE_CLOUD_TTS_LANGUAGE_CODE', 'pt-BR') or 'pt-BR'),
            default_voice=str(getattr(config, 'GOOGLE_CLOUD_TTS_VOICE_NAME', 'pt-BR-Standard-A') or 'pt-BR-Standard-A'),
        )

    def _gcloud_voice_matches_language(self, voice_name: str, language_code: str) -> bool:
        return gcloud_voice_matches_language(voice_name, language_code)

    def _pick_first_gcloud_voice_for_language(self, catalog: list[dict[str, object]], language_code: str) -> str:
        return pick_first_gcloud_voice_for_language(
            catalog,
            language_code,
            default_language=str(getattr(config, 'GOOGLE_CLOUD_TTS_LANGUAGE_CODE', 'pt-BR') or 'pt-BR'),
            default_voice=str(getattr(config, 'GOOGLE_CLOUD_TTS_VOICE_NAME', 'pt-BR-Standard-A') or 'pt-BR-Standard-A'),
        )

    async def _open_gcloud_language_picker(self, interaction: discord.Interaction, *, owner_id: int, guild_id: int, current_value: str, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        catalog = await self._load_gcloud_voices()
        options = self._build_gcloud_language_options_from_catalog(catalog, current_value=current_value)
        if not options:
            await self._respond(interaction, embed=self._make_embed('Google Cloud indisponível', 'Não consegui listar os idiomas do Google Cloud agora. Confira as credenciais e tente novamente.', ok=False), ephemeral=True)
            return
        description = 'Selecione um idioma disponível do Google Cloud. A lista é carregada das vozes que a sua conta consegue usar.'
        await _SimpleSelectView(self, owner_id, guild_id, 'Escolha o idioma do Google Cloud', description, GCloudLanguageSelect(self, server=server, options=options), source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name).send(interaction)

    async def _open_gcloud_voice_picker(self, interaction: discord.Interaction, *, owner_id: int, guild_id: int, language_code: str, current_value: str, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        effective_language = str(language_code or '').strip() or str(getattr(config, 'GOOGLE_CLOUD_TTS_LANGUAGE_CODE', 'pt-BR') or 'pt-BR')
        catalog = await self._load_gcloud_voices()
        options = self._build_gcloud_voice_options_from_catalog(catalog, effective_language, current_value=current_value)
        if not options:
            await self._respond(interaction, embed=self._make_embed('Nenhuma voz encontrada', f'Não encontrei vozes do Google Cloud para o idioma `{effective_language}`. Ajuste o idioma e tente de novo.', ok=False), ephemeral=True)
            return
        description = f'Selecione uma voz disponível para `{effective_language}`. O título mostra a família da voz e a variante; abaixo aparece o nome técnico completo.'
        await _SimpleSelectView(self, owner_id, guild_id, 'Escolha a voz do Google Cloud', description, GCloudVoiceSelect(self, server=server, options=options), source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name).send(interaction)

    def _make_embed(self, title: str, description: str, *, ok: bool = True) -> discord.Embed:
        return make_embed(title, description, ok=ok)

    async def _respond(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = True,
    ):
        if interaction.response.is_done():
            response_type = getattr(interaction.response, "type", None)
            if response_type == discord.InteractionResponseType.deferred_channel_message:
                await interaction.edit_original_response(
                    content=content,
                    embed=embed,
                    view=view,
                )
                try:
                    return await interaction.original_response()
                except Exception:
                    return None

            return await interaction.followup.send(
                content=content,
                embed=embed,
                view=view,
                ephemeral=ephemeral,
                wait=True,
            )
        await interaction.response.send_message(
            content=content,
            embed=embed,
            view=view,
            ephemeral=ephemeral,
        )
        try:
            return await interaction.original_response()
        except Exception:
            return None

    async def _defer_ephemeral(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    async def _require_guild(self, interaction: discord.Interaction) -> bool:
        if interaction.guild:
            return True
        await self._respond(interaction, embed=self._make_embed("Comando indisponível", "Esse comando só pode ser usado dentro de um servidor.", ok=False), ephemeral=True)
        return False

    async def _require_manage_guild(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.manage_guild:
            return True
        await self._respond(interaction, embed=self._make_embed("Sem permissão", "Você precisa da permissão `Gerenciar Servidor` para alterar as configurações do servidor.", ok=False), ephemeral=True)
        return False

    async def _require_kick_members(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.kick_members:
            return True
        await self._respond(interaction, embed=self._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para usar esse comando.", ok=False), ephemeral=True)
        return False

    async def _require_staff_or_kick_members(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms and (perms.kick_members or perms.manage_guild or perms.administrator):
            return True
        await self._respond(interaction, embed=self._make_embed("Sem permissão", "Você precisa ser staff ou ter a permissão `Expulsar Membros` para usar esse comando.", ok=False), ephemeral=True)
        return False

    def _format_metric_ms(self, value) -> str:
        try:
            return f"{float(value):.2f} ms"
        except Exception:
            return "n/a"

    def _format_bytes_human(self, value: int | float) -> str:
        try:
            size = float(value)
        except Exception:
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB"]
        idx = 0
        while size >= 1024.0 and idx < len(units) - 1:
            size /= 1024.0
            idx += 1
        if idx == 0:
            return f"{int(size)} {units[idx]}"
        return f"{size:.2f} {units[idx]}"

    def _build_tts_perf_embeds(self, guild: discord.Guild | None) -> list[discord.Embed]:
        snapshot = {}
        get_snapshot = getattr(self.bot, "get_health_snapshot", None)
        if callable(get_snapshot):
            try:
                snapshot = get_snapshot() or {}
            except Exception:
                snapshot = {}

        tts_metrics = dict(snapshot.get("tts_metrics") or {})
        engine_metrics = dict(tts_metrics.get("engines") or {})

        total_tmp_bytes = 0
        runtime_count = 0
        cache_count = 0
        tmp_files = []
        try:
            tmp_files = self._list_tmp_audio_files()
        except Exception:
            tmp_files = []
        for _, _, size, path in tmp_files:
            total_tmp_bytes += int(size or 0)
            parent_name = os.path.basename(os.path.dirname(path)).lower()
            if parent_name == "runtime":
                runtime_count += 1
            elif parent_name == "cache":
                cache_count += 1

        uptime_seconds = snapshot.get("uptime_seconds")
        try:
            uptime_seconds = int(float(uptime_seconds or 0))
        except Exception:
            uptime_seconds = 0
        days, rem = divmod(uptime_seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        uptime_parts = []
        if days:
            uptime_parts.append(f"{days}d")
        if hours:
            uptime_parts.append(f"{hours}h")
        if minutes:
            uptime_parts.append(f"{minutes}m")
        if seconds or not uptime_parts:
            uptime_parts.append(f"{seconds}s")
        uptime_text = " ".join(uptime_parts)

        cache_hits = int(tts_metrics.get("cache_hits", 0) or 0)
        cache_misses = int(tts_metrics.get("cache_misses", 0) or 0)
        total_cache_lookups = cache_hits + cache_misses
        cache_hit_rate = (cache_hits / total_cache_lookups * 100.0) if total_cache_lookups else 0.0

        embed = discord.Embed(
            title="🛠️ Status técnico do TTS",
            description="Métricas internas do TTS e saúde atual do bot para diagnóstico rápido.",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Saúde geral",
            value=(
                f"**Status:** `{snapshot.get('status', 'unknown')}`\n"
                f"**Healthy:** `{snapshot.get('healthy', False)}`\n"
                f"**Discord pronto:** `{snapshot.get('discord_ready', False)}`\n"
                f"**Mongo:** `{snapshot.get('mongo_ok', False)}`\n"
                f"**Uptime:** `{uptime_text}`\n"
                f"**Latência:** `{snapshot.get('latency_ms', 'n/a')} ms`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Fila e despacho",
            value=(
                f"**Na fila agora:** `{tts_metrics.get('queued_items_current', 0)}`\n"
                f"**Guild states:** `{tts_metrics.get('guild_states_current', 0)}`\n"
                f"**Enfileiradas:** `{tts_metrics.get('queue_enqueued', 0)}`\n"
                f"**Deduplicadas:** `{tts_metrics.get('queue_deduplicated', 0)}`\n"
                f"**Descartadas:** `{tts_metrics.get('queue_dropped', 0)}`\n"
                f"**Espera média:** `{self._format_metric_ms(tts_metrics.get('avg_queue_wait_ms'))}`\n"
                f"**Dispatch médio:** `{self._format_metric_ms(tts_metrics.get('avg_dispatch_ms'))}`\n"
                f"**Source setup:** `{self._format_metric_ms(tts_metrics.get('avg_source_setup_ms'))}`\n"
                f"**Play call:** `{self._format_metric_ms(tts_metrics.get('avg_play_call_ms'))}`\n"
                f"**Total até tocar:** `{self._format_metric_ms(tts_metrics.get('avg_total_to_playback_ms'))}`\n"
                f"**Playback médio:** `{self._format_metric_ms(tts_metrics.get('avg_playback_ms'))}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Cache e armazenamento",
            value=(
                f"**Cache hits:** `{cache_hits}`\n"
                f"**Cache misses:** `{cache_misses}`\n"
                f"**Hit rate:** `{cache_hit_rate:.1f}%`\n"
                f"**Cache stores:** `{tts_metrics.get('cache_stores', 0)}`\n"
                f"**tmp_audio:** `{self._format_bytes_human(total_tmp_bytes)}`\n"
                f"**Arquivos runtime:** `{runtime_count}`\n"
                f"**Arquivos cache:** `{cache_count}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Warmup",
            value=(
                f"**Warmups no boot:** `{tts_metrics.get('boot_warmups', 0)}`\n"
                f"**Último warmup:** `{self._format_metric_ms(tts_metrics.get('last_warmup_duration_ms'))}`"
            ),
            inline=False,
        )
        if guild is not None:
            try:
                state = self.guild_states.get(guild.id)
                guild_queue = state.queue.qsize() if state else 0
            except Exception:
                guild_queue = 0
            embed.add_field(
                name="Servidor atual",
                value=(
                    f"**Servidor:** `{guild.name}`\n"
                    f"**Fila deste servidor:** `{guild_queue}`"
                ),
                inline=False,
            )
        if self.bot.user and self.bot.user.display_avatar:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.set_footer(text="Visão técnica para staff/admin")

        engine_embed = discord.Embed(
            title="⚙️ Engines do TTS",
            description="Contadores e médias por engine desde o boot atual do bot.",
            color=discord.Color.dark_teal(),
            timestamp=discord.utils.utcnow(),
        )
        if engine_metrics:
            for engine_name, data in sorted(engine_metrics.items()):
                engine_embed.add_field(
                    name=f"{engine_name}",
                    value=(
                        f"**Synths:** `{int(data.get('synth_count', 0) or 0)}`\n"
                        f"**Falhas:** `{int(data.get('synth_failures', 0) or 0)}`\n"
                        f"**Falhas consecutivas:** `{int(data.get('consecutive_failures', 0) or 0)}`\n"
                        f"**Hits cache:** `{int(data.get('cache_hits', 0) or 0)}`\n"
                        f"**Misses cache:** `{int(data.get('cache_misses', 0) or 0)}`\n"
                        f"**Média synth:** `{self._format_metric_ms(data.get('avg_synth_ms'))}`\n"
                        f"**Última synth:** `{self._format_metric_ms(data.get('last_synth_ms'))}`\n"
                        f"**Slow alerts:** `{int(data.get('slow_alerts', 0) or 0)}`\n"
                        f"**Último erro:** `{str(data.get('last_error') or 'nenhum')[:120]}`"
                    ),
                    inline=False,
                )
        else:
            engine_embed.description = "Ainda não há métricas de engine suficientes para mostrar aqui."
        if self.bot.user and self.bot.user.display_avatar:
            engine_embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        engine_embed.set_footer(text="Use isto para diagnosticar lentidão, cache e falhas por engine")

        return [embed, engine_embed]

    async def _require_toggle_allowed_guild(self, interaction: discord.Interaction) -> bool:
        guild_ids = getattr(config, "GUILD_IDS", []) or []
        if not guild_ids:
            return True
        guild = getattr(interaction, "guild", None)
        if guild and guild.id in guild_ids:
            return True
        await self._respond(interaction, embed=self._make_embed("Indisponível aqui", "Esse comando só está habilitado nos servidores definidos na env.", ok=False), ephemeral=True)
        return False

    def _normalize_rate_value(self, raw: str) -> str | None:
        return normalize_rate_value(raw)

    def _normalize_pitch_value(self, raw: str) -> str | None:
        return normalize_pitch_value(raw)

    def _coerce_setting_bool(self, value, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "y", "on", "ativado", "ativo", "sim"}:
            return True
        if text in {"false", "0", "no", "n", "off", "desativado", "inativo", "nao", "não"}:
            return False
        return default

    async def _get_guild_toggle_value(self, guild_id: int, *, public_key: str, raw_key: str, default: bool) -> bool:
        db = self._get_db()
        if db is None:
            return default
        try:
            raw_doc = getattr(db, "guild_cache", {}).get(guild_id, {}) or {}
            if raw_key in raw_doc:
                return self._coerce_setting_bool(raw_doc.get(raw_key), default)
            data = db.get_guild_tts_defaults(guild_id)
            data = await self._maybe_await(data)
            return self._coerce_setting_bool((data or {}).get(public_key), default)
        except Exception as e:
            print(f"[tts_voice] Erro ao ler {public_key} da guild {guild_id}: {e}")
            return default

    def _get_voice_client_for_guild(self, guild: discord.Guild | None) -> Optional[discord.VoiceClient]:
        if guild is None:
            return None

        for vc in self.bot.voice_clients:
            try:
                if vc.guild and vc.guild.id == guild.id:
                    return vc
            except Exception:
                continue

        return guild.voice_client

    async def _should_block_for_voice_bot(self, guild: discord.Guild, voice_channel) -> bool:
        # Legacy toggle removed: nunca mais bloquear o TTS por presença de outro bot na call.
        return False

    async def _disconnect_and_clear(self, guild: discord.Guild):
        self._mark_manual_voice_disconnect(guild.id, seconds=60.0)
        self._suppress_runtime_voice_restore(guild.id, seconds=60.0)
        self._cancel_runtime_voice_restore(guild.id)
        self._remember_expected_voice_channel(guild.id, None)
        state = self._get_state(guild.id)
        try:
            while not state.queue.empty():
                state.queue.get_nowait()
                state.queue.task_done()
        except Exception:
            pass
        self._last_announced_author_by_guild.pop(int(guild.id), None)
        vc = self._get_voice_client_for_guild(guild)
        disconnected = False
        if vc and vc.is_connected():
            try:
                if vc.is_playing():
                    vc.stop()
            except Exception:
                pass
            try:
                await vc.disconnect(force=False)
                disconnected = True
            except Exception as e:
                print(f"[tts_voice] erro ao desconectar guild {guild.id}: {e}")
        if not disconnected and self._get_bot_voice_state_channel(guild) is not None:
            await self._clear_ghost_voice_state(guild, reason="disconnect_and_clear")
        await self._clear_remembered_voice_channel(guild.id)

    async def _disconnect_if_blocked(self, guild: discord.Guild):
        await self._disconnect_and_clear(guild)

    def _voice_channel_has_only_bots_or_is_empty(self, voice_channel) -> bool:
        if voice_channel is None:
            return True
        members = list(getattr(voice_channel, "members", []))
        return not any(not m.bot for m in members)

    async def _disconnect_if_alone_or_only_bots(self, guild: discord.Guild):
        auto_leave_enabled = await self._get_guild_toggle_value(
            guild.id,
            public_key="auto_leave",
            raw_key="auto_leave_enabled",
            default=True,
        )
        if not auto_leave_enabled:
            return

        vc = self._get_voice_client_for_guild(guild)
        if vc is None or not vc.is_connected() or vc.channel is None:
            return
        if self._voice_channel_has_only_bots_or_is_empty(vc.channel):
            print(f"[tts_voice] saindo da call | sozinho ou só com bots | guild={guild.id} channel={vc.channel.id}")
            await self._disconnect_and_clear(guild)

    async def _ensure_connected(self, guild: discord.Guild, voice_channel) -> Optional[discord.VoiceClient]:
        if voice_channel is None:
            print(f"[tts_voice] _ensure_connected recebeu canal None | guild={guild.id}")
            return None

        async def _desired_self_deaf() -> bool:
            try:
                return bool(await self._voice_should_self_deaf(guild.id))
            except Exception:
                return True

        async def _ensure_expected_voice_state() -> None:
            should_self_deaf = await _desired_self_deaf()
            last_error = None
            for _ in range(3):
                try:
                    me = getattr(guild, "me", None)
                    me_voice = getattr(me, "voice", None)
                    target_channel = getattr(me_voice, "channel", None) or voice_channel
                    current_self_deaf = bool(getattr(me_voice, "self_deaf", False)) if me_voice else None
                    if me_voice and current_self_deaf == should_self_deaf:
                        return
                    await guild.change_voice_state(channel=target_channel, self_deaf=should_self_deaf)
                    await asyncio.sleep(0.35)
                    me = getattr(guild, "me", None)
                    me_voice = getattr(me, "voice", None)
                    current_self_deaf = bool(getattr(me_voice, "self_deaf", False)) if me_voice else None
                    if me_voice and current_self_deaf == should_self_deaf:
                        return
                except Exception as e:
                    last_error = e
                    await asyncio.sleep(0.35)
            if last_error is not None:
                print(
                    f"[tts_voice] falha ao aplicar estado de voz | guild={guild.id} channel={getattr(voice_channel, 'id', None)} self_deaf={should_self_deaf} error={last_error}"
                )

        async def _build_connect_kwargs() -> dict:
            should_self_deaf = await _desired_self_deaf()
            kwargs = {"self_deaf": should_self_deaf}
            try:
                use_receive_client = bool(await self._should_use_receive_voice_client(guild.id))
            except Exception:
                use_receive_client = False
            if use_receive_client:
                try:
                    from discord.ext import voice_recv

                    kwargs["cls"] = voice_recv.VoiceRecvClient
                except Exception:
                    pass
            return kwargs

        lock = self._get_voice_connect_lock(guild.id)
        async with lock:
            vc = self._get_voice_client_for_guild(guild)
            if self._is_voice_client_stale(guild, vc):
                await self._recover_stale_voice_client(guild, reason="ensure_connected")
                vc = self._get_voice_client_for_guild(guild)
            try:
                should_use_receive_client = bool(await self._should_use_receive_voice_client(guild.id))
            except Exception:
                should_use_receive_client = False
            is_receive_client = bool(vc and hasattr(vc, "listen") and hasattr(vc, "is_listening"))

            if vc and vc.is_connected() and vc.channel and vc.channel.id == voice_channel.id:
                if not should_use_receive_client or is_receive_client:
                    await _ensure_expected_voice_state()
                    await self._set_remembered_voice_channel(guild.id, getattr(voice_channel, "id", None))
                    self._remember_expected_voice_channel(guild.id, getattr(voice_channel, "id", None))
                    self._runtime_voice_restore_failures[guild.id] = 0
                    self._runtime_voice_restore_next_allowed_at[guild.id] = 0.0
                    self._cancel_runtime_voice_restore(guild.id)
                    self._clear_manual_voice_disconnect(guild.id)
                    await self._notify_voice_moderation_ready(guild, vc)
                    return vc
                try:
                    if vc.is_playing() or vc.is_paused():
                        await _ensure_expected_voice_state()
                        await self._set_remembered_voice_channel(guild.id, getattr(voice_channel, "id", None))
                        await self._notify_voice_moderation_ready(guild, vc)
                        return vc
                except Exception:
                    pass
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
                vc = None

            async def _fresh_connect() -> Optional[discord.VoiceClient]:
                connect_kwargs = await _build_connect_kwargs()
                new_vc = await voice_channel.connect(**connect_kwargs)
                await _ensure_expected_voice_state()
                await self._set_remembered_voice_channel(guild.id, getattr(voice_channel, "id", None))
                self._remember_expected_voice_channel(guild.id, getattr(voice_channel, "id", None))
                self._runtime_voice_restore_failures[guild.id] = 0
                self._runtime_voice_restore_next_allowed_at[guild.id] = 0.0
                self._cancel_runtime_voice_restore(guild.id)
                self._clear_manual_voice_disconnect(guild.id)
                await self._notify_voice_moderation_ready(guild, new_vc)
                print(f"[tts_voice] Conectado no canal {voice_channel.id} na guild {guild.id}")
                return new_vc

            try:
                if vc and vc.is_connected():
                    if should_use_receive_client and not is_receive_client:
                        try:
                            await vc.disconnect(force=True)
                        except Exception:
                            pass
                        return await _fresh_connect()
                    try:
                        await vc.move_to(voice_channel)
                        await _ensure_expected_voice_state()
                        await self._set_remembered_voice_channel(guild.id, getattr(voice_channel, "id", None))
                        self._remember_expected_voice_channel(guild.id, getattr(voice_channel, "id", None))
                        self._runtime_voice_restore_failures[guild.id] = 0
                        self._runtime_voice_restore_next_allowed_at[guild.id] = 0.0
                        self._cancel_runtime_voice_restore(guild.id)
                        self._clear_manual_voice_disconnect(guild.id)
                        await self._notify_voice_moderation_ready(guild, vc)
                        print(f"[tts_voice] Movido para canal {voice_channel.id} na guild {guild.id}")
                        return vc
                    except Exception as move_err:
                        msg = str(move_err).lower()
                        if "closing transport" in msg or "not connected to voice" in msg:
                            try:
                                await vc.disconnect(force=True)
                            except Exception:
                                pass
                            return await _fresh_connect()
                        raise

                return await _fresh_connect()

            except Exception as e:
                msg = str(e).lower()
                current_vc = self._get_voice_client_for_guild(guild)

                if "already connected" in msg and current_vc and current_vc.is_connected():
                    if current_vc.channel and current_vc.channel.id == voice_channel.id:
                        await _ensure_expected_voice_state()
                        await self._set_remembered_voice_channel(guild.id, getattr(voice_channel, "id", None))
                        self._remember_expected_voice_channel(guild.id, getattr(voice_channel, "id", None))
                        self._runtime_voice_restore_failures[guild.id] = 0
                        self._runtime_voice_restore_next_allowed_at[guild.id] = 0.0
                        self._cancel_runtime_voice_restore(guild.id)
                        self._clear_manual_voice_disconnect(guild.id)
                        await self._notify_voice_moderation_ready(guild, current_vc)
                        return current_vc
                    try:
                        await current_vc.move_to(voice_channel)
                        await _ensure_expected_voice_state()
                        await self._set_remembered_voice_channel(guild.id, getattr(voice_channel, "id", None))
                        self._remember_expected_voice_channel(guild.id, getattr(voice_channel, "id", None))
                        self._runtime_voice_restore_failures[guild.id] = 0
                        self._runtime_voice_restore_next_allowed_at[guild.id] = 0.0
                        self._cancel_runtime_voice_restore(guild.id)
                        self._clear_manual_voice_disconnect(guild.id)
                        await self._notify_voice_moderation_ready(guild, current_vc)
                        print(f"[tts_voice] Movido para canal {voice_channel.id} na guild {guild.id}")
                        return current_vc
                    except Exception:
                        pass

                if "closing transport" in msg or "not connected to voice" in msg:
                    try:
                        if current_vc:
                            await current_vc.disconnect(force=True)
                    except Exception:
                        pass
                    try:
                        return await _fresh_connect()
                    except Exception as retry_err:
                        print(f"[tts_voice] Erro ao reconectar na guild {guild.id}: {retry_err}")
                        return None

                print(f"[tts_voice] Erro ao conectar na guild {guild.id}: {e}")
                return None

    def _chunk_lines(self, lines: list[str], max_chars: int = 3500) -> list[str]:
        chunks, current, size = [], [], 0
        for line in lines:
            extra = len(line) + 1
            if current and size + extra > max_chars:
                chunks.append("\n".join(current))
                current, size = [line], extra
            else:
                current.append(line)
                size += extra
        if current:
            chunks.append("\n".join(current))
        return chunks

    async def _send_list_embeds(self, interaction: discord.Interaction, *, title: str, lines: list[str], footer: str):
        chunks = self._chunk_lines(lines)
        if not chunks:
            await self._respond(interaction, embed=self._make_embed(title, "Nenhum item encontrado.", ok=False), ephemeral=True)
            return
        for index, chunk in enumerate(chunks, start=1):
            embed = discord.Embed(title=title if len(chunks) == 1 else f"{title} ({index}/{len(chunks)})", description=f"```{chunk}```", color=discord.Color.blurple())
            embed.set_footer(text=footer)
            await self._respond(interaction, embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        flow_started = time.perf_counter()
        gate = await analyze_message_for_tts(self, message)
        message_id = getattr(message, "id", None)
        guild_id = getattr(getattr(message, "guild", None), "id", None)
        author_id = getattr(getattr(message, "author", None), "id", None)

        if gate.should_dispatch_prefix_command:
            print(f"[tts_flow] prefix command detectado | guild={guild_id} user={author_id} message={message_id} reason={gate.reason}")
            if self._was_tts_message_seen(message.id):
                print(f"[tts_flow] ignorado | mensagem já vista antes do prefix dispatch | guild={guild_id} user={author_id} message={message_id}")
                return
            self._mark_tts_message_seen(message.id)
            if await dispatch_prefix_control_command(self, message, gate.prefix_command):
                print(f"[tts_flow] prefix command executado | guild={guild_id} user={author_id} message={message_id}")
                return
            print(f"[tts_flow] prefix command não executado | guild={guild_id} user={author_id} message={message_id}")

        if not gate.should_process_tts:
            if gate.reason not in {"author_bot", "no_guild", "empty_content"}:
                print(f"[tts_flow] ignorado no gate | guild={guild_id} user={author_id} message={message_id} reason={gate.reason}")
            return

        guild_defaults = gate.guild_defaults
        forced_engine = str(gate.forced_engine or "")
        active_prefix = str(gate.active_prefix or "")

        if isinstance(message.author, discord.Member) and self._member_has_ignored_tts_role(message.author, guild_defaults=guild_defaults):
            print(f"[tts_flow] ignorado | autor possui cargo ignorado | guild={guild_id} user={author_id} message={message_id}")
            return

        if self._was_tts_message_seen(message.id):
            print(f"[tts_flow] ignorado | mensagem já vista antes do dispatch | guild={guild_id} user={author_id} message={message_id}")
            return
        self._mark_tts_message_seen(message.id)

        author_voice = getattr(message.author, "voice", None)
        voice_channel = getattr(author_voice, "channel", None)
        if voice_channel is None:
            print(f"[tts_flow] ignorado | autor não está em call | guild={guild_id} user={author_id} message={message_id}")
            return

        print(
            f"[tts_flow] dispatch iniciado | guild={guild_id} user={author_id} message={message_id} "
            f"engine={forced_engine} prefix={active_prefix!r} channel_type={type(message.channel).__name__}"
        )
        dispatch_result = await dispatch_message_tts(
            self,
            message,
            guild_defaults=guild_defaults,
            active_prefix=active_prefix,
            forced_engine=forced_engine,
        )
        payload = dispatch_result.payload
        if payload is None:
            print(
                f"[tts_flow] dispatch abortado | guild={guild_id} user={author_id} message={message_id} "
                f"payload_ms={dispatch_result.payload_ms:.1f}"
            )
            return

        queue_item = payload.queue_item
        resolved = payload.resolved
        print(
            f"[tts_voice] trigger TTS | guild={guild_id} channel_type={type(message.channel).__name__} "
            f"user={author_id} raw={message.content!r}"
        )
        print(
            f"[tts_flow] payload pronto | guild={guild_id} user={author_id} message={message_id} "
            f"voice_channel={queue_item.channel_id} engine={queue_item.engine} payload_ms={dispatch_result.payload_ms:.1f} "
            f"text_len={len(queue_item.text)}"
        )
        if dispatch_result.deduplicated:
            print(f"[tts_voice] deduplicada | guild={guild_id} user={author_id} canal_voz={queue_item.channel_id} engine={resolved['engine']}")
        else:
            if dispatch_result.dropped_count:
                print(f"[tts_voice] fila cheia, itens descartados={dispatch_result.dropped_count} | guild={guild_id}")
            if dispatch_result.enqueued:
                print(
                    f"[tts_voice] enfileirada | guild={guild_id} user={author_id} canal_voz={queue_item.channel_id} "
                    f"engine={resolved['engine']} forced_gtts={payload.forced_gtts}"
                )
        print(
            f"[tts_flow] dispatch finalizado | guild={guild_id} user={author_id} message={message_id} "
            f"dispatch_ms={dispatch_result.dispatch_ms:.1f} total_ms={(time.perf_counter() - flow_started) * 1000.0:.1f}"
        )
        self._ensure_worker(message.guild.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        me = getattr(guild, "me", None)
        vc = self._get_voice_client_for_guild(guild)

        if me and member.id == me.id:
            if after.channel is not None:
                self._remember_expected_voice_channel(guild.id, getattr(after.channel, "id", None))
                self._runtime_voice_restore_failures[guild.id] = 0
                self._runtime_voice_restore_next_allowed_at[guild.id] = 0.0
                self._cancel_runtime_voice_restore(guild.id)
                self._clear_manual_voice_disconnect(guild.id)
                await self._set_remembered_voice_channel(guild.id, getattr(after.channel, "id", None))
                desired_self_deaf = True
                try:
                    desired_self_deaf = bool(await self._voice_should_self_deaf(guild.id))
                except Exception:
                    desired_self_deaf = True
                current_self_deaf = bool(getattr(after, "self_deaf", False))
                if current_self_deaf != desired_self_deaf:
                    try:
                        await guild.change_voice_state(channel=after.channel, self_deaf=desired_self_deaf)
                        print(
                            f"[tts_voice] estado de voz corrigido após mudança de canal | guild={guild.id} channel={after.channel.id} self_deaf={desired_self_deaf}"
                        )
                    except Exception as e:
                        print(
                            f"[tts_voice] falha ao corrigir estado de voz no voice_state_update | guild={guild.id} channel={getattr(after.channel, 'id', None)} self_deaf={desired_self_deaf} error={e}"
                        )
                await self._notify_voice_moderation_ready(guild, vc)
            elif before.channel is not None:
                print(f"[tts_voice] bot saiu da call | guild={guild.id} channel={getattr(before.channel, 'id', None)}")
                manual_or_intentional = (
                    self._is_manual_voice_disconnect_recent(guild.id)
                    or self._runtime_voice_restore_is_suppressed(guild.id)
                    or int(self._expected_voice_channel_ids.get(guild.id, 0) or 0) <= 0
                )
                if manual_or_intentional:
                    self._cancel_runtime_voice_restore(guild.id)
                    self._runtime_voice_restore_failures[guild.id] = 0
                    self._runtime_voice_restore_next_allowed_at[guild.id] = 0.0
                    self._remember_expected_voice_channel(guild.id, None)
                    await self._clear_remembered_voice_channel(guild.id)
                    print(f"[tts_voice] saída intencional/guardada; restore ignorado | guild={guild.id}")
                else:
                    self._remember_expected_voice_channel(guild.id, getattr(before.channel, "id", None))
                    await self._set_remembered_voice_channel(guild.id, getattr(before.channel, "id", None))
                    if await self._runtime_should_restore_voice(guild.id):
                        await self._schedule_runtime_voice_restore(
                            guild,
                            channel_id=getattr(before.channel, "id", None),
                            reason="voice_state_disconnect",
                            initial_delay=4.0,
                        )

        if vc is None or not vc.is_connected() or vc.channel is None:
            return

        await self._disconnect_if_alone_or_only_bots(guild)


    async def voice_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current = (current or "").strip().lower()
        voices = self.edge_voice_cache or sorted(self.edge_voice_names)
        voices = [voice for voice in voices if voice.lower().startswith("pt-")]

        results: list[app_commands.Choice[str]] = []
        for voice in voices:
            if current and current not in voice.lower():
                continue
            results.append(app_commands.Choice(name=voice[:100], value=voice))
            if len(results) >= 25:
                break
        return results

    async def language_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current = (current or "").strip().lower()

        results: list[app_commands.Choice[str]] = []
        for code, name in sorted(self.gtts_languages.items()):
            label = f"{code} — {name}"
            haystack = f"{code} {name}".lower()
            if current and current not in haystack:
                continue
            results.append(app_commands.Choice(name=label[:100], value=code))
            if len(results) >= 25:
                break
        return results


    async def _set_mode_common(self, interaction: discord.Interaction, *, mode: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        value = validate_mode(mode)
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, engine=value))
            title, desc = "Modo padrão atualizado", f"O modo padrão do servidor agora é `{value}`. Esse ajuste só afeta comandos antigos e compatibilidade; os prefixos gTTS, Edge e Google Cloud continuam escolhendo o motor por mensagem."
        else:
            await self._set_user_tts_and_refresh(interaction.guild.id, interaction.user.id, engine=value)
            title, desc = "Modo atualizado", f"O seu modo de TTS agora é `{value}`. Esse ajuste só afeta comandos antigos e compatibilidade; os prefixos gTTS, Edge e Google Cloud continuam escolhendo o motor por mensagem."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    async def _set_voice_common(self, interaction: discord.Interaction, *, voice: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        if voice not in self.edge_voice_names:
            await self._respond(interaction, embed=self._make_embed("Voz inválida", "Essa voz não foi encontrada na lista do Edge. Use `/tts voices edge` para ver as opções.", ok=False), ephemeral=True)
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, voice=voice))
            title, desc = "Voz padrão atualizada", f"A voz padrão do servidor agora é `{voice}`."
        else:
            await self._set_user_tts_and_refresh(interaction.guild.id, interaction.user.id, voice=voice)
            title, desc = "Voz atualizada", f"A sua voz do Edge agora é `{voice}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    async def _set_language_common(self, interaction: discord.Interaction, *, language: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        value = str(language or "").strip().lower()
        if value not in self.gtts_languages:
            await self._respond(interaction, embed=self._make_embed("Idioma inválido", "Esse código não foi encontrado na lista do gTTS. Toque em **Ver lista de idiomas** ou tente um destes exemplos: `pt-br`, `en`, `es`, `fr`, `ja`.", ok=False), ephemeral=True)
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, language=value))
            title, desc = "Idioma padrão atualizado", f"O idioma padrão do servidor agora é `{value}`."
        else:
            await self._set_user_tts_and_refresh(interaction.guild.id, interaction.user.id, language=value)
            title, desc = "Idioma atualizado", f"O seu idioma do gTTS agora é `{value}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    async def _set_speed_common(self, interaction: discord.Interaction, *, speed: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        value = self._normalize_rate_value(speed)
        if value is None:
            await self._respond(interaction, embed=self._make_embed("Velocidade inválida", "Use um valor como `10%`, `+10%` ou `-10%`.", ok=False), ephemeral=True)
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, rate=value))
            title, desc = "Velocidade padrão atualizada", f"A velocidade padrão do servidor agora é `{value}`."
        else:
            await self._set_user_tts_and_refresh(interaction.guild.id, interaction.user.id, rate=value)
            title, desc = "Velocidade atualizada", f"A sua velocidade do Edge agora é `{value}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    async def _set_pitch_common(self, interaction: discord.Interaction, *, pitch: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        value = self._normalize_pitch_value(pitch)
        if value is None:
            await self._respond(interaction, embed=self._make_embed("Tom inválido", "Use um valor como `10Hz`, `+10Hz` ou `-10Hz`.", ok=False), ephemeral=True)
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, pitch=value))
            title, desc = "Tom padrão atualizado", f"O tom padrão do servidor agora é `{value}`."
        else:
            await self._set_user_tts_and_refresh(interaction.guild.id, interaction.user.id, pitch=value)
            title, desc = "Tom atualizado", f"O seu tom do Edge agora é `{value}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)


    @app_commands.command(name="menu", description="Abre um painel guiado para configurar o seu TTS")
    async def menu(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        embed = await self._build_settings_embed(
            interaction.guild.id,
            interaction.user.id,
            server=False,
            panel_kind="user",
            viewer_user_id=interaction.user.id,
        )
        view = self._build_panel_view(interaction.user.id, interaction.guild.id, server=False)
        msg = await self._respond(interaction, embed=embed, view=view, ephemeral=True)
        if isinstance(view, TTSStatusView):
            view.attach_message(msg)
        else:
            view.message = msg



    async def _get_panel_command_mention(self, guild_id: int, panel_kind: str) -> str:
        command_path = {
            "user": "tts menu",
            "server": "tts server menu",
            "toggle": "toggle_menu",
        }.get(panel_kind, "tts menu")

        command_ids = await self._get_root_command_ids_cached()
        cmd_id = command_ids.get("tts")
        if cmd_id:
            return f"</{command_path}:{cmd_id}>"
        return f"`/{command_path}`"

    async def _get_panel_prefix_hint(self, guild_id: int, panel_kind: str) -> str:
        prefix_command = {
            "user": "panel",
            "server": "panel_server",
            "toggle": "panel_toggles",
        }.get(panel_kind, "panel")
        bot_prefix = getattr(config, "BOT_PREFIX", "_")

        db = self._get_db()
        if db is not None and hasattr(db, "get_guild_tts_defaults"):
            try:
                guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(guild_id))
                bot_prefix = str((guild_defaults or {}).get("bot_prefix") or bot_prefix)
            except Exception:
                pass

        return f"`{bot_prefix}{prefix_command}`"

    async def _build_expired_panel_embed(self, guild_id: int, panel_kind: str) -> discord.Embed:
        slash_mention = await self._get_panel_command_mention(guild_id, panel_kind)
        prefix_hint = await self._get_panel_prefix_hint(guild_id, panel_kind)
        return build_expired_panel_embed(slash_mention=slash_mention, prefix_hint=prefix_hint)

    async def _build_expired_panel_message(self, guild_id: int, panel_kind: str) -> str:
        slash_mention = await self._get_panel_command_mention(guild_id, panel_kind)
        prefix_hint = await self._get_panel_prefix_hint(guild_id, panel_kind)
        return (
            "Essa interação já expirou porque esse comando ficou aberto por tempo demais.\n\n"
            f"Para abrir um novo painel, tente usar {slash_mention} novamente"
            f" — ou, se preferir, {prefix_hint}."
        )

    def _build_panel_view(self, owner_id: int, guild_id: int, *, server: bool = False, timeout: float = 180, target_user_id: int | None = None, target_user_name: str | None = None) -> discord.ui.View:
        return TTSMainPanelView(self, owner_id, guild_id, server=server, timeout=timeout, target_user_id=target_user_id, target_user_name=target_user_name)

    def _member_panel_name(self, member: discord.abc.User | None) -> str:
        if member is None:
            return "@usuário"
        name = getattr(member, "name", None) or getattr(member, "display_name", None) or str(member)
        return name if str(name).startswith("@") else f"@{name}"

    async def _resolve_member_from_text(self, guild: discord.Guild, raw: str) -> discord.Member | None:
        query = str(raw or "").strip()
        if not query:
            return None

        mention_match = re.fullmatch(r"<@!?(\d+)>", query)
        if mention_match:
            member_id = int(mention_match.group(1))
            member = guild.get_member(member_id)
            if member is not None:
                return member
            try:
                return await guild.fetch_member(member_id)
            except Exception:
                return None

        if query.isdigit():
            member_id = int(query)
            member = guild.get_member(member_id)
            if member is not None:
                return member
            try:
                return await guild.fetch_member(member_id)
            except Exception:
                return None

        lowered = query.lower()
        exact_matches: list[discord.Member] = []
        fuzzy_matches: list[discord.Member] = []
        for member in guild.members:
            candidates = [
                str(member),
                getattr(member, "display_name", "") or "",
                getattr(member, "global_name", "") or "",
                getattr(member, "name", "") or "",
            ]
            candidate_values = [c.strip() for c in candidates if str(c).strip()]
            if any(c.lower() == lowered for c in candidate_values):
                exact_matches.append(member)
                continue
            if any(lowered in c.lower() for c in candidate_values):
                fuzzy_matches.append(member)

        if len(exact_matches) == 1:
            return exact_matches[0]
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0]
        return None

    def _normalize_language_query(self, value: str) -> str:
        return normalize_language_query(value)

    def _resolve_gtts_language_input(self, raw_language: str) -> tuple[str | None, str | None]:
        return resolve_gtts_language_input(raw_language, self.gtts_languages, self.gtts_language_aliases)

    async def _prefix_set_lang(self, message: discord.Message, raw_language: str):
        if message.guild is None:
            return

        value = str(raw_language or "").strip()
        if not value:
            await message.channel.send(embed=self._make_embed("Idioma obrigatório", f"Use esse comando assim: `_set lang português` ou `_set lang pt-br`.", ok=False))
            return

        code, language_name = self._resolve_gtts_language_input(value)
        if code is None:
            await message.channel.send(embed=self._make_embed("Idioma inválido", "Não reconheci esse idioma do gTTS. Use um código como `pt-br`, `pt`, `en`, `es` ou um nome em português como `português` e `espanhol`.", ok=False))
            return

        db = self._get_db()
        if db is None or not hasattr(db, "set_user_tts"):
            await message.channel.send(embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora para alterar o idioma do gTTS.", ok=False))
            return

        history_entry = f"Você alterou o próprio idioma para {code}"
        await self._set_user_tts_and_refresh(message.guild.id, message.author.id, language=code, history_entry=history_entry)

        pretty_name = language_name or code
        await message.channel.send(embed=self._make_embed("Idioma atualizado", f"Seu idioma pessoal do gTTS agora é `{code}` ({pretty_name}).", ok=True))

    async def _prefix_reset_user(self, message: discord.Message, raw_target: str):
        if message.guild is None:
            return
        if not getattr(message.author.guild_permissions, "kick_members", False):
            await message.channel.send(embed=self._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para resetar as configurações de TTS de outro usuário.", ok=False))
            return

        target_text = str(raw_target or "").strip()
        if not target_text:
            await message.channel.send(embed=self._make_embed("Usuário obrigatório", "Use esse comando assim: `reset @usuário`, `reset ID` ou `reset tag`.", ok=False))
            return

        db = self._get_db()
        if db is None or not hasattr(db, "reset_user_tts"):
            await message.channel.send(embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora para resetar as configurações.", ok=False))
            return

        member = await self._resolve_member_from_text(message.guild, target_text)
        if member is None:
            await message.channel.send(embed=self._make_embed("Usuário não encontrado", "Não consegui encontrar esse usuário. Use menção, ID ou tag exata do usuário no servidor.", ok=False))
            return

        history_entry = f"{self._member_panel_name(message.author)} resetou as configurações de TTS de {self._member_panel_name(member)} para os padrões do servidor"
        await self._reset_user_tts_and_refresh(message.guild.id, member.id, history_entry=history_entry)

        await message.channel.send(embed=self._make_embed("Configurações resetadas", f"As configurações de TTS de {self._member_panel_name(member)} agora seguem os padrões do servidor.", ok=True))

    def _resolve_target_user(self, interaction: discord.Interaction, target_user_id: int | None = None, target_user_name: str | None = None) -> tuple[int, str]:
        resolved_id = int(target_user_id or getattr(getattr(interaction, "user", None), "id", 0) or 0)
        resolved_name = str(target_user_name or self._member_panel_name(getattr(interaction, "user", None)))
        return resolved_id, resolved_name

    def _resolve_panel_target_user(
        self,
        interaction: discord.Interaction,
        *,
        server: bool,
        message_id: int | None = None,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ) -> tuple[int, str, bool]:
        resolved_id, resolved_name = self._resolve_target_user(interaction, target_user_id, target_user_name)

        if server or not message_id or message_id not in self._public_panel_states:
            return resolved_id, resolved_name, False

        state = self._public_panel_states.get(message_id, {}) or {}
        if state.get("panel_kind") != "user":
            return resolved_id, resolved_name, False

        actor_id = int(getattr(getattr(interaction, "user", None), "id", 0) or 0)
        explicit_target = target_user_id is not None or bool(str(target_user_name or "").strip())

        if explicit_target and resolved_id != actor_id:
            return resolved_id, resolved_name, False

        return actor_id, self._member_panel_name(getattr(interaction, "user", None)), True

    def _build_toggle_view(self, owner_id: int, guild_id: int, *, timeout: float = 180) -> discord.ui.View:
        return TTSTogglePanelView(self, owner_id, guild_id, timeout=timeout)


    async def _announce_panel_change(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        description: str,
        target_message: discord.Message | None = None,
    ):
        channel = interaction.channel
        if channel is None:
            return

        try:
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.blurple(),
            )
            if interaction.user and getattr(interaction.user, "display_avatar", None):
                embed.set_author(
                    name=str(interaction.user),
                    icon_url=interaction.user.display_avatar.url,
                )
            embed.set_footer(text="Alteração feita pelo painel de TTS")
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[tts_voice] Falha ao anunciar alteração do painel: {e}")


    def _get_saved_spoken_name(self, guild_id: int | None, user_id: int | None) -> str:
        if not guild_id or not user_id:
            return ""
        db = self._get_db()
        if db is None or not hasattr(db, "get_user_tts"):
            return ""
        try:
            data = db.get_user_tts(int(guild_id), int(user_id)) or {}
        except Exception:
            return ""
        return _normalize_spaces(str((data or {}).get("speaker_name", "") or ""))

    def _get_current_gcloud_voice(self, guild_id: int, user_id: int, *, server: bool = False) -> str:
        db = self._get_db()
        if db is None:
            return str(getattr(config, "GOOGLE_CLOUD_TTS_VOICE_NAME", "pt-BR-Standard-A") or "pt-BR-Standard-A")
        try:
            if server and hasattr(db, "get_guild_tts_defaults"):
                defaults = db.get_guild_tts_defaults(guild_id) or {}
                return str(defaults.get("gcloud_voice") or getattr(config, "GOOGLE_CLOUD_TTS_VOICE_NAME", "pt-BR-Standard-A") or "pt-BR-Standard-A")
            resolved = db.resolve_tts(guild_id, user_id) or {}
            return str(resolved.get("gcloud_voice") or getattr(config, "GOOGLE_CLOUD_TTS_VOICE_NAME", "pt-BR-Standard-A") or "pt-BR-Standard-A")
        except Exception:
            return str(getattr(config, "GOOGLE_CLOUD_TTS_VOICE_NAME", "pt-BR-Standard-A") or "pt-BR-Standard-A")

    def _get_current_gcloud_language(self, guild_id: int, user_id: int, *, server: bool = False) -> str:
        db = self._get_db()
        if db is None:
            return str(getattr(config, "GOOGLE_CLOUD_TTS_LANGUAGE_CODE", "pt-BR") or "pt-BR")
        try:
            if server and hasattr(db, "get_guild_tts_defaults"):
                defaults = db.get_guild_tts_defaults(guild_id) or {}
                return str(defaults.get("gcloud_language") or getattr(config, "GOOGLE_CLOUD_TTS_LANGUAGE_CODE", "pt-BR") or "pt-BR")
            resolved = db.resolve_tts(guild_id, user_id) or {}
            return str(resolved.get("gcloud_language") or getattr(config, "GOOGLE_CLOUD_TTS_LANGUAGE_CODE", "pt-BR") or "pt-BR")
        except Exception:
            return str(getattr(config, "GOOGLE_CLOUD_TTS_LANGUAGE_CODE", "pt-BR") or "pt-BR")

    def _validate_gcloud_language_input(self, raw_value: str) -> tuple[str | None, str | None]:
        return validate_gcloud_language_input(raw_value)

    def _validate_gcloud_voice_input(self, raw_value: str) -> tuple[str | None, str | None]:
        return validate_gcloud_voice_input(raw_value)

    def _normalize_gcloud_rate_value(self, raw_value: str | float) -> str:
        return normalize_gcloud_rate_value(raw_value, default_rate=float(getattr(config, 'GOOGLE_CLOUD_TTS_SPEAKING_RATE', 1.0) or 1.0))

    def _normalize_gcloud_pitch_value(self, raw_value: str | float) -> str:
        return normalize_gcloud_pitch_value(raw_value, default_pitch=float(getattr(config, 'GOOGLE_CLOUD_TTS_PITCH', 0.0) or 0.0))

    def _validate_spoken_name_input(self, raw_value: str) -> tuple[str | None, str | None]:
        value = _normalize_spaces(str(raw_value or ""))
        if not value:
            return "", None
        if not _looks_pronounceable_for_tts(value):
            return None, "Esse apelido tem caracteres que o TTS não consegue pronunciar bem. Use letras, números, espaço, ponto, traço ou underline."
        spoken = _speech_name(value)
        if not spoken or not _looks_pronounceable_for_tts(spoken):
            return None, "Esse apelido não ficou pronunciável depois da normalização do TTS."
        return spoken[:32], None

    def _resolve_spoken_name(self, member: discord.abc.User | None, *, guild_id: int | None = None) -> tuple[str, str]:
        if member is None:
            return "usuário", "padrão"

        guild_defaults = None
        if isinstance(member, discord.Member):
            try:
                guild_defaults = self.db.get_guild_tts_defaults(member.guild.id)
            except Exception:
                guild_defaults = None


        saved_spoken_name = self._get_saved_spoken_name(guild_id, getattr(member, "id", None))
        if saved_spoken_name:
            spoken = _speech_name(saved_spoken_name)
            if spoken and _looks_pronounceable_for_tts(spoken):
                return f"{spoken}", "personalizado"

        display_name = _normalize_spaces(getattr(member, "display_name", None) or "")
        username = _normalize_spaces(getattr(member, "name", None) or "")

        if _looks_pronounceable_for_tts(display_name):
            spoken = _speech_name(display_name)
            if spoken:
                return f"{spoken}", "apelido do servidor"

        if _looks_pronounceable_for_tts(username):
            spoken = _speech_name(username)
            if spoken:
                return f"{spoken}", "nome de usuário"

        return "usuário", "padrão"

    def _tts_user_reference(self, member: discord.abc.User | None, *, guild_id: int | None = None) -> str:
        spoken, _ = self._resolve_spoken_name(member, guild_id=guild_id)
        return spoken

    def _tts_role_reference(self, role: discord.Role | None) -> str:
        name = _normalize_spaces(getattr(role, "name", None) or "")
        if _looks_pronounceable_for_tts(name):
            spoken = _speech_name(name)
            if spoken:
                return f"cargo {spoken}"
        return "cargo do discord"

    def _tts_channel_reference(self, channel) -> str:
        name = _normalize_spaces(getattr(channel, "name", None) or "")
        if _looks_pronounceable_for_tts(name):
            spoken = _speech_name(name)
            if spoken:
                return f"canal {spoken}"
        return "canal do discord"

    def _tts_link_reference(self, url: str, *, guild: discord.Guild | None = None) -> str:
        cleaned_url = str(url or "").strip().rstrip(".,!?)]}")
        match = DISCORD_CHANNEL_URL_PATTERN.fullmatch(cleaned_url)
        if match and guild is not None:
            channel_id = int(match.group(2))
            channel = guild.get_channel(channel_id)
            return self._tts_channel_reference(channel)

        try:
            parsed = urlparse(cleaned_url)
        except Exception:
            return "link"

        domain = _extract_primary_domain(parsed.hostname or "")
        if _looks_pronounceable_for_tts(domain):
            spoken = _speech_name(domain)
            if spoken:
                return f"link do {spoken}"
        return "link"

    def _tts_attachment_descriptions(self, attachments) -> list[str]:
        descriptions: list[str] = []
        for attachment in attachments or []:
            content_type = str(getattr(attachment, "content_type", "") or "").lower()
            filename = str(getattr(attachment, "filename", "") or "").lower()
            if content_type == "image/gif" or filename.endswith(".gif"):
                descriptions.append("Anexo em GIF")
            elif content_type.startswith("image/") or filename.endswith(_ATTACHMENT_IMAGE_EXTENSIONS):
                descriptions.append("Anexo de imagem")
            elif content_type.startswith("video/") or filename.endswith(_ATTACHMENT_VIDEO_EXTENSIONS):
                descriptions.append("Anexo de vídeo")
        return descriptions

    def _append_tts_descriptions(self, text: str, descriptions: list[str]) -> str:
        return append_tts_descriptions(text, descriptions, normalize_spaces=_normalize_spaces)

    def _render_tts_text(self, message: discord.Message, raw_text: str) -> str:
        return render_message_tts_text(
            message,
            raw_text,
            guild_id=getattr(message.guild, "id", None),
            user_reference=self._tts_user_reference,
            role_reference=self._tts_role_reference,
            channel_reference=self._tts_channel_reference,
            link_reference=self._tts_link_reference,
            normalize_spaces=_normalize_spaces,
            image_extensions=_ATTACHMENT_IMAGE_EXTENSIONS,
            video_extensions=_ATTACHMENT_VIDEO_EXTENSIONS,
        )

    def _user_history_text(self, interaction: discord.Interaction, what: str, value: str, *, message_id: int | None = None, target_user_id: int | None = None, target_user_name: str | None = None) -> str:
        actor_id = int(getattr(getattr(interaction, "user", None), "id", 0) or 0)
        target_id = int(target_user_id or actor_id or 0)
        target_name = str(target_user_name or self._panel_actor_name(interaction))
        action_text = f"alterou {what} para {value}"
        if target_id == actor_id:
            return self._encode_public_owner_history(actor_id, self._panel_actor_name(interaction), action_text)
        return f"{self._panel_actor_name(interaction)} alterou {what} de {target_name} para {value}"

    def _server_history_text(self, interaction: discord.Interaction, what: str, value: str) -> str:
        return f"{self._panel_actor_name(interaction)} alterou {what} para {value}"

    def _toggle_history_text(self, interaction: discord.Interaction, text: str) -> str:
        return f"{self._panel_actor_name(interaction)} {text}"


    async def _build_toggle_embed(
        self,
        guild_id: int,
        user_id: int,
        *,
        last_changes: list[str] | None = None,
        message_id: int | None = None,
        target_user_name: str | None = None,
        viewer_user_id: int | None = None,
    ) -> discord.Embed:
        db = self._get_db()
        panel_history = await self._maybe_await(db.get_panel_history(guild_id, user_id)) if db and hasattr(db, "get_panel_history") else {}
        stored_last_changes = list((panel_history or {}).get("toggle_last_changes", []) or [])
        if not stored_last_changes:
            stored_last = str((panel_history or {}).get("toggle_last_change", "") or "")
            stored_last_changes = [stored_last] if stored_last else []
        if last_changes is None:
            last_changes = stored_last_changes
        last_changes = self._resolve_last_changes(stored_changes=last_changes, message_id=message_id)
        guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(guild_id)) if db else {}
        guild_defaults = guild_defaults or {}
        history_text = self._format_history_entries(last_changes or [], viewer_user_id=viewer_user_id or user_id, message_id=message_id)
        return build_toggle_embed(
            auto_leave_enabled=bool(guild_defaults.get("auto_leave", True)),
            history_text=history_text,
        )

    def _setting_origin_label(self, user_settings: dict, key: str) -> str:
        return "Usuário" if str((user_settings or {}).get(key, "") or "").strip() else "Servidor"

    def _status_bool(self, value: bool) -> str:
        return "Ativado" if bool(value) else "Desativado"

    def _status_badge(self, value: bool, *, on: str = "Ativo", off: str = "Inativo") -> str:
        return status_badge(value, on=on, off=off)

    def _status_source_badge(self, source: str) -> str:
        from .utils.embed import status_source_badge as _status_source_badge
        return _status_source_badge(source)

    def _status_engine_label(self, engine: str) -> str:
        from .utils.embed import status_engine_label as _status_engine_label
        return _status_engine_label(engine)

    def _status_voice_channel_text(self, guild: discord.Guild | None, target_user_id: int) -> str:
        return status_voice_channel_text(guild, target_user_id)

    def _spoken_name_status_text(self, guild_id: int, member: discord.abc.User | None, *, resolved: dict | None = None) -> tuple[str, str]:
        active_name, active_source = self._resolve_spoken_name(member, guild_id=guild_id)
        custom_name = _normalize_spaces(str((resolved or {}).get("speaker_name", "") or ""))
        return spoken_name_status_text(active_name=active_name, active_source=active_source, custom_name=custom_name)

    async def _build_status_embed(
        self,
        guild_id: int,
        user_id: int,
        *,
        viewer_user_id: int | None = None,
        target_user_name: str | None = None,
        public: bool = False,
    ) -> discord.Embed:
        db = self._get_db()
        user_settings = await self._maybe_await(db.get_user_tts(guild_id, user_id)) if db else {}
        resolved = await self._maybe_await(db.resolve_tts(guild_id, user_id)) if db else {}

        user_settings = user_settings or {}
        resolved = resolved or {}

        guild = self.bot.get_guild(guild_id)
        vc = self._get_voice_client_for_guild(guild)
        state = self.guild_states.get(guild_id)
        queue_size = int(getattr(getattr(state, "queue", None), "qsize", lambda: 0)() if state else 0)
        is_connected = bool(vc and vc.is_connected())
        is_playing = bool(vc and (vc.is_playing() or vc.is_paused()))
        bot_channel = getattr(getattr(vc, "channel", None), "mention", None) or (f"`{getattr(getattr(vc, 'channel', None), 'name', 'Desconhecido')}`" if getattr(vc, "channel", None) is not None else "Desconectado")
        user_channel = self._status_voice_channel_text(guild, user_id)
        member = guild.get_member(user_id) if guild else None
        target_name = str(target_user_name or self._member_panel_name(member))
        spoken_name_text, _ = self._spoken_name_status_text(guild_id, member, resolved=resolved)
        panel_history = await self._maybe_await(db.get_panel_history(guild_id, user_id)) if db and hasattr(db, "get_panel_history") else {}
        stored_last_changes = list((panel_history or {}).get("user_last_changes", []) or [])
        if not stored_last_changes:
            stored_last = str((panel_history or {}).get("user_last_change", "") or "")
            stored_last_changes = [stored_last] if stored_last else []
        history_text = self._format_status_history_entries(stored_last_changes or [], viewer_user_id=viewer_user_id or user_id)
        return build_status_embed(
            member=member,
            target_name=target_name,
            user_id=user_id,
            viewer_user_id=int(viewer_user_id or user_id or 0),
            public=public,
            is_connected=is_connected,
            is_playing=is_playing,
            queue_size=queue_size,
            resolved=resolved,
            user_settings=user_settings,
            user_channel=user_channel,
            bot_channel=bot_channel,
            spoken_name_text=spoken_name_text,
            history_text=history_text,
            google_language_default=getattr(config, 'GOOGLE_CLOUD_TTS_LANGUAGE_CODE', 'pt-BR'),
            google_voice_default=getattr(config, 'GOOGLE_CLOUD_TTS_VOICE_NAME', 'pt-BR-Standard-A'),
            google_rate_default=str(getattr(config, 'GOOGLE_CLOUD_TTS_SPEAKING_RATE', 1.0)),
            google_pitch_default=str(getattr(config, 'GOOGLE_CLOUD_TTS_PITCH', 0.0)),
        )

    def _build_status_view(self, owner_id: int, guild_id: int, *, target_user_id: int | None = None, target_user_name: str | None = None, timeout: float = 180) -> discord.ui.View:
        return TTSStatusView(self, owner_id, guild_id, timeout=timeout, target_user_id=target_user_id, target_user_name=target_user_name)

    async def _build_settings_embed(
        self,
        guild_id: int,
        user_id: int,
        *,
        server: bool = False,
        panel_kind: str = "user",
        last_changes: list[str] | None = None,
        message_id: int | None = None,
        target_user_name: str | None = None,
        viewer_user_id: int | None = None,
    ) -> discord.Embed:
        db = self._get_db()
        guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(guild_id)) if db else {}
        user_settings = await self._maybe_await(db.get_user_tts(guild_id, user_id)) if db else {}
        resolved = await self._maybe_await(db.resolve_tts(guild_id, user_id)) if db else {}

        guild_defaults = guild_defaults or {}
        user_settings = user_settings or {}
        resolved = resolved or {}

        panel_history = await self._maybe_await(db.get_panel_history(guild_id, user_id)) if db and hasattr(db, "get_panel_history") else {}
        stored_last_changes: list[str] = []
        if panel_kind == "server":
            stored_last_changes = list((panel_history or {}).get("server_last_changes", []) or [])
            if not stored_last_changes:
                stored_last = str((panel_history or {}).get("server_last_change", "") or "")
                stored_last_changes = [stored_last] if stored_last else []
        elif panel_kind == "toggle":
            stored_last_changes = list((panel_history or {}).get("toggle_last_changes", []) or [])
            if not stored_last_changes:
                stored_last = str((panel_history or {}).get("toggle_last_change", "") or "")
                stored_last_changes = [stored_last] if stored_last else []
        else:
            stored_last_changes = list((panel_history or {}).get("user_last_changes", []) or [])
            if not stored_last_changes:
                stored_last = str((panel_history or {}).get("user_last_change", "") or "")
                stored_last_changes = [stored_last] if stored_last else []

        if last_changes is None:
            last_changes = stored_last_changes
        last_changes = self._resolve_last_changes(stored_changes=last_changes, message_id=message_id)

        if server:
            title = "Painel de TTS do servidor"
            description = "Use os botões abaixo para ajustar os padrões do servidor. gTTS, Edge e Google Cloud têm controles separados por prefixo."
        elif target_user_name and int(user_id or 0) != int(viewer_user_id or user_id or 0):
            title = f"Painel de TTS de {target_user_name}"
            description = f"Use os botões abaixo para alterar as configurações de {target_user_name}. gTTS, Edge e Google Cloud têm controles separados por prefixo."
        else:
            title = "Painel de TTS"
            description = "Use os botões abaixo para alterar as suas configurações. gTTS, Edge e Google Cloud têm controles separados por prefixo."
        member = self.bot.get_guild(guild_id).get_member(user_id) if (not server and self.bot.get_guild(guild_id)) else None
        spoken_name_text = None
        if not server:
            spoken_name_text, _ = self._spoken_name_status_text(guild_id, member, resolved=resolved)
        history_text = self._format_history_entries(last_changes or [], viewer_user_id=viewer_user_id or user_id, message_id=message_id)
        return build_settings_embed(
            title=title,
            description=description,
            resolved=resolved,
            guild_defaults=guild_defaults,
            history_text=history_text,
            server=server,
            panel_kind=panel_kind,
            spoken_name_text=spoken_name_text,
            google_language_default=getattr(config, 'GOOGLE_CLOUD_TTS_LANGUAGE_CODE', 'pt-BR'),
            google_voice_default=getattr(config, 'GOOGLE_CLOUD_TTS_VOICE_NAME', 'pt-BR-Standard-A'),
            google_rate_default=str(getattr(config, 'GOOGLE_CLOUD_TTS_SPEAKING_RATE', 1.0)),
            google_pitch_default=str(getattr(config, 'GOOGLE_CLOUD_TTS_PITCH', 0.0)),
            google_prefix_default=getattr(config, 'GOOGLE_CLOUD_TTS_PREFIX', "'"),
            ignored_tts_role_text=self._ignored_tts_role_text(guild_id, guild_defaults=guild_defaults) if server else None,
        )


    async def _apply_ignored_tts_role_from_panel(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        *,
        source_panel_message: discord.Message | None = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=self._make_embed("Comando indisponível", "Esse painel só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message(
                embed=self._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para alterar o cargo ignorado do servidor.", ok=False),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, ignored_tts_role_id=int(role.id)))
        history_entry = self._server_history_text(interaction, "o cargo ignorado do TTS", role.mention)
        await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
        if source_panel_message is not None:
            self._append_public_panel_history(getattr(source_panel_message, "id", None), history_entry)

        panel_message = source_panel_message
        if panel_message is not None:
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("server_last_changes", []) or [])
            embed = await self._build_settings_embed(
                interaction.guild.id,
                interaction.user.id,
                server=True,
                panel_kind="server",
                last_changes=last_changes,
                message_id=getattr(panel_message, "id", None),
                viewer_user_id=interaction.user.id,
            )
            view = self._build_panel_view(0 if getattr(panel_message, "id", None) in self._public_panel_states else interaction.user.id, interaction.guild.id, server=True)
            view.message = panel_message
            try:
                await panel_message.edit(embed=embed, view=view)
            except discord.NotFound:
                pass
            except Exception as e:
                print(f"[tts_panel] falha ao editar painel: {e!r}")

        title = "Cargo ignorado atualizado"
        description = f"Agora o bot ignora mensagens de TTS dos usuários que estiverem em {role.mention}."
        await interaction.response.send_message(embed=self._make_embed(title, description, ok=True), ephemeral=True)
        if panel_message is not None:
            await self._announce_panel_change(interaction, title=title, description=description, target_message=panel_message)

    async def _remove_ignored_tts_role_from_panel(
        self,
        interaction: discord.Interaction,
        *,
        source_panel_message: discord.Message | None = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=self._make_embed("Comando indisponível", "Esse painel só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message(
                embed=self._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para remover o cargo ignorado do servidor.", ok=False),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        current_role_id = self._get_ignored_tts_role_id(interaction.guild.id)
        if current_role_id <= 0:
            await interaction.response.send_message(
                embed=self._make_embed("Nenhum cargo configurado", "Não existe cargo ignorado configurado no TTS deste servidor.", ok=False),
                ephemeral=True,
            )
            return

        current_role = interaction.guild.get_role(current_role_id)
        current_role_text = current_role.mention if current_role is not None else f"`{current_role_id}`"
        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, ignored_tts_role_id=0))
        history_entry = self._server_history_text(interaction, "removeu o cargo ignorado do TTS", current_role_text)
        await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
        if source_panel_message is not None:
            self._append_public_panel_history(getattr(source_panel_message, "id", None), history_entry)

        panel_message = source_panel_message
        if panel_message is not None:
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("server_last_changes", []) or [])
            embed = await self._build_settings_embed(
                interaction.guild.id,
                interaction.user.id,
                server=True,
                panel_kind="server",
                last_changes=last_changes,
                message_id=getattr(panel_message, "id", None),
                viewer_user_id=interaction.user.id,
            )
            view = self._build_panel_view(0 if getattr(panel_message, "id", None) in self._public_panel_states else interaction.user.id, interaction.guild.id, server=True)
            view.message = panel_message
            try:
                await panel_message.edit(embed=embed, view=view)
            except discord.NotFound:
                pass
            except Exception as e:
                print(f"[tts_panel] falha ao editar painel: {e!r}")

        title = "Cargo ignorado removido"
        description = "O bot voltou a considerar mensagens de TTS de todos os cargos deste servidor."
        await interaction.response.send_message(embed=self._make_embed(title, description, ok=True), ephemeral=True)
        if panel_message is not None:
            await self._announce_panel_change(interaction, title=title, description=description, target_message=panel_message)

    async def _apply_server_prefix_from_modal(
        self,
        interaction: discord.Interaction,
        *,
        prefix_kind: str,
        prefix: str,
        panel_message: discord.Message,
    ):
        return await apply_server_prefix_from_modal(self, interaction, prefix_kind=prefix_kind, prefix=prefix, panel_message=panel_message)

    async def _panel_update_after_change(
        self,
        interaction: discord.Interaction,
        *,
        embed: discord.Embed,
        view: discord.ui.View,
        title: str,
        description: str,
        target_message: discord.Message | None = None,
    ):
        edited = False
        message_to_edit = target_message or getattr(interaction, "message", None)
        current_interaction_message = getattr(interaction, "message", None)

        if message_to_edit is not None and hasattr(view, "message"):
            view.message = message_to_edit

        try:
            if (
                message_to_edit is not None
                and current_interaction_message is not None
                and getattr(current_interaction_message, "id", None) == getattr(message_to_edit, "id", None)
                and not interaction.response.is_done()
            ):
                await interaction.response.edit_message(embed=embed, view=view)
                edited = True
        except discord.NotFound as e:
            print(f"[tts_panel] falha ao editar via interaction.response.edit_message: {e!r}")
        except Exception as e:
            print(f"[tts_panel] falha ao editar via interaction.response.edit_message: {e!r}")

        if not edited and message_to_edit is not None:
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True, thinking=False)
                await message_to_edit.edit(embed=embed, view=view)
                edited = True
            except discord.NotFound as e:
                print(f"[tts_panel] painel alvo não existe mais via message.edit: {e!r}")
            except Exception as e:
                print(f"[tts_panel] falha ao editar painel alvo via message.edit: {e!r}")

        if not edited and message_to_edit is not None:
            try:
                await interaction.followup.edit_message(message_id=message_to_edit.id, embed=embed, view=view)
                edited = True
            except discord.NotFound as e:
                print(f"[tts_panel] painel alvo não existe mais via followup.edit_message: {e!r}")
            except Exception as e:
                print(f"[tts_panel] falha ao editar painel alvo via followup.edit_message: {e!r}")

        if not edited and current_interaction_message is not None:
            try:
                if hasattr(view, "message"):
                    view.message = current_interaction_message
                if not interaction.response.is_done():
                    await interaction.response.edit_message(embed=embed, view=view)
                else:
                    await interaction.followup.edit_message(message_id=current_interaction_message.id, embed=embed, view=view)
                edited = True
            except discord.NotFound as e:
                print(f"[tts_panel] falha ao editar a mensagem atual: {e!r}")
            except Exception as e:
                print(f"[tts_panel] falha ao editar a mensagem atual: {e!r}")

        if not edited:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        embed=embed,
                        view=view,
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        embed=embed,
                        view=view,
                        ephemeral=True,
                    )
            except Exception as e:
                print(f"[tts_panel] falha ao responder followup: {e!r}")



    async def _apply_mode_from_panel(self, interaction: discord.Interaction, mode: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        return await apply_mode_from_panel(self, interaction, mode, server=server, source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name)


    async def _apply_voice_from_panel(self, interaction: discord.Interaction, voice: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        return await apply_voice_from_panel(self, interaction, voice, server=server, source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name)


    async def _apply_language_from_panel(self, interaction: discord.Interaction, language: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        return await apply_language_from_panel(self, interaction, language, server=server, source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name)


    async def _apply_speed_from_panel(self, interaction: discord.Interaction, speed: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        return await apply_speed_from_panel(self, interaction, speed, server=server, source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name)


    async def _apply_pitch_from_panel(self, interaction: discord.Interaction, pitch: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        return await apply_pitch_from_panel(self, interaction, pitch, server=server, source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name)


    async def _apply_gcloud_language_from_modal(self, interaction: discord.Interaction, language: str, *, server: bool, panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        return await apply_gcloud_language_from_modal(self, interaction, language, server=server, panel_message=panel_message, target_user_id=target_user_id, target_user_name=target_user_name)

    async def _apply_gcloud_voice_from_modal(self, interaction: discord.Interaction, voice_name: str, *, server: bool, panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        return await apply_gcloud_voice_from_modal(self, interaction, voice_name, server=server, panel_message=panel_message, target_user_id=target_user_id, target_user_name=target_user_name)

    async def _apply_gcloud_language_from_panel(self, interaction: discord.Interaction, language: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        return await apply_gcloud_language_from_panel(self, interaction, language, server=server, source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name)

    async def _apply_gcloud_voice_from_panel(self, interaction: discord.Interaction, voice_name: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        return await apply_gcloud_voice_from_panel(self, interaction, voice_name, server=server, source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name)

    async def _apply_gcloud_speed_from_panel(self, interaction: discord.Interaction, speed: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        return await apply_gcloud_speed_from_panel(self, interaction, speed, server=server, source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name)

    async def _apply_gcloud_pitch_from_panel(self, interaction: discord.Interaction, pitch: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        return await apply_gcloud_pitch_from_panel(self, interaction, pitch, server=server, source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name)

    async def _apply_spoken_name_from_modal(
        self,
        interaction: discord.Interaction,
        spoken_name: str,
        *,
        panel_message: discord.Message | None = None,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        return await apply_spoken_name_from_modal(self, interaction, spoken_name, panel_message=panel_message, target_user_id=target_user_id, target_user_name=target_user_name)

    async def _apply_announce_author_from_panel(self, interaction: discord.Interaction, enabled: bool, source_panel_message: discord.Message | None = None):
        return await apply_announce_author_from_panel(self, interaction, enabled, source_panel_message)


    async def _apply_auto_leave_from_panel(self, interaction: discord.Interaction, enabled: bool, source_panel_message: discord.Message | None = None):
        return await apply_auto_leave_from_panel(self, interaction, enabled, source_panel_message)


    async def _join_from_panel(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                embed=self._make_embed("Comando indisponível", "Esse botão só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return

        user_voice = getattr(interaction.user, "voice", None)
        if user_voice is None or user_voice.channel is None:
            await interaction.response.send_message(
                embed=self._make_embed("Entre em uma call", "Você precisa estar em uma call para usar esse botão.", ok=False),
                ephemeral=True,
            )
            return

        vc = await self._ensure_connected(interaction.guild, user_voice.channel)
        if vc is None or not vc.is_connected():
            await interaction.response.send_message(
                embed=self._make_embed("Falha ao conectar", "Não consegui entrar na call agora.", ok=False),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=self._make_embed("Bot conectado", f"Entrei na call `{user_voice.channel.name}`.", ok=True),
            ephemeral=True,
        )



    async def _clear_queue_only(self, guild: discord.Guild | None, *, stop_playback: bool = True) -> int:
        if guild is None:
            return 0

        state = self._get_state(guild.id)
        cleared = 0

        while True:
            try:
                state.queue.get_nowait()
                state.queue.task_done()
                cleared += 1
            except Exception:
                break

        vc = self._get_voice_client_for_guild(guild)
        if stop_playback and vc and vc.is_connected():
            try:
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
            except Exception:
                pass

        task = getattr(state, "worker_task", None)
        if task and not task.done():
            task.cancel()
            state.worker_task = None

        return cleared

    async def _prefix_leave(self, message: discord.Message):
        if not message.guild:
            return

        await self._disconnect_and_clear(message.guild)

        embed = discord.Embed(
            title="Saindo da call",
            description="Saí da call e limpei a fila do TTS",
            color=discord.Color.red(),
        )
        await message.channel.send(embed=embed)

    async def _prefix_clear(self, message: discord.Message):
        if not message.guild:
            return

        await self._clear_queue_only(message.guild, stop_playback=True)

        try:
            await message.add_reaction("<:r_dot:1480307087522140331>")
        except Exception:
            try:
                await message.add_reaction("🟥")
            except Exception:
                pass

    async def _prefix_join(self, message: discord.Message):
        if not message.guild:
            return

        author_voice = getattr(message.author, "voice", None)
        if author_voice is None or author_voice.channel is None:
            embed = self._make_embed("Entre em uma call", "Você precisa estar em uma call para usar esse comando", ok=False)
            await message.channel.send(embed=embed)
            return

        self._suppress_runtime_voice_restore(message.guild.id, seconds=12.0)
        self._cancel_runtime_voice_restore(message.guild.id)
        self._remember_expected_voice_channel(message.guild.id, getattr(author_voice.channel, "id", None))
        await self._set_remembered_voice_channel(message.guild.id, getattr(author_voice.channel, "id", None))
        self._clear_manual_voice_disconnect(message.guild.id)

        vc = await self._ensure_connected(message.guild, author_voice.channel)
        if vc is None or not vc.is_connected():
            embed = self._make_embed("Falha ao conectar", "Não consegui entrar na call agora", ok=False)
            await message.channel.send(embed=embed)
            return

        embed = self._make_embed("Entrei na call com sucesso", f"Entrei na call `{author_voice.channel.name}`", ok=True)
        await message.channel.send(embed=embed)

    async def _send_prefix_panel(self, message: discord.Message, *, panel_type: str):
        if not message.guild:
            return

        panel_kind = "user"
        if panel_type == "server":
            panel_kind = "server"
            if not message.author.guild_permissions.kick_members:
                embed = self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Expulsar Membros` para abrir o painel do servidor",
                    ok=False,
                )
                await message.channel.send(embed=embed)
                return
            embed = await self._build_settings_embed(
                message.guild.id,
                message.author.id,
                server=True,
                panel_kind="server",
            )
            view = self._build_panel_view(0, message.guild.id, server=True, timeout=300)
        elif panel_type == "toggle":
            panel_kind = "toggle"
            fake_interaction = None
            if not message.author.guild_permissions.kick_members:
                embed = self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Expulsar Membros` para abrir o painel de toggles",
                    ok=False,
                )
                await message.channel.send(embed=embed)
                return
            guild_ids = getattr(config, "GUILD_IDS", []) or []
            if guild_ids and message.guild.id not in guild_ids:
                return
            embed = await self._build_toggle_embed(message.guild.id, message.author.id)
            view = self._build_toggle_view(0, message.guild.id, timeout=300)
        else:
            embed = await self._build_settings_embed(
                message.guild.id,
                message.author.id,
                server=False,
                panel_kind="user",
            )
            view = self._build_panel_view(0, message.guild.id, server=False, timeout=300)

        if await self._check_prefix_panel_cooldown(message, panel_kind):
            return

        await self._delete_prefix_panel(message.guild.id, message.author.id, panel_kind)

        sent = await message.channel.send(embed=embed, view=view)
        view.message = sent
        self._public_panel_states[sent.id] = {"panel_kind": panel_kind, "history": [], "owner_id": message.author.id}
        self._active_prefix_panels[self._prefix_panel_key(message.guild.id, message.author.id, panel_kind)] = sent

    async def _leave_from_panel(self, interaction: discord.Interaction):
        vc = self._get_voice_client_for_guild(interaction.guild)
        actual_channel = self._get_bot_voice_state_channel(interaction.guild)
        active_channel = getattr(vc, "channel", None) if vc and getattr(vc, "is_connected", lambda: False)() else actual_channel
        if active_channel is None:
            await interaction.response.send_message(
                embed=self._make_embed("Nada para desconectar", "O bot não está conectado em nenhum canal de voz agora.", ok=False),
                ephemeral=True,
            )
            return

        user_voice = getattr(interaction.user, "voice", None)
        if user_voice is None or user_voice.channel is None:
            await interaction.response.send_message(
                embed=self._make_embed("Entre em uma call", "Você precisa estar em uma call para usar esse botão.", ok=False),
                ephemeral=True,
            )
            return

        if active_channel and user_voice.channel.id != active_channel.id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                embed=self._make_embed("Canal diferente", "Você precisa estar na mesma call do bot, ou ter `Gerenciar Servidor`.", ok=False),
                ephemeral=True,
            )
            return

        await self._disconnect_and_clear(interaction.guild)
        await interaction.response.send_message(
            embed=self._make_embed("Bot desconectado", "Saí da call e limpei a fila de TTS.", ok=True),
            ephemeral=True,
        )



    @app_commands.command(name="status", description="Mostra o status atual do TTS ou copia a configuração de outro usuário")
    @app_commands.describe(acao="Escolha se quer ver o seu status, mostrar o de outro usuário ou copiar a configuração dele", usuario="Usuário alvo quando a ação envolver outro usuário")
    @app_commands.choices(acao=STATUS_ACTION_CHOICES)
    async def status(
        self,
        interaction: discord.Interaction,
        acao: app_commands.Choice[str] | None = None,
        usuario: discord.Member | None = None,
    ):
        if not await self._require_guild(interaction):
            return

        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return

        action_value = str(getattr(acao, "value", "self") or "self")
        if action_value == "self":
            await self._defer_ephemeral(interaction)
        elif not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)

        if action_value == "show_other":
            if usuario is None:
                await self._respond(interaction, embed=self._make_embed("Usuário obrigatório", "Escolha um usuário para mostrar o status público dele no chat.", ok=False), ephemeral=True)
                return
            embed = await self._build_status_embed(
                interaction.guild.id,
                usuario.id,
                viewer_user_id=interaction.user.id,
                target_user_name=self._member_panel_name(usuario),
                public=True,
            )
            embed.description = f"{self._member_panel_name(interaction.user)} mostrou no chat o status de TTS de {self._member_panel_name(usuario)}."
            await self._respond(interaction, embed=embed, ephemeral=False)
            return

        if action_value == "copy_other":
            if usuario is None:
                await self._respond(interaction, embed=self._make_embed("Usuário obrigatório", "Escolha um usuário para copiar as configurações de TTS dele.", ok=False), ephemeral=True)
                return
            if usuario.id == interaction.user.id:
                await self._respond(interaction, embed=self._make_embed("Escolha outro usuário", "Você já está usando as suas próprias configurações. Escolha outro usuário para copiar as configurações dele.", ok=False), ephemeral=True)
                return

            resolved = await self._maybe_await(db.resolve_tts(interaction.guild.id, usuario.id))
            resolved = resolved or {}
            history_entry = f"{self._panel_actor_name(interaction)} copiou as configurações de TTS de {self._member_panel_name(usuario)}"
            await self._set_user_tts_and_refresh(
                interaction.guild.id,
                interaction.user.id,
                engine=str(resolved.get('engine', 'gtts') or 'gtts'),
                voice=str(resolved.get('edge_voice', resolved.get('voice', 'pt-BR-FranciscaNeural')) or 'pt-BR-FranciscaNeural'),
                language=str(resolved.get('gtts_language', resolved.get('language', 'pt-br')) or 'pt-br'),
                rate=str(resolved.get('edge_rate', resolved.get('rate', '+0%')) or '+0%'),
                pitch=str(resolved.get('edge_pitch', resolved.get('pitch', '+0Hz')) or '+0Hz'),
                gcloud_voice=str(resolved.get('gcloud_voice', getattr(config, 'GOOGLE_CLOUD_TTS_VOICE_NAME', 'pt-BR-Standard-A')) or getattr(config, 'GOOGLE_CLOUD_TTS_VOICE_NAME', 'pt-BR-Standard-A')),
                gcloud_language=str(resolved.get('gcloud_language', getattr(config, 'GOOGLE_CLOUD_TTS_LANGUAGE_CODE', 'pt-BR')) or getattr(config, 'GOOGLE_CLOUD_TTS_LANGUAGE_CODE', 'pt-BR')),
                gcloud_rate=str(resolved.get('gcloud_rate', str(getattr(config, 'GOOGLE_CLOUD_TTS_SPEAKING_RATE', 1.0))) or str(getattr(config, 'GOOGLE_CLOUD_TTS_SPEAKING_RATE', 1.0))),
                gcloud_pitch=str(resolved.get('gcloud_pitch', str(getattr(config, 'GOOGLE_CLOUD_TTS_PITCH', 0.0))) or str(getattr(config, 'GOOGLE_CLOUD_TTS_PITCH', 0.0))),
                history_entry=history_entry,
            )

            embed = self._make_embed(
                "Configurações copiadas",
                f"{self._member_panel_name(interaction.user)} copiou as configurações de TTS de {self._member_panel_name(usuario)}.",
                ok=True,
            )
            embed.add_field(name="Engine", value=f"`{resolved.get('engine', 'gtts')}`", inline=True)
            embed.add_field(name="Voz do Edge", value=f"`{resolved.get('edge_voice', resolved.get('voice', 'pt-BR-FranciscaNeural'))}`", inline=True)
            embed.add_field(name="Idioma do gTTS", value=f"`{resolved.get('gtts_language', resolved.get('language', 'pt-br'))}`", inline=True)
            embed.add_field(name="Velocidade do Edge", value=f"`{resolved.get('edge_rate', resolved.get('rate', '+0%'))}`", inline=True)
            embed.add_field(name="Tom do Edge", value=f"`{resolved.get('edge_pitch', resolved.get('pitch', '+0Hz'))}`", inline=True)
            embed.add_field(name="Idioma do Google", value=f"`{resolved.get('gcloud_language', getattr(config, 'GOOGLE_CLOUD_TTS_LANGUAGE_CODE', 'pt-BR'))}`", inline=True)
            embed.add_field(name="Voz do Google", value=f"`{resolved.get('gcloud_voice', getattr(config, 'GOOGLE_CLOUD_TTS_VOICE_NAME', 'pt-BR-Standard-A'))}`", inline=True)
            embed.add_field(name="Velocidade do Google", value=f"`{resolved.get('gcloud_rate', str(getattr(config, 'GOOGLE_CLOUD_TTS_SPEAKING_RATE', 1.0)))}`", inline=True)
            embed.add_field(name="Tom do Google", value=f"`{resolved.get('gcloud_pitch', str(getattr(config, 'GOOGLE_CLOUD_TTS_PITCH', 0.0)))}`", inline=True)
            await self._respond(interaction, embed=embed, ephemeral=False)
            return

        embed = await self._build_status_embed(
            interaction.guild.id,
            interaction.user.id,
            viewer_user_id=interaction.user.id,
            target_user_name=self._member_panel_name(interaction.user),
            public=False,
        )
        view = self._build_status_view(
            interaction.user.id,
            interaction.guild.id,
            target_user_id=interaction.user.id,
            target_user_name=self._member_panel_name(interaction.user),
        )
        msg = await self._respond(interaction, embed=embed, view=view, ephemeral=True)
        if isinstance(view, TTSStatusView):
            view.attach_message(msg)
        else:
            view.message = msg

    @app_commands.command(name="usuario", description="Abre o painel, reseta ou altera o apelido falado de um usuário")
    @app_commands.describe(usuario="Usuário que terá as configurações alteradas", acao="Escolha se quer abrir o painel, alterar o apelido falado ou resetar")
    @app_commands.choices(acao=USER_CONFIG_ACTION_CHOICES)
    async def usuario(self, interaction: discord.Interaction, usuario: discord.Member, acao: app_commands.Choice[str]):
        if not await self._require_guild(interaction):
            return
        if not await self._require_kick_members(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return

        target_name = self._member_panel_name(usuario)
        action_value = str(getattr(acao, "value", "") or "")

        if action_value == "spoken_name":
            current_value = self._get_saved_spoken_name(interaction.guild.id, usuario.id)
            await interaction.response.send_modal(
                SpokenNameModal(
                    self,
                    None,
                    target_user_id=usuario.id,
                    target_user_name=target_name,
                    current_value=current_value,
                )
            )
            return

        await self._defer_ephemeral(interaction)

        if action_value == "reset":
            if not hasattr(db, "reset_user_tts"):
                await self._respond(interaction, embed=self._make_embed("Função indisponível", "Esse banco ainda não suporta resetar as configurações do usuário.", ok=False), ephemeral=True)
                return
            history_entry = f"{self._panel_actor_name(interaction)} resetou as configurações de TTS de {target_name} para os padrões do servidor"
            await self._reset_user_tts_and_refresh(interaction.guild.id, usuario.id, history_entry=history_entry)
            embed = await self._build_settings_embed(
                interaction.guild.id,
                usuario.id,
                server=False,
                panel_kind="user",
                target_user_name=target_name,
                viewer_user_id=interaction.user.id,
            )
            await self._respond(interaction, embed=embed, ephemeral=True)
            await interaction.followup.send(
                embed=self._make_embed("Configurações resetadas", f"As configurações de TTS de {target_name} agora seguem os padrões do servidor.", ok=True),
                ephemeral=True,
            )
            return

        embed = await self._build_settings_embed(
            interaction.guild.id,
            usuario.id,
            server=False,
            panel_kind="user",
            target_user_name=target_name,
            viewer_user_id=interaction.user.id,
        )
        view = self._build_panel_view(
            interaction.user.id,
            interaction.guild.id,
            server=False,
            target_user_id=usuario.id,
            target_user_name=target_name,
        )
        msg = await self._respond(interaction, embed=embed, view=view, ephemeral=True)
        if isinstance(view, TTSStatusView):
            view.attach_message(msg)
        else:
            view.message = msg


    @server.command(name="menu", description="Abre um painel guiado para configurar o TTS do servidor")
    async def server_menu(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if not await self._require_kick_members(interaction):
            return

        embed = await self._build_settings_embed(
            interaction.guild.id,
            interaction.user.id,
            server=True,
            panel_kind="server",
        )
        view = self._build_panel_view(interaction.user.id, interaction.guild.id, server=True)
        msg = await self._respond(
            interaction,
            embed=embed,
            view=view,
            ephemeral=True,
        )
        view.message = msg

    async def toggle_menu(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if not await self._require_toggle_allowed_guild(interaction):
            return
        if not await self._require_kick_members(interaction):
            return
        embed = await self._build_toggle_embed(interaction.guild.id, interaction.user.id)
        view = self._build_toggle_view(interaction.user.id, interaction.guild.id)
        msg = await self._respond(interaction, embed=embed, view=view, ephemeral=True)
        if isinstance(view, TTSStatusView):
            view.attach_message(msg)
        else:
            view.message = msg


async def setup(bot: commands.Bot):
    await bot.add_cog(TTSVoice(bot))
