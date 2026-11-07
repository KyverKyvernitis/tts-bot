# Etapa 2: vínculo Python/Rust para fluxo de áudio TTS

Este é um **protótipo isolado**. Não é importado por `cogs/tts`, não altera
`bot.py`, `FFmpeg`, `Phone Worker`, `_status` ou serviços da VPS. O crate Rust
da etapa 1 permanece sem dependências externas e sem modificações.

## Estrutura

- `src/lib.rs`: vínculo PyO3, exceções explícitas, liberação do GIL nas esperas.
- `python/tts_fluxo_experimental`: pacote opcional e adaptador `asyncio`.
- `testes_python/`: testes de integração e cancelamento; nenhum pacote de testes externo.
- `benchmarks/comparar_fluxos.py`: comparação sintética isolada com Python.
- `exemplos/consumo_assincrono.py`: produtor e consumidor assíncronos.
- `scripts/validar_etapa2.sh`: validação completa em ambiente de desenvolvimento.

## Pré-requisitos

- `rustc`, `cargo`, Python 3.10+ e venv de desenvolvimento.
- `maturin>=1.9,<2` para compilar a extensão PyO3 0.27.1.
- É preciso acesso ao registro de crates na primeira compilação; não há binário
  pré-compilado neste pacote. A compilação **não foi executada** no ambiente
  de autoria se `cargo`/`rustc` estiverem indisponíveis.

## Compilar e testar fora da VPS

Partindo da pasta `rust/nucleo_fluxo_tts/vinculo_python`:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install 'maturin>=1.9,<2'
bash scripts/validar_etapa2.sh
```

Ou, manualmente:

```bash
cargo test --manifest-path ../Cargo.toml --all-targets
maturin develop --release
python -m unittest discover -s testes_python -p 'teste_*.py' -v
python exemplos/consumo_assincrono.py
python benchmarks/comparar_fluxos.py --blocos 10000 --tamanho 1024
```

**Não executar compilação na VPS de 1 GB em produção.** O build gera um módulo
nativo para a arquitetura/ABI local. Na migração real, o updater precisará
validar wheel Linux x86-64 e suportar rollback. O nome dos módulos e das funções
é em português; identificadores de formato (`pyproject.toml`, `Cargo.toml`)
seguem as convenções das ferramentas.

## Contrato e limites

```python
from tts_fluxo_experimental._nucleo_fluxo_tts import FluxoDeAudio
from tts_fluxo_experimental import FluxoAssincrono

fluxo = FluxoDeAudio(capacidade_bytes=131072, maximo_blocos=64)
fluxo.escrever_bloco(b"audio", tempo_maximo_ms=1000)
fluxo.finalizar()
assert fluxo.ler_bloco(1000) == b"audio"
assert fluxo.ler_bloco(1000) is None  # EOF normal

# Em asyncio: await FluxoAssincrono().escrever_bloco(...) e ler_bloco(...)
```

- `ErroTempoEsgotado` (`TimeoutError`): prazo venceu; diferente de EOF.
- `ErroFluxoFinalizado` (`RuntimeError`): novas escritas proibidas.
- `ErroFluxoCancelado` (`RuntimeError`): dados pendentes descartados.
- `ValueError`: bloco maior que a capacidade ou configuração inválida.
- `tempo_maximo_ms` aceita 0 a 3.600.000. Zero = operação imediata.
- `escrever_bloco()` copia o bloco **uma vez** ao cruzar Python/Rust; `ler_bloco()`
  cria bytes Python na volta. Nunca armazena objetos Python no núcleo.
- PyO3 usa `Python::detach` ao aguardar em `Condvar` do Rust. Isso libera o GIL,
  mas a função *síncrona* ainda espera na thread chamadora: não chamá-la no loop.
- `FluxoAssincrono` utiliza `asyncio.to_thread()`. Cancelar uma corrotina cancela
  todo o fluxo para acordar a thread que continua executando.
- Um consumidor lógico; múltiplos leitores competiriam por blocos, sem replay.
- Sem interface para FFmpeg/Discord neste pacote. Nenhum fallback automático
  silencioso: se o binário nativo não estiver instalado, o adaptador falha claro.

## Limitações da medição

O benchmark compara apenas duas filas, não mede rede/FFmpeg/Discord. A fila
Python é referência simplificada, não é o ReplayBuffer de produção. Leituras
passam pela fronteira PyO3, com possível cópia; Rust não é necessariamente mais
rápido. Métrica RSS por processo Linux (em KiB); executar várias rodadas e
comparar nas mesmas condições antes de decidir migrar. Não há resultados
numéricos neste pacote antes da compilação real.

## Critérios para a etapa 3

Testar em ambiente não produtivo, com áudio real: EOF/timeout/cancelamento,
flush, perda de dados, concorrência e latência p50/p95/p99. Somente após isso
integrar por `feature flag`, com retorno imediato ao caminho Python.
