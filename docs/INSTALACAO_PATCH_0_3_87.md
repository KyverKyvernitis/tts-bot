# Patch incremental 0.3.87 — confirmação dos anexos de música

Este patch se aplica sobre a **0.3.86 já instalada**. O ZIP contém somente os
arquivos alterados e adicionados, mantendo o prefixo `tts-bot-main/` e suas
pastas. Não contém configurações, credenciais ou a base completa.

## O que foi corrigido

O diagnóstico no telefone mostrou dois anexos cujos espaços no nome foram
convertidos pelo Discord em `_`, com o tamanho esperado. A comparação literal
rejeitava esses anexos e interrompia a publicação antes do manifesto. A música
então precisava ser resolvida pela fonte online.

A confirmação agora aceita o nome exato ou somente essa substituição de
espaços ASCII, exige tamanho positivo e igual e rejeita candidatos ambíguos.
As verificações existentes de autoria e URL CDN continuam ativas. O manifesto
registra o nome efetivamente confirmado pelo Discord. A retomada reconhece
esses nomes no post e nas partes já enviadas.

A telemetria também passa a indicar a fonte efetivamente resolvida; o
benchmark usa a fonte do primeiro pacote, preservando o estado inicial de voz
e do helper. A seleção de formatos, os bytes de áudio, codecs, bitrate,
efeitos, buffers e prioridades não foram alterados.

## 1. Aplicar na VPS

Com a reprodução parada, aplique o ZIP pelo mesmo updater usado para instalar
a 0.3.86. O updater deve mesclar o conteúdo de `tts-bot-main/` com a raiz
instalada, preparar o rollback e publicar o runtime do telefone. Aguarde a
conclusão da atualização.

Se optar pela instalação manual, faça backup dos arquivos de destino antes de
mesclar. A raiz instalada é `/home/ubuntu/bot`, onde está o `bot.py`; evite
criar outra pasta `tts-bot-main` dentro dela. Reinicie o controller:

```bash
sudo systemctl restart tts-bot.service
sudo systemctl is-active tts-bot.service
```

Na instalação manual, publique também o runtime. O comando abaixo usa o
Python observado no serviço e carrega a configuração padrão da VPS sem
imprimi-la. Se sua configuração estiver em outro arquivo, ajuste esse caminho.
Essa etapa é dispensável quando o updater já publicou a release.

```bash
cd /home/ubuntu/bot
/home/ubuntu/bot-update-staging/candidates/python-runtimes/current/venv/bin/python - <<'PY'
from dotenv import load_dotenv
import runpy
import sys

load_dotenv('/home/ubuntu/bot/.env', override=False)
sys.argv = ['scripts/core-worker-automation.py', 'after-update', '--force-agent']
runpy.run_path('scripts/core-worker-automation.py', run_name='__main__')
PY
```

## 2. Receber a release no Termux

Depois da publicação na VPS, execute no telefone:

```bash
python ~/.core-worker-runtime/current/phone_worker_bootstrap.py --check --force
python ~/testar-music-agent.py start
python ~/testar-music-agent.py status
```

Use o auxiliar **v2** que já foi instalado durante o diagnóstico. O `start`
usa o contexto da release ativa. Aguarde alguns segundos se o login Discord
ainda estiver em andamento e consulte `status` novamente.

O resultado deve mostrar:

```json
{
  "version": "0.3.87",
  "discord_ready": true,
  "available": true,
  "executor_mode": "full",
  "voice_dependencies_ok": true,
  "missing_dependencies": []
}
```

Se continuar na 0.3.86, a atualização ainda não chegou ao agente. Confira a
saída do bootstrap e de `start` antes de medir. Preserve o prewarm desativado
nesta rodada e conserve os arquivos temporários de arquivamento: eles
permitem retomar o mesmo post e os bytes já preparados.

## 3. Confirmar a conclusão do arquivamento

O catálogo mantém a espera progressiva das falhas anteriores, limitada a
**seis horas**. A atualização não zera essa espera. As músicas novas e as
migrações incompletas continuam sendo tentadas em segundo plano conforme a
fila. Não é necessário limpar dados para que o novo código seja usado.

No Termux, capture somente os eventos posteriores ao início desta observação:

```bash
mkdir -p ~/music-start-tests
tail -n 0 -F ~/phone-worker/music_agent.log \
  | grep --line-buffered -E 'archive_finished|archive_failed|archive_fallback_source|\[music-start\]' \
  | tee -a ~/music-start-tests/validacao-0.3.87.log
```

Mantenha essa aba aberta. Procure `archive_finished` com `status='done'` e a
chave da música que pretende testar. Exemplos das chaves diagnosticadas:

- `587f92dbe272f5451a61ace70b4593f9` — Heaven Pierce Her — In the Presence of a King.
- `9b6017ae7b2d5deea7021d472400525b` — Kate Bush — Army Dreamers.

No Discord, a conta dona do bot pode consultar:

```text
_musicarquivo status
_musicarquivo auditoria 587f92dbe272f5451a61ace70b4593f9
```

O comando `reavaliar` existente só aceita músicas ainda sem referência
arquivada; ele não é uma ferramenta geral de reparo dos posts existentes.
Entradas já concluídas como v8 que perderam um manifesto depois da publicação
exigem diagnóstico separado; este patch não faz uma varredura desses posts.
Os erros de DNS também continuam exigindo observação própria.

## 4. Testar a primeira música

Após confirmar `done`, entre na call e envie separadamente:

```text
_stop
```

```text
_play LINK_DE_ORIGEM_DA_MUSICA_ARQUIVADA
```

Use o link correspondente à faixa cujo arquivamento acabou de ser confirmado.
Escute por cerca de vinte segundos, confira `_pause` e `_resume`, depois
envie `_stop`. O novo `music_first_packet_sent` deve indicar
`source_mode: archive`. Esse resultado mede o tempo dentro do agente até o
envio do primeiro pacote; a escuta no cliente verifica cortes e qualidade.

Encerre somente a captura com Ctrl+C e exporte o registro:

```bash
python ~/testar-music-agent.py status
cp ~/music-start-tests/validacao-0.3.87.log \
   ~/storage/downloads/validacao-0.3.87.log
```

Envie esse arquivo para avaliar o novo tempo e qualquer falha. As duas
amostras anteriores tinham contextos diferentes e não comprovam uma
porcentagem de ganho. O comparativo de prewarm continua para uma etapa
posterior, com cenários equivalentes.

## Retorno à 0.3.86

Use o rollback do updater para restaurar os arquivos anteriores e publicar o
runtime anterior no telefone. Confirme a versão em execução pelo auxiliar.
Conserve as configurações e os dados do arquivo. A 0.3.86 volta a ter o
problema de confirmação de nomes identificado neste diagnóstico.
