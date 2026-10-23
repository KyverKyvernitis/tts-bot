# Validação — Music Agent 0.3.87

## Evidência que motivou a correção

O diagnóstico no Termux encontrou, em dois posts estáveis durante a consulta:

| Faixa | Nome esperado | Nome retornado pelo Discord | Tamanho |
| --- | --- | --- | ---: |
| Heaven Pierce Her — In the Presence of a King | Espaços no título | Espaços convertidos em `_` | 4.427.803 bytes |
| Kate Bush — Army Dreamers | Espaços no título | Espaços convertidos em `_` | 2.980.329 bytes |

Ambos tinham o mesmo sufixo de hash esperado e nenhum manifesto publicado.
Isso identifica a divergência de nomes; os metadados sozinhos não são uma
verificação do hash do áudio remoto. O código original da 0.3.86 foi carregado
do ZIP anteriormente entregue e reproduziu a rejeição da segunda combinação
de nome/tamanho. Não houve alteração do ZIP usado como referência.

## Verificações locais

**71 verificações suplementares passaram:**

| Grupo | Casos |
| --- | ---: |
| Novos casos de confirmação, publicação e retomada | 15 |
| Casos existentes de segmentos, manifesto, cache e temporários | 25 |
| Telemetria e lifecycle | 20 |
| Benchmark e agrupamentos | 11 |

Os casos novos verificam nome exato e normalizado; recusam tamanho, nome,
caixa, separador, autoria, CDN e candidatos ambíguos. Publicam as duas faixas
do diagnóstico e uma faixa segmentada com Unicode. Conferem nome efetivo no
manifesto, tamanho, SHA e bytes dos segmentos, título e perfil de áudio.
Interrupções antes da confirmação do primeiro áudio, no envio do manifesto
e na confirmação final retomam o mesmo post sem duplicar áudio ou partes.

A regressão de telemetria reproduz uma tentativa iniciada como arquivo que
termina online. O runtime e o benchmark passam a classificar a fonte do
primeiro pacote corretamente sem mudar o estado inicial do helper e da voz.
Os dois testes novos de telemetria/benchmark também falharam, como esperado,
ao receber o código original da 0.3.86 carregado em memória.

Essas execuções **não foram pytest**. Usaram adaptadores explícitos de
fixtures/bibliotecas e tarefas locais de arquivo. Não exercitam a camada HTTP
real de discord.py, o executor em threads nem a integração completa de UI.
O sandbox pode impedir o despertar/encerramento de tarefas `asyncio.to_thread`;
por isso as tarefas de arquivo dos testes de publicação foram executadas por
um adaptador inline. Não se declara ausência de regressões a partir disso.

## Pacote do telefone

O publisher real do projeto gerou um runtime com **63 arquivos e um manifesto
(64 membros no ZIP)**. O bootstrap 1.0.1 do repositório validou manifesto,
extração, hashes e modos. CRC e comparação dos bytes extraídos com as fontes
passaram. O manifesto mantém o requisito mínimo de bootstrap 1.0.0; não houve
uma nova execução do bootstrap original 1.0.0 nesta rodada.

Os arquivos Python do runtime foram analisados com a gramática do Python
3.10. Nenhum runtime foi enviado a uma VPS ou telefone nesta validação local.
O ZIP incremental é comparado ao ZIP completo da 0.3.86 previamente entregue:
cada arquivo antigo incluído precisa ter bytes diferentes; as adições são
listadas separadamente no inventário do patch.

## O que ainda depende do ambiente real

É necessário confirmar a 0.3.87 em execução, observar o arquivamento terminar
com `done`, reproduzir a faixa arquivada e conferir cortes, controles e
qualidade na call. Os erros de DNS observados são independentes desta
divergência de nomes. O backoff de falhas anteriores continua limitado a seis
horas e não é zerado pela atualização.

Os dois primeiros envios observados na 0.3.86 levaram aproximadamente 5,35 s e
3,55 s dentro do agente, mas tinham entrada e estado diferentes. Não
constituem uma comparação controlada nem comprovam um percentual de melhoria.
O prewarm permanece desativado no procedimento de validação.

Em um ambiente de desenvolvimento com as dependências e pytest instalados,
execute os testes reais relevantes:

```bash
python -m pytest -q --tb=short \
  cogs/musica/testes/runtime_telefone/test_archive_attachment_confirmation.py \
  cogs/musica/testes/runtime_telefone/test_arquivo_segmentado.py \
  cogs/musica/testes/runtime_telefone/test_archive_manifest_cache.py \
  cogs/musica/testes/runtime_telefone/test_archive_staging.py \
  cogs/musica/testes/runtime_telefone/test_music_start_telemetry.py \
  cogs/musica/testes/test_benchmark_primeiro_som.py
```

A suíte completa de regressão continua sendo:

```bash
python -m pytest cogs/musica/testes tests -q --tb=short
```
