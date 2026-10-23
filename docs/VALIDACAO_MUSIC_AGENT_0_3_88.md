# Validação — Music Agent 0.3.88

## Evidência real

Dois diagnósticos estáveis no Termux mostraram:

| Esperado | Retornado pelo Discord | Tamanho |
| --- | --- | ---: |
| `The Cure - Boys Don't Cry-c7cd57487667.ogg` | `The_Cure_-_Boys_Dont_Cry-c7cd57487667.ogg` | 2.656.109 bytes |
| `yungatita - 7 Weeks & 3 Days-d69901f6d4db.ogg` | `yungatita_-_7_Weeks__3_Days-d69901f6d4db.ogg` | 3.395.259 bytes |

Ambos estavam sem manifesto. O matcher original da 0.3.87 rejeitou os dois
pares em reprodução local, confirmando a limitação. O tamanho e o sufixo de
hash nos nomes são evidência de metadados; não representam leitura ou
verificação dos bytes remotos.

O bootstrap também passou a informar `succeeded`, com a release 0.3.87
esperada. Este patch não altera o bootstrap ou o controle de processos.

## Implementação

Novos anexos usam somente ASCII no nome, com ordem e SHA-256 completo. Títulos
e áudio permanecem seguindo o comportamento atual. A retomada procura nomes
antigos por aliases explícitos, reconhecendo somente as transformações já
observadas. Não existe aceitação baseada apenas em tamanho ou sufixo de hash.
As verificações existentes de autoria e CDN permanecem ativas.

Cada anexo conta uma única vez na união dos aliases. Candidatos ambíguos são
rejeitados. O nome real do anexo reutilizado é fixado antes da confirmação
REST, e o manifesto registra o nome confirmado. Os aliases ficam fora dos
metadados duráveis. Posts v8 já concluídos não passam por migração de nomes.

## Resultado local

**58 verificações suplementares passaram: 33 focadas e 25 existentes.**

Os casos incluem:

- Correspondência exata, espaços normalizados e os dois pares reais novos.
- Recusa de tamanho, nome, caixa, hash, separadores, autoria, CDN e ambiguidade.
- Publicação com nomes ASCII e preservação do título Unicode, do perfil de
  áudio, do tamanho, dos bytes e do SHA dos segmentos.
- Retomada de posts antigos, inclusive segmentados, com os mesmos IDs de
  anexos e sem novos uploads de áudio quando os aliases conhecidos coincidem.
- Recuperação após interrupção no envio ou na confirmação do manifesto.
- Retomada canônica após mudança do título visível, conservando os anexos.
- Recusa de candidatos legados divergentes, seguida da publicação de um novo
  anexo canônico validado no mesmo post.
- Casos existentes de manifesto, cache, identidade, segmentos e temporários.

Houve também revisão independente do diff e verificações isoladas das funções
reais para aliases, limites de normalização e ambiguidade.

As 58 execuções **não foram pytest**. Usaram adaptadores explícitos de
fixtures, Discord e tarefas de arquivo inline. Não exercitam a camada HTTP
real de discord.py, as notificações do executor em threads nem a integração
com o cliente Discord. Não se declara ausência de regressões a partir disso.

## Pacotes

O publisher real gerou **63 arquivos de runtime mais um manifesto**, mantendo
64 membros no ZIP. O bootstrap 1.0.1 do repositório validou manifesto,
extração, hashes e modos. CRC e comparação dos bytes extraídos com as fontes
passaram. O requisito mínimo permanece bootstrap 1.0.0; esse bootstrap antigo
não foi executado novamente nesta rodada. A sintaxe dos arquivos Python do
runtime foi validada com a gramática do Python 3.10.

O patch incremental é comparado à entrega 0.3.87: só arquivos com bytes
alterados e novas adições entram no ZIP. As pastas originais são mantidas.
Nenhum runtime foi enviado ao telefone ou à VPS durante a validação local.

## Limites e validação no telefone

É preciso confirmar a 0.3.88 em execução, observar `archive_finished` com
`status='done'` e medir uma reprodução com `source_mode: archive`, incluindo
escuta e controles. As quatro reproduções medidas na 0.3.87 ainda eram online
e não comprovam a melhora do caminho arquivado.

O backoff das falhas anteriores, limitado a seis horas, é preservado. Esta
entrega não altera DNS, não varre posts concluídos que perderam manifesto e
não libera normalizações desconhecidas dos nomes legados. Transformações
legadas desconhecidas podem exigir novo anexo canônico no mesmo post; elas
não são aceitas por aproximação. Novos nomes não dependem da pontuação do
título.

O auxiliar de diagnóstico usado nas coletas anteriores reconstrói somente
nomes antigos baseados no título. Para analisar futuros uploads canônicos,
essa reconstrução precisa ser atualizada; ela não deve ser usada como prova
de correspondência dos novos nomes.

Em ambiente de desenvolvimento com as dependências instaladas:

```bash
python -m pytest -q --tb=short \
  cogs/musica/testes/runtime_telefone/test_archive_attachment_confirmation.py \
  cogs/musica/testes/runtime_telefone/test_arquivo_segmentado.py \
  cogs/musica/testes/runtime_telefone/test_archive_manifest_cache.py \
  cogs/musica/testes/runtime_telefone/test_archive_staging.py
```

A suíte completa de regressão permanece:

```bash
python -m pytest cogs/musica/testes tests -q --tb=short
```
