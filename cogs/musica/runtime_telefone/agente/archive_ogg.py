"""Preserve Opus sample counts when FFmpeg's segment muxer repeats pre-skip.

Each output is a valid, independently decodable Ogg stream. FFmpeg copies the
original OpusHead into every segment; its pre-skip must apply only once, and
intermediate EOS pages must not discard samples because of reset timestamps.
"""
from __future__ import annotations

import struct
from pathlib import Path


def _crc_table() -> list[int]:
    values = []
    for byte in range(256):
        value = byte << 24
        for _ in range(8):
            value = ((value << 1) ^ (0x04C11DB7 if value & 0x80000000 else 0)) & 0xFFFFFFFF
        values.append(value)
    return values


_CRC_TABLE = _crc_table()


def _crc(data: bytes) -> int:
    value = 0
    for byte in data:
        value = ((value << 8) ^ _CRC_TABLE[((value >> 24) ^ byte) & 255]) & 0xFFFFFFFF
    return value


def _packet_samples(packet: bytes) -> int:
    if not packet:
        raise ValueError("pacote Opus vazio")
    config, code = packet[0] >> 3, packet[0] & 3
    if config >= 16:
        samples = 120 << (config & 3)
    elif config >= 12:
        samples = 480 << (config & 1)
    else:
        samples = (480, 960, 1920, 2880)[config & 3]
    frames = 1 if code == 0 else 2 if code in (1, 2) else packet[1] & 63 if len(packet) > 1 else 0
    if not frames or samples * frames > 5760:
        raise ValueError("duração de pacote Opus inválida")
    return samples * frames


def _inspect(path: Path, *, checkpoint=None) -> tuple[int, int, int]:
    samples, preskip, last_granule, packet = 0, 0, 0, bytearray()
    with path.open("rb") as source:
        while True:
            if checkpoint:
                checkpoint()
            header = source.read(27)
            if not header:
                break
            if len(header) != 27 or header[:4] != b"OggS" or header[4] != 0:
                raise ValueError("página Ogg inválida")
            lacing = source.read(header[26])
            body = source.read(sum(lacing))
            if len(lacing) != header[26] or len(body) != sum(lacing):
                raise ValueError("página Ogg incompleta")
            cursor = 0
            for size in lacing:
                packet.extend(body[cursor:cursor + size])
                if len(packet) > 8 * 1024 * 1024:
                    raise ValueError("pacote Ogg excede o orçamento de memória")
                if size < 255:
                    data = bytes(packet)
                    if data.startswith(b"OpusHead"):
                        if len(data) < 19:
                            raise ValueError("cabeçalho Opus inválido")
                        preskip = struct.unpack_from("<H", data, 10)[0]
                    elif not data.startswith(b"OpusTags"):
                        samples += _packet_samples(data)
                    packet.clear()
                cursor += size
            granule = struct.unpack_from("<Q", header, 6)[0]
            if granule != 0xFFFFFFFFFFFFFFFF:
                last_granule = granule
    if packet:
        raise ValueError("pacote Ogg incompleto")
    return samples, preskip, last_granule


def _packets(path: Path, *, checkpoint=None):
    packet = bytearray()
    with path.open("rb") as source:
        while True:
            if checkpoint:
                checkpoint()
            header = source.read(27)
            if not header:
                break
            if len(header) != 27 or header[:4] != b"OggS":
                raise ValueError("página Ogg inválida")
            lacing = source.read(header[26])
            body = source.read(sum(lacing))
            if len(lacing) != header[26] or len(body) != sum(lacing):
                raise ValueError("página Ogg incompleta")
            cursor = 0
            for size in lacing:
                packet.extend(body[cursor:cursor + size])
                if len(packet) > 8 * 1024 * 1024:
                    raise ValueError("pacote Ogg excede o orçamento de memória")
                if size < 255:
                    yield bytes(packet)
                    packet.clear()
                cursor += size
    if packet:
        raise ValueError("pacote Ogg incompleto")


def _write_page(output, *, body: bytes, lacing: bytes, serial: int, sequence: int, granule: int, flags: int):
    page = bytearray(struct.pack("<4sBBQIIIB", b"OggS", 0, flags, granule, serial, sequence, 0, len(lacing)) + lacing + body)
    struct.pack_into("<I", page, 22, _crc(page))
    output.write(page)


