from __future__ import annotations

import sys
import types


def _payload(query: str = "notion", *, limit: int = 3) -> dict[str, object]:
    return {
        "query": query,
        "limit": limit,
        "metadata_only": True,
        "fast_search": True,
        "default_search": f"ytsearch{limit}",
    }


def test_phone_worker_reutiliza_ytdlp_quente_entre_buscas(monkeypatch) -> None:
    from cogs.musica.runtime_telefone.ponte_worker import resolucao

    resolucao._reset_warm_ytdlp_pool_for_tests()
    monkeypatch.setenv("PHONE_WORKER_MUSIC_YTDLP_WARM_POOL_SIZE", "2")
    criados: list[object] = []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = dict(opts)
            self.closed = False
            criados.append(self)

        def close(self):
            self.closed = True

        def extract_info(self, target, download=False):
            assert target == "ytsearch3:notion"
            return {
                "entries": [
                    {"id": "a", "title": "A", "uploader": "Canal", "extractor": "youtube"},
                    {"id": "b", "title": "B", "uploader": "Canal", "extractor": "youtube"},
                    {"id": "c", "title": "C", "uploader": "Canal", "extractor": "youtube"},
                ]
            }

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))

    primeiro = resolucao.resolve_ytdlp(_payload(), job_timeout=10)
    segundo = resolucao.resolve_ytdlp(_payload(), job_timeout=10)

    assert len(criados) == 1
    assert primeiro["warm_ytdlp"] is False
    assert segundo["warm_ytdlp"] is True
    assert primeiro["warm_pool_size"] == segundo["warm_pool_size"] == 1
    assert len(segundo["tracks"]) == 3
    assert segundo["ytdlp_init_ms"] >= 0
    assert segundo["ytdlp_extract_ms"] >= 0
    resolucao._reset_warm_ytdlp_pool_for_tests()
    assert criados[0].closed is True


def test_phone_worker_descarta_slot_quente_apos_falha(monkeypatch) -> None:
    from cogs.musica.runtime_telefone.ponte_worker import resolucao

    resolucao._reset_warm_ytdlp_pool_for_tests()
    monkeypatch.setenv("PHONE_WORKER_MUSIC_YTDLP_WARM_POOL_SIZE", "1")
    criados: list[object] = []

    class FakeYDL:
        def __init__(self, opts):
            self.index = len(criados)
            self.closed = False
            criados.append(self)

        def close(self):
            self.closed = True

        def extract_info(self, target, download=False):
            if self.index == 0:
                raise RuntimeError("falha simulada")
            return {"entries": [{"id": "ok", "title": "OK", "extractor": "youtube"}]}

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))

    falhou = resolucao.resolve_ytdlp(_payload(), job_timeout=10)
    recuperou = resolucao.resolve_ytdlp(_payload(), job_timeout=10)
    quente = resolucao.resolve_ytdlp(_payload(), job_timeout=10)

    assert falhou["tracks"] == []
    assert "falha simulada" in falhou["api_error"]
    assert criados[0].closed is True
    assert len(criados) == 2
    assert recuperou["warm_ytdlp"] is False
    assert quente["warm_ytdlp"] is True
    resolucao._reset_warm_ytdlp_pool_for_tests()


def test_phone_worker_mudanca_de_perfil_invalida_slot_antigo(monkeypatch) -> None:
    from cogs.musica.runtime_telefone.ponte_worker import resolucao

    resolucao._reset_warm_ytdlp_pool_for_tests()
    monkeypatch.setenv("PHONE_WORKER_MUSIC_YTDLP_WARM_POOL_SIZE", "2")
    criados: list[object] = []

    class FakeYDL:
        def __init__(self, opts):
            self.closed = False
            criados.append(self)

        def close(self):
            self.closed = True

        def extract_info(self, target, download=False):
            return {"entries": [{"id": "x", "title": "X", "extractor": "youtube"}]}

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))

    resolucao.resolve_ytdlp(_payload(limit=3), job_timeout=10)
    resolucao.resolve_ytdlp(_payload(limit=5), job_timeout=10)

    assert len(criados) == 2
    assert criados[0].closed is True
    assert len(resolucao._WARM_YDL_POOL) == 1
    resolucao._reset_warm_ytdlp_pool_for_tests()

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent


