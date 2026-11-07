"""Interface asyncio experimental, sem bloquear o loop com Condvar do Rust.

Operações que podem aguardar capacidade ou dados são executadas no executor
padrão do asyncio. O núcleo nativo libera o GIL durante a espera.
Um cancelamento da corrotina cancela *todo* o fluxo de propósito, para impedir
que a operação destacada permaneça bloqueada ou publique dados tardiamente.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable


class FluxoAssincrono:
    """Adaptador para fila limitada de um consumidor, sem integração com Discord.

    O fluxo é descartável: após ``cancelar()``, crie outro para continuar.
    Cancelar qualquer tarefa de leitura/escrita cancela o fluxo compartilhado.
    """

    def __init__(
        self,
        capacidade_bytes: int = 131_072,
        maximo_blocos: int = 64,
        *,
        _fabrica: Callable[..., Any] | None = None,
    ) -> None:
        if _fabrica is None:
            try:
                from ._nucleo_fluxo_tts import FluxoDeAudio
            except ImportError as exc:
                raise RuntimeError(
                    "Extensão Rust ausente. Compile com 'maturin develop --release' "
                    "em um ambiente de desenvolvimento."
                ) from exc
            _fabrica = FluxoDeAudio
        self._fluxo = _fabrica(capacidade_bytes, maximo_blocos)

    async def escrever_bloco(self, dados: bytes, tempo_maximo_ms: int = 1_000) -> None:
        """Insere bloco em thread, liberando o loop para outras tarefas."""
        try:
            await asyncio.to_thread(self._fluxo.escrever_bloco, dados, tempo_maximo_ms)
        except asyncio.CancelledError:
            # to_thread não interrompe a thread automaticamente.
            self.cancelar()
            raise

    async def ler_bloco(self, tempo_maximo_ms: int = 1_000) -> bytes | None:
        """Lê bloco ou None (EOF normal); erros são exceções distintas."""
        try:
            return await asyncio.to_thread(self._fluxo.ler_bloco, tempo_maximo_ms)
        except asyncio.CancelledError:
            self.cancelar()
            raise

    def finalizar(self) -> None:
        """Sinaliza EOF após drenar os blocos pendentes."""
        self._fluxo.finalizar()

    def cancelar(self) -> None:
        """Desperta leituras/escritas pendentes e descarta dados."""
        self._fluxo.cancelar()

    def estatisticas(self) -> dict[str, int | str]:
        return self._fluxo.estatisticas()
