#!/usr/bin/env bash
# Valida o modulo compilado na maquina Linux x86-64, sem instalar no bot.
set -Eeuo pipefail
umask 077
BASE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

[[ "$(uname -m)" = x86_64 && "$(uname -s)" = Linux ]] || {
  echo 'ERRO: esta etapa precisa de Linux x86-64.' >&2; exit 2;
}

cd "$BASE"
echo '=== INTEGRIDADE ==='
sha256sum -c SHA256SUMS

PY="${PYTHON_VPS:-}"
if [[ -z "$PY" ]]; then
  CANDIDATO=/home/ubuntu/bot-update-staging/candidates/python-runtimes/current/venv/bin/python
  if [[ -x "$CANDIDATO" ]]; then
    PY="$CANDIDATO"
  else
    PY="$(command -v python3 || true)"
  fi
fi
[[ -x "$PY" ]] || { echo 'ERRO: Python nao encontrado'; exit 3; }

export PYTHONPATH="$BASE${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1

echo '=== AMBIENTE DE TESTE ==='
"$PY" - <<'PY'
import sys, sysconfig, platform, importlib.util
print('Python:', sys.version.split()[0], sys.executable)
print('Plataforma:', sysconfig.get_platform(), platform.libc_ver())
assert sys.version_info >= (3, 10), 'abi3-py310 exige Python >= 3.10'
from tts_fluxo_experimental._nucleo_fluxo_tts import FluxoDeAudio
from tts_fluxo_experimental import FluxoAssincrono
fluxo = FluxoDeAudio(32, 2)
fluxo.escrever_bloco(b'teste')
fluxo.finalizar()
assert fluxo.ler_bloco() == b'teste'
assert fluxo.ler_bloco() is None
print('Importacao e EOF: OK')
PY

echo '=== TESTES NATIVOS E ASSINCRONOS ==='
"$PY" -B -m unittest discover -s "$BASE/testes_python" -p 'teste_*.py' -v

echo '=== STATUS ==='
echo 'Validacao isolada concluida, sem instalacao no bot.'

if [[ "${1:-}" == '--benchmark' ]]; then
  echo '=== BENCHMARK COM PROCESSOS ISOLADOS ==='
  "$PY" -B "$BASE/benchmarks/benchmark_repetido.py" --repeticoes 20 --blocos 3000
fi
