"""Mixer PCM e telemetria leve do Music Agent.

Mantém música e overlays de TTS na mesma sessão de voz do Discord. O caminho
normal sem overlay usa audioop quando disponível para evitar loops Python por
amostra a cada frame de 20 ms. A telemetria de leitura mede apenas latência do
source (FFmpeg/stream) e nunca processa/amostra o áudio.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import threading
import time
from array import array
from typing import Any, Callable

import discord

PCM_FRAME_BYTES = 3840
MAX_MUSIC_VOLUME = 1.5
PCM_PEAK = 32767
PCM_BOOST_KNEE = int(PCM_PEAK * 0.95)
BASS_PEAK_CEILING = 32000
# Low shelf biquad (Audio EQ Cookbook), 48 kHz. O antigo passa-baixa de
# primeira ordem reforçava 1 kHz e o ramo paralelo criava cancelamentos nos
# médios. O shelf mantém a resposta acima dos graves próxima de 1:1.
# Remover <25 Hz do ramo reforçado evita amplificar DC/rumble inaudível.
BASS_SHELF_GAIN_DB = 16.0
BASS_SHELF_HZ = 280.0
BASS_SUB_ALPHA = 1.0 - math.exp(-2.0 * math.pi * 25.0 / 48000.0)


def _bass_shelf_coefficients() -> tuple[float, float, float, float, float]:
    omega = 2.0 * math.pi * BASS_SHELF_HZ / 48000.0
    cosine = math.cos(omega)
    strength = 10.0 ** (BASS_SHELF_GAIN_DB / 40.0)
    alpha = math.sin(omega) / math.sqrt(2.0)  # shelf slope S = 1
    slope = 2.0 * math.sqrt(strength) * alpha
    a0 = (strength + 1.0) + (strength - 1.0) * cosine + slope
    b0 = strength * ((strength + 1.0) - (strength - 1.0) * cosine + slope)
    b1 = 2.0 * strength * ((strength - 1.0) - (strength + 1.0) * cosine)
    b2 = strength * ((strength + 1.0) - (strength - 1.0) * cosine - slope)
    a1 = -2.0 * ((strength - 1.0) + (strength + 1.0) * cosine)
    a2 = (strength + 1.0) + (strength - 1.0) * cosine - slope
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


BASS_SHELF = _bass_shelf_coefficients()


class _AudioReadTelemetry:
    """Contadores baratos para detectar stalls do source sem tocar no PCM.

    O custo no caminho ativo são duas chamadas de ``perf_counter_ns`` e alguns
    inteiros por ``read``. Pode ser desligado por configuração; nesse caso o
    método chama o source diretamente sem relógio/counters adicionais.
    """

    def _init_audio_telemetry(
        self,
        *,
        telemetry_enabled: bool,
        stall_threshold_ms: float,
        expected_frame_bytes: int = 0,
    ) -> None:
        self.telemetry_enabled = bool(telemetry_enabled)
        self.stall_threshold_ms = max(1.0, float(stall_threshold_ms or 80.0))
        self.expected_frame_bytes = max(0, int(expected_frame_bytes or 0))
        self._source_read_count = 0
        self._audio_frame_count = 0
        self._audio_bytes = 0
        self._source_read_total_ns = 0
        self._source_read_max_ns = 0
        self._source_stall_count = 0
        self._partial_frame_count = 0
        self._source_deadline_overruns = 0
        self._telemetry_source: Any = None

    def _read_source(self, source: discord.AudioSource) -> bytes:
        self._telemetry_source = source
        if not self.telemetry_enabled:
            return source.read()
        started_ns = time.perf_counter_ns()
        frame = source.read()
        elapsed_ns = max(0, time.perf_counter_ns() - started_ns)
        self._source_read_count += 1
        self._source_read_total_ns += elapsed_ns
        if elapsed_ns > self._source_read_max_ns:
            self._source_read_max_ns = elapsed_ns
        if elapsed_ns >= int(self.stall_threshold_ms * 1_000_000.0):
            self._source_stall_count += 1
        if elapsed_ns > 20_000_000:
            self._source_deadline_overruns += 1
        if frame:
            self._audio_frame_count += 1
            self._audio_bytes += len(frame)
            if self.expected_frame_bytes and len(frame) != self.expected_frame_bytes:
                self._partial_frame_count += 1
        return frame

    def audio_telemetry(self) -> dict[str, Any]:
        reads = int(self._source_read_count)
        total_ns = int(self._source_read_total_ns)
        metrics = {
            "telemetry_enabled": bool(self.telemetry_enabled),
            "first_frame_ms": round(float(getattr(self, "first_frame_ms", 0.0) or 0.0), 2),
            "source_read_count": reads,
            "audio_frame_count": int(self._audio_frame_count),
            "audio_bytes": int(self._audio_bytes),
            "source_read_avg_ms": round((total_ns / reads) / 1_000_000.0, 3) if reads else 0.0,
            "source_read_max_ms": round(float(self._source_read_max_ns) / 1_000_000.0, 3),
            "source_stall_count": int(self._source_stall_count),
            "stall_threshold_ms": round(float(self.stall_threshold_ms), 1),
            "partial_frame_count": int(self._partial_frame_count),
            "source_deadline_overruns": int(self._source_deadline_overruns),
            "mix_limited_frames": int(getattr(self, "_mix_limited_frames", 0)),
        }
        getter = getattr(self._telemetry_source, "audio_buffer_metrics", None)
        if callable(getter):
            metrics.update(getter())
        return metrics


class AgentTelemetryAudioSource(discord.AudioSource, _AudioReadTelemetry):
    """Wrapper transparente para telemetria em sources PCM ou Opus."""

    def __init__(
        self,
        source: discord.AudioSource,
        *,
        telemetry_enabled: bool = True,
        stall_threshold_ms: float = 80.0,
        expected_frame_bytes: int = 0,
    ) -> None:
        self.source = source
        self._closed = False
        self._started_monotonic = time.monotonic()
        self.first_frame_ms: float | None = None
        self.first_frame_monotonic: float | None = None
        self._init_audio_telemetry(
            telemetry_enabled=telemetry_enabled,
            stall_threshold_ms=stall_threshold_ms,
            expected_frame_bytes=expected_frame_bytes,
        )

    def is_opus(self) -> bool:
        return bool(getattr(self.source, "is_opus", lambda: False)())

    def read(self) -> bytes:
        if self._closed:
            return b""
        frame = self._read_source(self.source)
        if frame and self.first_frame_ms is None:
            now = time.monotonic()
            self.first_frame_monotonic = now
            self.first_frame_ms = (now - self._started_monotonic) * 1000.0
        return frame

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self.source.cleanup()


class AgentMixedAudioSource(discord.AudioSource, _AudioReadTelemetry):
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        music_source: discord.AudioSource,
        music_volume: float,
        duck_factor: float = 0.08,
        telemetry_enabled: bool = True,
        stall_threshold_ms: float = 80.0,
        on_music_end: Callable[[Exception | None, dict[str, Any]], None] | None = None,
        persistent: bool = False,
        bassboost: bool = False,
        bassboost_level: int | None = None,
    ) -> None:
        self.loop = loop
        self.music_source = music_source
        self.normal_music_volume = max(0.0, min(MAX_MUSIC_VOLUME, float(music_volume)))
        self.duck_factor = max(0.0, min(1.0, float(duck_factor)))
        self._overlays: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._closed = False
        self._music_ended = False
        self.persistent = bool(persistent)
        self._finish_when_idle = False
        self._on_music_end = on_music_end
        self._encoder_update: Callable[[], None] | None = None
        self._music_gain = self.normal_music_volume
        self._mix_gain = 1.0
        self._mix_limited_frames = 0
        requested_bass_level = int(bassboost_level) if bassboost_level is not None else (1 if bassboost else 0)
        self.bassboost_level = max(0, min(3, requested_bass_level))
        self.bassboost_enabled = self.bassboost_level > 0
        self._bass_mix = 0.0
        self._bass_limiter_gain = 1.0
        # Por canal: [entrada anterior, entrada anterior-2,
        #             saída anterior, saída anterior-2, subgrave].
        self._bass_filter = [[0.0] * 5 for _ in range(2)]
        self._started_monotonic = time.monotonic()
        self.first_frame_ms: float | None = None
        self.first_frame_monotonic: float | None = None
        self._init_audio_telemetry(
            telemetry_enabled=telemetry_enabled,
            stall_threshold_ms=stall_threshold_ms,
            expected_frame_bytes=PCM_FRAME_BYTES,
        )

    def is_opus(self) -> bool:
        return False

    def set_music_volume(self, volume: float) -> None:
        self.normal_music_volume = max(0.0, min(MAX_MUSIC_VOLUME, float(volume)))

    def set_bassboost(self, enabled: bool) -> None:
        self.set_bassboost_level(1 if enabled else 0)

    def set_bassboost_level(self, level: int) -> None:
        with self._lock:
            self.bassboost_level = max(0, min(3, int(level or 0)))
            self.bassboost_enabled = self.bassboost_level > 0

    def set_duck_factor(self, factor: float) -> None:
        self.duck_factor = max(0.0, min(1.0, float(factor)))

    def queue_encoder_update(self, update: Callable[[], None]) -> None:
        """Executa o CTL na thread que codifica, antes do próximo frame."""
        with self._lock:
            self._encoder_update = update

    @property
    def music_ended(self) -> bool:
        with self._lock:
            return self._music_ended

    def replace_music_source(
        self,
        source: discord.AudioSource,
        *,
        volume: float,
        on_music_end: Callable[[Exception | None, dict[str, Any]], None] | None,
        bassboost: bool | None = None,
        bassboost_level: int | None = None,
    ) -> None:
        """Mantém overlays TTS e a sessão de voz durante a troca da música."""
        with self._lock:
            if self._closed:
                raise RuntimeError("mixer de voz encerrado")
            previous = self.music_source
            self.music_source = source
            self._music_ended = False
            self._finish_when_idle = False
            self._on_music_end = on_music_end
            self.normal_music_volume = max(0.0, min(MAX_MUSIC_VOLUME, float(volume)))
            self._music_gain = self.normal_music_volume
            self._mix_limited_frames = 0
            if bassboost_level is not None:
                self.bassboost_level = max(0, min(3, int(bassboost_level or 0)))
                self.bassboost_enabled = self.bassboost_level > 0
            elif bassboost is not None:
                self.bassboost_level = 1 if bassboost else 0
                self.bassboost_enabled = self.bassboost_level > 0
            self._bass_filter = [[0.0] * 5 for _ in range(2)]
            self._bass_mix = 0.0
            self._bass_limiter_gain = 1.0
            self._started_monotonic = time.monotonic()
            self.first_frame_ms = None
            self.first_frame_monotonic = None
            self._init_audio_telemetry(
                telemetry_enabled=self.telemetry_enabled,
                stall_threshold_ms=self.stall_threshold_ms,
                expected_frame_bytes=PCM_FRAME_BYTES,
            )
        if previous is not None and previous is not source:
            with contextlib.suppress(Exception):
                previous.cleanup()

    def stop_music(self) -> None:
        """Para o áudio da faixa sem cancelar uma fala TTS em curso."""
        with self._lock:
            previous = self.music_source
            self.music_source = None
            self._music_ended = True
            self._on_music_end = None
        if previous is not None:
            with contextlib.suppress(Exception):
                previous.cleanup()

    def finish_when_idle(self) -> None:
        with self._lock:
            self._finish_when_idle = True

    def add_tts(self, source: discord.AudioSource, *, volume: float = 1.0) -> asyncio.Future:
        future = self.loop.create_future()
        with self._lock:
            if self._closed:
                raise RuntimeError("mixer de voz encerrado")
            self._overlays.append({"source": source, "volume": max(0.0, min(2.0, float(volume))), "future": future, "ended": False})
        return future

    def has_tts(self) -> bool:
        with self._lock:
            return bool(self._overlays)

    def _future_result(self, future: asyncio.Future, value: object = None) -> None:
        def _set() -> None:
            if not future.done():
                future.set_result(value)
        self.loop.call_soon_threadsafe(_set)

    def _future_exception(self, future: asyncio.Future, error: Exception) -> None:
        def _set() -> None:
            if not future.done():
                future.set_exception(error)
        self.loop.call_soon_threadsafe(_set)

    def cancel_tts(self, future: asyncio.Future) -> None:
        target = None
        with self._lock:
            for overlay in self._overlays:
                if overlay["future"] is future:
                    target = overlay
                    self._overlays.remove(overlay)
                    break
        if target is not None:
            with contextlib.suppress(Exception):
                target["source"].cleanup()
        if not future.done():
            future.cancel()

    def _limit(self, value: int) -> int:
        return max(-32768, min(32767, int(value)))

    def _audioop(self) -> Any | None:
        cached = getattr(self, "_audioop_module", ...)
        if cached is not ...:
            return cached
        try:
            import audioop as module  # type: ignore[import-not-found]
        except Exception:
            module = None
        self._audioop_module = module
        return module

    def _soft_limit_boosted_sample(self, sample: int, volume: float) -> int:
        """Amplifica >100% sem transformar picos em hard clipping.

        Até 100% este método nunca participa do hot path. Acima disso, a zona
        linear vai até 95% do PCM máximo e só os picos que entrariam na região
        de clipping são comprimidos suavemente.
        """
        scaled = int(sample * volume)
        sign = -1 if scaled < 0 else 1
        magnitude = abs(scaled)
        if magnitude <= PCM_BOOST_KNEE:
            return scaled
        headroom = max(1, PCM_PEAK - PCM_BOOST_KNEE)
        excess = magnitude - PCM_BOOST_KNEE
        compressed = PCM_BOOST_KNEE + (headroom * excess) // (excess + headroom)
        return sign * min(PCM_PEAK, int(compressed))

    def _scale_boosted_frame(self, frame: bytes, volume: float) -> bytes:
        samples = array("h")
        samples.frombytes(frame)
        for i, sample in enumerate(samples):
            samples[i] = self._soft_limit_boosted_sample(int(sample), volume)
        return samples.tobytes()

    def _scale_frame(self, frame: bytes, volume: float) -> bytes:
        if not frame or abs(volume - 1.0) <= 0.001:
            return frame
        # O caminho normal (<=100%) permanece C-backed e sem limiter. O custo
        # extra de proteção contra clipping só existe quando o usuário opta
        # explicitamente por boost acima de 100%.
        if volume > 1.0:
            return self._scale_boosted_frame(frame, min(MAX_MUSIC_VOLUME, volume))
        module = self._audioop()
        if module is not None:
            return module.mul(frame, 2, volume)
        return self._samples(frame, volume).tobytes()

    def _samples(self, frame: bytes, volume: float) -> array:
        samples = array("h")
        samples.frombytes(frame)
        if abs(volume - 1.0) > 0.001:
            for i, sample in enumerate(samples):
                samples[i] = self._limit(int(sample * volume))
        return samples

    def _mix_into(self, base: array, frame: bytes, volume: float) -> None:
        if not frame:
            return
        other = self._samples(frame, volume)
        if len(other) < len(base):
            other.extend([0] * (len(base) - len(other)))
        elif len(other) > len(base):
            del other[len(base):]
        for i, sample in enumerate(other):
            base[i] = self._limit(int(base[i]) + int(sample))

    def _mark_first_frame(self, frame: bytes) -> bytes:
        if frame and self.first_frame_ms is None:
            now = time.monotonic()
            self.first_frame_monotonic = now
            self.first_frame_ms = (now - self._started_monotonic) * 1000.0
        return frame

    def _mix_bytes(self, base: bytes, frame: bytes, volume: float) -> bytes:
        if not frame:
            return base
        target_size = len(base)
        if len(frame) < target_size:
            frame = frame + (b"\x00" * (target_size - len(frame)))
        elif len(frame) > target_size:
            frame = frame[:target_size]
        module = self._audioop()
        if module is not None:
            return module.add(base, self._scale_frame(frame, volume), 2)
        base_samples = array("h")
        base_samples.frombytes(base)
        self._mix_into(base_samples, frame, volume)
        return base_samples.tobytes()

    def _smooth_music(self, frame: bytes, target: float) -> bytes:
        previous = self._music_gain
        if abs(target - previous) <= 0.001:
            self._music_gain = target
            return self._scale_frame(frame, target)
        # Ataque ~40 ms e retorno ~120 ms; só a transição usa quatro blocos.
        rate = 0.5 if target < previous else 1.0 / 6.0
        self._music_gain = previous + (target - previous) * rate
        step = len(frame) // 4
        return b"".join(
            self._scale_frame(frame[i * step:(i + 1) * step], previous + (self._music_gain - previous) * (i + 1) / 4)
            for i in range(4)
        )

    def _enhance_music_bass(self, frame: bytes) -> bytes:
        """Reforça graves da música, limitando o acréscimo apenas perto dos picos.

        Um pico no quadro de 20 ms não pode reduzir todo o grave: em músicas
        masterizadas isso anulava o efeito por completo. O sinal original fica
        intacto e só o ramo reforçado usa a folga disponível em cada amostra.
        """
        with self._lock:
            target = float(self.bassboost_level)
            current = self._bass_mix
            if not target and current < 0.003:
                self._bass_mix = 0.0
                return frame
            if target and current == 0.0:
                self._bass_filter = [[0.0] * 5 for _ in range(2)]
                self._bass_limiter_gain = 1.0
            current += (target - current) * 0.5
            self._bass_mix = current
            states = self._bass_filter
            b0, b1, b2, a1, a2 = BASS_SHELF
            sub_alpha = BASS_SUB_ALPHA
            samples = array("h")
            samples.frombytes(frame)
            wet = array("f")
            for index, dry in enumerate(samples):
                channel = index & 1  # PCM s16le, 48 kHz estéreo
                state = states[channel]
                filtered = (b0 * dry + b1 * state[0] + b2 * state[1]
                            - a1 * state[2] - a2 * state[3])
                state[1], state[0] = state[0], float(dry)
                state[3], state[2] = state[2], filtered
                enhanced_low = filtered - dry
                sub = state[4] + sub_alpha * (enhanced_low - state[4])
                state[4] = sub
                added = current * (enhanced_low - sub)
                wet.append(added)
            # Blocos de 1 ms evitam que um único pico suprima os graves dos 20 ms
            # inteiros, sem modular o reforço na frequência dos médios/agudos.
            # O ganho comum aos dois canais preserva o panorama da música.
            previous_gain = self._bass_limiter_gain
            gain = previous_gain
            block_gains: list[float] = []
            for block in range(0, len(samples), 96):
                allowed = 1.0
                for index in range(block, min(block + 96, len(samples))):
                    dry = samples[index]
                    added = wet[index]
                    if added > 0 and dry + added > BASS_PEAK_CEILING:
                        allowed = min(allowed, max(0.0, (BASS_PEAK_CEILING - dry) / added))
                    elif added < 0 and dry + added < -BASS_PEAK_CEILING:
                        allowed = min(allowed, max(0.0, (-BASS_PEAK_CEILING - dry) / added))
                gain = min(allowed, gain + 0.5)
                block_gains.append(gain)
            # Pequena rampa em cada fronteira evita estalos quando a folga muda.
            # A rampa sempre reduz o ganho antes de um pico ou o restaura
            # depois dele: nunca ultrapassa o teto calculado para o bloco.
            for block_index, block_gain in enumerate(block_gains):
                start = block_index * 96
                before = block_gains[block_index - 1] if block_index else previous_gain
                after = block_gains[block_index + 1] if block_index + 1 < len(block_gains) else block_gain
                for frame_index in range(min(48, (len(samples) - start) // 2)):
                    actual = block_gain
                    if before < block_gain and frame_index < 12:
                        actual = before + (block_gain - before) * (frame_index + 1) / 12
                    elif after < block_gain and frame_index >= 36:
                        actual = block_gain + (after - block_gain) * (frame_index - 35) / 12
                    index = start + frame_index * 2
                    samples[index] = self._limit(round(samples[index] + wet[index] * actual))
                    samples[index + 1] = self._limit(round(samples[index + 1] + wet[index + 1] * actual))
            self._bass_limiter_gain = gain
            return samples.tobytes()

    def _safe_mix_gain(self, frames: list[tuple[bytes, float]]) -> float:
        # Estima um limite superior do pico da soma antes de audioop.add:
        # nenhuma parcela é saturada antes de aplicar a proteção comum.
        module = self._audioop()
        peak_bound = 0.0
        for frame, volume in frames:
            if not frame:
                continue
            if module is not None and hasattr(module, "max"):
                peak = module.max(frame, 2)
            else:
                samples = array("h")
                samples.frombytes(frame)
                peak = max((abs(sample) for sample in samples), default=0)
            peak_bound += peak * abs(volume)
        safe = min(1.0, 32000.0 / peak_bound) if peak_bound else 1.0
        self._mix_gain = min(safe, self._mix_gain + (1.0 - self._mix_gain) / 8.0)
        if self._mix_gain < 0.999:
            self._mix_limited_frames += 1
        return self._mix_gain

    def read(self) -> bytes:
        if self._closed:
            return b""
        with self._lock:
            overlays = list(self._overlays)
            music_source = None if self._music_ended else self.music_source
            encoder_update, self._encoder_update = self._encoder_update, None
        if encoder_update is not None:
            encoder_update()
        music_frame = b""
        read_error: Exception | None = None
        if music_source is not None:
            try:
                music_frame = self._read_source(music_source)
            except Exception as exc:
                if not self.persistent:
                    raise
                read_error = exc
            if not music_frame:
                with self._lock:
                    if self.music_source is music_source:
                        self.music_source = None
                        self._music_ended = True
                        callback, self._on_music_end = self._on_music_end, None
                    else:
                        callback = None
                with contextlib.suppress(Exception):
                    music_source.cleanup()
                if callback is not None:
                    with contextlib.suppress(Exception):
                        callback(read_error, self.audio_telemetry())
            else:
                with self._lock:
                    # Um skip pode substituir a fonte enquanto read() aguarda
                    # FFmpeg. Nunca envie um quadro atrasado da faixa anterior.
                    if self.music_source is not music_source or self._closed:
                        music_frame = b""
            if music_frame and len(music_frame) != PCM_FRAME_BYTES:
                # Discord espera 20 ms de PCM; preencha apenas o quadro final.
                music_frame = music_frame[:PCM_FRAME_BYTES].ljust(PCM_FRAME_BYTES, b"\x00")
        if not music_frame and not overlays:
            with self._lock:
                # A decisão de encerrar precisa ser atômica com uma troca de
                # faixa ou um overlay que entrou entre o snapshot e este ponto.
                if self.music_source is not None or self._overlays:
                    return b"\x00" * PCM_FRAME_BYTES
                if self.persistent and not self._finish_when_idle and not self._closed:
                    return b"\x00" * PCM_FRAME_BYTES
                self.cleanup()
            return b""
        music_volume = self.normal_music_volume * (self.duck_factor if overlays else 1.0)
        music_has_audio = bool(music_frame and getattr(music_source, "last_read_had_audio", True))
        if music_frame and not overlays:
            output = self._enhance_music_bass(self._smooth_music(music_frame, music_volume))
            if self._mix_gain < 0.999:
                output = self._scale_frame(output, self._safe_mix_gain([(output, 1.0)]))
            else:
                self._mix_gain = 1.0
            return self._mark_first_frame(output) if music_has_audio else output
        if music_frame:
            base = self._enhance_music_bass(self._smooth_music(music_frame, music_volume))
        else:
            base = b"\x00" * PCM_FRAME_BYTES
        overlay_frames: list[tuple[bytes, float]] = []
        ended: list[dict[str, Any]] = []
        for overlay in overlays:
            source = overlay.get("source")
            future = overlay.get("future")
            try:
                frame = source.read() if source is not None else b""
            except Exception as exc:
                with contextlib.suppress(Exception):
                    source.cleanup()
                if isinstance(future, asyncio.Future):
                    self._future_exception(future, exc)
                ended.append(overlay)
                continue
            if frame:
                frame = frame[:PCM_FRAME_BYTES].ljust(PCM_FRAME_BYTES, b"\0")
                overlay_frames.append((frame, float(overlay.get("volume", 1.0))))
            else:
                with contextlib.suppress(Exception):
                    source.cleanup()
                if isinstance(future, asyncio.Future):
                    self._future_result(future, None)
                ended.append(overlay)
        gain = self._safe_mix_gain([(base, 1.0), *overlay_frames])
        base = self._scale_frame(base, gain)
        for frame, volume in overlay_frames:
            base = self._mix_bytes(base, frame, volume * gain)
        if ended:
            with self._lock:
                self._overlays = [ov for ov in self._overlays if ov not in ended]
        return self._mark_first_frame(base) if music_has_audio else base

    def cleanup(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            music_source, self.music_source = self.music_source, None
            self._on_music_end = None
            self._encoder_update = None
            overlays = list(self._overlays)
            self._overlays.clear()
        if music_source is not None:
            with contextlib.suppress(Exception):
                music_source.cleanup()
        for overlay in overlays:
            with contextlib.suppress(Exception):
                overlay.get("source").cleanup()
            future = overlay.get("future")
            if isinstance(future, asyncio.Future):
                self._future_result(future, None)
