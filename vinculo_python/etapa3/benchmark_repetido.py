"""Comparacao Rust/Python repetida, cada ensaio em processo separado.

Somente buffer local; nao representa a latencia do TTS completo.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--repeticoes', type=int, default=20)
    p.add_argument('--blocos', type=int, default=3000)
    p.add_argument('--tamanho', type=int, default=1024)
    a = p.parse_args()
    if not 3 <= a.repeticoes <= 100 or not 1 <= a.blocos <= 100_000 or not 1 <= a.tamanho <= 8192:
        p.error('argumentos fora dos limites seguros')
    script = Path(__file__).with_name('comparar_fluxos.py')
    resultados = {'python': [], 'rust': []}
    # Alterna a ordem para atenuar vies por aquecimento do dispositivo.
    for i in range(a.repeticoes):
        ordem = ['rust', 'python'] if i % 2 else ['python', 'rust']
        for motor in ordem:
            comando = [sys.executable, str(script), '--motor', motor,
                       '--blocos', str(a.blocos), '--tamanho', str(a.tamanho)]
            proc = subprocess.run(comando, capture_output=True, text=True, timeout=45)
            if proc.returncode:
                raise RuntimeError(f'Falha em {motor}: {proc.stderr[-300:]}')
            resultados[motor].append(json.loads(proc.stdout))
    resumo = {}
    for motor, dados in resultados.items():
        resumo[motor] = {
            campo: {
                'mediana': round(statistics.median(x[campo] for x in dados), 4),
                'pior': max(x[campo] for x in dados),
                'melhor': min(x[campo] for x in dados),
            }
            for campo in ('segundos', 'throughput_mib_s', 'leitura_p95_ms',
                          'leitura_p99_ms', 'escrita_p95_ms', 'pico_rss_kib')
        }
    print(json.dumps({'repeticoes': a.repeticoes, 'blocos': a.blocos,
                      'resultados': resumo}, ensure_ascii=False, indent=2))
    print('Amostra sintética; não prova melhora no Discord/FFmpeg.')


if __name__ == '__main__':
    main()
