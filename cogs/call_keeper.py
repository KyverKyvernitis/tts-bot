from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger(__name__)


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return default


CALLKEEPER_GUILD_ID = _safe_int(getattr(config, "CALLKEEPER_GUILD_ID", 0), 0)
_CALLKEEPER_GROUP_KWARGS = {
    "name": "callkeeper",
    "description": "Mantém uma call ocupada com bots auxiliares.",
    "default_permissions": discord.Permissions(manage_guild=True),
}
if CALLKEEPER_GUILD_ID > 0:
    _CALLKEEPER_GROUP_KWARGS["guild_ids"] = [CALLKEEPER_GUILD_ID]


class _AuxVoiceClient(discord.Client):
    """Client mínimo usado pelos bots auxiliares.

    Não carrega cogs, comandos, message_content ou members. Ele só precisa de
    guilds + voice_states para conectar na call e avisar o manager quando o
    próprio estado de voz muda.
    """

    def __init__(self, slot: "_AuxSlot", manager: "CallKeeper"):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents, heartbeat_timeout=60.0)
        self.slot = slot
        self.manager = manager
        self.ready_event = asyncio.Event()

    async def on_ready(self):
        self.ready_event.set()
        self.slot.last_error = None
        user = self.user
        log.info(
            "[callkeeper] aux %s pronto: %s (%s)",
            self.slot.index,
            getattr(user, "name", "desconhecido"),
            getattr(user, "id", "?"),
        )
        self.manager._schedule_reconcile("aux_ready", delay=0.05)

    async def on_resumed(self):
        self.manager._schedule_reconcile("aux_resumed", delay=0.05)

    async def on_disconnect(self):
        self.manager._schedule_reconcile("aux_gateway_disconnect", delay=0.25)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        user = self.user
        if user is not None and int(member.id) == int(user.id):
            await self.manager._on_aux_voice_state(self.slot, before, after)
            return

        guild = getattr(member, "guild", None)
        if guild and int(guild.id) == int(self.manager.guild_id):
            before_id = getattr(getattr(before, "channel", None), "id", 0) or 0
            after_id = getattr(getattr(after, "channel", None), "id", 0) or 0
            target_id = await self.manager._get_target_channel_id()
            if target_id and (int(before_id) == target_id or int(after_id) == target_id):
                self.manager._schedule_reconcile("aux_seen_voice_update", delay=self.manager.event_debounce)


