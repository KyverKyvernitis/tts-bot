"""Comparação isolada da fila asyncio e Rust, sem acesso a Discord/FFmpeg.

Uso: python tests/benchmark_tts_fluxo_rust.py --repeticoes 20 --blocos 1000
Não ativa Rust no bot. Reporta resultados sintéticos, não latência de voz.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cogs.tts.fluxo_rust import FilaRust


def percentil(valores: list[float], porcentagem: float) -> float:
    valores = sorted(valores)
    return valores[min(len(valores) - 1, round((len(valores) - 1) * porcentagem / 100))]


async def rodada(motor: str, *, blocos: int, tamanho: int) -> tuple[float, float]:
    fim = object()
    fila = (FilaRust(maximo_blocos=64, bytes_por_bloco=tamanho, sentinela=fim)
            if motor == "rust" else asyncio.Queue(maxsize=64))
    dados = bytes([0x57]) * tamanho
    bytes_consumidos = 0

    async def produzir():
        for _ in range(blocos):
            await fila.put(dados)
        await fila.put(fim)

    async def consumir():
        nonlocal bytes_consumidos
        while True:
            dado = await fila.get()
            if dado is fim:
                fila.task_done()
                return
            bytes_consumidos += len(dado)
            fila.task_done()

    inicio = time.perf_counter()
    await asyncio.wait_for(asyncio.gather(produzir(), consumir()), 20)
    duracao = time.perf_counter() - inicio
    assert bytes_consumidos == blocos * tamanho
    return duracao, (blocos * tamanho / 1024**2) / duracao


async def principal(repeticoes: int, blocos: int, tamanho: int) -> list[dict]:
    resultados = []
    for motor in ("python", "rust"):
        amostras = [await rodada(motor, blocos=blocos, tamanho=tamanho)
                    for _ in range(repeticoes)]
        duracoes = [x[0] * 1000 for x in amostras]
        taxas = [x[1] for x in amostras]
        resultados.append({
            "motor": motor, "repeticoes": repeticoes,
            "blocos_por_repeticao": blocos, "bytes_por_bloco": tamanho,
            "duracao_mediana_ms": round(statistics.median(duracoes), 3),
            "duracao_p95_ms": round(percentil(duracoes, 95), 3),
            "duracao_p99_ms": round(percentil(duracoes, 99), 3),
            "throughput_mediano_mib_s": round(statistics.median(taxas), 3),
        })
    return resultados


if __name__ == "__main__":
    analisador = argparse.ArgumentParser()
    analisador.add_argument("--repeticoes", type=int, default=20)
    analisador.add_argument("--blocos", type=int, default=1000)
    analisador.add_argument("--bytes", type=int, default=4096)
    args = analisador.parse_args()
    if not 1 <= args.repeticoes <= 100 or not 1 <= args.blocos <= 100000 or not 1 <= args.bytes <= 65536:
        analisador.error("argumentos fora dos limites permitidos")
    print(json.dumps(asyncio.run(principal(args.repeticoes, args.blocos, args.bytes)), indent=2))
