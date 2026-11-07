"""Fila Rust opcional para um único consumidor do FIFO TTS.

Não substitui ReplayBuffer (que possui vários leitores independentes).
Sem TTS_FLUXO_RUST=1, não carrega a extensão nem altera a fila padrão.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)
_ESPERA_MS = 1_000
_AVISO_MODULO = False
_RUST_FALHOU = False


class FalhaFilaRust(RuntimeError):
    """Falha nativa inesperada. Próxima reprodução utiliza Python."""


def _marcar_falha(erro: BaseException) -> None:
    global _RUST_FALHOU, _AVISO_MODULO
    _RUST_FALHOU = True
    if not _AVISO_MODULO:
        _LOG.warning("Fila Rust desativada para novas reproduções: %s", erro)
        _AVISO_MODULO = True


def rust_habilitado() -> bool:
    return os.environ.get("TTS_FLUXO_RUST", "0").strip() == "1"


@lru_cache(maxsize=1)
def _carregar_nucleo() -> Any:
    # Não altera sys.path e não importa as bibliotecas de teste na inicialização.
    arquivo = (
        Path(__file__).resolve().parents[2]
        / "vinculo_python/python/tts_fluxo_experimental"
        / "_nucleo_fluxo_tts.abi3.so"
    )
    if not arquivo.is_file():
        raise ImportError(f"módulo Rust indisponível: {arquivo}")
    especificacao = importlib.util.spec_from_file_location(
        "tts_fluxo_experimental._nucleo_fluxo_tts", str(arquivo)
    )
    if especificacao is None or especificacao.loader is None:
        raise ImportError(f"módulo Rust inválido: {arquivo}")
    modulo = importlib.util.module_from_spec(especificacao)
    especificacao.loader.exec_module(modulo)
    return modulo


class FilaRust:
    """Adaptação mínima de put/get/get_nowait/task_done usada pelo FIFO.

    As operações que podem esperar usam to_thread e o núcleo libera o GIL.
    Cancelar uma corrotina cancela a instância inteira, despertando threads.
    """

    mecanismo = "rust"

    def __init__(self, *, maximo_blocos: int, bytes_por_bloco: int, sentinela: object,
                 _nucleo: Any = None) -> None:
        if maximo_blocos <= 0 or bytes_por_bloco <= 0:
            raise ValueError("limites da fila precisam ser positivos")
        self._modulo = _nucleo if _nucleo is not None else _carregar_nucleo()
        self._fluxo = self._modulo.FluxoDeAudio(maximo_blocos * bytes_por_bloco, maximo_blocos)
        self._sentinela = sentinela
        self._cancelada = False

    def cancelar(self) -> None:
        self._cancelada = True
        self._fluxo.cancelar()

    def _falha(self, erro: BaseException) -> None:
        _marcar_falha(erro)
        with contextlib.suppress(Exception):
            self.cancelar()
        raise FalhaFilaRust("falha na fila Rust; fluxo encerrado") from erro

    async def put(self, bloco: Any) -> None:
        if bloco is self._sentinela:
            # EOF não ocupa uma vaga da fila; drena os dados pendentes antes de None.
            if not self._cancelada:
                try:
                    self._fluxo.finalizar()
                except self._modulo.ErroFluxoCancelado:
                    return
                except Exception as erro:
                    self._falha(erro)
            return
        if self._cancelada:
            raise asyncio.CancelledError()
        dados = bytes(bloco)
        if not dados:
            return
        try:
            # Tentativa imediata: evita criar uma thread quando existe espaço.
            try:
                self._fluxo.escrever_bloco(dados, 0)
                return
            except self._modulo.ErroTempoEsgotado:
                pass
            while True:
                try:
                    await asyncio.to_thread(self._fluxo.escrever_bloco, dados, _ESPERA_MS)
                    return
                except self._modulo.ErroTempoEsgotado:
                    if self._cancelada:
                        raise asyncio.CancelledError()
                except self._modulo.ErroFluxoCancelado:
                    raise asyncio.CancelledError() from None
        except self._modulo.ErroFluxoCancelado:
            raise asyncio.CancelledError() from None
        except asyncio.CancelledError:
            self.cancelar()
            raise
        except Exception as erro:
            self._falha(erro)

    def get_nowait(self) -> Any:
        try:
            dados = self._fluxo.ler_bloco(0)
        except self._modulo.ErroTempoEsgotado:
            raise asyncio.QueueEmpty() from None
        except self._modulo.ErroFluxoCancelado:
            raise asyncio.CancelledError() from None
        except Exception as erro:
            self._falha(erro)
        return self._sentinela if dados is None else dados

    async def get(self) -> Any:
        if self._cancelada:
            raise asyncio.CancelledError()
        try:
            try:
                return self.get_nowait()  # Caminho sem thread se já há bloco.
            except asyncio.QueueEmpty:
                pass
            while True:
                try:
                    dados = await asyncio.to_thread(self._fluxo.ler_bloco, _ESPERA_MS)
                    return self._sentinela if dados is None else dados
                except self._modulo.ErroTempoEsgotado:
                    if self._cancelada:
                        raise asyncio.CancelledError()
                except self._modulo.ErroFluxoCancelado:
                    raise asyncio.CancelledError() from None
        except asyncio.CancelledError:
            self.cancelar()
            raise
        except FalhaFilaRust:
            raise
        except Exception as erro:
            self._falha(erro)

    def task_done(self) -> None:
        # A fila do FIFO não usa Queue.join(); Rust contabiliza cada leitura.
        return None

    def estatisticas(self) -> dict[str, Any]:
        return self._fluxo.estatisticas()


def criar_fila_streaming(*, maximo_blocos: int, bytes_por_bloco: int,
                         sentinela: object) -> tuple[Any, str]:
    """Ativa Rust somente em novas reproduções; falha na criação usa Python."""
    if rust_habilitado() and not _RUST_FALHOU:
        try:
            fila = FilaRust(maximo_blocos=maximo_blocos, bytes_por_bloco=bytes_por_bloco,
                            sentinela=sentinela)
            return fila, "rust"
        except (OSError, ImportError, ValueError, RuntimeError) as erro:
            _marcar_falha(erro)
    return asyncio.Queue(maxsize=maximo_blocos), "python"
