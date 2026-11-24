"""Verificações estáticas do modal Piper e da proteção da sessão Discord."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_modal_opcoes_iguais_a_edge():
    modal = (ROOT / "cogs/tts/interface/modal_piper.py").read_text()
    for name in ("Bem mais lenta", "Mais lenta", "Normal", "Mais rápida", "Bem mais rápida",
                 "Bem mais grave", "Mais grave", "Mais agudo", "Bem mais agudo"):
        assert name in modal
    for val in ('"0.5"', '"0.75"', '"1.0"', '"1.25"', '"1.5"', '"-4"', '"-2"', '"0"', '"2"', '"4"'):
        assert val in modal
    assert '"pt_BR-dii-medium"' in modal
    assert 'Valores personalizados' not in modal


def test_piper_erro_de_audio_nao_reseta_call():
    audio = (ROOT / "cogs/tts/audio.py").read_text()
    assert 'item.engine == "piper" and not self._is_voice_disconnected_error(exc)' in audio
    assert '[tts_piper] falha de áudio sem reset da call' in audio
    assert 'speech_seconds / piper_speed' in audio


def test_default_feminina_em_todos_os_caminhos():
    for rel in ('config.py', 'db.py', 'cogs/tts/mensagens/preparacao.py',
                'cogs/tts/interface/modal_piper.py',
                'deploy/termux/phone-worker/phone_worker.py'):
        text = (ROOT / rel).read_text()
        assert 'pt_BR-dii-medium' in text
    assert '("piper_voice", "pt_BR-dii-medium")' in (ROOT / 'db.py').read_text()