@dataclass(eq=False)
class _AuxSlot:
    index: int
    token: str
    manager: "CallKeeper"
    client: Optional[_AuxVoiceClient] = None
    task: Optional[asyncio.Task] = None
    connect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    blocked_until: float = 0.0
    intentional_until: float = 0.0
    last_error: Optional[str] = None

    @property
    def label(self) -> str:
        return f"Aux {self.index}"

    def is_ready(self) -> bool:
        return bool(self.client and not self.client.is_closed() and self.client.ready_event.is_set())

    def is_running(self) -> bool:
        return bool(self.client and not self.client.is_closed() and self.task and not self.task.done())

    def user_id(self) -> int:
        user = getattr(self.client, "user", None)
        return int(getattr(user, "id", 0) or 0)

    def display_name(self) -> str:
        user = getattr(self.client, "user", None)
        if user is None:
            return self.label
        return f"{user.name} ({user.id})"

    def voice_client(self) -> Optional[discord.VoiceClient]:
        client = self.client
        if client is None:
            return None
        for voice_client in list(getattr(client, "voice_clients", []) or []):
            if isinstance(voice_client, discord.VoiceClient):
                return voice_client
        return None

    def voice_channel_id(self) -> int:
        voice_client = self.voice_client()
        if voice_client is None or not voice_client.is_connected():
            return 0
        channel = getattr(voice_client, "channel", None)
        return int(getattr(channel, "id", 0) or 0)

    async def ensure_started(self) -> None:
        if not self.token:
            self.last_error = "token ausente"
            return
        if self.is_running():
            return

        if self.client and not self.client.is_closed():
            with contextlib.suppress(Exception):
                await self.client.close()

        self.client = _AuxVoiceClient(self, self.manager)
        self.task = asyncio.create_task(self._runner(), name=f"callkeeper-aux-{self.index}")

    async def _runner(self) -> None:
        assert self.client is not None
        try:
            await self.client.start(self.token, reconnect=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("[callkeeper] aux %s parou com erro", self.index)
        finally:
            self.manager._schedule_reconcile("aux_runner_finished", delay=1.0)

    async def close(self) -> None:
        self.intentional_until = time.monotonic() + 10.0
        if self.client is not None:
            with contextlib.suppress(Exception):
                for voice_client in list(self.client.voice_clients):
                    await voice_client.disconnect(force=True)
            with contextlib.suppress(Exception):
                await self.client.close()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(Exception):
                await self.task
        self.task = None
        self.client = None


class CallKeeper(commands.Cog):
    """Mantém uma call ocupada com até 3 bots auxiliares.

    Regras pedidas:
    - 0 ou 1 membro não-CallKeeper: 3 bots
    - 2 membros não-CallKeeper: 2 bots
    - 3 membros não-CallKeeper: 1 bot
    - 4+ membros não-CallKeeper: 0 bots

    Importante: a contagem ignora apenas os 3 bots auxiliares desta cog.
    Outros bots normais na call, como music bots, também contam como membros.

    Quando um bot cai, o evento de voice_state_update agenda correção imediata.
    Se houver outro bot livre, ele assume antes do bot caído. Se os 3 já eram
    necessários, não existe reserva: o próprio bot caído reconecta o mais rápido
    possível.
    """

    callkeeper = app_commands.Group(**_CALLKEEPER_GROUP_KWARGS)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_id = CALLKEEPER_GUILD_ID
        self.config_channel_id = _safe_int(getattr(config, "CALLKEEPER_CHANNEL_ID", 0), 0)
        self.tokens = list(getattr(config, "CALLKEEPER_BOT_TOKENS", []) or [])[:3]
        self.watchdog_interval = max(
            0.25,
            float(getattr(config, "CALLKEEPER_WATCHDOG_INTERVAL_SECONDS", 1.0) or 1.0),
        )
        self.event_debounce = max(
            0.05,
            float(getattr(config, "CALLKEEPER_EVENT_DEBOUNCE_SECONDS", 0.20) or 0.20),
        )
        self.disconnect_cooldown = max(
            0.0,
            float(getattr(config, "CALLKEEPER_DISCONNECTED_BOT_COOLDOWN_SECONDS", 3.0) or 3.0),
        )
        self.slots: list[_AuxSlot] = [
            _AuxSlot(index=i + 1, token=token, manager=self)
            for i, token in enumerate(self.tokens)
        ]
        self._reconcile_lock = asyncio.Lock()
        self._scheduled_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._boot_task: Optional[asyncio.Task] = None
        self._last_reconcile_reason = "boot"

    async def cog_load(self) -> None:
        if self.guild_id <= 0:
            log.warning("[callkeeper] CALLKEEPER_GUILD_ID não configurado; cog carregada desativada")
            return
        if len(self.slots) < 3:
            log.warning(
                "[callkeeper] apenas %s/3 tokens configurados. Configure CALLKEEPER_BOT_1_TOKEN..3_TOKEN.",
                len(self.slots),
            )
        self._boot_task = asyncio.create_task(self._after_bot_ready(), name="callkeeper-boot")

    def cog_unload(self) -> None:
        for task in (self._boot_task, self._watchdog_task, self._scheduled_task):
            if task and not task.done():
                task.cancel()
        for slot in list(self.slots):
            asyncio.create_task(slot.close())

    async def _after_bot_ready(self) -> None:
        try:
            await self.bot.wait_until_ready()
            if await self._is_enabled():
                await self._ensure_aux_clients_started()
                self._start_watchdog()
                await self._reconcile("boot_restore")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[callkeeper] erro no boot restore")

    def _db(self):
        return getattr(self.bot, "settings_db", None)

    async def _is_enabled(self) -> bool:
        if self.guild_id <= 0:
            return False
        db = self._db()
        if db is not None and hasattr(db, "get_callkeeper_enabled"):
            try:
                return bool(db.get_callkeeper_enabled(self.guild_id))
            except Exception:
                log.exception("[callkeeper] falha lendo enabled no DB")
        return False

    async def _set_enabled(self, value: bool) -> None:
        db = self._db()
        if db is not None and hasattr(db, "set_callkeeper_enabled"):
            await db.set_callkeeper_enabled(self.guild_id, bool(value))

    async def _get_target_channel_id(self) -> int:
        if self.guild_id <= 0:
            return 0
        db = self._db()
        if db is not None and hasattr(db, "get_callkeeper_channel_id"):
            try:
                saved = int(db.get_callkeeper_channel_id(self.guild_id) or 0)
                if saved > 0:
                    return saved
            except Exception:
                log.exception("[callkeeper] falha lendo channel_id no DB")
        return max(0, int(self.config_channel_id or 0))

    async def _set_target_channel_id(self, channel_id: int) -> None:
        db = self._db()
        if db is not None and hasattr(db, "set_callkeeper_channel_id"):
            await db.set_callkeeper_channel_id(self.guild_id, int(channel_id))

    async def _guard(self, interaction: discord.Interaction, *, require_tokens: bool = False) -> bool:
        if self.guild_id <= 0:
            await interaction.response.send_message(
                "CALLKEEPER_GUILD_ID não está configurado no `.env`.",
                ephemeral=True,
            )
            return False
        if interaction.guild is None or int(interaction.guild.id) != int(self.guild_id):
            await interaction.response.send_message(
                "Esse comando só funciona no servidor configurado para o CallKeeper.",
                ephemeral=True,
            )
            return False
        perms = getattr(getattr(interaction, "user", None), "guild_permissions", None)
        if not bool(getattr(perms, "manage_guild", False) or getattr(perms, "administrator", False)):
            await interaction.response.send_message(
                "Você precisa de **Gerenciar Servidor** para usar o CallKeeper.",
                ephemeral=True,
            )
            return False
        if require_tokens and len(self.tokens) < 3:
            await interaction.response.send_message(
                "Configure os 3 tokens: `CALLKEEPER_BOT_1_TOKEN`, `CALLKEEPER_BOT_2_TOKEN` e `CALLKEEPER_BOT_3_TOKEN`.",
                ephemeral=True,
            )
            return False
        return True

    def _start_watchdog(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            return
        self._watchdog_task = asyncio.create_task(self._watchdog_loop(), name="callkeeper-watchdog")

    async def _watchdog_loop(self) -> None:
        while not self.bot.is_closed():
            try:
                if await self._is_enabled():
                    await self._reconcile("watchdog")
                else:
                    break
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[callkeeper] erro no watchdog")
            await asyncio.sleep(self.watchdog_interval)

    async def _ensure_aux_clients_started(self) -> None:
        for slot in self.slots:
            await slot.ensure_started()

    def _schedule_reconcile(self, reason: str, *, delay: float | None = None) -> None:
        if self.bot.is_closed():
            return
        self._last_reconcile_reason = reason
        if self._scheduled_task and not self._scheduled_task.done():
            return
        self._scheduled_task = asyncio.create_task(
            self._delayed_reconcile(reason, self.event_debounce if delay is None else delay),
            name="callkeeper-reconcile-event",
        )

    async def _delayed_reconcile(self, reason: str, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            if await self._is_enabled():
                await self._reconcile(reason)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[callkeeper] erro no reconcile agendado")
        finally:
            self._scheduled_task = None

    async def _on_aux_voice_state(
        self,
        slot: _AuxSlot,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        before_id = int(getattr(getattr(before, "channel", None), "id", 0) or 0)
        after_id = int(getattr(getattr(after, "channel", None), "id", 0) or 0)
        target_id = await self._get_target_channel_id()
        if target_id and before_id == target_id and after_id != target_id:
            now = time.monotonic()
            # Se a saída foi externa, dá prioridade a outro bot livre. Se não
            # houver reserva, o reconcile ignora o cooldown e reconecta o mesmo.
            if now > slot.intentional_until:
                slot.blocked_until = now + self.disconnect_cooldown
                log.warning("[callkeeper] %s saiu da call alvo; procurando substituto", slot.label)
        if target_id and (before_id == target_id or after_id == target_id):
            self._schedule_reconcile("aux_voice_state", delay=0.05)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if self.guild_id <= 0:
            return
        guild = getattr(member, "guild", None)
        if guild is None or int(guild.id) != int(self.guild_id):
            return
        target_id = await self._get_target_channel_id()
        if target_id <= 0:
            return
        before_id = int(getattr(getattr(before, "channel", None), "id", 0) or 0)
        after_id = int(getattr(getattr(after, "channel", None), "id", 0) or 0)
        if before_id == target_id or after_id == target_id:
            self._schedule_reconcile("main_voice_state", delay=self.event_debounce)

    def _required_bots_for_member_count(self, member_count: int) -> int:
        member_count = max(0, int(member_count or 0))
        if member_count <= 1:
            return 3
        if member_count == 2:
            return 2
        if member_count == 3:
            return 1
        return 0

    def _main_guild(self) -> Optional[discord.Guild]:
        if self.guild_id <= 0:
            return None
        return self.bot.get_guild(self.guild_id)

    async def _target_channel(self) -> Optional[discord.VoiceChannel]:
        channel_id = await self._get_target_channel_id()
        if channel_id <= 0:
            return None
        guild = self._main_guild()
        if guild is None:
            return None
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.VoiceChannel):
            return channel
        fetched = self.bot.get_channel(channel_id)
        if isinstance(fetched, discord.VoiceChannel):
            return fetched
        return None

    def _aux_user_ids(self) -> set[int]:
        return {slot.user_id() for slot in self.slots if slot.user_id()}

    async def _member_count_without_callkeeper_bots(self) -> int:
        channel = await self._target_channel()
        if channel is None:
            return 0

        aux_ids = self._aux_user_ids()
        count = 0
        for member in list(channel.members):
            # Ignora somente os 3 bots auxiliares gerenciados pelo CallKeeper.
            # Outros bots normais da call contam como membros para a regra
            # 0/1 -> 3 bots, 2 -> 2 bots, 3 -> 1 bot, 4+ -> 0 bots.
            if int(member.id) in aux_ids:
                continue
            count += 1
        return count

    async def _aux_channel_for_slot(self, slot: _AuxSlot, channel_id: int) -> Optional[discord.VoiceChannel]:
        client = slot.client
        if client is None or not slot.is_ready():
            return None
        channel = client.get_channel(channel_id)
        if isinstance(channel, discord.VoiceChannel):
            return channel
        guild = client.get_guild(self.guild_id)
        if guild is not None:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.VoiceChannel):
                return channel
        try:
            fetched = await client.fetch_channel(channel_id)
        except Exception as exc:
            slot.last_error = f"fetch_channel: {type(exc).__name__}: {exc}"
            return None
        if isinstance(fetched, discord.VoiceChannel):
            return fetched
        return None

    async def _connect_slot(self, slot: _AuxSlot, channel_id: int) -> bool:
        async with slot.connect_lock:
            await slot.ensure_started()
            if slot.client is None:
                return False
            try:
                await asyncio.wait_for(slot.client.ready_event.wait(), timeout=12.0)
            except asyncio.TimeoutError:
                slot.last_error = "gateway ainda não ficou ready"
                return False

            current_vc = slot.voice_client()
            if current_vc is not None and current_vc.is_connected():
                current_channel_id = int(getattr(getattr(current_vc, "channel", None), "id", 0) or 0)
                if current_channel_id == int(channel_id):
                    await self._enforce_self_mute_deaf(slot)
                    return True
                slot.intentional_until = time.monotonic() + 3.0
                with contextlib.suppress(Exception):
                    await current_vc.disconnect(force=True)

            channel = await self._aux_channel_for_slot(slot, channel_id)
            if channel is None:
                return False

            try:
                await channel.connect(
                    timeout=10.0,
                    reconnect=True,
                    self_mute=True,
                    self_deaf=True,
                )
                slot.blocked_until = 0.0
                slot.last_error = None
                await self._enforce_self_mute_deaf(slot)
                log.info("[callkeeper] %s conectado em %s", slot.label, channel_id)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                slot.last_error = f"connect: {type(exc).__name__}: {exc}"
                log.warning("[callkeeper] falha conectando %s: %s", slot.label, slot.last_error)
                return False

    async def _disconnect_slot(self, slot: _AuxSlot, *, intentional_seconds: float = 3.0) -> None:
        slot.intentional_until = time.monotonic() + intentional_seconds
        voice_client = slot.voice_client()
        if voice_client is not None and voice_client.is_connected():
            with contextlib.suppress(Exception):
                await voice_client.disconnect(force=True)

    async def _enforce_self_mute_deaf(self, slot: _AuxSlot) -> None:
        client = slot.client
        if client is None or not slot.is_ready():
            return
        guild = client.get_guild(self.guild_id)
        if guild is None:
            return
        me = guild.me
        voice_state = getattr(me, "voice", None)
        channel = getattr(voice_state, "channel", None)
        if channel is None:
            return
        if bool(getattr(voice_state, "self_mute", False)) and bool(getattr(voice_state, "self_deaf", False)):
            return
        try:
            await guild.change_voice_state(channel=channel, self_mute=True, self_deaf=True)
        except Exception as exc:
            slot.last_error = f"self_mute_deaf: {type(exc).__name__}: {exc}"

    async def _disconnect_all(self, *, close_clients: bool = False) -> None:
        for slot in list(self.slots):
            try:
                if close_clients:
                    await slot.close()
                else:
                    await self._disconnect_slot(slot, intentional_seconds=10.0)
            except Exception:
                log.exception("[callkeeper] falha desconectando %s", slot.label)

    async def _reconcile(self, reason: str) -> None:
        if self.guild_id <= 0:
            return
        async with self._reconcile_lock:
            if not await self._is_enabled():
                await self._disconnect_all(close_clients=False)
                return

            channel_id = await self._get_target_channel_id()
            if channel_id <= 0:
                log.warning("[callkeeper] ativo, mas sem call alvo configurada")
                return

            await self._ensure_aux_clients_started()
            member_count = await self._member_count_without_callkeeper_bots()
            required = min(3, self._required_bots_for_member_count(member_count))

            target_connected: list[_AuxSlot] = []
            connected_elsewhere: list[_AuxSlot] = []
            for slot in self.slots:
                vc_channel_id = slot.voice_channel_id()
                if vc_channel_id == channel_id:
                    target_connected.append(slot)
                elif vc_channel_id:
                    connected_elsewhere.append(slot)

            for slot in connected_elsewhere:
                await self._disconnect_slot(slot, intentional_seconds=3.0)

            for slot in list(target_connected):
                await self._enforce_self_mute_deaf(slot)

            if len(target_connected) > required:
                extras = sorted(target_connected, key=lambda s: s.index, reverse=True)[: len(target_connected) - required]
                for slot in extras:
                    await self._disconnect_slot(slot, intentional_seconds=6.0)
                target_connected = [slot for slot in target_connected if slot not in extras]

            deficit = required - len(target_connected)
            if deficit <= 0:
                return

            now = time.monotonic()
            candidates = [
                slot
                for slot in self.slots
                if slot not in target_connected and slot.voice_channel_id() != channel_id
            ]
            ready_candidates = [slot for slot in candidates if slot.is_ready()]
            cold_candidates = [slot for slot in candidates if not slot.is_ready()]

            # Prioridade: outro bot livre não bloqueado. Se todos estiverem em
            # cooldown/sem ready, usa o que existir para reduzir downtime.
            ready_candidates.sort(key=lambda slot: (slot.blocked_until > now, slot.index))
            ordered = ready_candidates + cold_candidates

            connected_now = 0
            for slot in ordered:
                if connected_now >= deficit:
                    break
                ok = await self._connect_slot(slot, channel_id)
                if ok:
                    connected_now += 1

            log.debug(
                "[callkeeper] reconcile %s | membros_nao_callkeeper=%s required=%s connected=%s deficit=%s",
                reason,
                member_count,
                required,
                len(target_connected) + connected_now,
                max(0, deficit - connected_now),
            )

    async def _status_text(self) -> str:
        enabled = await self._is_enabled()
        channel_id = await self._get_target_channel_id()
        channel = await self._target_channel()
        member_count = await self._member_count_without_callkeeper_bots() if channel_id else 0
        required = self._required_bots_for_member_count(member_count) if enabled else 0
        connected = sum(1 for slot in self.slots if channel_id and slot.voice_channel_id() == channel_id)
        target_text = channel.mention if channel else (f"`{channel_id}`" if channel_id else "não definida")
        lines = [
            f"**CallKeeper:** {'ligado' if enabled else 'desligado'}",
            f"**Servidor alvo:** `{self.guild_id or 'não configurado'}`",
            f"**Call alvo:** {target_text}",
            f"**Membros não-CallKeeper na call:** `{member_count}`",
            f"**Bots exigidos pela regra:** `{required}`",
            f"**Bots conectados na call:** `{connected}`",
            "",
            "**Auxiliares:**",
        ]
        now = time.monotonic()
        for slot in self.slots:
            vc_id = slot.voice_channel_id()
            if vc_id == channel_id and channel_id:
                state = "conectado"
            elif vc_id:
                state = f"em outra call `{vc_id}`"
            elif slot.is_ready():
                state = "pronto/livre"
            elif slot.is_running():
                state = "iniciando gateway"
            else:
                state = "parado"
            if slot.blocked_until > now:
                state += " · aguardando substituição"
            if slot.last_error:
                state += f" · último erro: `{discord.utils.escape_markdown(slot.last_error[:120])}`"
            lines.append(f"- `{slot.label}`: {state}")
        return "\n".join(lines)

    @callkeeper.command(name="on", description="Liga o CallKeeper na call escolhida ou na sua call atual")
    @app_commands.describe(canal="Call que deve ser protegida. Se vazio, usa sua call atual ou CALLKEEPER_CHANNEL_ID.")
    async def callkeeper_on(
        self,
        interaction: discord.Interaction,
        canal: Optional[discord.VoiceChannel] = None,
    ):
        if not await self._guard(interaction, require_tokens=True):
            return

        target = canal
        if target is None:
            user_voice = getattr(getattr(interaction, "user", None), "voice", None)
            user_channel = getattr(user_voice, "channel", None)
            if isinstance(user_channel, discord.VoiceChannel):
                target = user_channel

        if target is None:
            saved_id = await self._get_target_channel_id()
            guild = interaction.guild
            if guild is not None and saved_id:
                maybe = guild.get_channel(saved_id)
                if isinstance(maybe, discord.VoiceChannel):
                    target = maybe

        if target is None:
            await interaction.response.send_message(
                "Escolha uma call em `canal`, entre numa call antes de usar o comando, ou configure `CALLKEEPER_CHANNEL_ID`.",
                ephemeral=True,
            )
            return

        if int(target.guild.id) != int(self.guild_id):
            await interaction.response.send_message(
                "Essa call não pertence ao servidor configurado para o CallKeeper.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._set_target_channel_id(int(target.id))
        await self._set_enabled(True)
        await self._ensure_aux_clients_started()
        self._start_watchdog()
        await self._reconcile("command_on")
        await interaction.followup.send(
            f"CallKeeper ligado em {target.mention}. Regra ativa: 0/1 membro não-CallKeeper = 3 bots, 2 membros = 2, 3 membros = 1, 4+ membros = 0.",
            ephemeral=True,
        )

    @callkeeper.command(name="off", description="Desliga o CallKeeper e remove os bots auxiliares da call")
    async def callkeeper_off(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._set_enabled(False)
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        await self._disconnect_all(close_clients=True)
        await interaction.followup.send("CallKeeper desligado. Todos os bots auxiliares foram desconectados.", ephemeral=True)

    @callkeeper.command(name="status", description="Mostra o estado atual do CallKeeper")
    async def callkeeper_status(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        await interaction.response.send_message(await self._status_text(), ephemeral=True)


async def setup(bot: commands.Bot):
    if CALLKEEPER_GUILD_ID <= 0:
        log.warning("[callkeeper] CALLKEEPER_GUILD_ID ausente; cog não registrada para evitar comando global")
        return
    await bot.add_cog(CallKeeper(bot))
