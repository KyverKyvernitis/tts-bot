#!/usr/bin/env bash
# Valida as fases separadamente e falha se o núcleo nativo não puder ser testado.
# Não modifica bot, serviços, VPS, Termux nem dependências do sistema.
set -Eeuo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$RAIZ"

if [[ "${VIRTUAL_ENV:-}" == "" ]]; then
  echo 'Ative um venv de desenvolvimento antes de validar (source .venv/bin/activate).' >&2
  exit 2
fi
for CMD in cargo rustc maturin python; do
  command -v "$CMD" >/dev/null 2>&1 || {
    echo "Ferramenta ausente: $CMD. Instale-a fora da VPS de produção." >&2
    exit 3
  }
done

echo '=== VERSÕES ==='
rustc --version
cargo --version
maturin --version
python --version

echo '=== TESTES DO NÚCLEO ORIGINAL ==='
cargo test --manifest-path ../Cargo.toml --all-targets

echo '=== COMPILAÇÃO/INSTALAÇÃO DA EXTENSÃO NO VENV ==='
maturin develop --release

echo '=== EXTENSÃO NATIVA IMPORTÁVEL ==='
python -c 'from tts_fluxo_experimental._nucleo_fluxo_tts import FluxoDeAudio; assert FluxoDeAudio(8, 2).capacidade_bytes == 8'

echo '=== TESTES PYTHON (INCLUINDO NATIVOS) ==='
python -m unittest discover -s testes_python -p 'teste_*.py' -v

echo '=== EXEMPLO ASYNC ==='
python exemplos/consumo_assincrono.py

echo '=== BENCHMARK ISOLADO ==='
python benchmarks/comparar_fluxos.py --blocos 5000 --tamanho 1024

echo '=== VALIDAÇÃO DA ETAPA 2 CONCLUÍDA ==='
