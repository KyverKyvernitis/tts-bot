"""Leitura de FFmpeg fora da thread de voz, com memória e espera limitadas."""
from __future__ import annotations

import asyncio
import contextlib
import math
import shlex
import struct
import subprocess
import threading
import time
from collections import deque
from typing import Any

import discord

FRAME_BYTES = 3840
SILENCE = bytes(FRAME_BYTES)


class BufferedPCMSource(discord.AudioSource):
    """Um produtor e um consumidor; a fila cheia aplica backpressure ao FFmpeg.

    ``read`` nunca espera a rede. Ausência temporária gera silêncio, não EOF,
    permitindo que TTS continue. Um stall prolongado vira erro recuperável.
    """

    def __init__(self, source: discord.AudioSource, *, max_frames: int = 75, stall_seconds: float = 12.0,
                 preserve_partial_frames: bool = False) -> None:
        self.source = source
        self._preserve_partial_frames = preserve_partial_frames
        self.max_frames = max(1, min(150, int(max_frames)))
        self.stall_seconds = max(0.1, min(30.0, float(stall_seconds)))
        self._frames: deque[Any] = deque()
        self._condition = threading.Condition()
        self._closed = False
        self._eof = False
        self._error: Exception | None = None
        self._empty_since: float | None = None
        self.last_read_had_audio = False
        self._underruns = 0
        self._peak_frames = 0
        self._decoder_max_ms = 0.0
        self._decoder_late_reads = 0
        self._thread = threading.Thread(target=self._produce, name="music-pcm-buffer", daemon=True)
        self._thread.start()

    def is_opus(self) -> bool:
        return False

    def _produce(self) -> None:
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closed or len(self._frames) < self.max_frames)
                    if self._closed:
                        return
                started = time.monotonic()
                frame = self.source.read()
                elapsed_ms = (time.monotonic() - started) * 1000.0
                with self._condition:
                    if self._closed:
                        return
                    self._decoder_max_ms = max(self._decoder_max_ms, elapsed_ms)
                    self._decoder_late_reads += int(elapsed_ms > 20.0)
                    if not frame:
                        error = getattr(self.source, "_current_error", None)
                        if isinstance(error, Exception):
                            self._error = error
                        self._eof = True
                        self._condition.notify_all()
                        return
                    self._frames.append(self._buffer_entry(frame))
                    self._peak_frames = max(self._peak_frames, len(self._frames))
                    self._condition.notify_all()
        except Exception as exc:
            with self._condition:
                if not self._closed:
                    self._error = exc
                    self._eof = True
                    self._condition.notify_all()

    async def wait_ready(self, *, timeout: float, min_frames: int = 1) -> None:
        deadline = time.monotonic() + max(0.01, timeout)
        target = max(1, min(self.max_frames, int(min_frames)))
        while True:
            with self._condition:
                if self._closed:
                    raise RuntimeError("buffer de áudio encerrado")
                if len(self._frames) >= target or (self._eof and self._frames):
                    return
                if self._error is not None:
                    raise self._error
                if self._eof:
                    raise RuntimeError("FFmpeg encerrou sem produzir áudio")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("FFmpeg não produziu o primeiro áudio a tempo")
            await asyncio.sleep(min(0.01, remaining))

    def read(self) -> bytes:
        with self._condition:
            self.last_read_had_audio = False
            if self._closed:
                return b""
            if self._frames:
                frame = self._frames.popleft()
                self.last_read_had_audio = True
                self._empty_since = None
                self._condition.notify_all()
                return self._read_buffer_entry(frame)
            if self._error is not None:
                raise self._error
            if self._eof:
                return b""
            now = time.monotonic()
            if self._empty_since is None:
                self._empty_since = now
            if now - self._empty_since >= self.stall_seconds:
                raise TimeoutError("stream de áudio parou de fornecer frames")
            self._underruns += 1
            return SILENCE

    def _buffer_entry(self, frame: bytes) -> Any:
        return frame[:FRAME_BYTES] if self._preserve_partial_frames else frame[:FRAME_BYTES].ljust(FRAME_BYTES, b"\0")

    def _read_buffer_entry(self, entry: Any) -> bytes:
        return entry

    def audio_buffer_metrics(self) -> dict[str, Any]:
        with self._condition:
            return {
                "buffer_frames": len(self._frames),
                "buffer_peak_frames": self._peak_frames,
                "buffer_max_bytes": self.max_frames * FRAME_BYTES,
                "buffer_underruns": self._underruns,
                "decoder_read_max_ms": round(self._decoder_max_ms, 3),
                "decoder_deadline_overruns": self._decoder_late_reads,
            }

    def set_max_frames(self, max_frames: int) -> None:
        """Aumenta o buffer após o aquecimento, sem reiniciar o decoder."""
        with self._condition:
            self.max_frames = max(1, min(150, int(max_frames)))
            self._condition.notify_all()

    def cleanup(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._frames.clear()
            self._condition.notify_all()
        # Matar FFmpeg desbloqueia a leitura do produtor; não aguardamos join
        # dentro do event loop ou da thread de áudio.
        with contextlib.suppress(Exception):
            self.source.cleanup()


class OpusPacketError(ValueError):
    """Contêiner/pacote inválido; nunca pode alcançar o transporte de voz."""


def _ogg_crc_table() -> tuple[int, ...]:
    result = []
    for value in range(256):
        value <<= 24
        for _ in range(8):
            value = ((value << 1) ^ (0x04C11DB7 if value & 0x80000000 else 0)) & 0xFFFFFFFF
        result.append(value)
    return tuple(result)


_OGG_CRC_TABLE = _ogg_crc_table()


def _ogg_crc(raw: bytes) -> int:
    crc = 0
    for byte in raw:
        crc = ((crc << 8) ^ _OGG_CRC_TABLE[((crc >> 24) ^ byte) & 255]) & 0xFFFFFFFF
    return crc


def opus_packet_samples(packet: bytes) -> int:
    """Duração TOC a 48 kHz; o decoder nativo ainda valida o payload inteiro."""
    if not packet or len(packet) > 61440:
        raise OpusPacketError("tamanho de pacote Opus inválido")
    toc = packet[0]
    code = toc & 3
    if code == 0:
        frames = 1
    elif code in (1, 2):
        frames = 2
    else:
        if len(packet) < 2:
            raise OpusPacketError("contagem de frames Opus ausente")
        frames = packet[1] & 63
        if not frames:
            raise OpusPacketError("contagem de frames Opus inválida")
    if toc & 128:
        samples = (48000 << ((toc >> 3) & 3)) // 400
    elif toc & 96 == 96:
        samples = 960 if toc & 8 else 480
    else:
        duration = (toc >> 3) & 3
        samples = 2880 if duration == 3 else (48000 << duration) // 100
    total = frames * samples
    if total > 5760:
        raise OpusPacketError("pacote Opus excede 120 ms")
    return total


class OggOpusPacketReader:
    """Demux Ogg limitado, com CRC, sequência e um único fluxo Opus estéreo.

    O campo input_sample_rate descreve a entrada original; Opus decodifica a
    48 kHz mesmo quando esse campo contém 44100 ou zero. Pre-skip/granule são
    expostos para diagnóstico; o transporte cru não promete corte sample-exact.
    """

    MAX_PACKET_BYTES = 65536

    def __init__(self, stream: Any, *, close_stream: bool = False,
                 _allow_archive_preroll: bool = False) -> None:
        self.stream = stream
        self.close_stream = close_stream
        self.metadata: dict[str, Any] = {}
        self._packets: deque[bytes] = deque()
        self._partial = bytearray()
        self._serial: int | None = None
        self._sequence = 0
        self._header_count = 0
        self._eos = False
        self._closed = False
        self._finished = False
        self._pages = 0
        self._audio_packets = 0
        # Only the continuous archive reader may consume a segment's duplicated
        # preroll. Standalone streams retain the original strict 20 ms bound.
        self._max_pre_skip = 9600 if _allow_archive_preroll else 960

    def _read_exact(self, size: int, *, allow_eof: bool = False) -> bytes:
        result = bytearray()
        while len(result) < size:
            chunk = self.stream.read(size - len(result))
            if not chunk:
                if allow_eof and not result:
                    return b""
                raise OpusPacketError("página Ogg truncada")
            result.extend(chunk)
        return bytes(result)

    def _load_page(self) -> None:
        header = self._read_exact(27, allow_eof=True)
        if not header:
            raise OpusPacketError("fluxo Ogg terminou antes de EOS")
        if header[:4] != b"OggS" or header[4] != 0 or header[5] & ~7:
            raise OpusPacketError("cabeçalho de página Ogg inválido")
        flags = header[5]
        granule, serial, sequence, checksum = struct.unpack_from("<QIII", header, 6)
        first = self._serial is None
        if first:
            if not flags & 2 or flags & 1 or sequence != 0:
                raise OpusPacketError("início Ogg inválido")
            self._serial = serial
        elif serial != self._serial or flags & 2:
            raise OpusPacketError("fluxos Ogg encadeados/múltiplos não suportados")
        if sequence != self._sequence:
            raise OpusPacketError("sequência de páginas Ogg inválida")
        if bool(flags & 1) != bool(self._partial):
            raise OpusPacketError("continuação Ogg inválida")
        lacing = self._read_exact(header[26])
        body = self._read_exact(sum(lacing))
        page = bytearray(header + lacing + body)
        page[22:26] = b"\0\0\0\0"
        if _ogg_crc(page) != checksum:
            raise OpusPacketError("CRC Ogg inválido")
        offset = 0
        completed = []
        for length in lacing:
            if len(self._partial) + length > self.MAX_PACKET_BYTES:
                raise OpusPacketError("pacote Ogg excede limite")
            self._partial.extend(body[offset:offset + length])
            offset += length
            if length < 255:
                completed.append(bytes(self._partial))
                self._partial.clear()
        if first and (len(completed) != 1 or self._partial):
            raise OpusPacketError("OpusHead deve ocupar a primeira página")
        if flags & 4 and self._partial:
            raise OpusPacketError("EOS Ogg contém pacote incompleto")
        if flags & 4 and not completed:
            raise OpusPacketError("EOS Ogg sem pacote final não suportado")
        if flags & 4 and (granule == 0xFFFFFFFFFFFFFFFF
                          or granule < int(self.metadata.get("pre_skip_samples", 0))):
            raise OpusPacketError("posição final Ogg inválida")
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        self._pages += 1
        self.metadata["ogg_pages"] = self._pages
        if flags & 4:
            self._eos = True
            self.metadata["end_granule_samples"] = granule
        self._packets.extend(completed)

    def _head(self, packet: bytes) -> None:
        if (len(packet) != 19 or packet[:8] != b"OpusHead" or packet[8] != 1
                or packet[9] != 2 or packet[18] != 0):
            raise OpusPacketError("OpusHead incompatível com estéreo mapping 0")
        preskip, input_rate, gain = struct.unpack_from("<HIh", packet, 10)
        if gain != 0 or preskip > self._max_pre_skip:
            raise OpusPacketError("ganho/pre-skip Opus incompatível")
        self.metadata.update(codec="opus", sample_rate_hz=48000, channels=2,
                             input_sample_rate_hz=input_rate, pre_skip_samples=preskip,
                             output_gain_q8=gain, channel_mapping=0,
                             trim_mode="raw_packets_no_sample_exact_trim")

    @staticmethod
    def _tags(packet: bytes) -> None:
        if len(packet) < 16 or packet[:8] != b"OpusTags":
            raise OpusPacketError("OpusTags ausente/inválido")
        vendor = struct.unpack_from("<I", packet, 8)[0]
        offset = 12 + vendor
        if offset + 4 > len(packet):
            raise OpusPacketError("vendor OpusTags truncado")
        comments = struct.unpack_from("<I", packet, offset)[0]
        offset += 4
        if comments > 1024:
            raise OpusPacketError("quantidade de tags Opus excede limite")
        for _ in range(comments):
            if offset + 4 > len(packet):
                raise OpusPacketError("tag Opus truncada")
            size = struct.unpack_from("<I", packet, offset)[0]
            offset += 4 + size
            if offset > len(packet):
                raise OpusPacketError("tag Opus truncada")

    def read_packet(self) -> bytes:
        if self._closed or self._finished:
            return b""
        while True:
            if not self._packets:
                if self._eos:
                    # Any bytes after EOS would be a second stream or junk.
                    if self.stream.read(1):
                        raise OpusPacketError("dados após EOS Ogg não suportados")
                    if self._header_count < 2:
                        raise OpusPacketError("cabeçalhos Opus incompletos")
                    self._finished = True
                    return b""
                self._load_page()
                continue
            packet = self._packets.popleft()
            if self._header_count == 0:
                self._head(packet)
                self._header_count = 1
            elif self._header_count == 1:
                self._tags(packet)
                self._header_count = 2
            else:
                if not packet:
                    raise OpusPacketError("pacote Opus vazio")
                if packet.startswith((b"OpusHead", b"OpusTags")):
                    raise OpusPacketError("cabeçalho Opus repetido")
                self._audio_packets += 1
                self.metadata["audio_packets"] = self._audio_packets
                return packet

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._packets.clear()
        self._partial.clear()
        if self.close_stream:
            with contextlib.suppress(Exception):
                self.stream.close()


class ArchiveOpusPacketReader:
    """One packet stream across verified archive parts, without duplicated preroll.

    ``open_segment`` is called by the buffer's producer thread and must return
    a bounded, cancellable binary stream or an OggOpusPacketReader. Only the
    active part is owned here. A caller may separately prefetch the next part.
    The same Opus decoder consumes every emitted packet; duplicated packets
    warm neither the decoder nor the wire a second time.
    """

    MAX_SEGMENTS = 4096
    MAX_PREROLL_SAMPLES = 9600

    def __init__(self, segments: list[dict], open_segment: Any) -> None:
        if (not isinstance(segments, (list, tuple)) or not 2 <= len(segments) <= self.MAX_SEGMENTS
                or not callable(open_segment)):
            raise OpusPacketError("partes Opus ausentes ou fora do limite")
        self._durations: list[int] = []
        offset = 0
        for order, segment in enumerate(segments):
            if not isinstance(segment, dict):
                raise OpusPacketError("parte Opus inválida")
            seconds, start = segment.get("duration"), segment.get("offset_seconds")
            if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                    or not math.isfinite(seconds) or seconds <= 0
                    or isinstance(start, bool) or not isinstance(start, (int, float))
                    or not math.isfinite(start) or abs(start * 48000 - offset) > 1.01
                    or ("order" in segment and segment["order"] != order)):
                raise OpusPacketError("duração ou ordem das partes Opus inválida")
            if seconds * 48000 > 0x7FFFFFFFFFFFFFFF - offset:
                raise OpusPacketError("duração das partes Opus excede o limite")
            count = round(seconds * 48000)
            if count <= 0 or abs(seconds * 48000 - count) > 0.01:
                raise OpusPacketError("duração das partes Opus não alinhada às amostras")
            if (segment.get("audio_codec", "opus") not in {"opus", "libopus"}
                    or segment.get("audio_sample_rate", 48000) != 48000
                    or segment.get("audio_channels", 2) != 2):
                raise OpusPacketError("perfil das partes Opus incompatível")
            self._durations.append(count)
            offset += count
        self._open_segment = open_segment
        self._current: OggOpusPacketReader | None = None
        self._index = 0
        self._closed = False
        self._finished = False
        self._eos = False
        self._packets: deque[bytes] = deque()
        self._part_samples = 0
        self._part_validated_eos = False
        self._emitted_samples = 0
        self._part_emitted_start = 0
        self._tail: deque[tuple[bytes, int]] = deque()
        self._tail_samples = 0
        self.metadata: dict[str, Any] = {
            "archive_reader_continuous": True, "archive_segment_index": 0,
            "archive_segments_total": len(segments),
            "archive_segments_completed": 0, "archive_preroll_packets_skipped": 0,
            "archive_preroll_samples_skipped": 0,
        }

    @property
    def segment_index(self) -> int:
        return self._index

    def _validate_eos(self, packet: bytes) -> None:
        child = self._current
        if child is None or self._part_validated_eos or not child._eos:
            return
        remaining = (packet, *child._packets)
        if not remaining or len(remaining) > 255:
            raise OpusPacketError("quantidade de pacotes EOS da parte inválida")
        counts = [opus_packet_samples(value) for value in remaining]
        total = self._part_samples + sum(counts)
        granule = child.metadata.get("end_granule_samples")
        pre = child.metadata.get("pre_skip_samples")
        if (not isinstance(granule, int) or not isinstance(pre, int)
                or granule > total or total - granule > counts[-1]
                or (self._index + 1 < len(self._durations) and granule != total)
                or abs(granule - pre - self._durations[self._index]) > 1):
            raise OpusPacketError("duração ou corte final da parte Opus inválido")
        self._part_validated_eos = True
        if self._index + 1 == len(self._durations):
            removed = 0 if self._index == 0 else pre
            self.metadata["end_granule_samples"] = self._part_emitted_start + granule - removed

    def _read_child(self) -> bytes:
        child = self._current
        if child is None:
            raise OpusPacketError("parte Opus não aberta")
        packet = child.read_packet()
        if packet:
            self._validate_eos(packet)
            self._part_samples += opus_packet_samples(packet)
        elif not self._part_validated_eos:
            raise OpusPacketError("parte Opus sem áudio ou EOS validado")
        return packet

    def _begin_part(self) -> bytes:
        opened = self._open_segment(self._index)
        child = (opened if isinstance(opened, OggOpusPacketReader) else
                 OggOpusPacketReader(opened, close_stream=True, _allow_archive_preroll=True))
        # Publish before checking closed: cleanup may run while opening the
        # stream or between the check and assignment. Either thread then owns
        # closing this child; no newly opened stream escapes cancellation.
        self._current = child
        if self._closed:
            child.cleanup()
            return b""
        self._part_samples = 0
        self._part_validated_eos = False
        self._part_emitted_start = self._emitted_samples
        packet = self._read_child()
        pre = int(child.metadata.get("pre_skip_samples", -1))
        if self._index == 0:
            if pre < 0 or pre > 960:
                raise OpusPacketError("pre-skip inicial do arquivo incompatível")
            self.metadata.update({key: child.metadata.get(key) for key in (
                "codec", "sample_rate_hz", "channels", "input_sample_rate_hz", "pre_skip_samples",
                "output_gain_q8", "channel_mapping", "trim_mode")})
            return packet
        # The archive writer repeats its preceding complete packets. Match
        # their bytes as well as their sample count; pre-skip alone is not proof.
        if not 0 < pre <= self.MAX_PREROLL_SAMPLES or pre > self._tail_samples:
            raise OpusPacketError("preroll da parte Opus não confirmado")
        expected: deque[bytes] = deque()
        count = 0
        for previous, samples in reversed(self._tail):
            expected.appendleft(previous)
            count += samples
            if count >= pre:
                break
        if count != pre:
            raise OpusPacketError("preroll Opus não alinhado aos pacotes")
        skipped = 0
        while expected:
            if packet != expected.popleft():
                raise OpusPacketError("pacotes de preroll Opus diferentes da parte anterior")
            skipped += 1
            packet = self._read_child()
        if not packet:
            raise OpusPacketError("parte Opus contém somente preroll")
        self.metadata["archive_preroll_packets_skipped"] += skipped
        self.metadata["archive_preroll_samples_skipped"] += pre
        return packet

    def read_packet(self) -> bytes:
        if self._closed or self._finished:
            return b""
        while True:
            packet = self._begin_part() if self._current is None else self._read_child()
            if self._closed:
                return b""
            if packet:
                samples = opus_packet_samples(packet)
                self._emitted_samples += samples
                self._tail.append((packet, samples))
                self._tail_samples += samples
                while len(self._tail) > 1 and self._tail_samples - self._tail[0][1] >= 3840:
                    self._tail_samples -= self._tail.popleft()[1]
                child = self._current
                final = self._index + 1 == len(self._durations)
                self._eos = bool(final and child is not None and child._eos)
                # The old decoder's bounded final-page validation sees only
                # the logical final page, after preroll has been removed.
                self._packets = child._packets if self._eos else deque()
                self.metadata["archive_segment_index"] = self._index
                self.metadata["audio_packets"] = self.metadata.get("audio_packets", 0) + 1
                return packet
            child = self._current
            if child is not None:
                child.cleanup()
                self._current = None
            self.metadata["archive_segments_completed"] = self._index + 1
            self._index += 1
            if self._index == len(self._durations):
                self._finished = True
                return b""

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        child = self._current
        if child is not None:
            child.cleanup()
        self._tail.clear()
        self._packets = deque()


