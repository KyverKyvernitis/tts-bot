# Núcleo de fluxo de áudio TTS em Rust (experimental)

Primeira etapa de desenvolvimento em Rust. **Não está integrado ao bot** e não muda o TTS em produção, o FFmpeg, o Termux nem o updater.

## Escopo

- Fila de blocos de áudio para **um consumidor lógico** e múltiplos produtores.
- Limites por bytes e quantidade de blocos, impedindo crescimento ilimitado da fila.
- Escrita e leitura com prazo, sem laços de espera ativa.
- Encerramento normal com drenagem e EOF explícito (`Ok(None)`).
- Cancelamento imediato, descarte contabilizado e despertar de tarefas bloqueadas.
- Estatísticas locais de volume, ocupação máxima e operações aguardadas.
- Nenhuma dependência externa; apenas a biblioteca padrão do Rust.

### Contrato

- `escrever_bloco(Vec<u8>, Duration)`: insere o bloco; em caso de erro o bloco entregue é descartado (o chamador deve reter cópia se desejar recuperar).
- `ler_bloco(Duration) -> Result<Option<Vec<u8>>, ErroDoFluxo>`: `Some` entrega bloco; `None` significa EOF normal após `finalizar()` e drenagem.
- `cancelar()`: descarta o restante; a leitura retorna `Err(Cancelado)`.
- `finalizar()`: proíbe novas escritas e preserva blocos pendentes.
- As operações bloqueantes devem ser chamadas numa thread de trabalho apropriada. Não devem ser executadas diretamente na thread do loop `asyncio`.
- **A fila não suporta replay/múltiplos consumidores**. Não substitui `cogs/tts/runtime.py` (`ReplayBuffer`) e não implementa ainda FIFO do sistema operacional, decodificação, transporte Discord nem integração Python.

## Compilar e testar

Com Rust e Cargo instalados numa máquina de desenvolvimento:

```bash
cargo test --manifest-path rust/nucleo_fluxo_tts/Cargo.toml --offline
cargo run --manifest-path rust/nucleo_fluxo_tts/Cargo.toml --example fluxo_local --offline
```

Não execute build na VPS de produção de 1 GB RAM. A fase posterior de migração deverá definir integração Python/Rust, versão de CPython e distribuição de artefatos binários pelo updater, com rollback. Nenhuma alteração de serviço é necessária nesta etapa.

## Próxima etapa (ainda não implementada)

1. Medir custo real do caminho FIFO/FFmpeg em Python, e fixar referência de latência e memória.
2. Prototipar uma interface Python/Rust segura, sem bloquear o `asyncio`, mantendo opção de fallback.
3. Testar EOF, backpressure, cancelamento, reinício do FFmpeg, concorrência e reprodução de áudio real.
4. Só ativar caso os resultados sejam melhores ou equivalentes e sem regressões.
