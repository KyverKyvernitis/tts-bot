from __future__ import annotations

from pathlib import Path

import pytest

flask = pytest.importorskip("flask")
Flask = flask.Flask
from cogs.musica.integracoes import webserver as musica_web


def test_registro_e_servico_de_audio_temporario(tmp_path: Path):
    app = Flask("musica-web-test")
    musica_web.registrar_rotas_musica(app)
    audio = tmp_path / "fala.mp3"
    audio.write_bytes(b"ID3fixture")
    token = musica_web.register_tts_audio_file(str(audio), ttl_seconds=60)
    assert token

    client = app.test_client()
    response = client.get(f"/tts-audio/{token}.mp3")
    assert response.status_code == 200
    assert response.data == b"ID3fixture"
    assert response.mimetype == "audio/mpeg"


def test_registrar_rotas_e_idempotente():
    app = Flask("musica-web-idempotent")
    musica_web.registrar_rotas_musica(app)
    musica_web.registrar_rotas_musica(app)
    rules = [str(rule) for rule in app.url_map.iter_rules()]
    assert rules.count("/tts-audio/<token>") == 1
    assert rules.count("/tts-audio/<token>.<ext>") == 1
