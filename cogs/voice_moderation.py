from __future__ import annotations

import asyncio
import audioop
import contextlib
import time
import threading
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


@dataclass
class _GuildVoiceModerationRuntime:
    sink: Any | None = None
    settings: dict[str, Any] | None = None


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
            except Exception:
                return
            self.cog._register_loud_sample(self.guild_id, int(user.id), rms)

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
        self._loud_hits: dict[tuple[int, int], deque[float]] = {}
        self._disconnect_cooldowns: dict[tuple[int, int], float] = {}
        self._sample_lock = threading.Lock()

    async def cog_load(self):
        for vc in list(getattr(self.bot, "voice_clients", []) or []):
            guild = getattr(vc, "guild", None)
            if guild is not None:
                asyncio.create_task(self.handle_voice_client_ready(guild, vc))

    def _get_db(self):
        return getattr(self.bot, "settings_db", None)

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(int(guild_id))
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[int(guild_id)] = lock
        return lock

    def _is_admin(self, member: discord.Member | None) -> bool:
        if member is None:
            return False
        perms = getattr(member, "guild_permissions", None)
        if perms is None:
            return False
        return bool(getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False))

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

    async def _get_settings(self, guild_id: int) -> dict[str, Any]:
        db = self._get_db()
        default = {
            "enabled": False,
            "disconnect_enabled": True,
            "threshold_rms": 4500,
            "hits_to_trigger": 3,
            "window_seconds": 1.2,
            "cooldown_seconds": 12.0,
        }
        if db is None or not hasattr(db, "get_voice_moderation_settings"):
            return dict(default)
        try:
            data = db.get_voice_moderation_settings(guild_id)
            if asyncio.iscoroutine(data):
                data = await data
            if not isinstance(data, dict):
                return dict(default)
            merged = dict(default)
            merged.update(data)
            return merged
        except Exception:
            return dict(default)

    async def _set_enabled(self, guild_id: int, value: bool) -> None:
        db = self._get_db()
        if db is None or not hasattr(db, "set_voice_moderation_enabled"):
            return
        result = db.set_voice_moderation_enabled(guild_id, bool(value))
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

    async def _ensure_receive_ready(self, guild: discord.Guild, preferred_channel=None) -> tuple[Optional[discord.VoiceClient], str]:
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
            is_receive_client = bool(vc and hasattr(vc, "listen") and hasattr(vc, "is_listening"))

            if has_receive and (vc is None or not getattr(vc, "is_connected", lambda: False)() or not is_receive_client):
                vc = await self._connect_receive_client(guild, target_channel)
                if vc is None:
                    return None, "falha_conectar"

            if vc is not None and getattr(vc, "channel", None) is not None and preferred_channel is not None:
                current_channel = getattr(vc, "channel", None)
                if current_channel is not None and getattr(current_channel, "id", None) != getattr(preferred_channel, "id", None):
                    try:
                        await vc.move_to(preferred_channel)
                    except Exception:
                        pass

            await self._apply_self_deaf(guild, False, channel=getattr(vc, "channel", None) or preferred_channel)

            if voice_recv is None or vc is None or not hasattr(vc, "listen"):
                return vc, "sem_voice_recv"

            try:
                if getattr(vc, "is_listening", lambda: False)():
                    return vc, "escutando"
            except Exception:
                pass

            sink = _LoudDisconnectSink(self, guild.id)
            runtime.sink = sink
            try:
                vc.listen(sink, after=lambda exc, guild_id=guild.id: self._on_listen_after(guild_id, exc))
                return vc, "escutando"
            except Exception:
                runtime.sink = None
                return vc, "falha_escuta"

    async def _stop_listening(self, guild: discord.Guild) -> None:
        vc = self._get_voice_client(guild)
        if vc is not None and hasattr(vc, "stop_listening"):
            with contextlib.suppress(Exception):
                vc.stop_listening()
        runtime = self._runtime.get(guild.id)
        if runtime is not None:
            runtime.sink = None

    def _on_listen_after(self, guild_id: int, exc: Exception | None) -> None:
        runtime = self._runtime.get(int(guild_id))
        if runtime is not None:
            runtime.sink = None
        if exc is not None:
            print(f"[voice_moderation] escuta finalizada com erro | guild={guild_id} erro={exc}")

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

    def _register_loud_sample(self, guild_id: int, user_id: int, rms: int) -> None:
        runtime = self._runtime.get(int(guild_id))
        settings = getattr(runtime, "settings", None) or {}
        if not settings.get("enabled") or not settings.get("disconnect_enabled", True):
            return
        threshold = int(settings.get("threshold_rms", 4500) or 4500)
        if rms < threshold:
            return

        now = time.monotonic()
        window = float(settings.get("window_seconds", 1.2) or 1.2)
        hits_to_trigger = int(settings.get("hits_to_trigger", 3) or 3)
        cooldown_seconds = float(settings.get("cooldown_seconds", 12.0) or 12.0)
        key = (int(guild_id), int(user_id))

        should_disconnect = False
        with self._sample_lock:
            samples = self._loud_hits.get(key)
            if samples is None:
                samples = deque()
                self._loud_hits[key] = samples
            samples.append(now)
            while samples and now - samples[0] > window:
                samples.popleft()

            last_disconnect = float(self._disconnect_cooldowns.get(key, 0.0) or 0.0)
            if len(samples) >= hits_to_trigger and now - last_disconnect >= cooldown_seconds:
                self._disconnect_cooldowns[key] = now
                samples.clear()
                should_disconnect = True

        if should_disconnect:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._disconnect_member_for_volume(guild_id, user_id, rms),
                    self.bot.loop,
                )
            except Exception:
                pass

    async def _disconnect_member_for_volume(self, guild_id: int, user_id: int, rms: int) -> None:
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return
        settings = await self._get_settings(guild.id)
        if not settings.get("enabled") or not settings.get("disconnect_enabled", True):
            return

        member = guild.get_member(int(user_id))
        if member is None or member.bot:
            return
        if member.voice is None or member.voice.channel is None:
            return

        vc = self._get_voice_client(guild)
        if vc is None or not getattr(vc, "is_connected", lambda: False)() or getattr(vc, "channel", None) is None:
            return
        if getattr(vc.channel, "id", None) != getattr(member.voice.channel, "id", None):
            return

        me = getattr(guild, "me", None)
        perms = getattr(getattr(me, "guild_permissions", None), "move_members", False)
        if not perms:
            return

        try:
            await member.move_to(None, reason=f"Moderação de voz: volume acima do limite ({rms})")
        except Exception as e:
            print(f"[voice_moderation] falha ao desconectar membro por volume | guild={guild.id} user={user_id} erro={e}")

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

        target_channel = getattr(vc, "channel", None) if vc is not None else None
        await self._ensure_receive_ready(guild, preferred_channel=target_channel)

    async def _enable_mode(self, guild: discord.Guild, preferred_channel=None) -> tuple[str, bool]:
        await self._set_enabled(guild.id, True)
        settings = await self._get_settings(guild.id)
        runtime = self._runtime.setdefault(guild.id, _GuildVoiceModerationRuntime())
        runtime.settings = dict(settings)
        vc, state = await self._ensure_receive_ready(guild, preferred_channel=preferred_channel)
        played = False
        if vc is not None:
            played = await self._play_activation_sfx(guild, vc)
        return state, played

    async def _disable_mode(self, guild: discord.Guild) -> None:
        await self._set_enabled(guild.id, False)
        runtime = self._runtime.setdefault(guild.id, _GuildVoiceModerationRuntime())
        settings = await self._get_settings(guild.id)
        runtime.settings = dict(settings)
        await self._stop_listening(guild)
        vc = self._get_voice_client(guild)
        if vc is not None and getattr(vc, "is_connected", lambda: False)() and getattr(vc, "channel", None) is not None:
            await self._apply_self_deaf(guild, True, channel=vc.channel)

    def _status_text(self, settings: dict[str, Any], guild: discord.Guild) -> str:
        vc = self._get_voice_client(guild)
        connected = bool(vc and getattr(vc, "is_connected", lambda: False)())
        channel_name = getattr(getattr(vc, "channel", None), "name", None) or "nenhum"
        listening = bool(vc and hasattr(vc, "is_listening") and getattr(vc, "is_listening", lambda: False)())
        self_deaf = bool(getattr(getattr(getattr(guild, "me", None), "voice", None), "self_deaf", False))
        return (
            f"modo: {'ativado' if settings.get('enabled') else 'desativado'}\n"
            f"canal: {channel_name if connected else 'desconectado'}\n"
            f"escuta: {'ativa' if listening else 'inativa'}\n"
            f"ensurdecido: {'sim' if self_deaf else 'não'}\n"
            f"limite rms: {int(settings.get('threshold_rms', 4500) or 4500)}"
        )

    @commands.command(name="modvoz", aliases=["voicemod", "voiceguard"])
    @commands.guild_only()
    async def voice_moderation_command(self, ctx: commands.Context, action: str | None = None):
        if not self._is_admin(getattr(ctx, "author", None)):
            await ctx.send("Só administradores podem controlar a moderação de voz.")
            return

        guild = ctx.guild
        normalized = str(action or "status").strip().lower()
        preferred_channel = getattr(getattr(ctx.author, "voice", None), "channel", None)

        if normalized in {"on", "ativar", "enable", "ligar"}:
            state, played = await self._enable_mode(guild, preferred_channel=preferred_channel)
            extra = []
            if state == "sem_canal":
                extra.append("vou aplicar assim que o bot entrar em call.")
            elif state == "sem_voice_recv":
                extra.append("o bot saiu do ensurdecido, mas a escuta avançada depende da extensão de voice receive instalada.")
            elif state == "falha_escuta":
                extra.append("o modo foi ativado, mas a escuta não conseguiu iniciar ainda.")
            elif state == "falha_conectar":
                extra.append("não consegui conectar o bot no canal agora.")
            if played:
                extra.append("som de ativação tocado.")
            message = "modo de moderação de voz ativado."
            if extra:
                message = f"{message} {' '.join(extra)}"
            await ctx.send(message)
            return

        if normalized in {"off", "desativar", "disable", "desligar"}:
            await self._disable_mode(guild)
            await ctx.send("modo de moderação de voz desativado.")
            return

        settings = await self._get_settings(guild.id)
        await ctx.send(self._status_text(settings, guild))

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
