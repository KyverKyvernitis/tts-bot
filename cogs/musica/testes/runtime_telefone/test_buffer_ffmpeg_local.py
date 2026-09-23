"""Integração offline: FFmpeg/discord.py reais contra HTTP em loopback."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg não instalado")
def test_pcm_http_finito_nao_reconecta_e_403_nao_entra_em_loop():
    # Outros testes carregam doubles em sys.modules; um processo limpo verifica
    # o decoder real sem contaminar os contratos desses testes.
    script = r'''
import asyncio, io, threading, time, wave
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from cogs.musica.runtime_telefone.agente.servidor import MusicAgent
from cogs.musica.runtime_telefone.agente.estado import AgentTrack
output = io.BytesIO()
with wave.open(output, 'wb') as wav:
    wav.setnchannels(2)
    wav.setsampwidth(2)
    wav.setframerate(48000)
    wav.writeframes(array('h', [1000] * 96000).tobytes())
payload = output.getvalue()
requests = []
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        requests.append(self.path)
        if self.path == '/forbidden':
            self.send_error(403)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'audio/wav')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, daemon=True).start()
base = 'http://127.0.0.1:' + str(server.server_port)
async def scenario():
    agent = MusicAgent()
    source = agent._create_pcm_source(AgentTrack(stream_url=base+'/song', audio_sample_rate=48000))
    try:
        await source.wait_ready(timeout=5, min_frames=15)
        count = 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pcm = source.read()
            if not pcm:
                break
            if source.last_read_had_audio:
                assert len(pcm) == 3840 and array('h', pcm)[0] == 1000
                count += 1
            else:
                await asyncio.sleep(.001)
        else:
            raise AssertionError('EOF não chegou')
        assert count == 50, count
        assert requests.count('/song') == 1, requests
        assert source.audio_buffer_metrics()['buffer_peak_frames'] <= 75
    finally:
        source.cleanup()
        await asyncio.to_thread(source._thread.join, 2)
    source = agent._create_pcm_source(AgentTrack(stream_url=base+'/forbidden'))
    try:
        try:
            await source.wait_ready(timeout=5)
        except RuntimeError as exc:
            assert 'sem produzir' in str(exc)
        else:
            raise AssertionError('403 confirmou áudio')
        assert requests.count('/forbidden') == 1, requests
    finally:
        source.cleanup()
        await asyncio.to_thread(source._thread.join, 2)
asyncio.run(scenario())
server.shutdown()
server.server_close()
print('PCM_HTTP_EOF_403_OK')
'''
    env = dict(os.environ, MUSIC_AGENT_TOKEN="test-token", PHONE_WORKER_ENV="/dev/null", MUSIC_AGENT_ENV="/dev/null")
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PCM_HTTP_EOF_403_OK" in result.stdout
