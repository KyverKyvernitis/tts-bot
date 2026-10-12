"""TTS provider adapters with no runtime state or import-time provider loading.

The facade supplies its live locks, renderer, clocks and normalization helpers.
Admission, cache, output envelopes and the shared transport keep their owners.
"""


def synthesize_teto(*, text, timeout, max_audio_bytes, logs, stage_ms,
                    heavy_lock, get_renderer, monotonic, normalize_format,
                    pitch_offset_semitones=0.0, speech_rate=None, reading_language=None,
                    score=None, segments=None, performance_hash=None):
    if not heavy_lock.acquire(blocking=False):
        raise RuntimeError("recurso pesado ocupado por build ou manutenção")
    teto_started = monotonic()
    try:
        controls = {}
        if speech_rate is not None:
            controls["speech_rate"] = float(speech_rate)
        if reading_language is not None:
            controls["reading_language"] = reading_language
        renderer = get_renderer()
        if score is not None or segments is not None:
            perform = getattr(renderer, "synthesize_performance", None)
            if not callable(perform):
                raise RuntimeError("motor sem suporte MIDI/efeitos TTV; use ESPER e atualize o runtime")
            data, metadata = perform(
                text, score=score, segments=segments,
                timeout_seconds=float(timeout), max_audio_bytes=max_audio_bytes,
                pitch_offset_semitones=float(pitch_offset_semitones), **controls)
            rendered = {**metadata, "audio": data, "audio_format": "wav"}
            if rendered.get("ttv_performance_hash") != performance_hash:
                raise RuntimeError("renderer retornou áudio de outra performance TTV")
        else:
            rendered = renderer.synthesize(
                text, timeout_seconds=float(timeout), max_audio_bytes=max_audio_bytes,
                pitch_offset_semitones=float(pitch_offset_semitones), **controls)
    finally:
        heavy_lock.release()
    data = bytes(rendered.pop("audio", b"") or b"")
    audio_format = normalize_format(rendered.get("audio_format") or "wav")
    teto_meta = dict(rendered)
    stage_ms["teto_render"] = round((monotonic() - teto_started) * 1000.0, 2)
    performance_log = (
        f"workers={rendered.get('render_workers') or 0} "
        f"fragment_hits={rendered.get('fragment_cache_hits') or 0} "
        f"fragment_misses={rendered.get('fragment_cache_misses') or 0} "
        f"resampler_calls={rendered.get('resampler_calls') or 0} "
        if "render_workers" in rendered else ""
    )
    logs.append(
        f"teto voicebank={rendered.get('voicebank') or 'Kasane Teto'} "
        f"renderer={rendered.get('renderer_version') or '?'} "
        f"phonemizer={rendered.get('phonemizer_version') or '?'} "
        f"language={rendered.get('reading_language') or reading_language or 'pt-BR'} "
        f"rendered={rendered.get('rendered_phonemes') or 0} "
        f"aux={rendered.get('auxiliary_phonemes') or 0} "
        f"epenthetic={rendered.get('epenthetic_phonemes') or 0} "
        f"profile={rendered.get('voicebank_profile') or 'standard'} "
        f"coverage={rendered.get('coverage_percent') if rendered.get('coverage_percent') is not None else '-'} "
        f"clusters={rendered.get('cluster_hits') or 0} "
        f"timeline={rendered.get('timeline_mode') or 'serial'} "
        f"pitch={rendered.get('pitch_offset_semitones') if rendered.get('pitch_offset_semitones') is not None else 0.0:+.1f}st "
        f"overlays={rendered.get('timeline_aux_overlays') or 0} "
        f"path_cost={rendered.get('alias_path_cost') if rendered.get('alias_path_cost') is not None else '-'} "
        f"pitch_jump={rendered.get('pitch_boundary_max_cents') if rendered.get('pitch_boundary_max_cents') is not None else '-'} "
        f"energy_jump={rendered.get('energy_boundary_max_db') if rendered.get('energy_boundary_max_db') is not None else '-'} "
        f"repairs={rendered.get('continuity_repairs') or 0} "
        f"{performance_log}"
        f"missing={len(rendered.get('missing_phonemes') or [])}"
    )
    return data, audio_format, teto_meta


def synthesize_edge(body, *, text, timeout, logs, normalize_rate, normalize_pitch,
                    short_text, asyncio_api, io_api):
    try:
        import edge_tts  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"edge-tts não instalado no worker: {type(exc).__name__}: {short_text(exc, limit=120)}") from exc
    voice = str(body.get("voice") or body.get("fallback_voice") or "pt-BR-FranciscaNeural").strip() or "pt-BR-FranciscaNeural"
    rate = normalize_rate(body.get("rate"))
    pitch = normalize_pitch(body.get("pitch"))

    async def _edge_bytes() -> bytes:
        communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
        buffer = io_api.BytesIO()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                buffer.write(chunk["data"])
        return buffer.getvalue()

    data = asyncio_api.run(asyncio_api.wait_for(_edge_bytes(), timeout=timeout))
    logs.append(f"edge voice={voice} rate={rate} pitch={pitch}")
    return data


def synthesize_gtts(body, *, text, timeout, logs, normalize_language, short_text, io_api):
    try:
        from gtts import gTTS  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"gTTS não instalado no worker: {type(exc).__name__}: {short_text(exc, limit=120)}") from exc
    language = normalize_language(body.get("language") or body.get("fallback_language"))
    buffer = io_api.BytesIO()
    tts = gTTS(text=text, lang=language, timeout=(min(3.5, timeout), min(8.0, timeout)))
    tts.write_to_fp(buffer)
    data = buffer.getvalue()
    logs.append(f"gtts language={language}")
    return data

