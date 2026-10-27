"""Verified, bounded native singing PCM transport for the local compositor."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import struct
import time
import wave

import aiohttp


class SingingPCMProtocolError(RuntimeError):
    pass


def _validate_start(meta, payload, maximum):
    if not isinstance(meta, dict):
        raise SingingPCMProtocolError('metadados PCM inválidos')
    if (type(meta.get('sample_rate')) is not int or meta['sample_rate'] != 44100
            or type(meta.get('channels')) is not int or meta['channels'] != 1
            or type(meta.get('sample_width_bytes')) is not int or meta['sample_width_bytes'] != 2
            or type(meta.get('normalization_gain')) not in (int, float)
            or meta['normalization_gain'] != 1.0):
        raise SingingPCMProtocolError('formato PCM incompatível')
    if (meta.get('voice_id') != payload.get('ttv_voice_id')
            or meta.get('reading_language') != payload.get('ttv_language')
            or meta.get('ttv_performance_hash') != payload.get('ttv_performance_hash')):
        raise SingingPCMProtocolError('o stream PCM retornou outra voz, idioma ou performance')
    preroll = meta.get('midi_preroll_ms')
    if type(preroll) not in (int, float) or not math.isfinite(preroll) or not 0 <= preroll <= 350:
        raise SingingPCMProtocolError('preroll PCM inválido')
    frames = meta.get('expected_frames')
    planned = round((payload['ttv_score']['duration_ms'] + preroll) * 44.1)
    if type(frames) is not int or frames <= 0 or frames * 2 + 44 > maximum or abs(frames - planned) > 2:
        raise SingingPCMProtocolError('duração PCM incompatível')


async def read_pcm_stream(content, payload, on_pcm, *, maximum):
    """Require a final hash receipt; a truncated prefix never enters a cache.

    The callback runs off the bot's event loop and is awaited in wire order.
    An empty first callback conveys the fixed format before any audio bytes.
    """
    start = end = None
    pcm = bytearray()
    hasher = hashlib.sha256()

    async def deliver(data, metadata):
        # The composition adapter only enqueues immutable bytes for its own
        # producer thread. Do not make that queue handoff wait for an executor
        # whose threads may already be waiting for HTTP composition results.
        if getattr(on_pcm, '_cd_enqueue_only', False) is True:
            on_pcm(data, metadata)
        else:
            await asyncio.to_thread(on_pcm, data, metadata)
    while True:
        try:
            header = await content.readexactly(5)
        except asyncio.IncompleteReadError as exc:
            if not exc.partial and end is not None:
                break
            raise SingingPCMProtocolError('stream PCM interrompido antes do recibo final') from exc
        kind, length = header[:1], struct.unpack('!I', header[1:])[0]
        limit = 65536 if kind == b'E' else 16384 if kind == b'M' else 32768
        if kind not in (b'M', b'P', b'E') or not 0 < length <= limit or end is not None:
            raise SingingPCMProtocolError('registro PCM inválido')
        try:
            data = await content.readexactly(length)
        except asyncio.IncompleteReadError as exc:
            raise SingingPCMProtocolError('registro PCM truncado') from exc
        if kind in (b'M', b'E'):
            try:
                meta = json.loads(data)
            except (ValueError, UnicodeDecodeError) as exc:
                raise SingingPCMProtocolError('metadados PCM corrompidos') from exc
            if kind == b'M':
                if start is not None or pcm:
                    raise SingingPCMProtocolError('metadados PCM duplicados')
                _validate_start(meta, payload, maximum)
                start = meta
                await deliver(b'', dict(start))
            else:
                if start is None or not pcm or not isinstance(meta, dict) or meta.get('ok') is not True:
                    raise SingingPCMProtocolError('recibo PCM inválido')
                if (meta.get('pcm_bytes') != len(pcm)
                        or len(pcm) != start['expected_frames'] * 2
                        or meta.get('pcm_sha256') != hasher.hexdigest()
                        or meta.get('ttv_performance_hash') != payload.get('ttv_performance_hash')
                        or meta.get('ttv_voice_id') != payload.get('ttv_voice_id')
                        or meta.get('ttv_language') != payload.get('ttv_language')
                        or meta.get('normalization_gain') != 1.0):
                    raise SingingPCMProtocolError('o recibo final não corresponde ao stream PCM')
                end = meta
        else:
            if start is None or length % 2 or len(pcm) + length > start['expected_frames'] * 2:
                raise SingingPCMProtocolError('bloco PCM fora da duração planejada')
            pcm.extend(data)
            hasher.update(data)
            await deliver(data, dict(start))
    if end is None:
        raise SingingPCMProtocolError('stream PCM sem recibo final')
    output = io.BytesIO()
    with wave.open(output, 'wb') as target:
        target.setparams((1, 2, 44100, 0, 'NONE', ''))
        target.writeframes(pcm)
    raw = output.getvalue()
    expected_hash = end.get('sha256')
    if not isinstance(expected_hash, str) or hashlib.sha256(raw).hexdigest() != expected_hash:
        raise SingingPCMProtocolError('WAV final não corresponde ao stream PCM')
    end.update(audio_format='wav', sample_rate=44100, channels=1, sample_width_bytes=2,
               midi_preroll_ms=start['midi_preroll_ms'], normalization_gain=1.0,
               native_pcm_stream=True)
    return raw, end


async def request_pcm_stream(cog, item, on_pcm):
    """Use the same frozen worker controls and authenticated shared session."""
    from .audio import PHONE_WORKER_TOKEN

    route = cog._tts_agent_route_state()
    if route.get('ttv_pcm_stream_protocol') != 1 or not cog._tts_agent_route_available():
        raise SingingPCMProtocolError('worker sem suporte ao stream PCM de canto')
    payload = dict(cog._tts_agent_payload_for_item(item), cache_mode='bypass')
    if payload.get('engine') != 'teto' or not isinstance(payload.get('ttv_score'), dict):
        raise SingingPCMProtocolError('stream PCM exige uma partitura TTV')
    base = cog._phone_worker_tts_base_url()
    if not base or not PHONE_WORKER_TOKEN:
        raise SingingPCMProtocolError('worker de canto não configurado')
    maximum = min(32 * 1024 * 1024, int(payload.get('max_audio_bytes') or 8 * 1024 * 1024))
    payload['max_audio_bytes'] = maximum
    session = await cog._get_phone_worker_http_session()
    headers = {'Authorization': 'Bearer ' + PHONE_WORKER_TOKEN,
               'Accept': 'application/x-osaka-ttv-pcm', 'Content-Type': 'application/json'}
    started = time.monotonic()
    metrics = cog._get_metrics_store()
    metrics['tts_agent_synth_attempts'] = int(metrics.get('tts_agent_synth_attempts', 0)) + 1
    async with session.post(base + '/tts-agent/singing.pcm', json=payload, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=124), allow_redirects=False) as response:
        if (response.status != 200
                or response.headers.get('X-Core-Worker-TTV-PCM-Protocol') != '1'
                or response.content_type != 'application/x-osaka-ttv-pcm'):
            await response.content.read(512)
            raise SingingPCMProtocolError(f'worker recusou o stream PCM (HTTP {response.status})')
        raw, metadata = await read_pcm_stream(response.content, payload, on_pcm, maximum=maximum)
    total_ms = (time.monotonic() - started) * 1000
    metadata['_vps_worker_request_ms'] = round(total_ms, 2)
    item._tts_agent_selected_engine = 'teto'
    item._tts_agent_render_metadata = metadata
    cog._record_tts_agent_synth_success(total_ms=total_ms, data=metadata)
    return raw, metadata
