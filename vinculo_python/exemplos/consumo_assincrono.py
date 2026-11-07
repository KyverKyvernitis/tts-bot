"""Exemplo experimental. Exige wheel Rust instalada no ambiente Python."""

import asyncio

from tts_fluxo_experimental import FluxoAssincrono


async def executar() -> None:
    fluxo = FluxoAssincrono(capacidade_bytes=8_192, maximo_blocos=4)

    async def produzir() -> None:
        for numero in range(15):
            await fluxo.escrever_bloco(bytes([numero]) * 512, tempo_maximo_ms=2_000)
        fluxo.finalizar()

    async def consumir() -> int:
        total = 0
        while (bloco := await fluxo.ler_bloco(tempo_maximo_ms=2_000)) is not None:
            total += len(bloco)
        return total

    produtor = asyncio.create_task(produzir())
    consumidor = asyncio.create_task(consumir())
    try:
        await produtor
        total = await consumidor
        print(f"Bytes recebidos: {total}")
        print("Estatísticas:", fluxo.estatisticas())
    finally:
        if not consumidor.done() or not produtor.done():
            fluxo.cancelar()
            produtor.cancel()
            consumidor.cancel()
            await asyncio.gather(produtor, consumidor, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(executar())
