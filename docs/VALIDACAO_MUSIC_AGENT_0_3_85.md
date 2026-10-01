# Validação — Music Agent 0.3.85

O usuário confirmou que voz na VPS nunca foi ativada. A análise e os testes
desta entrega se concentram no player do Termux e na ponte local do Phone
Worker. A 0.3.84 foi preservada como baseline antes das novas correções.

## Regressões reproduzidas

A cota global de decoders extras, introduzida na 0.3.83, podia impedir que uma
música arquivada carregasse sua próxima parte. Uma preparação opcional pronta
ou uma parte antecipada de outra call podia manter essa cota ocupada. O efeito
era silêncio até o timeout da transição.

Os logs da ponte de resolução também podiam lançar `BrokenPipeError` com stdout
fechado. Os testes verificam que o bloqueio de URL de metadata mantém sua
resposta normal e que uma busca sem resultados retorna `tracks=[]`, inclusive
quando stderr também está fechado. A URL é sanitizada e o bloqueio continua
acontecendo antes de importar yt-dlp.

## Resultado

Testes focados finais de arquivo segmentado, playback, qualidade e lifecycle:
**174 passaram**. Testes de transições e encerramento: **31 passaram**. Esses
números têm sobreposição com a suíte completa.

A verificação com FFmpeg real manteve a cota extra ocupada pela primeira call
e fez a segunda atravessar três partes. Todos os bytes PCM, a cauda final e o
EOF foram preservados, e a segunda call não consumiu uma vaga extra. As fontes
foram limpas ao terminar, sem cota residual.

Os seis novos testes de playback reproduzem a disputa com a próxima faixa e
com outra call, além de erro, timeout, cauda curta, cancelamento e cancelamentos
repetidos durante limpeza. As duas regressões centrais falharam antes da
correção. O teste de shutdown também cobre o helper ainda não iniciado, que
precisa executar sua limpeza antes de os registros serem descartados.

Suíte completa final:

```text
python -m pytest cogs/musica/testes tests -q --tb=short
2258 passed, 87 skipped, 5 failed, 108 subtests passed
```

As cinco falhas são as mesmas reproduzidas na base original: dois testes de
APK com fake App sem `add_url_rule`, expectativa literal de versão antiga do
worker, arquivo legado TTS ausente e fixture AST sem
`atualizar_estado_controle_remoto`. Não houve nova falha.

Ambiente: Python 3.12, dependências do projeto em venv e FFmpeg/ffprobe
disponíveis. O aviso de depreciação de `audioop` é preexistente.

## Implementação

- Preparação da próxima parte pode cancelar um decoder opcional de próxima
  faixa. A vaga permanece ocupada até o cleanup terminar.
- A parte obrigatória no EOF fecha o decoder encerrado antes de abrir seu
  substituto. É continuidade do playback, sem fonte extra.
- Erro e timeout da parte seguinte continuam propagados; não há silêncio
  infinito nem perda da cauda PCM.
- A limpeza opcional ocorre no executor, tolera cancelamentos repetidos e
  recolhe resultados de tarefas. Shutdown bloqueia novas cotas e aguarda os
  helpers, incluindo aqueles que ainda não começaram.
- O limite global de fontes extras, volume e efeitos continuam iguais.

## Limites

Não houve conexão com a call, a VPS ou o telefone do usuário. As regressões são
reproduções locais de defeitos do código; o traceback real ainda é necessário
para identificar a origem específica do print. O pacote não promete resolver
todo travamento de rede, decoder ou Android, nem atribui porcentagem medida de
melhoria.

As cinco falhas conhecidas da suíte original constam na
[validação 0.3.82](VALIDACAO_MUSIC_AGENT_0_3_82.md). As correções de entrega HTTP
da [0.3.84](VALIDACAO_MUSIC_AGENT_0_3_84.md) continuam aplicáveis à voz no Termux.

## Entrega

ZIP completo, delta cumulativo sobre o repositório enviado e incremental
`desde-0.3.84`, todos preservando `tts-bot-main/`. CRC, hashes e comparação byte
a byte verificam os pacotes. O incremental é comparado com o completo após
aplicação sobre 0.3.84. Nenhum arquivo original é excluído.

Atualize controller e runtime do Phone Worker juntos e preserve o ambiente
instalado. Os testes não instalaram mudanças em produção, e os pacotes não
acrescentam músicas nem tokens. Fórum, diretórios temporários e aprendizagem
durável permanecem conforme as entregas anteriores.
