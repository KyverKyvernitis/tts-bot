# Validação das melhorias de música — Music Agent 0.3.82

As mudanças foram aplicadas ao repositório enviado em `repo-20260930-154545.zip`.
A atualização mantém o acervo de áudio somente no fórum Discord configurado e
usa diretórios temporários retomáveis para download, preparação e upload.

## Resultado dos testes

Execução ampla:

```text
python -m pytest -q cogs/musica/testes tests
2154 passed, 87 skipped, 5 failed, 108 subtests passed
```

As cinco falhas também foram reproduzidas no repositório original, intacto:

| Teste | Erro já existente |
| --- | --- |
| `test_vps_rejects_old_apk_with_fake_version_and_preserves_release` | Fake App sem `add_url_rule` |
| `test_latest_endpoint_never_serves_declared_fake_version` | Fake App sem `add_url_rule` |
| `test_cleanup_release_versions_are_monotonic` | Expectativa literal de versão antiga do worker |
| `test_legacy_history_module_contains_no_executable_code` | Arquivo legado de TTS inexistente |
| `test_empty_remote_queue_allows_join_and_tts_even_with_monitor_alive` | Fixture AST sem `atualizar_estado_controle_remoto` |

O escopo de música e os testes de distribuição passaram. A validação inclui:

- Link arquivado entra na fila antes de consultar o provedor, com proteção de guild e identidade.
- Escolhas aprendidas sobrevivem à saída do cache RAM e a reinícios.
- Migração dos bancos, filas FIFO, transações e consumo durável do outbox.
- Retomada dos downloads e segmentos, limpeza após confirmação e expiração sem novos uploads.
- Upload dividido respeitando o limite real do servidor, com validação de manifesto e autoria.
- FFmpeg real: pacotes Opus preservados, preroll de decoder e contagem PCM sem perda ou duplicação.
- FFmpeg real: fronteiras PCM byte a byte e uma única cauda de reverb na faixa lógica.
- FFmpeg real: retomada de Opus/Vorbis preserva os hashes sem nova preparação.
- Pacote completo extraído e validado pelo bootstrap original 1.0.0, incluindo import dos novos módulos.
- Produtor VPS e worker calculam o mesmo hash da release, com 63 arquivos e um manifesto.

Ambiente: Python 3.12, dependências do projeto instaladas em ambiente virtual,
FFmpeg e ffprobe disponíveis. Houve um aviso de depreciação de audioop, já usado
pelo projeto; não impede os testes em Python 3.12.

## Aplicação

O ZIP completo preserva a estrutura `tts-bot-main/` do arquivo enviado. O ZIP
com somente alterações usa a mesma estrutura e deve ser aplicado sobre essa
base. Não houve exclusão de arquivos existentes. Os pacotes não contêm tokens,
bancos, logs de execução, caches Python nem áudio de testes.

Atualize o bot na VPS e o Music Agent no Termux pelo fluxo já existente; o
agente deve anunciar 0.3.82. Os novos uploads aguardam essa versão. As instruções
e configurações estão em `docs/MUSIC_AGENT_0_3_82.md` dentro do repositório.

Nenhuma atualização foi enviada a um servidor ou aparelho de produção. Os
resultados não medem latência nem qualidade percebida numa call real; isso
precisa ser verificado no aparelho e na rede em uso. A capacidade total depende
do armazenamento de metadados, do disco temporário e dos limites do Discord.
