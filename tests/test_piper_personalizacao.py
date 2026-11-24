"""Regressões de personalização do Piper, sem dependência de Discord no teste."""
import importlib.util
import math
from pathlib import Path
import wave

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _piper_module():
    path = ROOT / 'deploy/termux/phone-worker/phone_worker_runtime/tts_piper.py'
    spec = importlib.util.spec_from_file_location('piper_settings_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('speed,pitch', [('0.7', '-4'), ('1.0', '0'), ('1.15', '2'), ('1.5', '4')])
def test_piper_values_validos(speed, pitch):
    assert _piper_module().normalize_audio_settings(speed, pitch) == (float(speed), float(pitch))


@pytest.mark.parametrize('speed,pitch', [('0', '0'), ('1.6', '0'), ('1', '5'), ('nan', '0'), ('1', 'inf')])
def test_piper_values_invalidos(speed, pitch):
    with pytest.raises(ValueError):
        _piper_module().normalize_audio_settings(speed, pitch)


def test_dsp_piper_respeita_velocidade_e_cria_wav(tmp_path):
    import array
    import shutil
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg ausente')
    path = tmp_path / 'original.wav'
    samples = array.array('h', [int(9000 * math.sin(i * math.pi / 25)) for i in range(22050)])
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(22050)
        f.writeframes(samples.tobytes())
    _piper_module().apply_audio_settings(path, 1.15, 2)
    with wave.open(str(path), 'rb') as f:
        assert f.getnchannels() == 1
        assert f.getframerate() == 22050
        assert f.getsampwidth() == 2
        assert 18000 < f.getnframes() < 20500


def test_launcher_expoe_piper_com_modal_proprio():
    source = (ROOT / 'cogs/tts/interface/visao_lancador_publico.py').read_text()
    assert source.index('motor="edge"') < source.index('motor="gtts"') < source.index('motor="piper"')
    piper_handler = source[source.index('if acao == "piper":'):source.index('if acao == "teto":')]
    assert 'response.send_modal' in piper_handler
    assert '_enviar_modal_fallback' not in piper_handler


def test_cache_por_preferencias_e_roteamento():
    audio = (ROOT / 'cogs/tts/audio.py').read_text()
    stream = (ROOT / 'cogs/tts/streaming.py').read_text()
    worker = (ROOT / 'deploy/termux/phone-worker/phone_worker.py').read_text()
    assert '{item.piper_speed}|{item.piper_pitch}' in audio
    assert "'pitch_semitones': str(getattr(job.item, 'piper_pitch'" in stream
    assert 'audio_settings=settings' in worker
    assert 'stream_pcm_customized(' in worker


def test_modal_piper_sem_campos_personalizados_ou_descricoes_redundantes():
    source = (ROOT / 'cogs/tts/interface/modal_piper.py').read_text()
    assert '"custom"' not in source
    assert 'Valores personalizados' not in source
    assert 'criar_entrada_texto_modal' not in source
    assert 'description="Controla' not in source
    assert 'description="Ajuste' not in source
    assert '"1.0"' in source and '"0"' in source
    assert 'speed not in VELOCIDADES or pitch not in TONS' in source