def test_music_agent_prefere_helper_ytdlp_quente(monkeypatch) -> None:
    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()

    class Hot:
        def resolve(self, target, **kwargs):
            assert target == "https://youtube.com/watch?v=abc"
            return {
                "ok": True,
                "id": 1,
                "elapsed_ms": 12.0,
                "title": "Faixa",
                "uploader": "Canal",
                "duration": 120,
                "thumbnail": "https://img.example/x.jpg",
                "webpage_url": target,
                "stream_url": "https://media.example/audio",
                "audio_format_id": "251",
                "audio_ext": "webm",
                "audio_codec": "opus",
                "audio_abr": 128,
                "audio_sample_rate": 48000,
                "audio_channels": 2,
                "http_headers": {},
            }

    agent._ytdlp_warm_client = Hot()
    monkeypatch.setenv("MUSIC_AGENT_YTDLP_WARM_HELPER_ENABLED", "true")
    monkeypatch.setattr(agent, "_run_ytdlp_command", lambda *a, **k: (_ for _ in ()).throw(AssertionError("fallback nao deveria rodar")))

    result = agent._resolve_with_ytdlp("https://youtube.com/watch?v=abc")

    assert result["stream_url"] == "https://media.example/audio"
    assert result["title"] == "Faixa"
    assert "ok" not in result
    assert "id" not in result


def test_music_agent_helper_quente_falha_e_preserva_fallback(monkeypatch) -> None:
    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()

    class Hot:
        def resolve(self, target, **kwargs):
            return None

    agent._ytdlp_warm_client = Hot()
    monkeypatch.setenv("MUSIC_AGENT_YTDLP_WARM_HELPER_ENABLED", "true")
    stdout = "\n".join([
        "__title__:Faixa fallback",
        "__uploader__:Canal",
        "__duration__:123",
        "__thumbnail__:https://img.example/x.jpg",
        "__webpage_url__:https://youtube.com/watch?v=abc",
        "__format_id__:251",
        "__ext__:webm",
        "__acodec__:opus",
        "__abr__:128",
        "__asr__:48000",
        "__audio_channels__:2",
        "https://media.example/fallback",
    ])
    completed = type("Completed", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()
    monkeypatch.setattr(agent, "_run_ytdlp_command", lambda *a, **k: completed)

    result = agent._resolve_with_ytdlp("https://youtube.com/watch?v=abc")

    assert result["stream_url"] == "https://media.example/fallback"
    assert result["title"] == "Faixa fallback"


def test_music_agent_cancelamento_do_helper_nao_cai_no_fallback(monkeypatch) -> None:
    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()

    class Hot:
        def resolve(self, target, **kwargs):
            raise RuntimeError("resolução yt-dlp cancelada")

    agent._ytdlp_warm_client = Hot()
    monkeypatch.setenv("MUSIC_AGENT_YTDLP_WARM_HELPER_ENABLED", "true")
    monkeypatch.setattr(agent, "_run_ytdlp_command", lambda *a, **k: (_ for _ in ()).throw(AssertionError("cancelamento nao pode cair no fallback")))

    with pytest.raises(RuntimeError, match="cancelada"):
        agent._resolve_with_ytdlp("https://youtube.com/watch?v=abc")


def test_helper_ytdlp_persistente_reusa_mesmo_processo(tmp_path) -> None:
    from cogs.musica.runtime_telefone.agente.ytdlp_quente import WarmYTDLPResolver

    helper = tmp_path / "helper.py"
    helper.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line); print(json.dumps({'ok':True,'id':r['id'],'stream_url':'https://media.example/a'}), flush=True)\n",
        encoding="utf-8",
    )
    client = WarmYTDLPResolver(script_path=str(helper))
    try:
        one = client.resolve("x", format_selector="bestaudio", timeout=2)
        pid = client._process.pid if client._process is not None else 0
        two = client.resolve("y", format_selector="bestaudio", timeout=2)
        assert one and two
        assert pid > 0
        assert client._process is not None and client._process.pid == pid
    finally:
        client.close()


def test_helper_ytdlp_cancelavel_mata_processo(tmp_path) -> None:
    from cogs.musica.runtime_telefone.agente.ytdlp_quente import WarmYTDLPResolver

    helper = tmp_path / "helper_lento.py"
    helper.write_text(
        "import json, sys, time\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line); time.sleep(5); print(json.dumps({'ok':True,'id':r['id'],'stream_url':'https://media.example/a'}), flush=True)\n",
        encoding="utf-8",
    )
    client = WarmYTDLPResolver(script_path=str(helper))
    cancel = threading.Event()
    error: list[BaseException] = []

    def run():
        try:
            client.resolve("x", format_selector="bestaudio", timeout=4, cancel_event=cancel)
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.15)
    cancel.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert error and "cancelada" in str(error[0]).lower()
    assert client._process is None
    client.close()
