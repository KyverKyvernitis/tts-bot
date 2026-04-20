from __future__ import annotations

import asyncio
import audioop
import contextlib
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import discord
from discord.ext import commands

try:
    from discord.ext import voice_recv
except Exception:
    voice_recv = None


VOICE_MODERATION_SFX_PATH = Path(__file__).resolve().parents[1] / "assets" / "sfx" / "voice_moderation_on.wav"
VOICE_MODERATION_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "disconnect_enabled": True,
    "threshold_rms": 1800,
    "hits_to_trigger": 5,
    "window_seconds": 1.4,
    "cooldown_seconds": 10.0,
    "max_intensity": 11752,
}
VOICE_MODERATION_OLD_DEFAULTS: dict[str, Any] = {
    "threshold_rms": 1800,
    "hits_to_trigger": 6,
    "window_seconds": 1.4,
    "cooldown_seconds": 10.0,
}
VOICE_MODERATION_PREVIOUS_DEFAULTS: dict[str, Any] = {
    "threshold_rms": 3000,
    "hits_to_trigger": 6,
    "window_seconds": 0.9,
    "cooldown_seconds": 10.0,
}
VOICE_MODERATION_INTERMEDIATE_DEFAULTS: dict[str, Any] = {
    "threshold_rms": 2600,
    "hits_to_trigger": 7,
    "window_seconds": 1.6,
    "cooldown_seconds": 10.0,
}
VOICE_MODERATION_LEGACY_DEFAULTS: dict[str, Any] = {
    "threshold_rms": 4500,
    "hits_to_trigger": 3,
    "window_seconds": 1.2,
    "cooldown_seconds": 12.0,
}


@dataclass
class _GuildVoiceModerationRuntime:
    sink: Any | None = None
    settings: dict[str, Any] | None = None
    last_notice_channel_id: int | None = None
    suppress_after_until: float = 0.0
    tts_pause_depth: int = 0
    recover_fail_streak: int = 0
    last_recover_attempt_at: float = 0.0
    last_nonrecoverable_notice_at: float = 0.0
    last_hard_recover_at: float = 0.0


class _VoiceModerationStatusView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        title: str,
        lines: list[str],
        notes: list[str] | None = None,
        accent: discord.Color | None = None,
    ):
        super().__init__(timeout=None)
        items: list[discord.ui.Item[Any]] = [discord.ui.TextDisplay("\n".join([title, *lines]))]
        if notes:
            items.append(discord.ui.Separator())
            items.append(discord.ui.TextDisplay("\n".join(notes)))
        self.add_item(discord.ui.Container(*items, accent_color=accent or discord.Color.blurple()))


