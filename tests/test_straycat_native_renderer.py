"""Pinned native engines keep WORLD analysis private and leases physical."""
from __future__ import annotations

import array
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import wave

import pytest

WORKER = Path(__file__).resolve().parents[1] / 'deploy/termux/phone-worker'
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from teto_renderer.errors import TetoConfigurationError, TetoResourceError
from teto_renderer.renderer import TetoRenderer


# A real ELF process exercises descriptor inheritance, process-group cleanup,
# and native execution. It intentionally writes analysis before waiting, just
# as WORLD's non-atomic analysis writer can do when interrupted.
STUB_SOURCE = r'''
#include <fcntl.h>
#include <math.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
static volatile sig_atomic_t stopped=0;
static void stop(int sig){stopped=1;}
static void touch(const char *path,const char *data){FILE *f=fopen(path,"wb");if(f){fputs(data,f);fclose(f);}}
static void trace(const char *dir,const char *kind,const char *source){
 char path[4096];snprintf(path,sizeof(path),"%s/trace",dir);FILE *f=fopen(path,"a");
 if(f){fprintf(f,"%s %d %s\n",kind,getpid(),source);fclose(f);}
}
static void u32(FILE *f,uint32_t n){for(int i=0;i<4;i++)fputc((n>>(8*i))&255,f);}
static void u16(FILE *f,uint16_t n){fputc(n&255,f);fputc((n>>8)&255,f);}
int main(int argc,char **argv){
 if(argc!=14)return 12;
 const char *dir=getenv("STRAYCAT_TEST_CONTROL");if(!dir)return 13;
 char sc[4096],ready[4096],release[4096],exited[4096],active[4096];
 snprintf(sc,sizeof(sc),"%s",argv[1]);char *dot=strrchr(sc,'.');if(!dot)return 14;strcpy(dot,"_wav.sc");
 snprintf(ready,sizeof(ready),"%s/ready",dir);snprintf(release,sizeof(release),"%s/release",dir);
 snprintf(exited,sizeof(exited),"%s/exited",dir);snprintf(active,sizeof(active),"%s/active",dir);
 int busy=open(active,O_CREAT|O_EXCL|O_WRONLY,0600);if(busy<0){trace(dir,"overlap",argv[1]);return 15;}close(busy);
 trace(dir,"start",argv[1]);
 FILE *old=fopen(sc,"rb");if(!old){trace(dir,"analyze",argv[1]);touch(sc,"partial");}
 else{char data[32]={0};fread(data,1,31,old);fclose(old);if(strcmp(data,"complete-v1")){unlink(active);return 16;}}
 signal(SIGTERM,stop);signal(SIGINT,stop);
 FILE *r=fopen(ready,"w");if(r){fprintf(r,"%d",getpid());fclose(r);}
 if(getenv("STRAYCAT_TEST_BLOCK"))while(access(release,F_OK)&&!stopped)usleep(5000);
 if(stopped){usleep(80000);touch(sc,"partial-after-term");touch(exited,"drained");unlink(active);return 143;}
 touch(sc,"complete-v1");
 double duration=atof(argv[7])+atof(argv[8])*pow(2.0,1.0-atof(argv[4])/100.0);
 int count=(int)llround(duration*44.1);if(count<1)count=1;
 FILE *out=fopen(argv[2],"wb");if(!out){unlink(active);return 17;}
 fwrite("RIFF",1,4,out);u32(out,36+count*2);fwrite("WAVEfmt ",1,8,out);u32(out,16);u16(out,1);u16(out,1);
 u32(out,44100);u32(out,88200);u16(out,2);u16(out,16);fwrite("data",1,4,out);u32(out,count*2);
 for(int i=0;i<count;i++)u16(out,(uint16_t)(int16_t)(4000*sin(2*3.141592653589793*261.625565*i/44100.0)));
 fclose(out);trace(dir,"done",argv[1]);unlink(active);return 0;
}
'''


