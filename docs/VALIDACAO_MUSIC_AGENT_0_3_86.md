# Validação — Music Agent 0.3.86

## Resultado local

A implementação está salva e foi revisada, incluindo revisão independente de
cancelamento, encerramento do helper, identidade de worker e contagem dos
traces. O aquecimento é opcional e permanece desativado por padrão.

Foram reproduzidas **86 verificações suplementares**, executando diretamente
os corpos dos testes com adaptadores locais de fixtures:

| Grupo | Verificações aprovadas |
| --- | ---: |
| Benchmark, grupos, falhas, replays e compatibilidade legada | 10 |
| Telemetria de runtime e regressões existentes de lifecycle | 19 |
| Aquecimento, compartilhamento, cancelamento e subprocessos | 7 |
| Seleção de fonte, bitrate, resampling, filtros e TTS | 25 |
| Efeitos com FFmpeg/ffprobe reais | 3 |
| Mixer: estéreo, clipping, TTS e silêncio sintético | 6 |
| Admissão, TTL, vínculo, expiração e recuperação de rota | 16 |

Esses resultados **não são uma execução de pytest**. Os adaptadores usam
stubs de dependências e as mesmas funções dos testes indicados; não validam
por completo a integração com discord.py, as fixtures de pytest nem a UI.
O teste com a biblioteca yt-dlp real não executou porque a dependência não
está instalada. Dois testes de resolução com executor ficaram pendentes devido
ao bloqueio de sockets locais do ambiente. Os demais testes de integração de
thread/UI também continuam pendentes.

As três verificações com FFmpeg real cobrem duração e frequência do nightcore,
saída 48 kHz, rejeição de aliasing, mudança de tom e cauda do slowed/reverb,
incluindo limites de clipping. Elas não equivalem a uma escuta no cliente
Discord nem à verificação integrada de todas as fronteiras de segmentos.

A análise de sintaxe dos **516 arquivos Python** de música, scripts e testes
terminou sem erros. O publisher local gerou um release de **63 arquivos mais o
manifest**: o bootstrap original 1.0.0 validou manifesto, extração, modos e
hashes; CRC e comparação dos bytes extraídos com as fontes também passaram.
Nenhum release foi enviado a um servidor ou aparelho.

## Correções verificadas durante a revisão

- A validação reaproveitada não renova a idade do cache e é invalidada por
  configuração, token, guild ou mudança do dono da sessão. Um endpoint
  atualizado do mesmo telefone pode ser recuperado após nova admissão.
- A expiração seguida de indisponibilidade preserva o comando diferido com
  seu ID e exige admissão antes do novo envio. Cancelamento termina a
  observação uma vez.
- Inicialização parcial ou travada respeita o prazo; encerramento recolhe o
  helper e seus filhos e impede reabertura por uma thread atrasada.
- Fila, espera humana, pedidos coalescidos, recuperação de stream e loop não
  contaminam as métricas da primeira música. O primeiro pacote real prevalece
  sobre uma resposta HTTP perdida; confirmação do controller é mostrada
  separadamente.

## Verificações pendentes antes da liberação geral

A instalação das dependências e as execuções com permissão adicional foram
interrompidas. O ambiente padrão bloqueia comunicações de rede e os sockets
locais usados pelo asyncio; por isso a suíte integrada não foi executada.
Rode, com as dependências do projeto e pytest/pytest-asyncio/pytest-subtests:

```bash
python -m pytest cogs/musica/testes tests -q --tb=short
```

A troca do ambiente também removeu o ZIP original e a cópia da 0.3.85 que
estavam em `/tmp`. Portanto não foi possível reproduzir a comparação ampla
antes/depois nesta execução. Os números de testes e as cinco falhas
preexistentes documentados na 0.3.85 são históricos: não foram reproduzidos
nem usados para declarar esta entrega sem regressões. Compare as falhas
individualmente ao executar as duas versões no mesmo ambiente.

Ainda é necessário executar o piloto no Termux descrito na
[entrega 0.3.86](MUSIC_AGENT_0_3_86.md), incluindo primeira música arquivada,
link novo, seleção textual, playlist, duas calls e arquivamento ativo. Não
houve medição em call real nem porcentagem de melhoria alegada. O aquecimento
permanece desativado até o resultado desse comparativo.