def add_opus_preroll(original: Path, paths: list[Path], *, checkpoint=None) -> list[float]:
    """Repackage copied packets with 80 ms decoder preroll, without encoding.

    Middle parts include the preceding packets. Their OpusHead pre-skip consumes
    those packets before playback, restoring decoder state and exact offsets.
    Returns decoded durations (container duration also includes pre-skip).
    """
    from collections import deque
    import os
    import shutil
    import time
    samples, source_preskip, source_granule = _inspect(original, checkpoint=checkpoint)
    end_trim = samples - source_granule
    if end_trim < 0 or end_trim > 5760:
        raise ValueError("granule final Opus inválido")
    if sum(_inspect(path, checkpoint=checkpoint)[0] for path in paths) != samples:
        raise ValueError("a segmentação perdeu pacotes Opus")
    tail, tail_samples, decoded_durations = deque(), 0, []
    for order, path in enumerate(paths):
        packets = iter(_packets(path, checkpoint=checkpoint))
        head, tags = next(packets), next(packets)
        if not head.startswith(b"OpusHead") or not tags.startswith(b"OpusTags"):
            raise ValueError("cabeçalhos Opus ausentes")
        pre = source_preskip if order == 0 else tail_samples
        head = bytearray(head)
        struct.pack_into("<H", head, 10, pre)
        temporary = path.with_name(path.name + ".preroll")
        serial, sequence, granule, current_samples = int.from_bytes(os.urandom(4), "little"), 0, 0, 0
        def write_packet(output, packet, *, position, final=False):
            nonlocal sequence
            # A packet may span Ogg pages; only its final page has a granule.
            laces = [255] * (len(packet) // 255) + [len(packet) % 255]
            cursor, continued = 0, False
            while laces:
                page_laces, laces = laces[:255], laces[255:]
                length = sum(page_laces)
                last = not laces
                flags = (1 if continued else 0) | (2 if sequence == 0 else 0) | (4 if final and last else 0)
                _write_page(output, body=packet[cursor:cursor + length], lacing=bytes(page_laces), serial=serial,
                            sequence=sequence, granule=position if last else 0xFFFFFFFFFFFFFFFF, flags=flags)
                sequence += 1
                cursor += length
                continued = True
        free = shutil.disk_usage(path.parent).free
        reserve = max(64 * 1024 * 1024, int(free * 0.05))
        if free - reserve < path.stat().st_size * 1.15 + 65536:
            raise OSError("espaço temporário insuficiente para preparar Opus")
        last_disk_check = time.monotonic()
        with temporary.open("wb") as output:
            write_packet(output, bytes(head), position=0)
            write_packet(output, tags, position=0)
            for packet, count in list(tail) if order else []:
                granule += count
                write_packet(output, packet, position=granule)
            previous = next(packets, None)
            if previous is None:
                raise ValueError("segmento Opus vazio")
            while previous is not None:
                if checkpoint:
                    checkpoint()
                packet, next_packet = previous, next(packets, None)
                if time.monotonic() - last_disk_check > 1.0:
                    if shutil.disk_usage(path.parent).free < reserve:
                        raise OSError("espaço temporário insuficiente para preparar Opus")
                    last_disk_check = time.monotonic()
                count = _packet_samples(packet)
                granule += count
                current_samples += count
                final = next_packet is None
                trim = end_trim if final and order == len(paths) - 1 else 0
                write_packet(output, packet, position=granule - trim, final=final)
                tail.append((packet, count))
                tail_samples += count
                while len(tail) > 1 and tail_samples - tail[0][1] >= 3840:
                    tail_samples -= tail.popleft()[1]
                previous = next_packet
        temporary.replace(path)
        trim = end_trim if order == len(paths) - 1 else 0
        decoded_durations.append((current_samples - (source_preskip if order == 0 else 0) - trim) / 48000)
    return decoded_durations


def decoded_opus_duration(path: Path, *, checkpoint=None) -> float:
    _samples, preskip, granule = _inspect(path, checkpoint=checkpoint)
    duration = (granule - preskip) / 48000
    if duration <= 0:
        raise ValueError("duração Opus inválida")
    return duration
