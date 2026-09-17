from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cogs.musica.agente_telefone import transporte_http
from cogs.musica.interface import tarefas


class _SessaoFake:
    def __init__(self, *args, **kwargs) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_sessao_http_reutiliza_conexao_e_fecha_no_shutdown(monkeypatch) -> None:
    criadas: list[_SessaoFake] = []

    def criar_sessao(*args, **kwargs):
        sessao = _SessaoFake(*args, **kwargs)
        criadas.append(sessao)
        return sessao

    monkeypatch.setattr(transporte_http.aiohttp, "TCPConnector", lambda **kwargs: object())
    monkeypatch.setattr(transporte_http.aiohttp, "ClientSession", criar_sessao)
    transporte_http._SESSAO = None
    transporte_http._SESSAO_LOOP = None
    transporte_http._SESSAO_LOCK = None
    transporte_http._SESSAO_LOCK_LOOP = None

    primeira = await transporte_http.obter_sessao_http()
    segunda = await transporte_http.obter_sessao_http()

    assert primeira is segunda
    assert len(criadas) == 1

    await transporte_http.fechar_sessao_http()
    assert primeira.closed is True
    assert transporte_http._SESSAO is None


class _RespostaFake:
    status = 200

    async def text(self) -> str:
        return "[]"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SessaoPostFake:
    closed = False

    def post(self, *args, **kwargs):
        return _RespostaFake()


@pytest.mark.asyncio
async def test_transporte_rejeita_json_2xx_que_nao_seja_objeto(monkeypatch) -> None:
    async def obter_fake():
        return _SessaoPostFake()

    monkeypatch.setattr(transporte_http, "obter_sessao_http", obter_fake)

    with pytest.raises(RuntimeError, match="resposta JSON inesperada"):
        await transporte_http.post_json_worker(
            url="http://worker/task",
            token="token",
            payload={"task": "music_agent_status"},
            timeout_seconds=1.0,
        )


@pytest.mark.asyncio
async def test_tarefa_unica_cancela_watch_anterior_da_mesma_guild() -> None:
    tarefas._TAREFAS.clear()
    primeira_cancelada = asyncio.Event()
    liberar_segunda = asyncio.Event()

    async def primeira() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            primeira_cancelada.set()

    async def segunda() -> None:
        await liberar_segunda.wait()

    tarefa_1 = tarefas.agendar_tarefa_unica(("watch", 123), primeira())
    await asyncio.sleep(0)
    tarefa_2 = tarefas.agendar_tarefa_unica(("watch", 123), segunda())
    await asyncio.wait_for(primeira_cancelada.wait(), timeout=1.0)

    assert tarefa_1.cancelled()
    assert tarefa_2.cancelled() is False
    assert tarefas.quantidade_tarefas_interface() == 1

    liberar_segunda.set()
    await tarefa_2
    await asyncio.sleep(0)
    assert tarefas.quantidade_tarefas_interface() == 0


@pytest.mark.asyncio
async def test_cancelamento_global_de_tarefas_da_interface() -> None:
    tarefas._TAREFAS.clear()

    async def pendente() -> None:
        await asyncio.Event().wait()

    tarefa = tarefas.agendar_tarefa_unica(("prefetch", 999), pendente())
    await asyncio.sleep(0)
    await tarefas.cancelar_tarefas_interface()

    assert tarefa.cancelled()
    assert tarefas.quantidade_tarefas_interface() == 0


def test_comandos_e_resolucao_nao_criam_clientsession_por_requisicao() -> None:
    raiz = Path(__file__).resolve().parents[1]
    comandos = (raiz / "agente_telefone" / "comandos.py").read_text(encoding="utf-8")
    resolucao = (raiz / "agente_telefone" / "transporte_resolucao.py").read_text(encoding="utf-8")
    integracao = (raiz / "integracoes" / "bot.py").read_text(encoding="utf-8")

    assert "ClientSession(" not in comandos
    assert "ClientSession(" not in resolucao
    assert "post_json_worker" in comandos
    assert "post_json_worker" in resolucao
    assert "fechar_sessao_http" in integracao
    assert "cancelar_tarefas_interface" in integracao


def test_fila_remota_nao_e_truncada_pelo_limite_de_historico() -> None:
    from cogs.musica.nucleo.estado import MusicGuildState, MUSIC_QUEUE_MAXSIZE
    from cogs.musica.reproducao.sincronizacao import sincronizar_fila_remota

    state = MusicGuildState()
    quantidade = min(40, MUSIC_QUEUE_MAXSIZE)
    remote = {
        "queue_size": quantidade,
        "queue": [
            {
                "title": f"Faixa {i}",
                "webpage_url": f"https://example.test/{i}",
                "requester_id": 1,
            }
            for i in range(quantidade)
        ],
    }

    sincronizar_fila_remota(
        state,
        remote,
        limite_fila=MUSIC_QUEUE_MAXSIZE,
        limite_historico=5,
    )

    assert len(state.forward_queue) == quantidade
    assert state.forward_queue[0].title == "Faixa 0"
    assert state.forward_queue[-1].title == f"Faixa {quantidade - 1}"
    assert state.agent_remote_queue_size == quantidade


def test_forward_queue_usa_limite_de_fila_e_nao_limite_de_historico() -> None:
    from cogs.musica.nucleo.estado import MUSIC_QUEUE_MAXSIZE, MusicGuildState

    state = MusicGuildState()
    assert state.forward_queue.maxlen == MUSIC_QUEUE_MAXSIZE
