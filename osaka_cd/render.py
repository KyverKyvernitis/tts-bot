"""Compose independently rendered monophonic UTAU parts on one MIDI clock.

The renderer is injected: the composition layer never starts or modifies a
worker. Each request remains a version 1 score understood by Phone Worker
1.11.41. Musical simultaneity does not require concurrent CPU synthesis.
"""
from __future__ import annotations

import array
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import wave

RENDER_VERSION = "osaka-multipart-2"
MAX_AUDIO_BYTES = 128 * 1024 * 1024
MAX_STEM_BYTES = 16 * 1024 * 1024
SAMPLE_RATE = 48000


class RenderError(ValueError):
    """Stable, credential-free failure code suitable for a pending artifact."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _check(cancelled, deadline):
    if cancelled is not None and cancelled():
        raise RenderError("render_cancelled")
    if time.monotonic() >= deadline:
        raise RenderError("render_deadline")


def _wav_info(raw):
    if not isinstance(raw, bytes) or not 44 < len(raw) <= MAX_AUDIO_BYTES:
        raise RenderError("invalid_stem_audio")
    try:
        with wave.open(io.BytesIO(raw), "rb") as source:
            channels, width, rate, frames = (source.getnchannels(), source.getsampwidth(),
                                           source.getframerate(), source.getnframes())
            if channels != 1 or width != 2 or not 8000 <= rate <= 96000 or frames <= 0:
                raise RenderError("invalid_stem_pcm")
            pcm = source.readframes(frames)
            if len(pcm) != frames * channels * width:
                raise RenderError("truncated_stem_audio")
            samples = array.array("h", pcm)
            if sys.byteorder != "little":
                samples.byteswap()
            if not any(samples):
                raise RenderError("silent_stem_audio")
            return rate, frames, pcm
    except (wave.Error, EOFError, OSError) as exc:
        raise RenderError("invalid_stem_audio") from exc


def _result_audio(result):
    if isinstance(result, tuple) and len(result) == 2:
        audio, meta = result
    elif isinstance(result, dict):
        audio = result.get("audio", result.get("raw_audio"))
        meta = result.get("metadata", result)
    else:
        raise RenderError("invalid_renderer_result")
    if not isinstance(meta, dict):
        raise RenderError("invalid_renderer_metadata")
    _wav_info(audio)
    declared = meta.get("sha256")
    if declared and declared != hashlib.sha256(audio).hexdigest():
        raise RenderError("renderer_audio_hash_mismatch")
    return audio, meta


def _preroll(audio, metadata, score):
    rate, frames, _ = _wav_info(audio)
    actual = frames * 1000 / rate - score["duration_ms"]
    supplied = metadata.get("midi_preroll_ms")
    if supplied is None:
        # The immutable 1.11.41 renderer trims to exactly duration + preroll;
        # its binary endpoint omits preroll but preserves the original WAV.
        version = metadata.get("worker_version", metadata.get("version"))
        timeline = metadata.get("teto_timeline_mode", metadata.get("timeline_mode"))
        cached_utau = (metadata.get("cache_hit") is True
                       and metadata.get("renderer_version") == "speech-4g-cvvc-pauses")
        if version != "1.11.41" or not (timeline == "midi-vowel-continuous" or cached_utau):
            raise RenderError("stem_alignment_metadata_missing")
        supplied = actual
    if isinstance(supplied, bool) or not isinstance(supplied, (int, float)):
        raise RenderError("invalid_stem_preroll")
    if not math.isfinite(supplied) or not -1000 / rate <= supplied <= 350 + 1000 / rate:
        raise RenderError("invalid_stem_preroll")
    if abs(actual - supplied) > 1.6 * 1000 / rate:
        raise RenderError("stem_duration_mismatch")
    return max(0.0, float(supplied))


def _atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="." + path.name + ".", dir=path.parent,
                                     delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _cached_stem(cache, key, score):
    if cache is None:
        return None
    try:
        path = cache / "stems" / (key + ".wav")
        if not 44 < path.stat().st_size <= MAX_STEM_BYTES:
            return None
        meta_path = path.with_suffix(".json")
        if meta_path.stat().st_size > 16 * 1024:
            return None
        record = json.loads(meta_path.read_text())
        audio = path.read_bytes()
        if record.get("key") != key or record.get("sha256") != hashlib.sha256(audio).hexdigest():
            return None
        meta = record["metadata"]
        _preroll(audio, meta, score)
        return audio, meta
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _remember_stem(cache, key, audio, metadata, preroll):
    if cache is None:
        return
    # Store only timing and opaque identities, never arbitrary backend metadata
    # which can contain paths, private config or command diagnostics.
    meta = {"midi_preroll_ms": preroll}
    for name in ("worker_version", "renderer_version", "renderer_fingerprint",
                 "voicebank_fingerprint", "teto_fingerprint"):
        value = metadata.get(name)
        if isinstance(value, str) and len(value) <= 128:
            meta[name] = value
    path = cache / "stems" / (key + ".wav")
    record = {"key": key, "sha256": hashlib.sha256(audio).hexdigest(), "metadata": meta}
    try:
        _atomic(path, audio)
        _atomic(path.with_suffix(".json"), json.dumps(record, sort_keys=True).encode())
        _prune_cache(cache, path)
    except OSError:
        # Reuse is optional. A full cache disk must not discard a good render.
        pass


def _ffmpeg(arguments, *, data=None, timeout=60):
    try:
        result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *arguments],
                                input=data, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderError("composition_process_failed") from exc
    if result.returncode:
        raise RenderError("composition_process_failed")
    return result.stdout


def _word_allocation(text, requests):
    """Assign message words once, in order; never repeat the full utterance."""
    words = text.split()
    if not words:
        return [""] * len(requests)
    if len(requests) == 1:
        return [text.strip()]
    if len(words) < len(requests):
        raise RenderError("insufficient_text_for_chunks")
    weights = [max(1, item.get("minimum_syllables", len(item["score"]["notes"])))
               for item in requests]
    cursor, remaining_weight = 0, sum(weights)
    assigned = []
    for number, weight in enumerate(weights):
        remaining_words = len(words) - cursor
        count = remaining_words if number == len(weights) - 1 else max(
            1, min(remaining_words - (len(weights) - number - 1),
                   round(remaining_words * weight / remaining_weight)))
        assigned.append(" ".join(words[cursor:cursor + count]))
        cursor += count
        remaining_weight -= weight
    return assigned


def _message_allocation(text, requests, segments):
    """Partition effects with their words, keeping holds attached to an anchor.

    Each old-worker request is independent. A standalone hold or phrase marker
    therefore stays in the same request as its preceding word. If there are
    fewer anchored units than physical chunks, fail instead of duplicating a
    lyric or starting an isolated request with an unanchored hold.
    """
    if segments is None:
        return [(words, None) for words in _word_allocation(text, requests)]
    expected = {"text", "fade_in", "fade_out", "hold", "pause_after_ms"}
    if not isinstance(segments, list) or not 1 <= len(segments) <= 128:
        raise RenderError("invalid_composition_segments")
    for segment in segments:
        if (not isinstance(segment, dict) or set(segment) != expected
                or not isinstance(segment["text"], str) or not segment["text"].strip()
                or len(segment["text"]) > 2048
                or any(type(segment[key]) is not bool for key in ("fade_in", "fade_out", "hold"))
                or type(segment["pause_after_ms"]) is not int or segment["pause_after_ms"] not in {0, 180}):
            raise RenderError("invalid_composition_segments")
    joined = " ".join(" ".join(segment["text"].split()) for segment in segments)
    if joined != " ".join(text.split()):
        raise RenderError("composition_segments_text_mismatch")
    if not requests:
        return []
    units = []
    for segment in segments:
        words = segment["text"].split()
        for index, word in enumerate(words):
            fragment = {"text": word, "fade_in": segment["fade_in"] and index == 0,
                        "fade_out": segment["fade_out"] and index == len(words) - 1,
                        "hold": segment["hold"] and index == len(words) - 1,
                        "pause_after_ms": segment["pause_after_ms"] if index == len(words) - 1 else 0}
            marker = word == "|" or word == "~" and fragment["hold"]
            if marker:
                if not units:
                    raise RenderError("composition_effect_anchor_missing")
                units[-1].append(fragment)
            else:
                units.append([fragment])
    # Reuse the weighted, exact-once allocator for opaque unit indices.
    allocation = _word_allocation(" ".join(map(str, range(len(units)))), requests)
    result = []
    for allocated in allocation:
        fragments = [dict(fragment) for token in allocated.split()
                     for fragment in units[int(token)]]
        lyrics = " ".join(fragment["text"] for fragment in fragments)
        if len(lyrics) > 2048 or len(fragments) > 128:
            raise RenderError("composition_chunk_text_limit")
        result.append((lyrics, fragments))
    return result


def _mix_controls(part):
    default_gain = 0.85 if part.get("role") == "lead" else 0.50
    gain, pan = part.get("gain", default_gain), part.get("pan", 0.0)
    if (isinstance(gain, bool) or not isinstance(gain, (int, float))
            or not math.isfinite(gain) or not 0 <= gain <= 2):
        raise RenderError("invalid_part_gain")
    if (isinstance(pan, bool) or not isinstance(pan, (int, float))
            or not math.isfinite(pan) or not -1 <= pan <= 1):
        raise RenderError("invalid_part_pan")
    return float(gain), float(pan)


def _composition_result(audio, output_format, mix_key, arrangement, rendered,
                        common_preroll, profile, chunks, *, cache_hit=False, alignment_warnings=()):
    return {"audio": audio, "audio_format": output_format,
            "sha256": hashlib.sha256(audio).hexdigest(), "mix_sha256": mix_key,
            "arrangement_sha256": _digest(arrangement), "duration_ms": arrangement["duration_ms"],
            "mix_cache_hit": cache_hit,
            "stems": [{key: item[key] for key in ("part_id", "voice_id", "cache_hit", "key")}
                      | {"score_sha256": _digest(item["score"]), "render_sha256": item["sha256"],
                         "start_ms": item["start_ms"]} for item in rendered],
            "metadata": {"renderer_version": RENDER_VERSION,
                         **({'lyrics_adaptation': arrangement['lyrics_adaptation']}
                            if 'lyrics_adaptation' in arrangement else {}),
                         "render_profile_fingerprint": profile,
                         "midi_preroll_ms": common_preroll, "sample_rate": SAMPLE_RATE,
                         "channels": 2, "parts": len(arrangement["parts"]), "chunks": chunks,
                         "alignment_warnings": list(alignment_warnings),
                         "chunk_continuations_reattacked": any(item["continuations"] for item in rendered)}}


def _cached_mix(cache, mix_key, output_format):
    if cache is None:
        return None
    path = cache / "mixes" / (mix_key + "." + output_format)
    try:
        if not 44 < path.stat().st_size <= MAX_AUDIO_BYTES:
            return None
        metadata_path = path.with_suffix(".json")
        if metadata_path.stat().st_size > 16 * 1024:
            return None
        record = json.loads(metadata_path.read_text())
        if record.get("key") != mix_key:
            return None
        audio = path.read_bytes()
        if record.get("sha256") != hashlib.sha256(audio).hexdigest():
            return None
        return audio
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _prune_cache(cache, protected):
    """Bound reusable artifacts to 256 MiB/512 audio entries, oldest first."""
    if cache is None:
        return
    try:
        entries = [(path.stat().st_mtime_ns, path.stat().st_size, path)
                   for directory in (cache / "stems", cache / "mixes")
                   if directory.is_dir() for path in directory.iterdir()
                   if path.suffix in {".wav", ".mp3"} and path.is_file()]
        total, count = sum(entry[1] for entry in entries), len(entries)
        for _, size, path in sorted(entries):
            if total <= 256 * 1024 * 1024 and count <= 512:
                break
            if path == protected:
                continue
            path.unlink(missing_ok=True)
            path.with_suffix(".json").unlink(missing_ok=True)
            total -= size
            count -= 1
    except OSError:
        # An optional cache failure cannot invalidate a completed composition.
        pass


def render_arrangement(arrangement, text, render_part, *, segments=None, cache_dir=None,
                       render_profile_fingerprint="", language="pt-BR",
                       output_format="wav", cancelled=None, timeout_seconds=1200):
    with tempfile.TemporaryDirectory(prefix="osaka-cd-stems-") as workspace:
        return _render_arrangement(arrangement, text, render_part, segments=segments, cache_dir=cache_dir,
            render_profile_fingerprint=render_profile_fingerprint, language=language,
            output_format=output_format, cancelled=cancelled, timeout_seconds=timeout_seconds,
            workspace=Path(workspace))


def _render_arrangement(arrangement, text, render_part, *, segments, cache_dir,
                        render_profile_fingerprint, language, output_format,
                        cancelled, timeout_seconds, workspace):
    """Return a synchronized stereo composition from the existing v1 backend.

    render_part(**request) returns ``(mono_pcm16_wav_bytes, metadata)`` or a
    dictionary containing audio/raw_audio and metadata. A cancellation check
    occurs before/after each physical request and composition stage. The
    caller must keep its admission lease until a blocking request has ended.
    """
    from .arrangement import arrangement_render_plan, validate_arrangement, vowel_guide_score
    arrangement = validate_arrangement(arrangement)
    if not isinstance(text, str) or language not in {"pt-BR", "en-US"}:
        raise RenderError("invalid_render_text_or_language")
    if output_format not in {"wav", "mp3"}:
        raise RenderError("invalid_composition_format")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 3600):
        raise RenderError("invalid_render_timeout")
    deadline = time.monotonic() + timeout_seconds
    _check(cancelled, deadline)
    # An empty fingerprint deliberately disables cross-request stem reuse:
    # a version number alone cannot identify a changed voicebank/resampler.
    cache = Path(cache_dir) if cache_dir is not None and render_profile_fingerprint else None
    automatic = arrangement.get('message_lyrics_mode') == 'automatic-lyrics-v1'
    if automatic:
        from .lyrics import automatic_render_plan
        plan = automatic_render_plan(arrangement, text, segments, language=language)
    else:
        plan = arrangement_render_plan(arrangement)
    parts = {part["id"]: part for part in arrangement["parts"]}
    controls = {key: _mix_controls(value) for key, value in parts.items()}
    if not any(gain > 0 for gain, _ in controls.values()):
        raise RenderError("silent_composition_gain")
    rendered = []
    # Each explicitly requested message part sings the utterance once on its
    # own phrase windows. A harmony marked "message" must not silently become
    # a guide vowel; the usual imported harmony remains explicitly "vowel".
    message_words = {}
    for identity in parts if not automatic else ():
        requests = [part for chunk in plan["chunks"] for part in chunk["parts"]
                    if part["id"] == identity and part["score"].get("lyrics_source") == "message"]
        if requests:
            message_words[identity] = iter(_message_allocation(text, requests, segments))
    for chunk in plan["chunks"]:
        for request in chunk["parts"]:
            _check(cancelled, deadline)
            score = json.loads(json.dumps(request["score"]))
            effects = None
            if score.get("lyrics_source") == "message":
                if automatic:
                    lyrics, effects = request['adaptive_text'], request['adaptive_segments']
                else:
                    lyrics, effects = next(message_words[request["id"]])
                if not lyrics:
                    score = vowel_guide_score(score)
                    lyrics = "a"
            elif score.get("lyrics_source") == "midi":
                lyrics = "a"  # the worker reads each note's MIDI lyric itself
            else:
                score["lyrics_source"] = "vowel"
                lyrics = "a"
            call = {"score": score, "text": lyrics, "voice_id": request["voice_id"],
                    "language": language, "pitch": 0.0, "rate": 1.0,
                    "part_id": request["id"], "role": request.get("role", "harmony")}
            if effects is not None:
                call["segments"] = effects
            # Role/id/mix are orchestration labels, not synthesis inputs.
            key = _digest({"version": RENDER_VERSION, "score": score, "text": lyrics,
                           "segments": effects,
                           "voice_id": call["voice_id"], "language": language,
                           "render_profile": render_profile_fingerprint})
            saved = _cached_stem(cache, key, score)
            hit = saved is not None
            audio, meta = saved if hit else _result_audio(render_part(**call))
            _check(cancelled, deadline)
            preroll = _preroll(audio, meta, score)
            if len(audio) > MAX_STEM_BYTES:
                raise RenderError("stem_size_limit")
            if not hit:
                _remember_stem(cache, key, audio, meta, preroll)
            audio_sha256 = hashlib.sha256(audio).hexdigest()
            audio_path = workspace / (str(len(rendered)) + ".wav")
            audio_path.write_bytes(audio)
            del audio
            rendered.append({"part_id": request["id"], "start_ms": chunk["start_ms"],
                             "score": score, "audio_path": audio_path, "sha256": audio_sha256,
                             "preroll_ms": preroll,
                             "key": key, "cache_hit": hit, "voice_id": call["voice_id"],
                             "continuations": any(bool(marker.get("continues_from_previous")
                                                       or marker.get("continues_in_next"))
                                                  for marker in request.get("continuations", []))})
    if not rendered:
        raise RenderError("empty_composition")
    common_preroll = max(item["preroll_ms"] for item in rendered)
    frames = round((arrangement["duration_ms"] + common_preroll) * SAMPLE_RATE / 1000)
    if frames * 4 + 44 > MAX_AUDIO_BYTES:
        raise RenderError("composition_size_limit")
    mix_key = _digest({"version": RENDER_VERSION, "parts": [
        {"key": item["key"], "sha256": item["sha256"],
         "offset": item["start_ms"], "preroll": item["preroll_ms"]} for item in rendered],
        "controls": controls, "duration_ms": arrangement["duration_ms"],
        "sample_rate": SAMPLE_RATE, "format": output_format})
    saved_mix = _cached_mix(cache, mix_key, output_format)
    if saved_mix is not None:
        _check(cancelled, deadline)
        return _composition_result(saved_mix, output_format, mix_key, arrangement, rendered,
                                   common_preroll, render_profile_fingerprint, len(plan["chunks"]),
                                   cache_hit=True, alignment_warnings=plan["warnings"])
    with tempfile.TemporaryDirectory(prefix="osaka-cd-mix-") as temporary:
        directory = Path(temporary)
        inputs, labels, filters = [], [], []
        for number, (part_id, part) in enumerate(parts.items()):
            _check(cancelled, deadline)
            raw_path = directory / (str(number) + ".pcm")
            # Sparse on-disk timelines avoid holding full-song float arrays in
            # phone RAM. Only individual <=18s chunks and <=350ms overlap live
            # in memory; unchanged stems remain independently reusable.
            with raw_path.open("w+b") as stream:
                stream.truncate(frames * 2)
                previous_end = 0
                for item in (entry for entry in rendered if entry["part_id"] == part_id):
                    stem_audio = item["audio_path"].read_bytes()
                    source_rate, _, pcm = _wav_info(stem_audio)
                    if source_rate != SAMPLE_RATE:
                        pcm = _ffmpeg(["-i", "pipe:0", "-ac", "1", "-ar", str(SAMPLE_RATE),
                                       "-f", "s16le", "pipe:1"], data=stem_audio, timeout=40)
                    del stem_audio
                    offset = max(0, round((common_preroll + item["start_ms"] - item["preroll_ms"])
                                         * SAMPLE_RATE / 1000))
                    pcm = pcm[:max(0, frames - offset) * 2]
                    overlap = max(0, min(len(pcm) // 2, previous_end - offset))
                    stream.seek(offset * 2)
                    if overlap:
                        old = array.array("h", stream.read(overlap * 2))
                        incoming = array.array("h", pcm[:overlap * 2])
                        if sys.byteorder != "little":
                            old.byteswap(); incoming.byteswap()
                        # Two consecutive requests may overlap only in their
                        # consonantal preroll/tail. Blend these same-part
                        # waveforms instead of adding then hard-clipping them.
                        # Constant sustained vowels retain constant amplitude.
                        denominator = max(1, overlap - 1)
                        combined = array.array("h", (round(first * (1 - index / denominator)
                                                           + second * (index / denominator))
                                                     for index, (first, second)
                                                     in enumerate(zip(old, incoming))))
                        if sys.byteorder != "little":
                            combined.byteswap()
                        pcm = combined.tobytes() + pcm[overlap * 2:]
                    stream.seek(offset * 2)
                    stream.write(pcm)
                    previous_end = max(previous_end, offset + len(pcm) // 2)
            inputs += ["-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", str(raw_path)]
            gain, pan = controls[part_id]
            left, right = min(1.0, 1 - pan), min(1.0, 1 + pan)
            filters.append(f"[{number}:a]volume={gain:.9f},pan=stereo|c0={left:.9f}*c0|c1={right:.9f}*c0[p{number}]")
            labels.append(f"[p{number}]")
        # Guaranteed headroom bounds the sum even for identical phase-aligned
        # voices. No per-chunk peak normalization can pump the musical mix.
        attenuation = 0.92 / sum(value[0] for value in controls.values())
        filters.append("".join(labels) + f"amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0,volume={attenuation:.9f}[out]")
        target = directory / ("composition." + output_format)
        encoding = ["-c:a", "pcm_s16le"] if output_format == "wav" else ["-c:a", "libmp3lame", "-b:a", "128k"]
        _check(cancelled, deadline)
        _ffmpeg([*inputs, "-filter_complex", ";".join(filters), "-map", "[out]", *encoding, str(target)],
                timeout=min(120, max(1, deadline - time.monotonic())))
        _check(cancelled, deadline)
        if not 44 < target.stat().st_size <= MAX_AUDIO_BYTES:
            raise RenderError("invalid_composition_audio")
        _ffmpeg(["-xerror", "-i", str(target), "-f", "null", "-"], timeout=60)
        audio = target.read_bytes()
    if cache is not None:
        mix_path = cache / "mixes" / (mix_key + "." + output_format)
        try:
            _atomic(mix_path, audio)
            _atomic(mix_path.with_suffix(".json"), json.dumps({"key": mix_key,
                "sha256": hashlib.sha256(audio).hexdigest()}, sort_keys=True).encode())
            _prune_cache(cache, mix_path)
        except OSError:
            pass
    return _composition_result(audio, output_format, mix_key, arrangement, rendered,
                               common_preroll, render_profile_fingerprint, len(plan["chunks"]),
                               alignment_warnings=plan["warnings"])