@pytest.fixture(scope='module')
def native_engine(tmp_path_factory):
    compiler = shutil.which('cc') or shutil.which('gcc')
    if not compiler or not sys.platform.startswith('linux'):
        pytest.skip('a Linux native compiler is required for process lifecycle tests')
    root = tmp_path_factory.mktemp('straycat-native-elf')
    source = root / 'engine.c'
    source.write_text(STUB_SOURCE)
    binary = root / 'straycat-rs-test'
    subprocess.run([compiler, '-O2', str(source), '-lm', '-o', str(binary)], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    return binary


@pytest.fixture
def native_settings(tmp_path, native_engine):
    bank = tmp_path / 'bank'
    bank.mkdir()
    with wave.open(str(bank / 'a.wav'), 'wb') as target:
        target.setparams((1, 2, 44100, 0, 'NONE', ''))
        target.writeframes(array.array('h', [1800, -1800] * 44100).tobytes())
    (bank / 'oto.ini').write_text(''.join(
        f'a.wav={alias},0,{fixed},-900,{lead},10\n'
        for alias, fixed, lead in [('-a', 100, 60), ('*a', 400, 30), ('a a', 100, 50)]))
    (bank / 'character.txt').write_text('name=Native fixture\n')
    control = tmp_path / 'control'
    control.mkdir()
    return {
        'PHONE_WORKER_TETO_ENABLED': 'true',
        'PHONE_WORKER_TETO_VOICEBANK_DIR': str(bank),
        'PHONE_WORKER_TETO_MIN_ALIASES': '1',
        'PHONE_WORKER_TETO_RESAMPLER_COMMAND': str(native_engine),
        'PHONE_WORKER_TETO_NATIVE_RESAMPLER_SHA256': hashlib.sha256(native_engine.read_bytes()).hexdigest(),
        'PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR': str(tmp_path / 'fragments'),
        'PHONE_WORKER_TETO_LENGTH_MODE': 'auto',
        'STRAYCAT_TEST_CONTROL': str(control),
    }


def renderer(settings):
    return TetoRenderer(environment=settings, voice_id='pururu-purpura')


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError('native operation did not reach the expected boundary')


def traces(settings, kind):
    path = Path(settings['STRAYCAT_TEST_CONTROL']) / 'trace'
    return [line for line in path.read_text().splitlines() if line.startswith(kind + ' ')] if path.exists() else []


def bank_inventory(settings):
    bank = Path(settings['PHONE_WORKER_TETO_VOICEBANK_DIR'])
    return {str(path.relative_to(bank)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in bank.rglob('*') if path.is_file()}


def test_only_explicit_digest_and_native_elf_enable_background_rendering(native_settings, tmp_path):
    trusted = renderer(native_settings)
    assert trusted._verified_native_straycat() is not None
    assert trusted.supports_background_cancellation()
    assert trusted._length_mode() == 'post-consonant'
    for change in ['', 'a' * 64, 'invalid-sha']:
        settings = {**native_settings, 'PHONE_WORKER_TETO_NATIVE_RESAMPLER_SHA256': change}
        rejected = renderer(settings)
        assert rejected._verified_native_straycat() is None
        assert not rejected.supports_background_cancellation()
    script = tmp_path / 'straycat-rs-script'
    script.write_text('#!/bin/sh\nexit 0\n' + '# not an ELF\n' * 10)
    script.chmod(0o700)
    rejected = renderer({**native_settings, 'PHONE_WORKER_TETO_RESAMPLER_COMMAND': str(script),
        'PHONE_WORKER_TETO_NATIVE_RESAMPLER_SHA256': hashlib.sha256(script.read_bytes()).hexdigest()})
    assert rejected._verified_native_straycat() is None


def test_wrong_architecture_and_wrapped_commands_are_not_native_trusted(native_settings, tmp_path):
    original = Path(native_settings['PHONE_WORKER_TETO_RESAMPLER_COMMAND'])
    raw = bytearray(original.read_bytes())
    current = int.from_bytes(raw[18:20], 'little')
    raw[18:20] = (183 if current != 183 else 62).to_bytes(2, 'little')
    wrong = tmp_path / 'straycat-rs-wrong-architecture'
    wrong.write_bytes(raw)
    wrong.chmod(0o700)
    assert renderer({**native_settings, 'PHONE_WORKER_TETO_RESAMPLER_COMMAND': str(wrong),
        'PHONE_WORKER_TETO_NATIVE_RESAMPLER_SHA256': hashlib.sha256(raw).hexdigest()})._verified_native_straycat() is None
    assert renderer({**native_settings, 'PHONE_WORKER_TETO_RESAMPLER_COMMAND': str(original) + ' --wrapped'})._verified_native_straycat() is None


def test_engine_replacement_invalidates_cached_trust(native_settings, tmp_path):
    path = tmp_path / 'straycat-rs-replaced'
    shutil.copyfile(native_settings['PHONE_WORKER_TETO_RESAMPLER_COMMAND'], path)
    path.chmod(0o700)
    current = renderer({**native_settings, 'PHONE_WORKER_TETO_RESAMPLER_COMMAND': str(path)})
    assert current._verified_native_straycat() is not None
    with path.open('ab') as output:
        output.write(b'changed after verification')
    assert current._verified_native_straycat() is None


@pytest.mark.parametrize('operation', ['speech', 'singing'])
@pytest.mark.parametrize('damage', ['wrong-sha', 'replaced-after-verification'])
def test_failed_explicit_native_pin_rejects_synthesis_before_any_process_or_bank_write(
        native_settings, tmp_path, monkeypatch, operation, damage):
    settings = dict(native_settings)
    if damage == 'wrong-sha':
        settings['PHONE_WORKER_TETO_NATIVE_RESAMPLER_SHA256'] = 'f' * 64
        current = renderer(settings)
    else:
        path = tmp_path / 'straycat-rs-replaced-after-pinning'
        shutil.copyfile(settings['PHONE_WORKER_TETO_RESAMPLER_COMMAND'], path)
        path.chmod(0o700)
        settings['PHONE_WORKER_TETO_RESAMPLER_COMMAND'] = str(path)
        current = renderer(settings)
        assert current._verified_native_straycat() is not None
        with path.open('ab') as changed:
            changed.write(b'updated binary requires a new explicit pin')
    before = bank_inventory(settings)
    def unexpected(*args, **kwargs):
        pytest.fail('an unverified pinned engine reached a process or private source lease')
    monkeypatch.setattr('teto_renderer.renderer.subprocess.Popen', unexpected)
    monkeypatch.setattr('teto_renderer.renderer.subprocess.run', unexpected)
    monkeypatch.setattr(current, '_native_source_lease', unexpected)
    status = current.status(force=True)
    assert not status['ready']
    assert 'Straycat' in status['last_error']
    assert not current.supports_background_cancellation()
    with pytest.raises(TetoConfigurationError, match='Straycat'):
        if operation == 'speech':
            current.synthesize('a')
        else:
            current.synthesize_performance('a', score={
                'version': 1, 'track_index': 0, 'lyrics_source': 'message', 'duration_ms': 500,
                'notes': [{'midi_note': 60, 'start_ms': 0, 'duration_ms': 500, 'velocity': 100}]})
    assert bank_inventory(settings) == before
    assert not current._native_analysis_root().exists()
    assert not traces(settings, 'start')


def test_native_analysis_is_private_persistent_and_shared_by_new_note_pitches(native_settings):
    current = renderer(native_settings)
    before = bank_inventory(native_settings)
    first = current.synthesize('a')
    second = current.synthesize('a', pitch_offset_semitones=2)
    assert first['audio'] and second['audio']
    assert len(traces(native_settings, 'start')) == 2
    assert len(traces(native_settings, 'analyze')) == 1
    assert bank_inventory(native_settings) == before
    analysis = list(current._native_analysis_root().glob('*/source_wav.sc'))
    assert len(analysis) == 1 and analysis[0].read_text() == 'complete-v1'
    assert (analysis[0].parent / 'analysis-ready.json').is_file()
    for line in traces(native_settings, 'start'):
        assert str(current._native_analysis_root()) in line


def test_flags_get_an_independent_analysis_namespace(native_settings):
    first = renderer(native_settings)
    second = renderer({**native_settings, 'PHONE_WORKER_TETO_FLAGS': 'G80'})
    first.synthesize('a')
    second.synthesize('a')
    assert len(traces(native_settings, 'analyze')) == 2
    assert len(list(first._native_analysis_root().glob('*/source_wav.sc'))) == 2


@pytest.mark.parametrize('damage', ['missing-marker', 'changed-analysis', 'changed-private-recording'])
def test_unconfirmed_or_changed_analysis_is_regenerated(native_settings, damage):
    current = renderer(native_settings)
    current.synthesize('a')
    analysis = next(current._native_analysis_root().glob('*/source_wav.sc'))
    if damage == 'missing-marker':
        (analysis.parent / 'analysis-ready.json').unlink()
    elif damage == 'changed-analysis':
        analysis.write_bytes(b'interrupted cache')
    else:
        (analysis.parent / 'source.wav').write_bytes(b'changed recording')
    current.synthesize('a', pitch_offset_semitones=2)
    assert len(traces(native_settings, 'analyze')) == 2
    assert analysis.read_text() == 'complete-v1'
    assert (analysis.parent / 'source.wav').read_bytes() == (
        Path(native_settings['PHONE_WORKER_TETO_VOICEBANK_DIR']) / 'a.wav').read_bytes()


def test_two_renderer_instances_serialize_one_physical_source(native_settings):
    settings = {**native_settings, 'STRAYCAT_TEST_BLOCK': '1'}
    first, second = renderer(settings), renderer(settings)
    control = Path(settings['STRAYCAT_TEST_CONTROL'])
    errors = []
    def run(current, pitch):
        try:
            current.synthesize('a', pitch_offset_semitones=pitch, timeout_seconds=10)
        except BaseException as error:
            errors.append(error)
    one = threading.Thread(target=run, args=(first, 0))
    two = threading.Thread(target=run, args=(second, 2))
    one.start()
    try:
        wait_for(lambda: (control / 'ready').exists())
        two.start()
        time.sleep(.12)
        assert len(traces(settings, 'start')) == 1
    finally:
        (control / 'release').touch()
        one.join(5)
        if two.ident is not None:
            two.join(5)
    assert not one.is_alive() and not two.is_alive() and not errors
    assert len(traces(settings, 'start')) == 2
    assert len(traces(settings, 'analyze')) == 1
    assert not traces(settings, 'overlap')


def test_cancellation_drains_native_child_before_removing_partial_analysis_and_releasing_lease(native_settings):
    settings = {**native_settings, 'STRAYCAT_TEST_BLOCK': '1'}
    current = renderer(settings)
    control = Path(settings['STRAYCAT_TEST_CONTROL'])
    cancelled = threading.Event()
    errors = []
    source = Path(settings['PHONE_WORKER_TETO_VOICEBANK_DIR']) / 'a.wav'
    identity = current._verified_native_straycat()
    def run():
        current._reading_context.cancelled = cancelled.is_set
        try:
            with current._native_source_lease(source, identity, time.monotonic() + 10) as (private, fd):
                command = [str(identity[0]), str(private), str(control / 'output.wav'),
                           'C4', '100', '', '0', '500', '100', '-900', '100', '0', '!140', 'AA']
                current._run_native_resampler(command, time.monotonic() + 10, fd)
        except BaseException as error:
            errors.append(error)
    task = threading.Thread(target=run)
    task.start()
    try:
        wait_for(lambda: (control / 'ready').exists() and (control / 'ready').read_text())
        pid = int((control / 'ready').read_text())
        cancelled.set()
        task.join(3)
        assert not task.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], TetoResourceError)
        assert (control / 'exited').read_text() == 'drained'
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert not list(current._native_analysis_root().glob('*/source_wav.sc'))
        assert not list(current._native_analysis_root().glob('*/analysis-ready.json'))
        with renderer(settings)._native_source_lease(source, identity, time.monotonic() + 1):
            pass
    finally:
        cancelled.set()
        (control / 'release').touch()
        task.join(3)


