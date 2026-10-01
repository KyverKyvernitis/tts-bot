# Validação — Music Agent 0.3.84

Esta entrega restaura o perfil de voz no Termux, preserva as melhorias de
arquivamento e início de reprodução das versões anteriores e corrige caminhos
de falha transitória de transporte. A alternância automática VPS/Termux não
foi implementada.

## Resultado local

A suíte focada de executor, reconexão, proxy, lifecycle e auditoria passou:

```text
195 passed
```

A suíte completa foi executada após as correções:

```text
python -m pytest cogs/musica/testes tests -q --tb=short
2248 passed, 87 skipped, 5 failed, 108 subtests passed
```

As cinco falhas são as mesmas previamente reproduzidas no repositório original:
dois testes de APK com fake App sem `add_url_rule`, expectativa literal de
versão antiga do worker, arquivo legado TTS ausente e fixture AST sem
`atualizar_estado_controle_remoto`. Não houve nova falha nessa execução.
Os números da suíte focada têm sobreposição com a suíte completa.

Ambiente: Python 3.12 com as dependências do projeto em venv, FFmpeg e ffprobe. O aviso
de depreciação de `audioop` não ocasionou falha operacional nos testes.

## Comportamentos verificados

- Perfil Termux ignora flags antigas de API direta, descarta vínculo antigo
  com a VPS e envia play/status ao Phone Worker.
- O supervisor restaura modo `full` no telefone mesmo após configuração
  anterior em `archive`. A verificação executou o carregamento dos ambientes
  em diretório temporário, sem iniciar o bot.
- Resposta serializada `ok=false` com BrokenPipe recebe recuperação de
  transporte. A playlist recebeu dois erros seguidos e depois foi entregue,
  preservando `command_id` e o mesmo executor.
- Rejeições HTTP 400/401/403/422 contendo BrokenPipe, tanto em resposta JSON
  quanto em exceção, não são reenviadas automaticamente. Uma rejeição interna
  pode ocorrer após mutação parcial na fila.
- Socket keep-alive encerrado recebe conexão nova e o mesmo ID, sem auditoria
  pesada ou reinício da voz. Status também recebe essa tentativa limitada.
- Controles temporais, como pause, não entram na fila de entrega tardia.
- Stdout fechado não interrompe logs de playback nem o trace do primeiro
  pacote musical; fallback para stderr foi verificado.
- Os dois scripts de retorno passam em `bash -n`. A exigência de root e a
  parada exclusiva de `music-agent-voice.service` foram verificadas com
  `systemctl` simulado; nenhum serviço real foi parado nesta execução.

## Limites da conclusão

O print mostra BrokenPipe ao executar uma playlist, mas não identifica qual
processo ou conexão falhou. Não houve acesso ao traceback, à VPS/Termux de
produção ou à call. As correções cobrem falhas reproduzíveis no código; não
constituem medição da fluidez no ambiente do usuário.

As falhas existentes da base original estão documentadas na
[validação 0.3.82](VALIDACAO_MUSIC_AGENT_0_3_82.md). Os ganhos anteriores e suas
verificações constam na [validação 0.3.83](VALIDACAO_MUSIC_AGENT_0_3_83.md).

## Pacotes

O ZIP completo preserva `tts-bot-main/`. Há um delta cumulativo sobre o
repositório original e um incremental `desde-0.3.83` para quem aplicou a versão
anterior. Os ZIPs recebem verificação de CRC e comparação byte a byte; o
incremental é aplicado sobre o pacote 0.3.83 e comparado com o completo 0.3.84.
Nenhum arquivo original foi excluído e nenhum token ou áudio foi acrescentado.

Atualize controller e runtime do telefone juntos. Se o serviço opcional de voz
na VPS tiver sido ativado, pare-o antes de iniciar a voz no Termux, conforme
[instruções de retorno](MUSIC_AGENT_0_3_84.md). A configuração e o pacote foram
preparados localmente; esta execução não instalou alterações em produção.
