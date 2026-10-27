#!/usr/bin/env python3
"""Measure real local CD singing with isolated ESPER, fragment and mix caches.

Requires installed voicebanks, ESPER and FFmpeg. Never calls Discord/network or
modifies the worker configuration. Run while the phone worker is idle.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'deploy/termux/phone-worker'
for directory in (ROOT, WORKER):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

VOICES = {'teto': ('kasane-teto', 'TETO', (1, 2)),
          'miku': ('hatsune-miku', 'MIKU', (1, 2, 4))}
TELEMETRY_KEYS = ('render_workers', 'source_groups', 'fragment_cache_hits',
                  'fragment_cache_misses', 'resampler_calls', 'resample_ms',
                  'worker_synth_ms', 'compose_ms', 'postprocess_ms',
                  'cache_maintenance_ms', 'cache_scans', 'cache_evictions',
                  'cache_size_bytes', 'rendered_phonemes', 'pcm_cache_hits',
                  'pcm_cache_misses', 'pcm_cache_bytes', 'normalization_gain',
                  'first_pcm_ms', 'streamed_frames')
NOTE = ('Local UTAU/ESPER singing and composition only; excludes HTTP, bot queue and Discord playback. '
        'Cold means empty temporary ESPER analysis cache, not a cold OS/filesystem. '
        'Production caches are not measured. Temperature and other apps affect results.')


def quality_helpers():
    path = ROOT / 'deploy/ttv-quality/termux/compare-quality.py'
    spec = importlib.util.spec_from_file_location('_cd_latency_quality', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_settings(quality, env):
    """Read audio literals only, without evaluating .env or retaining tokens."""
    helper = quality.installer()
    allowed = quality.ALLOWED_KEYS | frozenset(
        f'PHONE_WORKER_{voice}_{key}' for voice in ('TETO', 'MIKU')
        for key in ('MIN_ALIASES', 'ENGLISH_MIN_ALIASES', 'FRAGMENT_CACHE_MB', 'PCM_CACHE_MB'))
    raw = helper.regular_bytes(env, helper.MAX_ENV_BYTES, allow_empty=True)
    if b'\0' in raw:
        raise ValueError('invalid configuration')
    values = {}
    for line in raw.decode('utf-8').splitlines():
        match = helper.KEY.match(line)
        if not match or match.group(1) not in allowed:
            continue
        value, _ = helper.split_comment(line[match.end():].strip())
        if value.startswith('"'):
            value = json.loads(value.replace('\\$', '$').replace('\\`', '`'))
        elif value.startswith("'"):
            quoted = re.fullmatch(r"'([^']*)'", value)
            if not quoted:
                raise ValueError('invalid quoted audio setting')
            value = quoted.group(1)
        if not isinstance(value, str) or any(c in value for c in '\0\r\n'):
            raise ValueError('invalid audio setting')
        values[match.group(1)] = value
    return values


def isolated_environment(quality, values, *, voice, bank, container, esper_root,
                         analysis, fragments, threads, timeout):
    env = quality.controls(quality.installer(), values, voice=voice, directory=bank,
        container=container, analysis=analysis, fragments=fragments,
        timeout=timeout, esper_root=esper_root)
    # An arrangement can contain several physical renders. Their individual
    # deadlines are enforced by synthesize_performance, not one expired env var.
    # An empty override also prevents the renderer's subprocess environment
    # from inheriting a stale deadline from a shell that launched this tool.
    env['PHONE_WORKER_ESPER_DEADLINE'] = ''
    env['PHONE_WORKER_TETO_RENDER_THREADS'] = str(threads)
    prefix = VOICES[voice][1]
    for key in ('MIN_ALIASES', 'ENGLISH_MIN_ALIASES', 'FRAGMENT_CACHE_MB', 'PCM_CACHE_MB'):
        value = values.get(f'PHONE_WORKER_{prefix}_{key}')
        if value:
            env[f'PHONE_WORKER_TETO_{key}'] = value
    return env


def renderer_factory(environment, voice_id):
    from teto_renderer import TetoRenderer
    return TetoRenderer(environment=environment, voice_id=voice_id)


def load_arrangement(cd_id, arrangement_file=None):
    from osaka_cd.arrangement import validate_arrangement
    if arrangement_file is not None:
        path = Path(arrangement_file)
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('arrangement JSON exceeds 8 MiB')
        return validate_arrangement(json.loads(path.read_text(encoding='utf-8')))
    from cogs.tts.melodies import arrangement_for_melody, score_for_melody
    value = arrangement_for_melody(cd_id, preview=True)
    if value is None:
        score = score_for_melody(cd_id)
        value = {'score_schema': 2, 'duration_ms': score['duration_ms'], 'parts': [
            {'id': 'lead', 'role': 'lead', 'voice_id': 'kasane-teto',
             'lyrics_source': 'message', 'notes': score['notes']}]}
    return validate_arrangement(value)


def prepare(arrangement, text, voice_id, language):
    from cogs.tts.arrangement_audio import execution_arrangement
    from cogs.tts.performance import parse_performance_text
    from osaka_cd.lyrics import prepare_message_arrangement
    clean, segments = parse_performance_text(text, singing=True)
    frozen = execution_arrangement(arrangement, voice_id)
    return clean, segments, prepare_message_arrangement(frozen, clean, segments, language=language)


def pcm_digest(raw):
    with wave.open(io.BytesIO(raw), 'rb') as audio:
        if audio.getsampwidth() != 2 or audio.getcomptype() != 'NONE':
            raise ValueError('renderer did not produce PCM16 WAV')
        pcm = audio.readframes(audio.getnframes())
        if len(pcm) != audio.getnframes() * audio.getnchannels() * 2:
            raise ValueError('truncated WAV')
        identity = f'{audio.getframerate()}:{audio.getnchannels()}:'.encode()
        return hashlib.sha256(identity + pcm).hexdigest()


def battery_temperature():
    if not shutil.which('termux-battery-status'):
        return None
    try:
        result = subprocess.run(['termux-battery-status'], capture_output=True, timeout=2, check=True)
        value = json.loads(result.stdout).get('temperature')
        return value if type(value) in (int, float) and math.isfinite(value) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def measure(arrangement, text, *, renderer, voice_id, language, cache, timeout,
            phase, threads, round_number, references, pcm_hashes, progressive=False,
            memory_cache=None):
    from osaka_cd.render import render_arrangement
    record = {'phase': phase, 'threads_requested': threads, 'round': round_number,
              'ok': False, 'temperature_before_c': battery_temperature(), 'physical_parts': [],
              'output_mode': 'progressive_pcm' if progressive else 'complete_mp3'}
    started = time.perf_counter()
    try:
        clean, segments, adapted = prepare(arrangement, text, voice_id, language)
        record['prepare_ms'] = round((time.perf_counter() - started) * 1000, 3)
        profile_start = time.perf_counter()
        profile = renderer.singing_fingerprint(reading_language=language)
        record['profile_ms'] = round((time.perf_counter() - profile_start) * 1000, 3)
        if not profile or profile == 'unavailable':
            raise ValueError('singing voicebank/resampler unavailable')

        def render_part(**request):
            on_pcm = request.pop('_native_pcm_consumer', None)
            part_start = time.perf_counter()
            raw, meta = renderer.synthesize_performance(request['text'],
                score=request['score'], segments=request.get('segments'),
                reading_language=request['language'], pitch_offset_semitones=0.0,
                speech_rate=1.0, timeout_seconds=timeout, max_audio_bytes=16 * 1024 * 1024,
                **({'emit_pcm': on_pcm} if on_pcm is not None else {}))
            if (meta.get('voice_id') != voice_id or meta.get('reading_language') != language
                    or meta.get('pitch_offset_semitones') != 0.0 or meta.get('speech_rate') != 1.0):
                raise ValueError('renderer changed requested voice/language/controls')
            pcm_hash = pcm_digest(raw)
            pcm_hashes[hashlib.sha256(raw).hexdigest()] = pcm_hash
            record['physical_parts'].append({'part_id': request['part_id'], 'role': request['role'],
                'started_ms': round((part_start-started)*1000, 3),
                'elapsed_ms': round((time.perf_counter() - part_start) * 1000, 3),
                'pcm_sha256': pcm_hash,
                'metrics': {key: meta[key] for key in TELEMETRY_KEYS if key in meta}})
            return raw, meta

        def render_part_stream(*, on_pcm, **request):
            return render_part(_native_pcm_consumer=on_pcm, **request)

        render_start = time.perf_counter()
        if progressive:
            from cogs.tts.cd_stream import PCMStreamBuffer
            from osaka_cd.progressive import PREROLL_MS, stream_arrangement
            buffer = PCMStreamBuffer(adapted['duration_ms'] + PREROLL_MS,
                                     prebuffer_ms=500, safety_ms=200)
            output_hash = hashlib.sha256()
            output_bytes = 0
            record.update(first_pcm_available_ms=None, first_nonzero_pcm_available_ms=None,
                          buffer_ready_ms=None, buffer_ready_audio_ms=None)

            def capture_ready():
                if record['buffer_ready_ms'] is None and buffer.ready:
                    record['buffer_ready_ms'] = round((time.perf_counter()-started)*1000, 3)
                    record['buffer_ready_audio_ms'] = round(buffer.produced_ms, 3)

            def emit(pcm):
                nonlocal output_bytes
                available_ms = round((time.perf_counter()-started)*1000, 3)
                if record['first_pcm_available_ms'] is None:
                    record['first_pcm_available_ms'] = available_ms
                if record['first_nonzero_pcm_available_ms'] is None and any(pcm):
                    record['first_nonzero_pcm_available_ms'] = available_ms
                output_hash.update(pcm)
                output_bytes += len(pcm)
                # This is a readiness probe, not a simulated Discord player.
                # Retain only the runway needed to observe its first readiness.
                if record['buffer_ready_ms'] is None:
                    buffer.append(pcm, generation_ms=0)
                    capture_ready()

            try:
                native_options = ({'render_part_stream': render_part_stream}
                    if getattr(renderer, 'SINGING_PCM_PROTOCOL', 0) == 1 else {})
                result = stream_arrangement(adapted, clean, render_part, emit,
                    segments=segments, cache_dir=cache, memory_cache=memory_cache,
                    render_profile_fingerprint=profile, language=language,
                    timeout_seconds=1200, require_normalization=True,
                    **native_options,
                    on_generation_boundary=buffer.begin_generation,
                    on_generation=lambda *values: (
                        buffer.observe_generation(*values) if record['buffer_ready_ms'] is None else None))
                buffer.finish()
                capture_ready()
                if output_hash.hexdigest() != result['pcm_sha256'] or output_bytes != result['audio_bytes']:
                    raise ValueError('progressive PCM publication hash/length mismatch')
            finally:
                buffer.cleanup()
        else:
            result = render_arrangement(adapted, clean, render_part, segments=segments, cache_dir=cache,
                render_profile_fingerprint=profile, language=language, output_format='mp3',
                timeout_seconds=1200)
        record['render_ms'] = round((time.perf_counter() - render_start) * 1000, 3)
        stems = []
        for stem in result['stems']:
            # Keep hashes, not PCM, across phases. Large arrangements can evict
            # early cache entries before composition ends; those successfully
            # rendered stems must remain verifiable after optional eviction.
            digest = stem['render_sha256']
            if digest not in pcm_hashes:
                raw = (cache / 'stems' / (stem['key'] + '.wav')).read_bytes()
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise ValueError('cached stem changed while measuring')
                pcm_hashes[digest] = pcm_digest(raw)
            stems.append({**stem, 'pcm_sha256': pcm_hashes[digest]})
        signature = [(stem['part_id'], stem['score_sha256'], stem['start_ms'], stem['pcm_sha256'])
                     for stem in stems]
        if progressive:
            signature.append(('final_progressive_pcm', result['pcm_sha256']))
        reference_key = json.dumps({'text': clean, 'segments': segments, 'progressive': progressive},
                                   sort_keys=True, ensure_ascii=False)
        reference = references.setdefault(reference_key, signature)
        fixed_ids = {part['id'] for part in adapted['parts'] if part.get('lyrics_source') != 'message'}
        record.update(ok=signature == reference, pcm_matches_reference=signature == reference,
            stems=stems, mix_cache_hit=result.get('mix_cache_hit', False),
            fixed_stem_cache_hits=sum(stem['cache_hit'] for stem in stems if stem['part_id'] in fixed_ids),
            resampler_calls=sum(part['metrics'].get('resampler_calls', 0) for part in record['physical_parts']),
            duration_ms=result['duration_ms'], parts=len(adapted['parts']),
            composition_metrics=(result if progressive else result['metadata']).get('timing_ms', {}),
            output_bytes=result['audio_bytes'] if progressive else len(result['audio']))
        if progressive:
            record['pcm_sha256'] = result['pcm_sha256']
            record['first_nonzero_offset_ms'] = result['first_nonzero_offset_ms']
        else:
            record['mp3_sha256'] = result['sha256']
        if not record['ok']:
            record['error'] = 'PCM differs for identical text/score across configurations'
    except Exception as exc:
        record['error'] = f'{type(exc).__name__}: {exc}'
    record['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 3)
    if record.get('duration_ms'):
        record['synthesis_to_audio_ratio'] = round(record['render_ms'] / record['duration_ms'], 4)
    record['temperature_after_c'] = battery_temperature()
    return record


def summaries(records):
    output = []
    for threads, phase in sorted({(r['threads_requested'], r['phase']) for r in records}):
        selected = [r for r in records if r['ok'] and (r['threads_requested'], r['phase']) == (threads, phase)]
        item = {'threads_requested': threads, 'phase': phase, 'successful_runs': len(selected)}
        for key in ('elapsed_ms', 'prepare_ms', 'profile_ms', 'render_ms',
                    'first_nonzero_pcm_available_ms', 'buffer_ready_ms'):
            values = sorted(r[key] for r in selected if r.get(key) is not None)
            for label, fraction in (('p50', .5), ('p95', .95)):
                if values:
                    position = (len(values) - 1) * fraction
                    low, high = math.floor(position), math.ceil(position)
                    item[f'{key}_{label}'] = round(values[low] + (values[high] - values[low]) * (position - low), 3)
        output.append(item)
    return output


def benchmark(*, values, arrangement, voice='teto', bank=None, container='voicepeak-arm64',
              esper_root=None, language='pt-BR', text='Olá meu amor', new_text='Eu vou cantar',
              rounds=2, threads=None, timeout=120, factory=renderer_factory, quality=None,
              progressive=False):
    quality = quality or quality_helpers()
    if voice not in VOICES or language not in ('pt-BR', 'en-US'):
        raise ValueError('invalid voice/language')
    voice_id, prefix, allowed_threads = VOICES[voice]
    threads = tuple(threads or allowed_threads)
    if (not threads or len(set(threads)) != len(threads)
            or any(type(t) is not int or t not in allowed_threads for t in threads)):
        raise ValueError('threads outside supported per-voice limits')
    if (type(rounds) is not int or not 1 <= rounds <= 3
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 120):
        raise ValueError('rounds must be 1..3 and timeout 1..120 seconds')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', container):
        raise ValueError('invalid container')
    if not all(isinstance(t, str) and 0 < len(t) <= 2048 and '\0' not in t for t in (text, new_text)):
        raise ValueError('lyrics must contain 1..2048 characters')
    if ' '.join(text.split()) == ' '.join(new_text.split()):
        raise ValueError('new text must differ from initial text')
    bank = Path(bank or values.get(f'PHONE_WORKER_{prefix}_ENGLISH_VOICEBANK_DIR')
                or values.get(f'PHONE_WORKER_{prefix}_VOICEBANK_DIR')
                or Path.home() / ('voicebanks/hatsune-miku-english' if voice == 'miku'
                                  else 'voicebanks/kasane-teto-english')).expanduser().absolute()
    records, references, pcm_hashes = [], {}, {}
    from osaka_cd.arrangement import arrangement_sha256
    report = {'ok': False, 'voice_id': voice_id, 'language': language, 'note': NOTE,
              'physical_utau_requested': factory is renderer_factory,
              'injected_renderer': factory is not renderer_factory,
              'network_called': False, 'discord_called': False,
              'production_configuration_changed': False, 'production_caches_changed': False,
              'samples': records, 'rounds': rounds,
              'arrangement_sha256': arrangement_sha256(arrangement),
              'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
              'new_text_sha256': hashlib.sha256(new_text.encode()).hexdigest()}
    report['output_mode'] = 'progressive_pcm' if progressive else 'complete_mp3'
    report['first_audio_note'] = ('PCM availability and buffer readiness only; no audio is played. '
                                  'Use runtime cd_message_to_first_audible for Discord delivery.')
    with tempfile.TemporaryDirectory(prefix='ttv-cd-benchmark-') as temporary:
        root = Path(temporary)
        analysis = root / 'analysis'
        if progressive:
            from osaka_cd.progressive import DecodedPCMCache
        else:
            DecodedPCMCache = lambda: None

        def renderer_for(fragments, count):
            environment = isolated_environment(quality, values, voice=voice, bank=bank,
                container=container, esper_root=esper_root, analysis=analysis,
                fragments=fragments, threads=count, timeout=timeout)
            return factory(environment, voice_id)

        cold = renderer_for(root / 'cold-fragments', threads[0])
        records.append(measure(arrangement, text, renderer=cold, voice_id=voice_id, language=language,
            cache=root / 'cold-composition', timeout=timeout, phase='analysis_cold', threads=threads[0],
            round_number=0, references=references, pcm_hashes=pcm_hashes,
            progressive=progressive, memory_cache=DecodedPCMCache()))
        if records[-1]['ok']:
            for number in range(1, rounds + 1):
                for count in (threads if number % 2 else tuple(reversed(threads))):
                    case = root / f'round-{number}-threads-{count}'
                    renderer = renderer_for(case / 'fragments', count)
                    decoded = DecodedPCMCache()
                    for phase, lyrics in (('analysis_warm_new_fragments', text), ('exact_repeat', text),
                                          ('new_text_same_cd', new_text)):
                        records.append(measure(arrangement, lyrics, renderer=renderer, voice_id=voice_id,
                            language=language, cache=case / 'composition', timeout=timeout,
                            phase=phase, threads=count, round_number=number, references=references,
                            pcm_hashes=pcm_hashes, progressive=progressive, memory_cache=decoded))
                        if not records[-1]['ok']:
                            break
                    if not records[-1]['ok']:
                        break
                if not records[-1]['ok']:
                    break
        report['ok'] = all(r['ok'] for r in records)
        report['physical_utau_completed'] = report['physical_utau_requested'] and report['ok']
        report['summary'] = summaries(records)
    report['temporary_files_removed'] = not root.exists()
    return report


def safe_report_path(path, *, env, bank, values):
    path = path.expanduser().absolute()
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('report must be a regular file')
    path = path.parent.resolve() / path.name
    protected = [ROOT.resolve(), bank.expanduser().resolve(), (Path.home() / '.esper-utau').resolve(),
                 (Path.home() / 'phone-worker/cache').resolve()]
    protected += [Path(v).expanduser().resolve() for k, v in values.items()
                  if v and (k.endswith('_VOICEBANK_DIR') or k.endswith('_CACHE_DIR') or k == 'PHONE_WORKER_ESPER_ROOT')]
    if path == env.resolve() or any(path.is_relative_to(p) for p in protected):
        raise ValueError('report must be outside code, voicebanks, configuration and production caches')
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--cd', default='ttv-demo-v1', help='CD id from the installed catalog')
    source.add_argument('--arrangement', type=Path, help='Local arrangement JSON, read only')
    parser.add_argument('--voice', choices=tuple(VOICES), default='teto')
    parser.add_argument('--language', choices=('pt-BR', 'en-US'), default='pt-BR')
    parser.add_argument('--text', default='Olá meu amor')
    parser.add_argument('--new-text', default='Eu vou cantar')
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--threads', type=int, nargs='+', help='Teto: 1/2; Miku: 1/2/4')
    parser.add_argument('--timeout', type=float, default=120, help='Seconds per physical stem')
    parser.add_argument('--env', type=Path, default=Path(os.getenv('PHONE_WORKER_ENV') or Path.home() / '.phone-worker.env'))
    parser.add_argument('--voicebank', type=Path, help='Installed English CVVC bank')
    parser.add_argument('--container', default='voicepeak-arm64')
    parser.add_argument('--esper-root', type=Path)
    parser.add_argument('--output', type=Path, help='Optional JSON report outside the repository')
    parser.add_argument('--progressive', action='store_true',
                        help='Measure progressive PCM availability/readiness instead of complete MP3')
    options = parser.parse_args(argv)
    try:
        quality = quality_helpers()
        values = read_settings(quality, options.env.expanduser().absolute())
        prefix = VOICES[options.voice][1]
        bank = options.voicebank or Path(values.get(f'PHONE_WORKER_{prefix}_ENGLISH_VOICEBANK_DIR')
            or values.get(f'PHONE_WORKER_{prefix}_VOICEBANK_DIR')
            or Path.home() / ('voicebanks/hatsune-miku-english' if options.voice == 'miku'
                              else 'voicebanks/kasane-teto-english'))
        target = safe_report_path(options.output, env=options.env, bank=bank, values=values) if options.output else None
        if target and options.arrangement and target == options.arrangement.expanduser().resolve():
            raise ValueError('report cannot overwrite arrangement input')
        result = benchmark(values=values, arrangement=load_arrangement(options.cd, options.arrangement),
            voice=options.voice, bank=bank, container=options.container, esper_root=options.esper_root,
            language=options.language, text=options.text, new_text=options.new_text, rounds=options.rounds,
            threads=options.threads, timeout=options.timeout, quality=quality,
            progressive=options.progressive)
        if target:
            target.parent.mkdir(parents=True, exist_ok=True)
            quality.installer().atomic_write(target, (json.dumps(result, ensure_ascii=False,
                indent=2, allow_nan=False) + '\n').encode())
    except Exception as exc:
        result = {'ok': False, 'error': f'{type(exc).__name__}: {exc}', 'note': NOTE}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