class OpusPCMDecoderSource(discord.AudioSource):
    """Um decode aquecido acompanha cada pacote original antes de copiá-lo.

    A duração pode variar até 120 ms, mas só um pacote validado de 20 ms e
    no máximo 1275 bytes fica disponível para cópia. Qualquer outra duração
    deixa a faixa definitivamente no caminho PCM, preservando o áudio válido.
    """

    def __init__(self, reader: OggOpusPacketReader, *, decoder_factory: Any = None,
                 cleanup: Any = None) -> None:
        self.reader = reader
        self.decoder = (decoder_factory or discord.opus.Decoder)()
        self._cleanup = cleanup
        self._closed = False
        self._eof = False
        self._pcm = bytearray()
        self._decoded_samples = 0
        self._validated_eos = False
        self.last_opus_packet: bytes | None = None
        self.passthrough_reason = ""
        self.last_read_had_audio = False

    @property
    def opus_metadata(self) -> dict[str, Any]:
        # The producer may discover EOS while the event loop takes a snapshot.
        # Read fixed keys rather than iterating a dictionary growing on it.
        fields = ("codec", "sample_rate_hz", "channels", "input_sample_rate_hz", "pre_skip_samples",
                  "output_gain_q8", "channel_mapping", "trim_mode", "ogg_pages", "audio_packets",
                  "end_granule_samples", "tail_padding_samples", "raw_duration_samples",
                  "archive_reader_continuous", "archive_segment_index", "archive_segments_total", "archive_segments_completed",
                  "archive_preroll_packets_skipped", "archive_preroll_samples_skipped")
        return {**{key: self.reader.metadata.get(key) for key in fields}, "decoded_samples": self._decoded_samples}

    def _validate_eos_page(self, packet: bytes) -> None:
        if self._validated_eos or not self.reader._eos:
            return
        # Validate the entire final page before exposing any of its packets.
        # At most 255 completions fit in an Ogg page, so this lookahead is
        # bounded and performed once, without buffering the rest of the file.
        remaining = (packet, *self.reader._packets)
        if not remaining or len(remaining) > 255:
            raise OpusPacketError("quantidade de pacotes EOS inválida")
        durations = [opus_packet_samples(value) for value in remaining]
        total = self._decoded_samples + sum(durations)
        granule = self.reader.metadata.get("end_granule_samples")
        if (not isinstance(granule, int) or total > 0x7FFFFFFFFFFFFFFF
                or granule > total or total - granule > durations[-1]):
            raise OpusPacketError("corte final Opus excede o último pacote")
        self.reader.metadata.update(raw_duration_samples=total, tail_padding_samples=total - granule)
        self._validated_eos = True

    def is_opus(self) -> bool:
        return False

    def read(self) -> bytes:
        self.last_opus_packet = None
        self.last_read_had_audio = False
        if self._closed:
            return b""
        while len(self._pcm) < FRAME_BYTES and not self._eof:
            packet = self.reader.read_packet()
            if not packet:
                self._eof = True
                break
            self._validate_eos_page(packet)
            samples = opus_packet_samples(packet)
            pcm = self.decoder.decode(packet, fec=False)
            if not isinstance(pcm, bytes) or len(pcm) != samples * 4:
                raise OpusPacketError("decoder Opus não confirmou duração/estéreo")
            self._decoded_samples += samples
            if samples != 960:
                self.passthrough_reason = self.passthrough_reason or "packet_duration"
            elif len(packet) > 1275:
                self.passthrough_reason = self.passthrough_reason or "packet_size"
            if (not self.passthrough_reason and not self._pcm
                    and samples == 960 and len(packet) <= 1275):
                self.last_opus_packet = packet
            self._pcm.extend(pcm)
        if not self._pcm:
            return b""
        frame = bytes(self._pcm[:FRAME_BYTES])
        del self._pcm[:FRAME_BYTES]
        if len(frame) != FRAME_BYTES:
            self.passthrough_reason = self.passthrough_reason or "partial_frame"
            self.last_opus_packet = None
            frame = frame.ljust(FRAME_BYTES, b"\0")
        self.last_read_had_audio = True
        return frame

    @classmethod
    def from_ffmpeg(cls, stream_url: str, *, executable: str = "ffmpeg", before_options: str = "",
                    audio_stream_index: int = -1, decoder_factory: Any = None) -> OpusPCMDecoderSource:
        selected_stream = "0:a:0" if int(audio_stream_index) < 0 else f"0:{int(audio_stream_index)}"
        command = [str(executable), *shlex.split(before_options), "-nostdin", "-i", str(stream_url),
                   "-map", selected_stream, "-vn", "-sn", "-dn",
                   "-c:a", "copy", "-f", "opus", "-page_duration", "20000", "-flush_packets", "1",
                   "-loglevel", "error", "pipe:1"]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, bufsize=0)

        def close_process() -> None:
            # kill closes the blocked producer's stdout read; never wait forever.
            if process.poll() is None:
                with contextlib.suppress(Exception):
                    process.kill()
            with contextlib.suppress(Exception):
                process.stdout.close()
            with contextlib.suppress(Exception):
                process.wait(timeout=0.2)

        try:
            return cls(OggOpusPacketReader(process.stdout), decoder_factory=decoder_factory, cleanup=close_process)
        except BaseException:
            close_process()
            raise

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.last_opus_packet = None
        self._pcm.clear()
        if callable(self._cleanup):
            with contextlib.suppress(Exception):
                self._cleanup()
        self.reader.cleanup()
        # Decoder lifetime follows the producer/source. Do not destroy a native
        # decoder here while another thread may still be completing decode().


class BufferedOpusPCMSource(BufferedPCMSource):
    """Fila atômica PCM+Opus; nunca associa pacote de outra posição ao PCM."""

    opus_direct_capable = True

    def __init__(self, source: OpusPCMDecoderSource, *, max_frames: int = 75,
                 stall_seconds: float = 12.0) -> None:
        self.last_opus_packet: bytes | None = None
        self.passthrough_reason = ""
        super().__init__(source, max_frames=max_frames, stall_seconds=stall_seconds)

    @property
    def opus_metadata(self) -> dict[str, Any]:
        return dict(self.source.opus_metadata)

    def _buffer_entry(self, frame: bytes) -> tuple[bytes, bytes | None, str]:
        return (frame, self.source.last_opus_packet, self.source.passthrough_reason)

    def _read_buffer_entry(self, entry: Any) -> bytes:
        frame, self.last_opus_packet, self.passthrough_reason = entry
        return frame

    def read(self) -> bytes:
        self.last_opus_packet = None
        return super().read()

    def audio_buffer_metrics(self) -> dict[str, Any]:
        return {**super().audio_buffer_metrics(), "opus_packet_buffer_max_bytes": self.max_frames * 1275}
