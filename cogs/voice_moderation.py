from __future__ import annotations

import asyncio
import audioop
import contextlib
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import discord
from discord.ext import commands

try:
    from discord.ext import voice_recv
except Exception:
    voice_recv = None

try:
    # É o mesmo Decoder que o voice_recv usa internamente. Decodificamos por conta
    # própria para isolar `OpusError: corrupted stream` por pacote (ver diagnóstico
    # no topo do arquivo).
    from discord.opus import Decoder as _OpusDecoder, OpusError as _OpusError
except Exception:  # pragma: no cover - ambientes sem libopus carregada
    _OpusDecoder = None  # type: ignore[assignment]
    _OpusError = Exception  # type: ignore[assignment]


log = logging.getLogger(__name__)

# Janela em segundos das métricas de queda/erro mantidas em memória (painel/logs).
_METRIC_WINDOW_SECONDS = 60.0


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
    status_channel_id: int | None = None
    status_message_id: int | None = None
    suppress_after_until: float = 0.0
    tts_pause_depth: int = 0
    last_tts_pause_at: float = 0.0
    recover_fail_streak: int = 0
    last_recover_attempt_at: float = 0.0
    last_nonrecoverable_notice_at: float = 0.0
    last_hard_recover_at: float = 0.0
    last_listen_error: str | None = None
    last_listen_error_at: float = 0.0
    listening_armed: bool = False
    last_listen_start_at: float = 0.0
    last_listen_packet_at: float = 0.0
    # ---- instrumentação objetiva -------------------------------------------------
    # Cada vc.listen() recebe um id incremental. O after= captura esse id, e se
    # quando disparar o runtime já estiver em outro listen_id, é um callback
    # atrasado que NÃO deve apagar o estado saudável atual.
    active_listen_id: int = 0
    # Timestamps das últimas quedas do listener (após callback com erro) e das
    # OpusError por-pacote capturadas no sink. Janelas curtas (_METRIC_WINDOW_SECONDS).
    listen_drop_timeline: deque[float] = field(default_factory=deque)
    opus_decode_errors_timeline: deque[float] = field(default_factory=deque)
    # Última exceção técnica crua (classe + mensagem), sem texto de UI.
    last_technical_reason: str | None = None
    last_technical_reason_at: float = 0.0
    # Prevenção de recuperação concorrente (watchdog vs after-callback vs TTS resume).
    recovery_in_flight: bool = False


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
        """Sink que consome PCM já decodado pelo voice_recv.

        A proteção contra `OpusError: corrupted stream` fatal NÃO está aqui —
        está no patch monkey-aplicado em `PacketRouter._do_run` (ver
        `VoiceModeration._install_router_patch`). Esse patch envolve a
        iteração por-pacote do router em try/except, então um pacote ruim
        vira um drop contabilizado e o listener **permanece ativo**.

        Tentativa anterior (commit anterior) tinha `wants_opus()=True` e
        decodificava aqui dentro. Isso falhou na prática: o painel mostrou
        151 erros/min em pacotes reais de voz, porque o caminho de acesso
        ao opus cru pelo sink diverge sutilmente do caminho oficial do
        voice_recv. Voltar ao PCM oficial e proteger o loop do router é o
        caminho robusto.
        """

        def __init__(self, cog: "VoiceModeration", guild_id: int, listen_id: int):
            super().__init__()
            self.cog = cog
            self.guild_id = int(guild_id)
            self.listen_id = int(listen_id)

        def wants_opus(self) -> bool:
            return False

        def write(self, user, data):
            # Ignora se esse sink foi substituído por um listen() mais novo.
            runtime = self.cog._runtime.get(self.guild_id)
            if runtime is None or int(getattr(runtime, "active_listen_id", 0)) != self.listen_id:
                return

            speaker = user or getattr(data, "source", None)
            if speaker is None:
                try:
                    packet = getattr(data, "packet", None)
                    ssrc = getattr(packet, "ssrc", None)
                    vc = self.voice_client
                    user_id = vc._get_id_from_ssrc(int(ssrc)) if vc is not None and ssrc is not None and hasattr(vc, "_get_id_from_ssrc") else None
                    guild = getattr(vc, "guild", None)
                    if guild is not None and user_id:
                        speaker = guild.get_member(int(user_id))
                except Exception:
                    speaker = None
            if speaker is None or getattr(speaker, "bot", False):
                return

            pcm = getattr(data, "pcm", None)
            if not pcm:
                return

            runtime.listening_armed = True
            runtime.last_listen_packet_at = time.monotonic()
            if runtime.last_listen_error:
                self.cog._clear_listen_error(self.guild_id)
                self.cog._schedule_status_refresh(self.guild_id)
            try:
                rms = int(audioop.rms(pcm, 2))
                peak = int(audioop.max(pcm, 2))
                avgpp = int(audioop.avgpp(pcm, 2))
                score = max(rms, int(peak * 0.45), int(avgpp * 0.9))
            except Exception:
                return
            self.cog._register_loud_sample(
                self.guild_id,
                int(speaker.id),
                score=score,
                rms=rms,
                peak=peak,
            )

        def cleanup(self):
            return None
else:
    class _LoudDisconnectSink:  # pragma: no cover - fallback sem dependência opcional
        def __init__(self, cog: "VoiceModeration", guild_id: int, listen_id: int = 0):
            self.cog = cog
            self.guild_id = int(guild_id)
            self.listen_id = int(listen_id)


