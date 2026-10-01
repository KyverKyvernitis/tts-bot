# Validação — Music Agent 0.3.83

Base: repositório enviado em `repo-20260930-154545.zip`, com as mudanças da
0.3.82 preservadas. Os pacotes incluem código, testes, documentação e perfil
opcional para executar voz na VPS. O fórum atual continua sendo o acervo de
áudio definitivo e os diretórios temporários são mantidos.

## Resultado

```text
python -m pytest cogs/musica/testes tests -q --tb=short
2231 passed, 87 skipped, 5 failed, 108 subtests passed
```

Depois dessa execução foram acrescentados dois testes de integração: migração
de anexo via streaming HTTP e rota direta até o servidor HTTP autenticado do
agente. Suítes focadas finais: prioridade do arquivamento **8 passaram**;
executor direto + ferramenta de benchmark **20 passaram**. A suite de lifecycle
com as novas verificações de lease, buffer segmentado e pacote musical teve
**125 testes passando**. Esses números incluem sobreposição e não devem ser
somados ao resultado amplo.

As cinco falhas amplas são as mesmas reproduzidas na base original na primeira
etapa, conforme [validação 0.3.82](VALIDACAO_MUSIC_AGENT_0_3_82.md):

| Teste | Falha existente |
| --- | --- |
| `test_vps_rejects_old_apk_with_fake_version_and_preserves_release` | Fake App sem `add_url_rule` |
| `test_latest_endpoint_never_serves_declared_fake_version` | Fake App sem `add_url_rule` |
| `test_cleanup_release_versions_are_monotonic` | Expectativa literal de versão antiga |
| `test_legacy_history_module_contains_no_executable_code` | Arquivo legado TTS inexistente |
| `test_empty_remote_queue_allows_join_and_tts_even_with_monitor_alive` | Fixture AST sem `atualizar_estado_controle_remoto` |

Python 3.12, dependências do projeto em venv, FFmpeg/ffprobe disponíveis. Aviso
de depreciação de audioop é conhecido, sem falha operacional neste ambiente.

## Evidência das mudanças

- Preparação PCM de fontes segmentadas antes da conexão: só a parte atual
  aquece; a seguinte inicia após adoção; a cota é compartilhada e liberada.
- Cancelamento identifica lease própria; adoção durante handshake mantém a
  conexão; TTS e sessões existentes não são deslocados.
- Primeiro pacote: silêncio/TTS isolado, fonte trocada, geração antiga e
  `OSError` suprimido pelo discord.py não registram um falso início.
- Proxy usa o mesmo socket HTTP em comandos consecutivos, com teste TCP real;
  reparo mantém ID, não reinicia agente alcançável após resposta perdida.
- Rota VPS envia `/command` sem Phone Worker; teste HTTP real comprova Bearer,
  versão, prontidão, status, deduplicação e recusa de ações fora do modo.
- Arquivamento pausa grupo de processo próprio, retoma com timeout ativo
  correto, libera fatias justas e mata/recolhe ao cancelar. Upload em executor
  mantém heartbeat do loop, bytes íntegros e seek para retry.
- Migração de anexo antigo transmite blocos de até 64KiB ao staging e só
  renomeia o arquivo completo. Não usa buffering integral de `Attachment.save`.
- Manifests usam conexão HTTPS reaproveitada; renovação de assinatura não
  repete download de JSON, mas alteração de revisão invalida o cache.
- Journal de aprendizagem sobrevive a restart e falha de commit, preserva
  escolhas mais novas e mantém outbox durável. Consulta de catálogo em lote
  preserva duplicatas e isolamento de guild.
- Testes de release passam com bootstrap original 1.0.0: 63 arquivos mais
  manifesto, hash coincidente entre produtor e worker, imports fora do checkout.
- Unidade systemd foi validada por `systemd-analyze verify` substituindo apenas
  caminhos de checkout/Python pelos disponíveis no ambiente de validação. O
  serviço não foi iniciado nem instalado em máquina de produção.

## Entrega

ZIP completo e delta cumulativo preservam `tts-bot-main/`, comparados com a base
original. Há também delta incremental `desde-0.3.82`, comparado com o snapshot
da etapa anterior. Nenhum arquivo original foi excluído. ZIPs são verificados
por CRC e comparação byte a byte. SHA256SUMS acompanha os artefatos.

Os pacotes não acrescentam músicas, bancos, tokens, logs ou caches de execução.
Exemplos de ambiente precisam ser preenchidos; não substituem secrets
instalados. Atualize controller e runtime juntos para ativar o fluxo completo.

A suíte local valida comportamento e integridade. Não houve call real em
VPS/Termux do usuário, portanto não há porcentagem medida de melhoria até o som
ouvido. A ferramenta de traces calcula mediana/p95 com durações locais e evita
comparar timestamps de máquinas diferentes.
