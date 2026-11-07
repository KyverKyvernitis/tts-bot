"""Microbenchmark isolado: fila Rust/Python vs fila Python de referência.

Não representa o pipeline completo de TTS. Rodar fora de produção.
A comparação avalia throughput e custo local, não Edge, Discord ou FFmpeg.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import resource
import subprocess
import sys
import threading
import time


class FilaPython:
    """Referência limitada por bytes e quantidade de blocos, com um consumidor."""

    def __init__(self, capacidade_bytes: int, maximo_blocos: int) -> None:
        from collections import deque
        self._dados = deque()
        self._condicao = threading.Condition()
        self._capacidade = capacidade_bytes
        self._maximo = maximo_blocos
        self._ocupacao = 0
        self._finalizado = False
        self._cancelado = False

    def escrever_bloco(self, dados: bytes, tempo_maximo_ms: int = 0) -> None:
        if len(dados) > self._capacidade:
            raise ValueError("bloco maior que o limite")
        limite = time.monotonic() + tempo_maximo_ms / 1_000
        with self._condicao:
            while True:
                if self._cancelado:
                    raise RuntimeError("cancelado")
                if self._finalizado:
                    raise RuntimeError("finalizado")
                if not dados:
                    return
                if (self._ocupacao + len(dados) <= self._capacidade
                        and len(self._dados) < self._maximo):
                    self._dados.append(dados)
                    self._ocupacao += len(dados)
                    self._condicao.notify_all()
                    return
                restante = limite - time.monotonic()
                if restante <= 0:
                    raise TimeoutError("escrita")
                self._condicao.wait(restante)

    def ler_bloco(self, tempo_maximo_ms: int = 0) -> bytes | None:
        limite = time.monotonic() + tempo_maximo_ms / 1_000
        with self._condicao:
            while True:
                if self._cancelado:
                    raise RuntimeError("cancelado")
                if self._dados:
                    dados = self._dados.popleft()
                    self._ocupacao -= len(dados)
                    self._condicao.notify_all()
                    return dados
                if self._finalizado:
                    return None
                restante = limite - time.monotonic()
                if restante <= 0:
                    raise TimeoutError("leitura")
                self._condicao.wait(restante)

    def finalizar(self) -> None:
        with self._condicao:
            self._finalizado = True
            self._condicao.notify_all()

    def cancelar(self) -> None:
        with self._condicao:
            self._cancelado = True
            self._dados.clear()
            self._ocupacao = 0
            self._condicao.notify_all()


def percentil(valores: list[float], percentual: int) -> float:
    if not valores:
        return 0.0
    ordenado = sorted(valores)
    indice = (len(ordenado) - 1) * percentual / 100
    menor = int(indice)
    maior = min(menor + 1, len(ordenado) - 1)
    fracao = indice - menor
    return ordenado[menor] * (1 - fracao) + ordenado[maior] * fracao


def executar(motor: str, blocos: int, tamanho: int, capacidade: int, max_blocos: int) -> dict:
    if motor == "rust":
        from tts_fluxo_experimental._nucleo_fluxo_tts import FluxoDeAudio
        fluxo = FluxoDeAudio(capacidade, max_blocos)
    else:
        fluxo = FilaPython(capacidade, max_blocos)

    latencias_leitura_ms: list[float] = []
    latencias_escrita_ms: list[float] = []
    erros: list[str] = []

    def produzir() -> None:
        try:
            for indice in range(blocos):
                dados = bytes([indice % 256]) * tamanho
                antes = time.perf_counter_ns()
                fluxo.escrever_bloco(dados, 5_000)
                latencias_escrita_ms.append((time.perf_counter_ns() - antes) / 1e6)
            fluxo.finalizar()
        except Exception as exc:
            erros.append(f"{type(exc).__name__}: {exc}")
            fluxo.cancelar()

    inicio = time.perf_counter()
    tarefa = threading.Thread(target=produzir, name="produtor-de-teste")
    tarefa.start()
    consumidos = 0
    try:
        while True:
            antes = time.perf_counter_ns()
            dados = fluxo.ler_bloco(5_000)
            latencias_leitura_ms.append((time.perf_counter_ns() - antes) / 1e6)
            if dados is None:
                break
            consumidos += len(dados)
    finally:
        tarefa.join(timeout=8)
        if tarefa.is_alive():
            fluxo.cancelar()
            tarefa.join(timeout=3)
            erros.append("produtor não encerrou")
    duracao = time.perf_counter() - inicio
    if erros:
        raise RuntimeError("; ".join(erros))
    if consumidos != blocos * tamanho:
        raise RuntimeError(f"bytes perdidos: {consumidos} != {blocos * tamanho}")

    return {
        "motor": motor,
        "blocos": blocos,
        "bytes": consumidos,
        "segundos": round(duracao, 5),
        "throughput_mib_s": round(consumidos / 1048576 / duracao, 2),
        "leitura_p50_ms": round(percentil(latencias_leitura_ms, 50), 4),
        "leitura_p95_ms": round(percentil(latencias_leitura_ms, 95), 4),
        "leitura_p99_ms": round(percentil(latencias_leitura_ms, 99), 4),
        "escrita_p95_ms": round(percentil(latencias_escrita_ms, 95), 4),
        "pico_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--motor", choices=["rust", "python"])
    ap.add_argument("--blocos", type=int, default=10_000)
    ap.add_argument("--tamanho", type=int, default=1_024)
    ap.add_argument("--capacidade", type=int, default=131_072)
    ap.add_argument("--maximo-blocos", type=int, default=64)
    args = ap.parse_args()

    if (args.blocos <= 0 or args.tamanho <= 0 or args.maximo_blocos <= 0
            or args.capacidade < args.tamanho):
        ap.error("limites inválidos para o benchmark")

    if args.motor:
        print(json.dumps(executar(args.motor, args.blocos, args.tamanho,
                                  args.capacidade, args.maximo_blocos)))
        return

    resultados = []
    for motor in ("python", "rust"):
        comando = [sys.executable, str(Path(__file__).resolve()),
                   "--motor", motor, "--blocos", str(args.blocos),
                   "--tamanho", str(args.tamanho),
                   "--capacidade", str(args.capacidade),
                   "--maximo-blocos", str(args.maximo_blocos)]
        processo = subprocess.run(comando, text=True, capture_output=True)
        if processo.returncode != 0:
            print(f"Falha ao executar benchmark {motor}: {processo.stderr.strip()}",
                  file=sys.stderr)
            raise SystemExit(1)
        resultados.append(json.loads(processo.stdout.strip()))

    print(json.dumps(resultados, ensure_ascii=False, indent=2))
    print("Amostra sintética, uma execução por motor. Não inferir ganho do TTS completo.")


if __name__ == "__main__":
    main()