def test_inherited_lease_stays_locked_if_python_parent_is_killed(native_settings):
    settings = {**native_settings, 'STRAYCAT_TEST_BLOCK': '1'}
    control = Path(settings['STRAYCAT_TEST_CONTROL'])
    code = '''import json,sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nfrom teto_renderer.renderer import TetoRenderer\nr=TetoRenderer(environment=json.loads(sys.argv[2]),voice_id='pururu-purpura')\nr.synthesize('a',timeout_seconds=30)\n'''
    parent = subprocess.Popen([sys.executable, '-c', code, str(WORKER), json.dumps(settings)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    native_pid = None
    current = renderer(settings)
    try:
        wait_for(lambda: (control / 'ready').exists() and (control / 'ready').read_text())
        native_pid = int((control / 'ready').read_text())
        parent.kill()
        parent.wait(timeout=3)
        source = Path(settings['PHONE_WORKER_TETO_VOICEBANK_DIR']) / 'a.wav'
        identity = current._verified_native_straycat()
        with pytest.raises(TimeoutError):
            with current._native_source_lease(source, identity, time.monotonic() + .15):
                pytest.fail('a new writer bypassed the still-running orphan native process')
        os.killpg(native_pid, signal.SIGTERM)
        wait_for(lambda: (control / 'exited').exists())
        with current._native_source_lease(source, identity, time.monotonic() + 3) as (private, _):
            assert not (private.parent / 'source_wav.sc').exists()
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=3)
        if native_pid:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(native_pid, signal.SIGKILL)


def test_post_consonant_native_canto_and_three_second_hold_stream_match(native_settings):
    current = renderer(native_settings)
    before = bank_inventory(native_settings)
    chunks = []
    score = {'version': 1, 'track_index': 0, 'lyrics_source': 'message', 'duration_ms': 3000,
             'notes': [{'midi_note': pitch, 'start_ms': number * 1000, 'duration_ms': 1000, 'velocity': 100}
                       for number, pitch in enumerate((60, 64, 67))]}
    raw, metadata = current.synthesize_performance_stream('a~~', score=score,
        emit_pcm=lambda pcm, _: chunks.append(pcm) if pcm else None)
    with wave.open(io.BytesIO(raw), 'rb') as audio:
        assert audio.getnchannels() == 1 and audio.getframerate() == 44100
        pcm = audio.readframes(audio.getnframes())
        assert abs(audio.getnframes() / 44.1 - (3000 + metadata['midi_preroll_ms'])) <= 1
    assert b''.join(chunks) == pcm and any(array.array('h', pcm))
    assert metadata['pitchbend_fallbacks'] == 0
    assert metadata['resampler_calls'] == 1 and metadata['melisma_notes'] == 2
    assert metadata['voice_id'] == 'pururu-purpura' and not metadata['missing_phonemes']
    assert bank_inventory(native_settings) == before