class _AdjustMaxIntensityModal(discord.ui.Modal, title="Ajustar intensidade máxima"):
    def __init__(self, view: "_VoiceModerationCommandView"):
        super().__init__()
        self.view = view
        current = self.view.current_max_intensity()
        self.max_intensity = discord.ui.TextInput(
            label="Intensidade máxima",
            placeholder="Ex.: 11752",
            default=str(current),
            min_length=1,
            max_length=6,
            required=True,
        )
        self.add_item(self.max_intensity)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.view.cog._can_manage_mode(getattr(interaction, "user", None)):
            await interaction.response.send_message(
                view=self.view.cog._build_notice_panel(
                    title="# 🔊 Moderação de voz",
                    lines=["Você precisa de **Administrador** ou **Desconectar membros** para ajustar isso."],
                    accent=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
        raw = str(self.max_intensity.value or "").strip()
        try:
            value = int(raw)
        except Exception:
            await interaction.response.send_message(
                view=self.view.cog._build_notice_panel(
                    title="# 🔊 Moderação de voz",
                    lines=["Digite um número válido para a intensidade máxima."],
                    accent=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
        value = max(3000, min(32768, value))
        await self.view.cog._update_settings(interaction.guild.id, max_intensity=value)
        await self.view.refresh(interaction, note=f"Intensidade máxima ajustada para `{value}`.")


class _AdjustMaxIntensityButton(discord.ui.Button):
    def __init__(self, view: "_VoiceModerationCommandView"):
        super().__init__(label="Ajustar intensidade máxima", style=discord.ButtonStyle.secondary)
        self.vm_view = view

    async def callback(self, interaction: discord.Interaction):
        if not self.vm_view.cog._can_manage_mode(getattr(interaction, "user", None)):
            await interaction.response.send_message(
                view=self.vm_view.cog._build_notice_panel(
                    title="# 🔊 Moderação de voz",
                    lines=["Você precisa de **Administrador** ou **Desconectar membros** para usar esse botão."],
                    accent=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
        self.vm_view.message = interaction.message or self.vm_view.message
        await interaction.response.send_modal(_AdjustMaxIntensityModal(self.vm_view))


class _VoiceModerationCommandView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: "VoiceModeration",
        guild: discord.Guild,
        *,
        title: str,
        lines: list[str],
        notes: list[str] | None = None,
        accent: discord.Color | None = None,
    ):
        super().__init__(timeout=900)
        self.cog = cog
        self.guild_id = int(guild.id)
        self.title = title
        self.lines = list(lines)
        self.notes = list(notes or [])
        self.accent = accent or discord.Color.blurple()
        self.message: discord.Message | None = None
        self._build_layout()

    def current_max_intensity(self) -> int:
        for line in self.lines:
            if "**Intensidade máxima:**" in line:
                digits = "".join(ch for ch in line if ch.isdigit())
                if digits:
                    try:
                        return int(digits)
                    except Exception:
                        break
        return int(VOICE_MODERATION_DEFAULTS["max_intensity"])

    def _build_layout(self) -> None:
        self.clear_items()
        children: list[discord.ui.Item[Any]] = [discord.ui.TextDisplay("\n".join([self.title, *self.lines]))]
        if self.notes:
            children.append(discord.ui.Separator())
            children.append(discord.ui.TextDisplay("\n".join(self.notes)))
        children.append(discord.ui.ActionRow(_AdjustMaxIntensityButton(self)))
        self.add_item(discord.ui.Container(*children, accent_color=self.accent))

    async def refresh(self, interaction: discord.Interaction, *, note: str | None = None) -> None:
        guild = interaction.guild or self.cog.bot.get_guild(self.guild_id)
        if guild is None:
            if not interaction.response.is_done():
                await interaction.response.defer()
            return
        settings = await self.cog._get_settings(guild.id)
        lines, notes, accent = self.cog._status_snapshot(settings, guild)
        if note:
            notes.append(note)
        self.lines = lines
        self.notes = notes
        self.accent = accent
        self._build_layout()
        payload = {"view": self}
        target = interaction.message or self.message
        if target is None:
            if not interaction.response.is_done():
                await interaction.response.defer()
            return
        if not interaction.response.is_done():
            await interaction.response.defer()
        await target.edit(**payload)
        self.message = target


if voice_recv is not None:
    class _LoudDisconnectSink(voice_recv.AudioSink):
        def __init__(self, cog: "VoiceModeration", guild_id: int):
            super().__init__()
            self.cog = cog
            self.guild_id = int(guild_id)

        def wants_opus(self) -> bool:
            return False

        def write(self, user, data):
            if user is None or getattr(user, "bot", False):
                return
            pcm = getattr(data, "pcm", None)
            if not pcm:
                return
            try:
                rms = int(audioop.rms(pcm, 2))
                peak = int(audioop.max(pcm, 2))
                avgpp = int(audioop.avgpp(pcm, 2))
                score = max(rms, int(peak * 0.45), int(avgpp * 0.9))
            except Exception:
                return
            self.cog._register_loud_sample(
                self.guild_id,
                int(user.id),
                score=score,
                rms=rms,
                peak=peak,
            )

        def cleanup(self):
            return None
else:
    class _LoudDisconnectSink:  # pragma: no cover - fallback sem dependência opcional
        def __init__(self, cog: "VoiceModeration", guild_id: int):
            self.cog = cog
            self.guild_id = int(guild_id)


class VoiceModeration(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._runtime: dict[int, _GuildVoiceModerationRuntime] = {}
        self._loud_hits: dict[tuple[int, int], deque[tuple[float, int]]] = {}
        self._over_limit_windows: dict[tuple[int, int], tuple[float, float, int]] = {}
        self._disconnect_cooldowns: dict[tuple[int, int], float] = {}
        self._sample_lock = threading.Lock()
        self._watchdog_task: asyncio.Task | None = None

    async def cog_load(self):
        for vc in list(getattr(self.bot, "voice_clients", []) or []):
            guild = getattr(vc, "guild", None)
            if guild is not None:
                asyncio.create_task(self.handle_voice_client_ready(guild, vc))
        self._watchdog_task = asyncio.create_task(self._listening_watchdog())

    async def cog_unload(self):
        task = self._watchdog_task
        self._watchdog_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(Exception):
                await task

    def _get_db(self):
        return getattr(self.bot, "settings_db", None)

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(int(guild_id))
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[int(guild_id)] = lock
        return lock

    def _suppress_after_errors(self, guild_id: int, seconds: float = 2.5) -> None:
        runtime = self._runtime.setdefault(int(guild_id), _GuildVoiceModerationRuntime())
        runtime.suppress_after_until = max(float(runtime.suppress_after_until or 0.0), time.monotonic() + max(0.0, float(seconds)))

    async def _listening_watchdog(self) -> None:
        await asyncio.sleep(2.0)
        while True:
            try:
                await self._tick_listening_watchdog()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(3.0)

    async def _tick_listening_watchdog(self) -> None:
        guild_ids: set[int] = set()
        guild_ids.update(int(gid) for gid in self._runtime.keys())
        for guild in list(getattr(self.bot, "guilds", []) or []):
            guild_ids.add(int(guild.id))
        for guild_id in guild_ids:
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                continue
            settings = await self._get_settings(guild.id)
            if not settings.get("enabled"):
                continue
            runtime = self._runtime.setdefault(guild.id, _GuildVoiceModerationRuntime())
            runtime.settings = dict(settings)
            if int(getattr(runtime, "tts_pause_depth", 0) or 0) > 0:
                continue
            vc = self._get_voice_client(guild)
            if vc is None or not getattr(vc, "is_connected", lambda: False)() or getattr(vc, "channel", None) is None:
                continue
            try:
                listening = bool(hasattr(vc, "is_listening") and getattr(vc, "is_listening", lambda: False)())
            except Exception:
                listening = False
            if listening:
                runtime.recover_fail_streak = 0
                continue
            try:
                if getattr(vc, "is_playing", lambda: False)() or getattr(vc, "is_paused", lambda: False)():
                    continue
            except Exception:
                pass
            now = time.monotonic()
            if float(getattr(runtime, "suppress_after_until", 0.0) or 0.0) > now:
                continue
            if (now - float(getattr(runtime, "last_recover_attempt_at", 0.0) or 0.0)) < 1.5:
                continue
            runtime.last_recover_attempt_at = now
            await self.handle_voice_client_ready(guild, vc)


    def _can_manage_mode(self, member: discord.Member | None) -> bool:
        if member is None:
            return False
        perms = getattr(member, "guild_permissions", None)
        if perms is None:
            return False
        return bool(getattr(perms, "administrator", False) or getattr(perms, "disconnect_members", False))

    def _get_voice_client(self, guild: discord.Guild | None) -> Optional[discord.VoiceClient]:
        if guild is None:
            return None
        for vc in getattr(self.bot, "voice_clients", []) or []:
            try:
                if getattr(getattr(vc, "guild", None), "id", None) == guild.id:
                    return vc
            except Exception:
                continue
        return getattr(guild, "voice_client", None)

    @staticmethod
    def _is_receive_client(vc: discord.VoiceClient | None) -> bool:
        return bool(vc and hasattr(vc, "listen") and hasattr(vc, "is_listening"))

    @staticmethod
    def _is_voice_client_busy(vc: discord.VoiceClient | None) -> bool:
        if vc is None:
            return False
        try:
            return bool(getattr(vc, "is_playing", lambda: False)() or getattr(vc, "is_paused", lambda: False)())
        except Exception:
            return False

    def _remember_notice_channel(self, guild_id: int, channel_id: int | None) -> None:
        runtime = self._runtime.setdefault(int(guild_id), _GuildVoiceModerationRuntime())
        runtime.last_notice_channel_id = int(channel_id) if channel_id else None

    def _get_tts_last_text_channel_id(self, guild_id: int) -> int | None:
        cog = self.bot.get_cog("TTSVoice")
        if cog is None:
            return None
        state = getattr(cog, "guild_states", {}).get(int(guild_id))
        value = getattr(state, "last_text_channel_id", None)
        return int(value) if value else None

    def _resolve_notice_channel(self, guild: discord.Guild, *, voice_channel=None):
        runtime = self._runtime.get(int(guild.id))
        candidates: list[Any] = []
        if voice_channel is not None:
            candidates.append(voice_channel)
        if runtime is not None and runtime.last_notice_channel_id:
            channel = guild.get_channel(int(runtime.last_notice_channel_id)) or self.bot.get_channel(int(runtime.last_notice_channel_id))
            if channel is not None:
                candidates.append(channel)
        tts_channel_id = self._get_tts_last_text_channel_id(guild.id)
        if tts_channel_id:
            channel = guild.get_channel(int(tts_channel_id)) or self.bot.get_channel(int(tts_channel_id))
            if channel is not None:
                candidates.append(channel)
        if getattr(guild, "system_channel", None) is not None:
            candidates.append(guild.system_channel)

        seen: set[int] = set()
        for channel in candidates:
            channel_id = getattr(channel, "id", None)
            if channel is None or channel_id in seen or not hasattr(channel, "send"):
                continue
            seen.add(channel_id)
            return channel
        return None

    async def _send_call_notice(
        self,
        guild: discord.Guild,
        *,
        title: str,
        lines: list[str],
        notes: list[str] | None = None,
        accent: discord.Color | None = None,
        voice_channel=None,
    ) -> bool:
        channel = self._resolve_notice_channel(guild, voice_channel=voice_channel)
        if channel is None:
            return False
        try:
            await channel.send(view=self._build_notice_panel(title=title, lines=lines, notes=notes or [], accent=accent or discord.Color.blurple()))
            return True
        except Exception:
            return False

    def _schedule_call_notice(
        self,
        guild_id: int,
        *,
        title: str,
        lines: list[str],
        notes: list[str] | None = None,
        accent: discord.Color | None = None,
    ) -> None:
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._send_call_notice(
                    guild,
                    title=title,
                    lines=lines,
                    notes=notes or [],
                    accent=accent or discord.Color.blurple(),
                    voice_channel=getattr(self._get_voice_client(guild), "channel", None),
                ),
                self.bot.loop,
            )
        except Exception:
            pass

    def _normalize_settings(self, data: dict[str, Any] | None) -> dict[str, Any]:
        merged = dict(VOICE_MODERATION_DEFAULTS)
        if isinstance(data, dict):
            merged.update(data)
        def _matches_defaults(candidate: dict[str, Any]) -> bool:
            return (
                int(merged.get("threshold_rms", 0) or 0) == int(candidate["threshold_rms"])
                and int(merged.get("hits_to_trigger", 0) or 0) == int(candidate["hits_to_trigger"])
                and abs(float(merged.get("window_seconds", 0.0) or 0.0) - float(candidate["window_seconds"])) < 1e-9
                and abs(float(merged.get("cooldown_seconds", 0.0) or 0.0) - float(candidate["cooldown_seconds"])) < 1e-9
            )

        if (
            _matches_defaults(VOICE_MODERATION_LEGACY_DEFAULTS)
            or _matches_defaults(VOICE_MODERATION_PREVIOUS_DEFAULTS)
            or _matches_defaults(VOICE_MODERATION_OLD_DEFAULTS)
            or _matches_defaults(VOICE_MODERATION_INTERMEDIATE_DEFAULTS)
        ):
            merged.update({
                "threshold_rms": VOICE_MODERATION_DEFAULTS["threshold_rms"],
                "hits_to_trigger": VOICE_MODERATION_DEFAULTS["hits_to_trigger"],
                "window_seconds": VOICE_MODERATION_DEFAULTS["window_seconds"],
                "cooldown_seconds": VOICE_MODERATION_DEFAULTS["cooldown_seconds"],
            })
        return {
            "enabled": bool(merged.get("enabled", False)),
            "disconnect_enabled": bool(merged.get("disconnect_enabled", True)),
            "threshold_rms": max(500, min(30000, int(merged.get("threshold_rms", VOICE_MODERATION_DEFAULTS["threshold_rms"]) or VOICE_MODERATION_DEFAULTS["threshold_rms"]))),
            "hits_to_trigger": max(1, min(20, int(merged.get("hits_to_trigger", VOICE_MODERATION_DEFAULTS["hits_to_trigger"]) or VOICE_MODERATION_DEFAULTS["hits_to_trigger"]))),
            "window_seconds": max(0.2, min(10.0, float(merged.get("window_seconds", VOICE_MODERATION_DEFAULTS["window_seconds"]) or VOICE_MODERATION_DEFAULTS["window_seconds"]))),
            "cooldown_seconds": max(1.0, min(600.0, float(merged.get("cooldown_seconds", VOICE_MODERATION_DEFAULTS["cooldown_seconds"]) or VOICE_MODERATION_DEFAULTS["cooldown_seconds"]))),
            "max_intensity": max(3000, min(32768, int(merged.get("max_intensity", VOICE_MODERATION_DEFAULTS["max_intensity"]) or VOICE_MODERATION_DEFAULTS["max_intensity"]))),
        }

    async def _get_settings(self, guild_id: int) -> dict[str, Any]:
        db = self._get_db()
        if db is None or not hasattr(db, "get_voice_moderation_settings"):
            return self._normalize_settings(None)
        try:
            data = db.get_voice_moderation_settings(guild_id)
            if asyncio.iscoroutine(data):
                data = await data
            return self._normalize_settings(data if isinstance(data, dict) else None)
        except Exception:
            return self._normalize_settings(None)

    async def _set_enabled(self, guild_id: int, value: bool) -> None:
        db = self._get_db()
        if db is None or not hasattr(db, "set_voice_moderation_enabled"):
            return
        result = db.set_voice_moderation_enabled(guild_id, bool(value))
        if asyncio.iscoroutine(result):
            await result

    async def _update_settings(self, guild_id: int, **kwargs: Any) -> None:
        db = self._get_db()
        if db is None or not hasattr(db, "update_voice_moderation_settings"):
            return
        result = db.update_voice_moderation_settings(guild_id, **kwargs)
        if asyncio.iscoroutine(result):
            await result

    async def _apply_self_deaf(self, guild: discord.Guild, enabled: bool, *, channel=None) -> bool:
        target_channel = channel
        me = getattr(guild, "me", None)
        me_voice = getattr(me, "voice", None)
        if target_channel is None:
            target_channel = getattr(me_voice, "channel", None)
        if target_channel is None:
            return False
        desired = bool(enabled)
        for _ in range(3):
            try:
                current = bool(getattr(getattr(guild, "me", None), "voice", None) and getattr(getattr(guild, "me", None).voice, "self_deaf", False))
            except Exception:
                current = None
            if current == desired:
                return True
            try:
                await guild.change_voice_state(channel=target_channel, self_deaf=desired)
            except Exception:
                await asyncio.sleep(0.35)
                continue
            await asyncio.sleep(0.35)
            try:
                current = bool(getattr(getattr(guild, "me", None), "voice", None) and getattr(getattr(guild, "me", None).voice, "self_deaf", False))
            except Exception:
                current = None
            if current == desired:
                return True
        return False

    async def _connect_receive_client(self, guild: discord.Guild, target_channel) -> Optional[discord.VoiceClient]:
        vc = self._get_voice_client(guild)
        if vc is not None and getattr(vc, "is_connected", lambda: False)():
            try:
                if getattr(vc, "is_playing", lambda: False)():
                    vc.stop()
            except Exception:
                pass
            with contextlib.suppress(Exception):
                await vc.disconnect(force=True)

        connect_kwargs = {"self_deaf": False}
        if voice_recv is not None:
            connect_kwargs["cls"] = voice_recv.VoiceRecvClient

        try:
            return await target_channel.connect(**connect_kwargs)
        except TypeError:
            connect_kwargs.pop("cls", None)
            return await target_channel.connect(**connect_kwargs)
        except Exception:
            return None

    async def _ensure_receive_ready(self, guild: discord.Guild, preferred_channel=None, *, start_listening: bool = True) -> tuple[Optional[discord.VoiceClient], str]:
        lock = self._guild_lock(guild.id)
        async with lock:
            settings = await self._get_settings(guild.id)
            runtime = self._runtime.setdefault(guild.id, _GuildVoiceModerationRuntime())
            runtime.settings = dict(settings)

            vc = self._get_voice_client(guild)
            target_channel = getattr(vc, "channel", None) or preferred_channel
            if target_channel is None:
                return vc, "sem_canal"

            has_receive = voice_recv is not None
            is_receive_client = self._is_receive_client(vc)
            try:
                vc_busy = bool(vc and (getattr(vc, "is_playing", lambda: False)() or getattr(vc, "is_paused", lambda: False)()))
            except Exception:
                vc_busy = False

            should_force_reconnect = False
            if has_receive and (vc is None or not getattr(vc, "is_connected", lambda: False)() or not is_receive_client):
                should_force_reconnect = True

            if should_force_reconnect:
                if vc_busy:
                    return vc, "ocupado_playback"
                vc = await self._connect_receive_client(guild, target_channel)
                if vc is None:
                    return None, "falha_conectar"
                is_receive_client = self._is_receive_client(vc)

            if vc is not None and getattr(vc, "channel", None) is not None and preferred_channel is not None:
                current_channel = getattr(vc, "channel", None)
                if current_channel is not None and getattr(current_channel, "id", None) != getattr(preferred_channel, "id", None):
                    try:
                        await vc.move_to(preferred_channel)
                    except Exception:
                        pass

            await self._apply_self_deaf(guild, False, channel=getattr(vc, "channel", None) or preferred_channel)

            if voice_recv is None or vc is None or not hasattr(vc, "listen"):
                runtime.recover_fail_streak = 0
                return vc, "sem_voice_recv"

            if not start_listening:
                return vc, "pronto"

            try:
                if getattr(vc, "is_listening", lambda: False)():
                    runtime.recover_fail_streak = 0
                    return vc, "escutando"
            except Exception:
                pass

            try:
                if getattr(vc, "is_playing", lambda: False)() or getattr(vc, "is_paused", lambda: False)():
                    return vc, "ocupado_playback"
            except Exception:
                pass

            with contextlib.suppress(Exception):
                if hasattr(vc, "stop_listening") and getattr(vc, "is_listening", lambda: False)():
                    self._suppress_after_errors(guild.id, 2.0)
                    vc.stop_listening()
            await asyncio.sleep(0.08)

            listen_attempts = 2 if has_receive else 1
            last_error: Exception | None = None
            for attempt in range(listen_attempts):
                sink = _LoudDisconnectSink(self, guild.id)
                runtime.sink = sink
                try:
                    vc.listen(sink, after=lambda exc, guild_id=guild.id: self._on_listen_after(guild_id, exc))
                    await asyncio.sleep(0.18)
                    try:
                        if getattr(vc, "is_listening", lambda: False)():
                            runtime.recover_fail_streak = 0
                            runtime.last_recover_attempt_at = 0.0
                            return vc, "escutando"
                    except Exception:
                        pass
                except Exception as exc:
                    last_error = exc

                runtime.sink = None
                runtime.recover_fail_streak = int(runtime.recover_fail_streak or 0) + 1

                if attempt + 1 >= listen_attempts or vc_busy:
                    break

                vc = await self._connect_receive_client(guild, target_channel)
                if vc is None:
                    return None, "falha_conectar"
                is_receive_client = self._is_receive_client(vc)
                if not is_receive_client:
                    break
                await self._apply_self_deaf(guild, False, channel=getattr(vc, "channel", None) or preferred_channel)
                await asyncio.sleep(0.12)

            if last_error is not None:
                self._suppress_after_errors(guild.id, 3.0)
            return vc, "falha_escuta"

    async def _stop_listening(self, guild: discord.Guild) -> None:
        vc = self._get_voice_client(guild)
        if vc is not None and hasattr(vc, "stop_listening"):
            self._suppress_after_errors(guild.id, 3.5)
            with contextlib.suppress(Exception):
                vc.stop_listening()
        runtime = self._runtime.get(guild.id)
        if runtime is not None:
            runtime.sink = None

    async def _soft_restart_listening(self, guild: discord.Guild, vc: discord.VoiceClient | None = None, *, preferred_channel=None) -> tuple[Optional[discord.VoiceClient], str]:
        runtime = self._runtime.setdefault(guild.id, _GuildVoiceModerationRuntime())
        settings = await self._get_settings(guild.id)
        runtime.settings = dict(settings)
        if not settings.get("enabled"):
            return vc, "desativado"
        current_vc = vc or self._get_voice_client(guild)
        if current_vc is None or not getattr(current_vc, "is_connected", lambda: False)() or getattr(current_vc, "channel", None) is None:
            return current_vc, "sem_canal"
        if not self._is_receive_client(current_vc):
            return current_vc, "sem_receive"
        if self._is_voice_client_busy(current_vc):
            return current_vc, "ocupado_playback"

        self._suppress_after_errors(guild.id, 2.0)
        with contextlib.suppress(Exception):
            if hasattr(current_vc, "stop_listening"):
                current_vc.stop_listening()
        await asyncio.sleep(0.12)

        sink = _LoudDisconnectSink(self, guild.id)
        runtime.sink = sink
        try:
            current_vc.listen(sink, after=lambda exc, guild_id=guild.id: self._on_listen_after(guild_id, exc))
            await asyncio.sleep(0.18)
            if bool(getattr(current_vc, "is_listening", lambda: False)()):
                runtime.recover_fail_streak = 0
                runtime.last_recover_attempt_at = 0.0
                return current_vc, "escutando"
        except Exception:
            pass
        runtime.sink = None
        runtime.recover_fail_streak = int(runtime.recover_fail_streak or 0) + 1
        return current_vc, "falha_escuta"

    async def _hard_recover_receive_client(self, guild: discord.Guild, *, preferred_channel=None) -> tuple[Optional[discord.VoiceClient], str]:
        runtime = self._runtime.setdefault(guild.id, _GuildVoiceModerationRuntime())
        now = time.monotonic()
        if (now - float(runtime.last_hard_recover_at or 0.0)) < 45.0:
            return self._get_voice_client(guild), "cooldown_hard_recover"
        runtime.last_hard_recover_at = now
        runtime.last_recover_attempt_at = now
        vc, state = await self._ensure_receive_ready(guild, preferred_channel=preferred_channel, start_listening=True)
        if state == "escutando":
            runtime.recover_fail_streak = 0
        return vc, state

    def _is_corrupted_stream_error(self, exc: Exception | None) -> bool:
        if exc is None:
            return False
        message = str(exc).strip().lower()
        if not message:
            return False
        return "corrupted stream" in message or "opus" in message and "corrupt" in message

    def _is_recoverable_listen_error(self, exc: Exception | None) -> bool:
        if exc is None:
            return True
        message = str(exc).strip().lower()
        if not message:
            return True
        if self._is_corrupted_stream_error(exc):
            return True
        return "invalid argument" in message or "bad argument" in message

    async def _recover_listening_after_error(self, guild_id: int, exc: Exception | None) -> None:
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return

        runtime = self._runtime.setdefault(int(guild_id), _GuildVoiceModerationRuntime())
        runtime.sink = None
        settings = await self._get_settings(guild.id)
        runtime.settings = dict(settings)
        if not settings.get("enabled"):
            return

        vc = self._get_voice_client(guild)
        voice_channel = getattr(vc, "channel", None) if vc is not None else None
        if vc is not None and hasattr(vc, "stop_listening"):
            self._suppress_after_errors(guild.id, 3.0)
            with contextlib.suppress(Exception):
                vc.stop_listening()

        if self._is_recoverable_listen_error(exc):
            runtime.last_recover_attempt_at = time.monotonic()
            await asyncio.sleep(0.45)
            _vc, state = await self._soft_restart_listening(guild, vc, preferred_channel=voice_channel)
            if state in {"escutando", "sem_voice_recv", "ocupado_playback"}:
                runtime.recover_fail_streak = 0
                return
            if int(runtime.recover_fail_streak or 0) >= 4:
                await asyncio.sleep(0.4)
                _vc, hard_state = await self._hard_recover_receive_client(guild, preferred_channel=voice_channel)
                if hard_state in {"escutando", "sem_voice_recv", "ocupado_playback", "cooldown_hard_recover"}:
                    if hard_state == "escutando":
                        runtime.recover_fail_streak = 0
                    return
            self._suppress_after_errors(guild.id, 4.0)
            return

        now = time.monotonic()
        if (now - float(runtime.last_nonrecoverable_notice_at or 0.0)) < 12.0:
            return
        runtime.last_nonrecoverable_notice_at = now
        await self._send_call_notice(
            guild,
            title="# 🔊 Moderação de voz",
            lines=["A escuta do canal foi encerrada com erro."],
            notes=[f"Detalhe: `{exc}`"],
            accent=discord.Color.red(),
            voice_channel=voice_channel,
        )

    def _on_listen_after(self, guild_id: int, exc: Exception | None) -> None:
        runtime = self._runtime.get(int(guild_id))
        if runtime is not None:
            runtime.sink = None
            if float(runtime.suppress_after_until or 0.0) > time.monotonic():
                return
        if exc is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._recover_listening_after_error(int(guild_id), exc),
                self.bot.loop,
            )
        except Exception:
            pass

    async def _play_activation_sfx(self, guild: discord.Guild, vc: discord.VoiceClient | None = None) -> bool:
        voice_client = vc or self._get_voice_client(guild)
        if voice_client is None or not getattr(voice_client, "is_connected", lambda: False)():
            return False
        if not VOICE_MODERATION_SFX_PATH.exists():
            return False
        try:
            if voice_client.is_playing() or voice_client.is_paused():
                return False
        except Exception:
            pass
        try:
            source = discord.FFmpegPCMAudio(str(VOICE_MODERATION_SFX_PATH))
            voice_client.play(source)
            return True
        except Exception:
            return False

    def _register_loud_sample(self, guild_id: int, user_id: int, *, score: int, rms: int, peak: int) -> None:
        runtime = self._runtime.get(int(guild_id))
        settings = getattr(runtime, "settings", None) or {}
        if not settings.get("enabled") or not settings.get("disconnect_enabled", True):
            return
        if int(getattr(runtime, "tts_pause_depth", 0) or 0) > 0:
            with self._sample_lock:
                self._over_limit_windows.pop((int(guild_id), int(user_id)), None)
            return

        max_intensity = int(settings.get("max_intensity", VOICE_MODERATION_DEFAULTS["max_intensity"]) or VOICE_MODERATION_DEFAULTS["max_intensity"])
        threshold = int(settings.get("threshold_rms", VOICE_MODERATION_DEFAULTS["threshold_rms"]) or VOICE_MODERATION_DEFAULTS["threshold_rms"])
        cooldown_seconds = float(settings.get("cooldown_seconds", VOICE_MODERATION_DEFAULTS["cooldown_seconds"]) or VOICE_MODERATION_DEFAULTS["cooldown_seconds"])

        clipped_peak_only = peak >= 32760 and rms < max(2200, int(max_intensity * 0.42))
        intensity = max(score, int(rms * 1.18), int((rms * 0.90) + (peak * 0.12)), rms)
        if clipped_peak_only:
            intensity = max(score, int(rms * 1.16), rms)
        intensity = min(32768, int(intensity))

        if intensity < max(threshold, 900):
            with self._sample_lock:
                self._over_limit_windows.pop((int(guild_id), int(user_id)), None)
            return

        now = time.monotonic()
        key = (int(guild_id), int(user_id))
        should_disconnect = False
        chosen_score = intensity
        sustain_seconds_required = 2.0
        sustain_gap_reset = 0.35

        with self._sample_lock:
            last_disconnect = float(self._disconnect_cooldowns.get(key, 0.0) or 0.0)
            if intensity > max_intensity:
                start_at, last_seen_at, max_seen = self._over_limit_windows.get(key, (now, now, intensity))
                if now - float(last_seen_at or now) > sustain_gap_reset:
                    start_at = now
                    max_seen = intensity
                else:
                    max_seen = max(int(max_seen or intensity), intensity)
                last_seen_at = now
                self._over_limit_windows[key] = (start_at, last_seen_at, max_seen)
                if (now - last_disconnect) >= cooldown_seconds and (last_seen_at - start_at) >= sustain_seconds_required:
                    should_disconnect = True
                    chosen_score = min(32768, int(max_seen))
                    self._disconnect_cooldowns[key] = now
                    self._over_limit_windows.pop(key, None)
            else:
                self._over_limit_windows.pop(key, None)

        if should_disconnect:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._disconnect_member_for_volume(guild_id, user_id, chosen_score),
                    self.bot.loop,
                )
                future.add_done_callback(
                    lambda fut, gid=guild_id: self._schedule_call_notice(
                        gid,
                        title="# 🔊 Moderação de voz",
                        lines=["Falhei ao executar a desconexão automática."],
                        notes=[f"Detalhe: `{fut.exception()}`"],
                        accent=discord.Color.red(),
                    ) if fut.exception() else None
                )
            except Exception as e:
                self._schedule_call_notice(
                    guild_id,
                    title="# 🔊 Moderação de voz",
                    lines=["Falhei ao agendar a desconexão automática."],
                    notes=[f"Detalhe: `{e}`"],
                    accent=discord.Color.red(),
                )

    async def pause_for_tts_playback(self, guild: discord.Guild, vc: discord.VoiceClient | None = None) -> None:
        if guild is None:
            return
        should_stop_listening = False
        lock = self._guild_lock(guild.id)
        async with lock:
            runtime = self._runtime.setdefault(guild.id, _GuildVoiceModerationRuntime())
            runtime.tts_pause_depth = int(runtime.tts_pause_depth or 0) + 1
            if runtime.tts_pause_depth == 1:
                runtime.sink = None
                runtime.recover_fail_streak = 0
                should_stop_listening = True
        self._suppress_after_errors(guild.id, 8.0)
        if should_stop_listening:
            await self._stop_listening(guild)

    async def resume_after_tts_playback(self, guild: discord.Guild, vc: discord.VoiceClient | None = None) -> None:
        if guild is None:
            return
        should_resume = False
        should_restore_deaf = False
        lock = self._guild_lock(guild.id)
        async with lock:
            runtime = self._runtime.setdefault(guild.id, _GuildVoiceModerationRuntime())
            depth = max(0, int(runtime.tts_pause_depth or 0) - 1)
            runtime.tts_pause_depth = depth
            if depth == 0:
                settings = await self._get_settings(guild.id)
                runtime.settings = dict(settings)
                should_resume = bool(settings.get("enabled"))
                should_restore_deaf = not should_resume
        current_vc = self._get_voice_client(guild)
        if should_resume:
            await asyncio.sleep(0.25)
            await self.handle_voice_client_ready(guild, current_vc or vc)
        elif should_restore_deaf:
            current_vc = current_vc or vc
            if current_vc is not None and getattr(current_vc, "is_connected", lambda: False)() and getattr(current_vc, "channel", None) is not None:
                await self._apply_self_deaf(guild, True, channel=current_vc.channel)

    async def _disconnect_member_for_volume(self, guild_id: int, user_id: int, score: int) -> None:
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return
        settings = await self._get_settings(guild.id)
        if not settings.get("enabled") or not settings.get("disconnect_enabled", True):
            return

        member = guild.get_member(int(user_id))
        if member is None:
            with contextlib.suppress(Exception):
                member = await guild.fetch_member(int(user_id))
        if member is None or member.bot:
            return
        if member.voice is None or member.voice.channel is None:
            return

        vc = self._get_voice_client(guild)
        if vc is None or not getattr(vc, "is_connected", lambda: False)() or getattr(vc, "channel", None) is None:
            return
        if getattr(vc.channel, "id", None) != getattr(member.voice.channel, "id", None):
            return

        me = getattr(guild, "me", None) or guild.get_member(getattr(self.bot.user, "id", 0))
        if me is None:
            return

        channel_perms = member.voice.channel.permissions_for(me)
        perms = bool(getattr(getattr(me, "guild_permissions", None), "move_members", False) and getattr(channel_perms, "move_members", False))
        if not perms:
            await self._send_call_notice(
                guild,
                title="# 🔊 Moderação de voz",
                lines=["Não consigo desconectar ninguém nessa call."],
                notes=["Está faltando a permissão **Mover membros** para o bot."],
                accent=discord.Color.red(),
                voice_channel=member.voice.channel,
            )
            return
        try:
            await member.move_to(None, reason=f"Moderação de voz: volume acima do limite ({score})")
            await self._send_call_notice(
                guild,
                title="# 🔊 Moderação de voz",
                lines=[f"**{discord.utils.escape_markdown(member.display_name)}** foi desconectado da call por gritar alto demais."],
                notes=[f"Intensidade detectada: `{score}`"],
                accent=discord.Color.orange(),
                voice_channel=getattr(vc, "channel", None),
            )
        except Exception as e:
            await self._send_call_notice(
                guild,
                title="# 🔊 Moderação de voz",
                lines=[f"Falhei ao desconectar **{discord.utils.escape_markdown(member.display_name)}** da call."],
                notes=[f"Detalhe: `{e}`"],
                accent=discord.Color.red(),
                voice_channel=getattr(vc, "channel", None),
            )

    async def handle_voice_client_ready(self, guild: discord.Guild, vc: discord.VoiceClient | None = None) -> None:
        settings = await self._get_settings(guild.id)
        runtime = self._runtime.setdefault(guild.id, _GuildVoiceModerationRuntime())
        runtime.settings = dict(settings)
        if not settings.get("enabled"):
            await self._stop_listening(guild)
            if vc is None:
                vc = self._get_voice_client(guild)
            if vc is not None and getattr(vc, "is_connected", lambda: False)() and getattr(vc, "channel", None) is not None:
                await self._apply_self_deaf(guild, True, channel=vc.channel)
            return

        if int(getattr(runtime, "tts_pause_depth", 0) or 0) > 0:
            return

        target_channel = getattr(vc, "channel", None) if vc is not None else None
        await self._ensure_receive_ready(guild, preferred_channel=target_channel)

    async def _enable_mode(self, guild: discord.Guild, preferred_channel=None) -> tuple[str, bool]:
        await self._set_enabled(guild.id, True)
        settings = await self._get_settings(guild.id)
        runtime = self._runtime.setdefault(guild.id, _GuildVoiceModerationRuntime())
        runtime.settings = dict(settings)

        existing_vc = self._get_voice_client(guild)
        if self._is_voice_client_busy(existing_vc):
            self._suppress_after_errors(guild.id, 8.0)
            return "ocupado_playback", False

        vc, state = await self._ensure_receive_ready(guild, preferred_channel=preferred_channel, start_listening=False)
        played = False
        if vc is not None and not self._is_voice_client_busy(vc):
            played = await self._play_activation_sfx(guild, vc)
            if played:
                for _ in range(50):
                    try:
                        if not vc.is_playing() and not vc.is_paused():
                            break
                    except Exception:
                        break
                    await asyncio.sleep(0.1)
        vc, listen_state = await self._ensure_receive_ready(guild, preferred_channel=getattr(vc, "channel", None) or preferred_channel, start_listening=True)
        if listen_state == "falha_escuta" and vc is not None and not self._is_voice_client_busy(vc):
            await asyncio.sleep(0.35)
            vc, retry_state = await self._ensure_receive_ready(guild, preferred_channel=getattr(vc, "channel", None) or preferred_channel, start_listening=True)
            if retry_state in {"escutando", "sem_voice_recv", "ocupado_playback"}:
                listen_state = retry_state
        if listen_state in {"escutando", "sem_voice_recv"}:
            state = listen_state
        elif state not in {"falha_conectar", "sem_canal"}:
            state = listen_state
        return state, played

    async def _disable_mode(self, guild: discord.Guild) -> None:
        await self._set_enabled(guild.id, False)
        runtime = self._runtime.setdefault(guild.id, _GuildVoiceModerationRuntime())
        settings = await self._get_settings(guild.id)
        runtime.settings = dict(settings)
        vc = self._get_voice_client(guild)
        if self._is_voice_client_busy(vc):
            self._suppress_after_errors(guild.id, 8.0)
            return
        await self._stop_listening(guild)
        if vc is not None and getattr(vc, "is_connected", lambda: False)() and getattr(vc, "channel", None) is not None:
            await self._apply_self_deaf(guild, True, channel=vc.channel)

    def _sensitivity_label(self, settings: dict[str, Any]) -> str:
        threshold = int(settings.get("threshold_rms", VOICE_MODERATION_DEFAULTS["threshold_rms"]) or VOICE_MODERATION_DEFAULTS["threshold_rms"])
        if threshold <= 1300:
            return "alta"
        if threshold <= 2300:
            return "média"
        return "baixa"

    def _status_snapshot(self, settings: dict[str, Any], guild: discord.Guild) -> tuple[list[str], list[str], discord.Color]:
        vc = self._get_voice_client(guild)
        connected = bool(vc and getattr(vc, "is_connected", lambda: False)())
        channel_name = getattr(getattr(vc, "channel", None), "name", None) or "desconectado"
        listening = bool(vc and hasattr(vc, "is_listening") and getattr(vc, "is_listening", lambda: False)())
        self_deaf = bool(getattr(getattr(getattr(guild, "me", None), "voice", None), "self_deaf", False))
        enabled = bool(settings.get("enabled"))

        lines = [
            f"**Modo:** {'ativado' if enabled else 'desativado'}",
            f"**Canal:** {channel_name if connected else 'desconectado'}",
            f"**Escuta:** {'ativa' if listening else 'inativa'}",
            f"**Ensurdecido:** {'não' if enabled and connected else ('sim' if self_deaf else 'não')}",
            f"**Sensibilidade:** {self._sensitivity_label(settings)}",
            f"**Intensidade máxima:** {int(settings.get('max_intensity', VOICE_MODERATION_DEFAULTS['max_intensity']) or VOICE_MODERATION_DEFAULTS['max_intensity'])}",
        ]

        notes: list[str] = []
        if enabled:
            if not connected:
                notes.append("Vou aplicar a escuta assim que o bot entrar em um canal de voz.")
            elif voice_recv is None:
                notes.append("A extensão de voice receive não está ativa, então o bot só sai do ensurdecido por enquanto.")
            elif not listening:
                try:
                    busy = bool(vc and (getattr(vc, "is_playing", lambda: False)() or getattr(vc, "is_paused", lambda: False)()))
                except Exception:
                    busy = False
                if busy:
                    notes.append("A escuta retoma automaticamente quando o áudio atual terminar.")
                elif int(getattr(runtime := self._runtime.get(guild.id), "recover_fail_streak", 0) or 0) >= 2:
                    notes.append("A escuta caiu e o bot está tentando estabilizar automaticamente.")
                else:
                    notes.append("O modo foi ativado, mas a escuta ainda não iniciou direito.")
        accent = discord.Color.green() if enabled else discord.Color.red()
        return lines, notes, accent

    def _build_notice_panel(self, *, title: str, lines: list[str], notes: list[str] | None = None, accent: discord.Color | None = None) -> _VoiceModerationStatusView:
        return _VoiceModerationStatusView(title=title, lines=lines, notes=notes or [], accent=accent or discord.Color.blurple())

    def _build_command_panel(self, guild: discord.Guild, *, title: str, lines: list[str], notes: list[str] | None = None, accent: discord.Color | None = None) -> _VoiceModerationCommandView:
        return _VoiceModerationCommandView(self, guild, title=title, lines=lines, notes=notes or [], accent=accent or discord.Color.blurple())

    async def _send_panel(
        self,
        ctx: commands.Context,
        *,
        title: str,
        lines: list[str],
        notes: list[str] | None = None,
        accent: discord.Color | None = None,
    ) -> None:
        view = self._build_command_panel(ctx.guild, title=title, lines=lines, notes=notes, accent=accent)
        message = await ctx.send(view=view)
        view.message = message

    @commands.command(name="modvoz", aliases=["voicemod", "voiceguard"])
    @commands.guild_only()
    async def voice_moderation_command(self, ctx: commands.Context, *ignored_tokens: str):
        member = getattr(ctx, "author", None)
        if not self._can_manage_mode(member):
            await self._send_panel(
                ctx,
                title="# 🔊 Moderação de voz",
                lines=["Você precisa de **Administrador** ou **Desconectar membros** para usar este comando."],
                accent=discord.Color.red(),
            )
            return

        guild = ctx.guild
        self._remember_notice_channel(guild.id, getattr(ctx.channel, "id", None))
        settings = await self._get_settings(guild.id)
        preferred_channel = getattr(getattr(member, "voice", None), "channel", None)

        if settings.get("enabled"):
            await self._disable_mode(guild)
            final_settings = await self._get_settings(guild.id)
            lines, notes, accent = self._status_snapshot(final_settings, guild)
            await self._send_panel(
                ctx,
                title="# 🔊 Moderação de voz",
                lines=lines,
                notes=notes,
                accent=accent,
            )
            return

        state, played = await self._enable_mode(guild, preferred_channel=preferred_channel)
        if state == "falha_escuta":
            await asyncio.sleep(0.45)
            await self.handle_voice_client_ready(guild, self._get_voice_client(guild))
        final_settings = await self._get_settings(guild.id)
        lines, notes, accent = self._status_snapshot(final_settings, guild)
        extra: list[str] = []
        if state == "sem_canal":
            extra.append("O bot vai começar a escutar assim que entrar em call.")
        elif state == "sem_voice_recv":
            extra.append("O bot saiu do ensurdecido, mas a detecção avançada depende da extensão de voice receive.")
        elif state == "falha_escuta":
            extra.append("O modo foi ativado, mas a escuta ainda não conseguiu iniciar.")
        elif state == "ocupado_playback":
            extra.append("O áudio atual termina primeiro; depois a escuta volta sozinha.")
        elif state == "falha_conectar":
            extra.append("Não consegui conectar o bot no canal agora.")
        if played:
            extra.append("Som de ativação tocado.")
        if extra:
            notes.extend(extra)
        await self._send_panel(
            ctx,
            title="# 🔊 Moderação de voz",
            lines=lines,
            notes=notes,
            accent=accent,
        )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        me = getattr(guild, "me", None)
        if me is None or member.id != me.id:
            return
        settings = await self._get_settings(guild.id)
        if after.channel is None:
            await self._stop_listening(guild)
            return
        if settings.get("enabled"):
            await self.handle_voice_client_ready(guild, self._get_voice_client(guild))
        else:
            await self._apply_self_deaf(guild, True, channel=after.channel)


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceModeration(bot))