class VoiceModeration(commands.Cog):
    # Flag de classe para garantir que o monkey-patch do PacketRouter._do_run
    # seja aplicado apenas uma vez no processo, mesmo se o cog for recarregado.
    _ROUTER_PATCH_APPLIED: bool = False

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
        # Aplica o patch do router ANTES de qualquer listen() acontecer.
        # Isso protege TODAS as instâncias de VoiceRecvClient do processo.
        self._install_router_patch()

        for vc in list(getattr(self.bot, "voice_clients", []) or []):
            guild = getattr(vc, "guild", None)
            if guild is not None:
                asyncio.create_task(self.handle_voice_client_ready(guild, vc))
        self._watchdog_task = asyncio.create_task(self._listening_watchdog())

    @classmethod
    def _install_router_patch(cls) -> None:
        """Monkey-patch de `discord.ext.voice_recv.router.PacketRouter._do_run`.

        CAUSA-RAIZ (documentada):
          O `_do_run` original do voice_recv itera todos os decoders num único loop:
              for decoder in self.waiter.items:
                  data = decoder.pop_data()            # <- OpusError nasce aqui
                  if data is not None:
                      self.sink.write(data.source, data)
          Se `pop_data()` levanta `OpusError: corrupted stream` (pacote Opus
          malformado, comum em reconexão RTP e jitter alto), a exceção sobe
          para `run()` que chama `voice_client.stop_listening()` — **matando o
          listener inteiro**. Depois, o `after=` dispara com o erro e o bot
          entra em loop de reconnect.

        FIX:
          Envolver a operação **por-decoder** em try/except. Um pacote ruim
          fica contido no seu SSRC, o decoder daquele SSRC é resetado, e a
          thread do router continua viva processando os outros speakers.
          Os pacotes bons (maioria) continuam fluindo normalmente.

        O patch é idempotente (flag de classe) e só é aplicado se o
        `voice_recv` estiver disponível. Para ativar, `cog_load` chama este
        método; desfazer não é necessário no uso típico.
        """
        if cls._ROUTER_PATCH_APPLIED:
            return
        if voice_recv is None:
            return
        try:
            from discord.ext.voice_recv.router import PacketRouter
        except Exception as exc:
            log.warning("voicemod: não consegui importar PacketRouter para patch: %s", exc)
            return

        _original_do_run = PacketRouter._do_run
        # Captura a classe SenderReportPacket para silenciar o ruído "unexpected rtcp"
        # que polui muito o log.
        try:
            from discord.ext.voice_recv.rtp import SenderReportPacket as _SenderReportPacket
        except Exception:
            _SenderReportPacket = None  # type: ignore[assignment]

        def _patched_do_run(self) -> None:
            # Mantém a mesma semântica do loop original, MAS isola cada iteração.
            # Usa os mesmos símbolos do módulo original (self.waiter / self._lock /
            # self.sink / self._end_thread) — se voice_recv mudar esses nomes, cai
            # no fallback que preserva o comportamento original.
            while not self._end_thread.is_set():
                self.waiter.wait()
                with self._lock:
                    # .items pode ser alterado durante a iteração se outro thread
                    # registrar/desregistrar decoder — copiamos para um snapshot.
                    for decoder in list(self.waiter.items):
                        # Intercepta o pacote que o decoder vai processar, ANTES do
                        # pop_data executar o decode. Se o decode falhar, temos o
                        # payload real em mãos para diagnóstico (diferente de usar
                        # buf.peek() que só mostra o PRÓXIMO na fila, que em rajada
                        # nunca tem nada).
                        captured_packet = None
                        try:
                            buf = getattr(decoder, "_buffer", None)
                            if buf is not None and hasattr(buf, "peek"):
                                captured_packet = buf.peek()
                        except Exception:
                            captured_packet = None

                        try:
                            data = decoder.pop_data()
                            if data is not None:
                                self.sink.write(data.source, data)
                        except _OpusError as exc:
                            ssrc = getattr(decoder, "ssrc", None)
                            _contabilizar_opus_error(self, ssrc, exc, captured_packet)
                            with contextlib.suppress(Exception):
                                decoder.reset()
                        except Exception as exc:
                            ssrc = getattr(decoder, "ssrc", None)
                            _contabilizar_opus_error(self, ssrc, exc, captured_packet)
                            with contextlib.suppress(Exception):
                                decoder.reset()
                            log.debug("voicemod router-patch: erro em iter decoder ssrc=%s: %s",
                                      ssrc, exc)

        def _contabilizar_opus_error(router, ssrc, exc, captured_packet=None) -> None:
            """Contabiliza o erro no runtime do guild + (se debug ligado) loga
            o payload real do pacote que falhou."""
            try:
                sink = getattr(router, "sink", None)
                if sink is None:
                    return
                cog = getattr(sink, "cog", None)
                guild_id = getattr(sink, "guild_id", None)
                if cog is None or guild_id is None:
                    return
                cog._register_opus_decode_error(int(guild_id), exc, ssrc=ssrc)

                import os as _os
                if _os.environ.get("VOICEMOD_PAYLOAD_DEBUG") != "1":
                    return

                # Amostragem: 1 em cada 50 erros, para evitar spam em rajada.
                cnt = getattr(router, "_vm_dbg_counter", 0) + 1
                setattr(router, "_vm_dbg_counter", cnt)
                if cnt % 50 != 1:
                    return

                if captured_packet is None:
                    log.warning("voicemod payload-dbg: ssrc=%s pacote não capturado (erros=%d)",
                                ssrc, cnt)
                    return

                raw = getattr(captured_packet, "decrypted_data", None)
                pkt_class = type(captured_packet).__name__
                pkt_size = getattr(captured_packet, "sequence", "?")

                if raw is None:
                    log.warning("voicemod payload-dbg: ssrc=%s class=%s decrypted_data=None (erros=%d)",
                                ssrc, pkt_class, cnt)
                    return

                # ------ Classificação do payload ------
                head = bytes(raw[:12]).hex() if raw else "<vazio>"
                if not raw:
                    hint = "PAYLOAD_VAZIO — decryptor retornou bytes nulos"
                elif raw.startswith(b"\xbe\xde"):
                    hint = "EXT_HEADER_NAO_REMOVIDO (0xbede) — decrypt não stripou extension"
                elif len(raw) < 3:
                    hint = f"PAYLOAD_CURTO ({len(raw)}B) — pequeno demais pra ser Opus"
                elif raw == b"\xf8\xff\xfe":
                    hint = "OPUS_SILENCE — não deveria quebrar no decode"
                else:
                    # TOC (1º byte Opus): bits 7-3 = config (0-31), bit 2 = stereo, bits 1-0 = frame count code
                    toc = raw[0]
                    config = (toc >> 3) & 0x1F
                    stereo = bool((toc >> 2) & 0x1)
                    fcc = toc & 0x3
                    if config > 31:
                        hint = f"TOC invalido (0x{toc:02x}, config={config}>31)"
                    else:
                        hint = f"TOC=0x{toc:02x} (config={config} stereo={stereo} fcc={fcc}) — deveria ser válido"

                log.warning(
                    "voicemod payload-dbg: ssrc=%s class=%s seq=%s len=%d head=%s -> %s (erros=%d)",
                    ssrc, pkt_class, pkt_size, len(raw), head, hint, cnt,
                )
            except Exception:
                log.exception("voicemod router-patch: falha ao contabilizar opus-error")

        PacketRouter._do_run = _patched_do_run

        # --- Silencia o spam "Received unexpected rtcp packet: type=200 SenderReport" ---
        # Esse log do voice_recv.reader é INFO e polui muito. SR de RTCP é
        # comportamento NORMAL do Discord (~1Hz). Não vale log nenhum além de DEBUG.
        try:
            from discord.ext.voice_recv import reader as _vr_reader
            _vr_reader_log = getattr(_vr_reader, "log", None)
            if _vr_reader_log is not None:
                _orig_info = _vr_reader_log.info

                def _quiet_info(msg, *args, **kwargs):
                    # Filtra a mensagem específica "Received unexpected rtcp packet"
                    # Reservando os outros INFOs do reader intactos.
                    try:
                        if isinstance(msg, str) and "unexpected rtcp packet" in msg:
                            return _vr_reader_log.debug(msg, *args, **kwargs)
                    except Exception:
                        pass
                    return _orig_info(msg, *args, **kwargs)

                _vr_reader_log.info = _quiet_info
        except Exception:
            pass

        cls._ROUTER_PATCH_APPLIED = True
        log.info("voicemod: patch do PacketRouter._do_run aplicado (isolamento de OpusError por pacote)")

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
            vc = self._get_voice_client(guild)
            if vc is None or not getattr(vc, "is_connected", lambda: False)() or getattr(vc, "channel", None) is None:
                continue
            pause_depth = int(getattr(runtime, "tts_pause_depth", 0) or 0)
            if pause_depth > 0:
                busy = self._is_voice_client_busy(vc)
                pause_started = float(getattr(runtime, "last_tts_pause_at", 0.0) or 0.0)
                if busy:
                    continue
                if pause_started and (time.monotonic() - pause_started) < 6.0:
                    continue
                runtime.tts_pause_depth = 0
                runtime.last_tts_pause_at = 0.0
                self._set_listen_error(guild.id, "A pausa da escuta por TTS travou; retomando escuta automaticamente.")
            try:
                listening = bool(hasattr(vc, "is_listening") and getattr(vc, "is_listening", lambda: False)())
            except Exception:
                listening = False
            if listening:
                runtime.listening_armed = True
                runtime.recover_fail_streak = 0
                continue
            try:
                if getattr(vc, "is_playing", lambda: False)() or getattr(vc, "is_paused", lambda: False)():
                    continue
            except Exception:
                pass
            now = time.monotonic()
            if bool(getattr(runtime, "listening_armed", False)):
                last_start = float(getattr(runtime, "last_listen_start_at", 0.0) or 0.0)
                last_packet = float(getattr(runtime, "last_listen_packet_at", 0.0) or 0.0)
                if (now - last_start) < 20.0 or (last_packet and (now - last_packet) < 20.0):
                    continue
                runtime.listening_armed = False
            if float(getattr(runtime, "suppress_after_until", 0.0) or 0.0) > now:
                continue
            # Evita dar mais um pontapé se o after-callback já iniciou recuperação.
            if bool(getattr(runtime, "recovery_in_flight", False)):
                continue
            if (now - float(getattr(runtime, "last_recover_attempt_at", 0.0) or 0.0)) < 5.0:
                continue
            runtime.last_recover_attempt_at = now
            log.info(
                "voicemod/guild=%s watchdog: listener inativo há %.1fs, tentando soft restart",
                guild.id, now - float(getattr(runtime, "last_listen_start_at", 0.0) or now),
            )
            if self._is_receive_client(vc):
                _vc, state = await self._soft_restart_listening(guild, vc, preferred_channel=getattr(vc, "channel", None))
                if state == "falha_escuta" and int(getattr(runtime, "recover_fail_streak", 0) or 0) >= 2:
                    await self._hard_recover_receive_client(guild, preferred_channel=getattr(vc, "channel", None))
            else:
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

    @staticmethod
    def _is_listening_confirmed(vc: discord.VoiceClient | None, runtime: _GuildVoiceModerationRuntime | None) -> bool:
        if vc is None:
            return False
        try:
            if bool(getattr(vc, "is_listening", lambda: False)()):
                return True
        except Exception:
            pass
        if runtime is None:
            return False
        last_packet = float(getattr(runtime, "last_listen_packet_at", 0.0) or 0.0)
        return bool(last_packet and (time.monotonic() - last_packet) <= 1.5)

    async def _wait_listen_ready(
        self,
        guild_id: int,
        vc: discord.VoiceClient | None,
        runtime: _GuildVoiceModerationRuntime | None,
        *,
        expected_sink: Any | None = None,
        timeout: float = 2.0,
        poll: float = 0.1,
    ) -> bool:
        if vc is None:
            return False
        deadline = time.monotonic() + max(0.2, float(timeout))
        while time.monotonic() < deadline:
            if self._is_listening_confirmed(vc, runtime):
                return True
            # Durante restart, callbacks atrasados de um listener anterior podem
            # mexer no sink atual; isso não deve derrubar a confirmação imediatamente.
            await asyncio.sleep(max(0.03, float(poll)))
        return False

    def _remember_notice_channel(self, guild_id: int, channel_id: int | None) -> None:
        runtime = self._runtime.setdefault(int(guild_id), _GuildVoiceModerationRuntime())
        runtime.last_notice_channel_id = int(channel_id) if channel_id else None

    def _remember_status_message(self, guild_id: int, channel_id: int | None, message_id: int | None) -> None:
        runtime = self._runtime.setdefault(int(guild_id), _GuildVoiceModerationRuntime())
        runtime.status_channel_id = int(channel_id) if channel_id else None
        runtime.status_message_id = int(message_id) if message_id else None

    def _set_listen_error(self, guild_id: int, reason: str | None) -> None:
        runtime = self._runtime.setdefault(int(guild_id), _GuildVoiceModerationRuntime())
        text = str(reason or "").strip() or None
        runtime.last_listen_error = text
        runtime.last_listen_error_at = time.monotonic() if text else 0.0
        if text:
            runtime.listening_armed = False

    @staticmethod
    def _format_exception(exc: Exception | None) -> str:
        if exc is None:
            return "erro desconhecido"
        name = exc.__class__.__name__
        details = str(exc).strip()
        return f"{name}: {details}" if details else name

    # ----------------------------------------------------------------- métricas
    @staticmethod
    def _prune_timeline(tl: deque[float], now: float, window: float = _METRIC_WINDOW_SECONDS) -> None:
        cutoff = now - max(1.0, float(window))
        while tl and tl[0] < cutoff:
            tl.popleft()

    def _register_opus_decode_error(self, guild_id: int, exc: Exception | None, *, ssrc: int | None = None) -> None:
        """Chamado da thread do packet-router quando um pacote Opus falha o decode.

        Não derruba o listener — apenas contabiliza. Mantém uma janela curta para
        o painel mostrar "quedas por minuto" de forma útil.
        """
        now = time.monotonic()
        runtime = self._runtime.setdefault(int(guild_id), _GuildVoiceModerationRuntime())
        tl = runtime.opus_decode_errors_timeline
        tl.append(now)
        self._prune_timeline(tl, now)
        runtime.last_technical_reason = self._format_exception(exc)
        runtime.last_technical_reason_at = now
        # Log rate-limited por nível DEBUG: muitos pacotes ruins em rajada é esperado
        # em reconexões; não queremos spam em INFO.
        log.debug(
            "voicemod/guild=%s ssrc=%s opus-decode-error (%s)",
            guild_id, ssrc, self._format_exception(exc),
        )

    def _register_listen_drop(self, guild_id: int, exc: Exception | None) -> None:
        """Contabiliza uma queda real do listener (after= com erro ou stop silencioso)."""
        now = time.monotonic()
        runtime = self._runtime.setdefault(int(guild_id), _GuildVoiceModerationRuntime())
        tl = runtime.listen_drop_timeline
        tl.append(now)
        self._prune_timeline(tl, now)
        if exc is not None:
            runtime.last_technical_reason = self._format_exception(exc)
            runtime.last_technical_reason_at = now
        log.warning(
            "voicemod/guild=%s listener-dropped (%s) drops_last_min=%d opus_errs_last_min=%d",
            guild_id,
            self._format_exception(exc),
            len(tl),
            len(runtime.opus_decode_errors_timeline),
        )

    def _next_listen_id(self, guild_id: int) -> int:
        """Gera e registra um listen_id incremental para um novo vc.listen()."""
        runtime = self._runtime.setdefault(int(guild_id), _GuildVoiceModerationRuntime())
        runtime.active_listen_id = int(runtime.active_listen_id or 0) + 1
        return int(runtime.active_listen_id)

    def _vc_state_snapshot(self, vc: discord.VoiceClient | None) -> dict[str, Any]:
        """Retorna o estado bruto do VoiceClient — usado no painel/logs/notices."""
        def _call(name: str) -> bool:
            try:
                fn = getattr(vc, name, None)
                return bool(fn()) if callable(fn) else False
            except Exception:
                return False
        return {
            "is_connected": _call("is_connected"),
            "is_listening": _call("is_listening"),
            "is_playing": _call("is_playing"),
            "is_paused": _call("is_paused"),
        }

    def _clear_listen_error(self, guild_id: int) -> None:
        runtime = self._runtime.setdefault(int(guild_id), _GuildVoiceModerationRuntime())
        runtime.last_listen_error = None
        runtime.last_listen_error_at = 0.0

    async def _refresh_status_message(self, guild: discord.Guild, *, extra_notes: list[str] | None = None) -> bool:
        runtime = self._runtime.get(int(guild.id))
        if runtime is None or not runtime.status_channel_id or not runtime.status_message_id:
            return False
        channel = guild.get_channel(int(runtime.status_channel_id)) or self.bot.get_channel(int(runtime.status_channel_id))
        if channel is None or not hasattr(channel, "fetch_message"):
            return False
        try:
            message = await channel.fetch_message(int(runtime.status_message_id))
        except Exception:
            return False
        settings = await self._get_settings(guild.id)
        lines, notes, accent = self._status_snapshot(settings, guild)
        if extra_notes:
            notes.extend(str(note).strip() for note in extra_notes if str(note or "").strip())
        view = self._build_command_panel(guild, title="# 🔊 Moderação de voz", lines=lines, notes=notes, accent=accent)
        view.message = message
        try:
            await message.edit(view=view)
            return True
        except Exception:
            return False

    def _schedule_status_refresh(self, guild_id: int, *, extra_notes: list[str] | None = None) -> None:
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._refresh_status_message(guild, extra_notes=extra_notes),
                self.bot.loop,
            )
        except Exception:
            pass

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
            try:
                return await target_channel.connect(**connect_kwargs)
            except Exception as exc:
                self._set_listen_error(guild.id, f"Falha ao conectar cliente de voz: {self._format_exception(exc)}")
                return None
        except Exception as exc:
            self._set_listen_error(guild.id, f"Falha ao conectar cliente de voz: {self._format_exception(exc)}")
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
                    self._set_listen_error(guild.id, "O cliente de voz está ocupado tocando áudio agora.")
                    return vc, "ocupado_playback"
                vc = await self._connect_receive_client(guild, target_channel)
                if vc is None:
                    self._set_listen_error(guild.id, "Não consegui conectar um cliente de escuta na call.")
                    return None, "falha_conectar"
                is_receive_client = self._is_receive_client(vc)
                runtime.listening_armed = False

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
                runtime.listening_armed = False
                if voice_recv is None:
                    self._set_listen_error(guild.id, "A extensão de voice receive não está disponível.")
                return vc, "sem_voice_recv"

            if not start_listening:
                self._clear_listen_error(guild.id)
                return vc, "pronto"

            try:
                if getattr(vc, "is_listening", lambda: False)():
                    runtime.recover_fail_streak = 0
                    runtime.listening_armed = True
                    runtime.last_listen_start_at = time.monotonic()
                    self._clear_listen_error(guild.id)
                    return vc, "escutando"
            except Exception:
                pass

            try:
                if getattr(vc, "is_playing", lambda: False)() or getattr(vc, "is_paused", lambda: False)():
                    self._set_listen_error(guild.id, "A escuta só pode iniciar quando o áudio atual terminar.")
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
            last_state_reason: str | None = None
            for attempt in range(listen_attempts):
                # Cada tentativa recebe um listen_id único. O after= carrega esse id
                # para filtrar callbacks atrasados de sinks antigos.
                listen_id = self._next_listen_id(guild.id)
                sink = _LoudDisconnectSink(self, guild.id, listen_id)
                runtime.sink = sink
                try:
                    vc.listen(
                        sink,
                        after=lambda exc, gid=guild.id, lid=listen_id: self._on_listen_after(gid, exc, lid),
                    )
                    runtime.listening_armed = True
                    runtime.last_listen_start_at = time.monotonic()
                    self._clear_listen_error(guild.id)
                    if await self._wait_listen_ready(guild.id, vc, runtime, expected_sink=sink, timeout=2.2, poll=0.1):
                        runtime.recover_fail_streak = 0
                        runtime.last_recover_attempt_at = 0.0
                        log.info(
                            "voicemod/guild=%s listen() armado (listen_id=%s canal=%s)",
                            guild.id, listen_id, getattr(getattr(vc, "channel", None), "id", None),
                        )
                        return vc, "escutando"
                    last_state_reason = "Falha ao iniciar a escuta: listen() retornou, mas a escuta não ficou estável."
                except Exception as exc:
                    last_error = exc
                    last_state_reason = f"Falha ao iniciar a escuta: {self._format_exception(exc)}"
                    log.warning("voicemod/guild=%s listen() raised: %s", guild.id, self._format_exception(exc))

                runtime.sink = None
                runtime.listening_armed = False
                runtime.recover_fail_streak = int(runtime.recover_fail_streak or 0) + 1

                if attempt + 1 >= listen_attempts or vc_busy:
                    break

                self._suppress_after_errors(guild.id, 1.5)
                with contextlib.suppress(Exception):
                    if hasattr(vc, "stop_listening") and getattr(vc, "is_listening", lambda: False)():
                        vc.stop_listening()
                await asyncio.sleep(0.12)

            if last_error is not None:
                self._suppress_after_errors(guild.id, 3.0)
                self._set_listen_error(guild.id, last_state_reason or f"Falha ao iniciar a escuta: {self._format_exception(last_error)}")
            else:
                self._set_listen_error(guild.id, "Falha ao iniciar a escuta: listen() não confirmou estado ativo (is_listening=False).")
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
            runtime.listening_armed = False

    async def _soft_restart_listening(self, guild: discord.Guild, vc: discord.VoiceClient | None = None, *, preferred_channel=None) -> tuple[Optional[discord.VoiceClient], str]:
        runtime = self._runtime.setdefault(guild.id, _GuildVoiceModerationRuntime())
        settings = await self._get_settings(guild.id)
        runtime.settings = dict(settings)
        if not settings.get("enabled"):
            return vc, "desativado"
        current_vc = vc or self._get_voice_client(guild)
        if current_vc is None or not getattr(current_vc, "is_connected", lambda: False)() or getattr(current_vc, "channel", None) is None:
            self._set_listen_error(guild.id, "O bot não está conectado em nenhuma call para ouvir.")
            return current_vc, "sem_canal"
        if not self._is_receive_client(current_vc):
            self._set_listen_error(guild.id, "O cliente de voz atual não suporta escuta de áudio.")
            return current_vc, "sem_receive"
        if self._is_voice_client_busy(current_vc):
            self._set_listen_error(guild.id, "A escuta está aguardando o fim do áudio atual.")
            return current_vc, "ocupado_playback"

        self._suppress_after_errors(guild.id, 2.0)
        with contextlib.suppress(Exception):
            if hasattr(current_vc, "stop_listening"):
                current_vc.stop_listening()
        await asyncio.sleep(0.12)

        listen_id = self._next_listen_id(guild.id)
        sink = _LoudDisconnectSink(self, guild.id, listen_id)
        runtime.sink = sink
        try:
            current_vc.listen(
                sink,
                after=lambda exc, gid=guild.id, lid=listen_id: self._on_listen_after(gid, exc, lid),
            )
            runtime.listening_armed = True
            runtime.last_listen_start_at = time.monotonic()
            self._clear_listen_error(guild.id)
            if await self._wait_listen_ready(guild.id, current_vc, runtime, expected_sink=sink, timeout=2.2, poll=0.1):
                runtime.recover_fail_streak = 0
                runtime.last_recover_attempt_at = 0.0
                log.info(
                    "voicemod/guild=%s soft-restart listen() armado (listen_id=%s)",
                    guild.id, listen_id,
                )
                return current_vc, "escutando"
            self._set_listen_error(guild.id, "Falha ao reiniciar a escuta: listen() não ficou estável.")
        except Exception as exc:
            self._set_listen_error(guild.id, f"Falha ao reiniciar a escuta: {self._format_exception(exc)}")
            log.warning("voicemod/guild=%s soft-restart listen() raised: %s", guild.id, self._format_exception(exc))
        runtime.sink = None
        runtime.listening_armed = False
        runtime.recover_fail_streak = int(runtime.recover_fail_streak or 0) + 1
        return current_vc, "falha_escuta"

    async def _hard_recover_receive_client(self, guild: discord.Guild, *, preferred_channel=None) -> tuple[Optional[discord.VoiceClient], str]:
        runtime = self._runtime.setdefault(guild.id, _GuildVoiceModerationRuntime())
        now = time.monotonic()
        if (now - float(runtime.last_hard_recover_at or 0.0)) < 90.0:
            self._set_listen_error(guild.id, "A recuperação pesada da escuta está em cooldown.")
            return self._get_voice_client(guild), "cooldown_hard_recover"
        runtime.last_hard_recover_at = now
        runtime.last_recover_attempt_at = now

        target_channel = preferred_channel
        current_vc = self._get_voice_client(guild)
        if target_channel is None and current_vc is not None:
            target_channel = getattr(current_vc, "channel", None)
        if target_channel is None:
            self._set_listen_error(guild.id, "Não encontrei um canal de voz para recuperar a escuta.")
            return current_vc, "sem_canal"
        if self._is_voice_client_busy(current_vc):
            self._set_listen_error(guild.id, "A recuperação da escuta está aguardando o fim do áudio atual.")
            return current_vc, "ocupado_playback"

        new_vc = await self._connect_receive_client(guild, target_channel)
        if new_vc is None:
            self._set_listen_error(guild.id, "Falhei ao recriar o cliente de voz da moderação.")
            return None, "falha_conectar"
        await self._apply_self_deaf(guild, False, channel=getattr(new_vc, "channel", None) or target_channel)
        vc, state = await self._ensure_receive_ready(guild, preferred_channel=getattr(new_vc, "channel", None) or target_channel, start_listening=True)
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

        # LOOP GUARD: se já existe uma recuperação rodando, descarta esta.
        # Watchdog, after-callback e silent-stop podem disparar quase simultaneamente.
        if bool(getattr(runtime, "recovery_in_flight", False)):
            log.debug("voicemod/guild=%s recover: já em andamento, ignorando nova tentativa", guild_id)
            return
        runtime.recovery_in_flight = True
        try:
            runtime.sink = None
            runtime.listening_armed = False
            settings = await self._get_settings(guild.id)
            runtime.settings = dict(settings)
            if not settings.get("enabled"):
                return

            if exc is not None:
                self._set_listen_error(guild.id, f"A escuta caiu com erro: {self._format_exception(exc)}")
                await self._refresh_status_message(guild)

            # BACKOFF PROGRESSIVO: se cai demais em sequência, para de insistir por
            # uma janela. Isso evita entrar/sair da call em loop.
            fail_streak = int(getattr(runtime, "recover_fail_streak", 0) or 0)
            if fail_streak >= 5:
                backoff = min(60.0, 5.0 * fail_streak)
                self._suppress_after_errors(guild.id, backoff)
                notice_reason = self._format_exception(exc) if exc is not None else "escuta encerrou sem erro"
                log.warning(
                    "voicemod/guild=%s recover: backoff %.1fs após %d falhas consecutivas (%s)",
                    guild_id, backoff, fail_streak, notice_reason,
                )
                # Notifica no chat da call apenas uma vez por ciclo de falhas.
                now = time.monotonic()
                if (now - float(runtime.last_nonrecoverable_notice_at or 0.0)) >= 30.0:
                    runtime.last_nonrecoverable_notice_at = now
                    vc_for_channel = self._get_voice_client(guild)
                    state = self._vc_state_snapshot(vc_for_channel)
                    await self._send_call_notice(
                        guild,
                        title="# 🔊 Moderação de voz",
                        lines=["A escuta caiu várias vezes seguidas e vou esperar antes de tentar de novo."],
                        notes=[
                            f"Motivo técnico: `{notice_reason}`",
                            f"Estado do VC: connected={state['is_connected']} listening={state['is_listening']} playing={state['is_playing']}",
                            f"Backoff: {backoff:.1f}s",
                        ],
                        accent=discord.Color.red(),
                        voice_channel=getattr(vc_for_channel, "channel", None),
                    )
                await self._refresh_status_message(guild)
                return

            vc = self._get_voice_client(guild)
            voice_channel = getattr(vc, "channel", None) if vc is not None else None
            if vc is not None and hasattr(vc, "stop_listening"):
                self._suppress_after_errors(guild.id, 3.0)
                with contextlib.suppress(Exception):
                    vc.stop_listening()

            if self._is_recoverable_listen_error(exc):
                runtime.last_recover_attempt_at = time.monotonic()
                if self._is_corrupted_stream_error(exc):
                    # Com wants_opus=True no sink, corrupted-stream do voice_recv
                    # não deve mais chegar aqui; se chegou, algo muito estranho —
                    # vale um hard recover para zerar o SSRC/decoders internos.
                    await asyncio.sleep(0.25)
                    _vc, hard_state = await self._hard_recover_receive_client(guild, preferred_channel=voice_channel)
                    if hard_state in {"escutando", "sem_voice_recv", "ocupado_playback"}:
                        if hard_state == "escutando":
                            runtime.recover_fail_streak = 0
                        await self._refresh_status_message(guild)
                        return
                    if hard_state == "cooldown_hard_recover" and exc is not None:
                        self._set_listen_error(guild.id, f"A escuta caiu com erro: {self._format_exception(exc)}")
                await asyncio.sleep(0.45)
                _vc, state = await self._soft_restart_listening(guild, vc, preferred_channel=voice_channel)
                if state in {"escutando", "sem_voice_recv", "ocupado_playback"}:
                    runtime.recover_fail_streak = 0
                    await self._refresh_status_message(guild)
                    return
                # Hard recover só depois de algumas falhas do soft — e não imediato.
                if int(runtime.recover_fail_streak or 0) >= 3:
                    await asyncio.sleep(1.2)
                    _vc, hard_state = await self._hard_recover_receive_client(guild, preferred_channel=voice_channel)
                    if hard_state in {"escutando", "sem_voice_recv", "ocupado_playback", "cooldown_hard_recover"}:
                        if hard_state == "escutando":
                            runtime.recover_fail_streak = 0
                        await self._refresh_status_message(guild)
                        return
                self._suppress_after_errors(guild.id, 6.0)
                await self._refresh_status_message(guild)
                return

            now = time.monotonic()
            if (now - float(runtime.last_nonrecoverable_notice_at or 0.0)) < 12.0:
                return
            runtime.last_nonrecoverable_notice_at = now
            vc_state = self._vc_state_snapshot(vc)
            await self._send_call_notice(
                guild,
                title="# 🔊 Moderação de voz",
                lines=["A escuta do canal foi encerrada com erro."],
                notes=[
                    f"Motivo técnico: `{self._format_exception(exc)}`",
                    f"Estado do VC: connected={vc_state['is_connected']} listening={vc_state['is_listening']} playing={vc_state['is_playing']}",
                ],
                accent=discord.Color.red(),
                voice_channel=voice_channel,
            )
        finally:
            runtime.recovery_in_flight = False

    async def _recover_listening_after_silent_stop(self, guild_id: int) -> None:
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return
        runtime = self._runtime.setdefault(int(guild_id), _GuildVoiceModerationRuntime())
        settings = await self._get_settings(guild.id)
        runtime.settings = dict(settings)
        if not settings.get("enabled"):
            self._clear_listen_error(guild.id)
            await self._refresh_status_message(guild)
            return
        if int(getattr(runtime, "tts_pause_depth", 0) or 0) > 0:
            return
        vc = self._get_voice_client(guild)
        if vc is None or not getattr(vc, "is_connected", lambda: False)() or getattr(vc, "channel", None) is None:
            return
        if self._is_voice_client_busy(vc):
            return
        self._set_listen_error(guild.id, "A escuta encerrou sem erro explícito e será rearmada automaticamente.")
        await self._refresh_status_message(guild)
        await asyncio.sleep(0.35)
        await self._recover_listening_after_error(guild.id, None)

    def _on_listen_after(self, guild_id: int, exc: Exception | None, listen_id: int | None = None) -> None:
        """Callback disparado pelo voice_recv quando a escuta encerra.

        Roda na thread do reader (audioreader-stopper-*). Toda lógica async aqui
        é agendada via run_coroutine_threadsafe.

        IMPORTANTE: `listen_id` é o id do vc.listen() que disparou este callback.
        Se enquanto este callback estava enfileirado um NOVO listen() já foi
        armado (runtime.active_listen_id != listen_id), este callback é de um
        listener antigo e NÃO deve apagar o estado saudável atual.
        """
        runtime = self._runtime.get(int(guild_id))
        current_id = int(getattr(runtime, "active_listen_id", 0)) if runtime is not None else 0
        # Stale: callback atrasado de um listen() que já foi substituído.
        if listen_id is not None and int(listen_id) != current_id and current_id > 0:
            log.debug(
                "voicemod/guild=%s after= stale ignorado (cb_listen_id=%s current=%s exc=%s)",
                guild_id, listen_id, current_id, self._format_exception(exc),
            )
            # Ainda contabiliza como queda se veio com erro — é um sinal técnico real.
            if exc is not None:
                self._register_listen_drop(int(guild_id), exc)
            return

        was_armed = False
        if runtime is not None:
            was_armed = bool(runtime.listening_armed)
            runtime.sink = None
            runtime.listening_armed = False
            if float(runtime.suppress_after_until or 0.0) > time.monotonic():
                # Encerramento esperado (stop_listening() intencional, ex.: TTS).
                log.debug("voicemod/guild=%s after= dentro da janela de suppress", guild_id)
                return

        if exc is None:
            self._schedule_status_refresh(int(guild_id))
            if was_armed:
                log.info("voicemod/guild=%s after= silent stop (listen_id=%s), reagendando", guild_id, listen_id)
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._recover_listening_after_silent_stop(int(guild_id)),
                        self.bot.loop,
                    )
                except Exception:
                    pass
            else:
                guild = self.bot.get_guild(int(guild_id))
                vc = self._get_voice_client(guild) if guild is not None else None
                enabled = bool(getattr(getattr(runtime, "settings", None), "get", lambda *_: False)("enabled")) if runtime is not None else False
                try:
                    is_listening_now = bool(vc and hasattr(vc, "is_listening") and getattr(vc, "is_listening", lambda: False)())
                except Exception:
                    is_listening_now = False
                if (not enabled) or is_listening_now:
                    self._clear_listen_error(int(guild_id))
                elif not str(getattr(runtime, "last_listen_error", "") or "").strip():
                    self._set_listen_error(int(guild_id), "A escuta encerrou sem erro explícito e está aguardando recuperação.")
            return

        # Listener derrubado com erro real — contabiliza na janela de métricas.
        self._register_listen_drop(int(guild_id), exc)
        self._set_listen_error(int(guild_id), f"A escuta caiu com erro: {self._format_exception(exc)}")
        self._schedule_status_refresh(int(guild_id))
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
            guild = self.bot.get_guild(int(guild_id))
            vc = self._get_voice_client(guild) if guild is not None else None
            if self._is_voice_client_busy(vc):
                with self._sample_lock:
                    self._over_limit_windows.pop((int(guild_id), int(user_id)), None)
                return
            runtime.tts_pause_depth = 0
            runtime.last_tts_pause_at = 0.0

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
        sustain_gap_reset = 0.75

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
                window = self._over_limit_windows.get(key)
                if window is not None:
                    _start_at, last_seen_at, _max_seen = window
                    if now - float(last_seen_at or now) > sustain_gap_reset:
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
            runtime.last_tts_pause_at = time.monotonic()
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
            if depth <= 0:
                runtime.last_tts_pause_at = 0.0
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

        # --- Diagnóstico: loga modo de cripto + endpoint do VC uma vez por sessão ---
        # Isso é ESSENCIAL para diagnosticar corrupted-stream em rajada: se o Discord
        # negociou um modo de cripto que o voice_recv instalado não suporta direito,
        # os pacotes vêm com payload lixo e o Opus rejeita 100% deles.
        vc = self._get_voice_client(guild) or vc
        if vc is not None and not getattr(runtime, "_crypto_logged", False):
            # Log imediato (cedo, provavelmente com valores None/0) + um log
            # adiado após o handshake completar, com os valores reais.
            self._log_vc_info(guild, vc, phase="imediato")
            asyncio.create_task(self._log_vc_info_after_delay(guild, delay=3.5))

    def _log_vc_info(self, guild: discord.Guild, vc, *, phase: str) -> None:
        try:
            mode = getattr(vc, "mode", None)
            endpoint = getattr(vc, "endpoint", None)
            secret_key = getattr(vc, "secret_key", None) or b""
            secret_len = len(secret_key) if secret_key else 0
            my_ssrc = getattr(vc, "ssrc", None)
            ssrc_map = getattr(vc, "_ssrc_to_id", {}) or {}
            log.info(
                "voicemod/guild=%s VC info (%s): mode=%r endpoint=%r secret_key_len=%d my_ssrc=%r ssrc_map=%s",
                guild.id, phase, mode, endpoint, secret_len, my_ssrc,
                # Mostra SSRCs mapeados de verdade, não só o tamanho
                {str(k): v for k, v in list(ssrc_map.items())[:10]},
            )
        except Exception as exc:
            log.debug("voicemod/guild=%s: falha ao logar info do VC: %s", guild.id, exc)

    async def _log_vc_info_after_delay(self, guild: discord.Guild, *, delay: float) -> None:
        await asyncio.sleep(delay)
        vc = self._get_voice_client(guild)
        if vc is not None:
            self._log_vc_info(guild, vc, phase=f"+{delay:.1f}s")
            runtime = self._runtime.get(guild.id)
            if runtime is not None:
                runtime._crypto_logged = True  # type: ignore[attr-defined]

    async def _enable_mode(self, guild: discord.Guild, preferred_channel=None) -> tuple[str, bool]:
        await self._set_enabled(guild.id, True)
        settings = await self._get_settings(guild.id)
        runtime = self._runtime.setdefault(guild.id, _GuildVoiceModerationRuntime())
        runtime.settings = dict(settings)

        existing_vc = self._get_voice_client(guild)
        if self._is_voice_client_busy(existing_vc):
            self._suppress_after_errors(guild.id, 8.0)
            return "ocupado_playback", False

        vc, state = await self._ensure_receive_ready(guild, preferred_channel=preferred_channel, start_listening=True)
        played = False
        if state == "falha_escuta" and vc is not None and not self._is_voice_client_busy(vc):
            await asyncio.sleep(0.35)
            vc, retry_state = await self._ensure_receive_ready(guild, preferred_channel=getattr(vc, "channel", None) or preferred_channel, start_listening=True)
            if retry_state in {"escutando", "sem_voice_recv", "ocupado_playback"}:
                state = retry_state
            else:
                recovered_vc, hard_state = await self._hard_recover_receive_client(
                    guild,
                    preferred_channel=getattr(vc, "channel", None) or preferred_channel,
                )
                if recovered_vc is not None:
                    vc = recovered_vc
                if hard_state in {"escutando", "sem_voice_recv", "ocupado_playback", "cooldown_hard_recover"}:
                    state = hard_state

        # O SFX de ativação pode manter o cliente em playback e impedir o listen()
        # de armar no mesmo ciclo. Mantemos o foco na estabilidade da escuta.
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
        runtime = self._runtime.get(guild.id)
        actual_listening = bool(vc and hasattr(vc, "is_listening") and getattr(vc, "is_listening", lambda: False)())
        listening = bool(actual_listening or getattr(runtime, "listening_armed", False))
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
                if int(getattr(runtime, "tts_pause_depth", 0) or 0) > 0:
                    notes.append("A escuta está pausada pelo TTS e volta automaticamente ao fim do áudio.")
                elif busy:
                    notes.append("A escuta retoma automaticamente quando o áudio atual terminar.")
                elif int(getattr(runtime, "recover_fail_streak", 0) or 0) >= 2:
                    notes.append("A escuta caiu e o bot está tentando estabilizar automaticamente.")
                else:
                    notes.append("O modo foi ativado, mas a escuta ainda não iniciou direito.")
            elif not actual_listening and getattr(runtime, "listening_armed", False):
                notes.append("A escuta foi armada e está aguardando pacotes de voz do Discord.")
        error_text = str(getattr(runtime, "last_listen_error", "") or "").strip() if runtime is not None else ""
        if error_text:
            notes.append(f"Motivo da última falha: `{error_text}`")

        # ---- bloco de diagnóstico técnico objetivo -------------------------------
        # Atende o requisito "contador de quedas por minuto, último estado do VC,
        # tempo desde último pacote, motivo técnico com classe da exceção".
        if enabled and runtime is not None:
            now = time.monotonic()
            self._prune_timeline(runtime.listen_drop_timeline, now)
            self._prune_timeline(runtime.opus_decode_errors_timeline, now)
            drops = len(runtime.listen_drop_timeline)
            opus_errs = len(runtime.opus_decode_errors_timeline)

            last_packet = float(getattr(runtime, "last_listen_packet_at", 0.0) or 0.0)
            last_start = float(getattr(runtime, "last_listen_start_at", 0.0) or 0.0)
            if last_packet:
                since_packet_s = now - last_packet
                since_packet_txt = (
                    f"{since_packet_s:.1f}s atrás" if since_packet_s < 300
                    else f"{since_packet_s/60:.1f}min atrás"
                )
            elif last_start and (now - last_start) > 2.0:
                # Listener armou há algum tempo e NUNCA recebeu pacote bom. É
                # sinal diferente de "parou de chegar" — ajuda a diagnosticar:
                # ninguém falou, ou todos os pacotes estão sendo rejeitados.
                since_start = now - last_start
                if opus_errs > 0:
                    since_packet_txt = f"nenhum válido em {since_start:.0f}s (mas chegando e falhando decode)"
                else:
                    since_packet_txt = f"nenhum em {since_start:.0f}s (canal silencioso ou sem permissão)"
            else:
                since_packet_txt = "nenhum ainda"

            vc_state = self._vc_state_snapshot(vc)
            diag_parts = [
                "**Diagnóstico:**",
                f"`is_connected={vc_state['is_connected']} "
                f"is_listening={vc_state['is_listening']} "
                f"is_playing={vc_state['is_playing']}`",
                f"Últ. pacote recebido: {since_packet_txt}",
                f"Quedas no último min: {drops} · Pacotes Opus ruins no último min: {opus_errs}",
            ]
            # TTL na exceção técnica — 5min sem erro novo é considerada obsoleta
            # e não é exibida, para o painel refletir só o estado atual.
            tech = str(getattr(runtime, "last_technical_reason", "") or "").strip()
            tech_at = float(getattr(runtime, "last_technical_reason_at", 0.0) or 0.0)
            if tech and tech_at and (now - tech_at) < 300.0:
                tech_age = now - tech_at
                diag_parts.append(f"Últ. exceção técnica ({tech_age:.0f}s atrás): `{tech}`")
            elif tech and tech_at and (now - tech_at) >= 300.0:
                # Limpa o resíduo: se há mais de 5min que nada dá errado,
                # a exceção antiga some do painel.
                runtime.last_technical_reason = None
                runtime.last_technical_reason_at = 0.0
            notes.append("\n".join(diag_parts))

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
        self._remember_status_message(ctx.guild.id, getattr(message.channel, "id", None), getattr(message, "id", None))

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
            runtime = self._runtime.get(guild.id)
            if not str(getattr(runtime, "last_listen_error", "") or "").strip():
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
