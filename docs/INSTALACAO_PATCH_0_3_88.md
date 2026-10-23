# Patch incremental 0.3.88 — nomes estáveis dos anexos

Este ZIP se aplica sobre a **0.3.87 instalada nesta conversa**. Contém somente
os arquivos alterados/adicionados, sob as pastas originais `tts-bot-main/`.

## Correção

Os novos diagnósticos confirmaram que o Discord também remove o apóstrofo
ASCII e o `&` de nomes antigos. A 0.3.87 tratava somente espaços convertidos
em `_`. Isso explica os dois arquivamentos ainda sem manifesto.

Novos anexos de áudio passam a usar nomes ASCII estáveis com ordem e SHA-256
completo, por exemplo `audio-000001-<hash>.ogg`. O título visível no post
continua igual, incluindo acentos e pontuação. Os formatos, bytes de áudio,
bitrate, buffers, efeitos e prioridades permanecem seguindo o pipeline atual.

Na retomada, o código procura também o nome antigo, permitindo somente as
transformações observadas: espaços ASCII para `_` e remoção de `'` e `&`.
Exige tamanho exato positivo, correspondência única e as verificações
existentes de autoria e CDN. O manifesto registra o nome confirmado. Os
anexos antigos reconhecidos são reutilizados no mesmo post; arquivos já
concluídos continuam com seus nomes atuais.

## 1. Instalação na VPS

Com a reprodução parada, aplique o ZIP pelo updater habitual e aguarde
concluir a publicação do runtime do telefone. O conteúdo de `tts-bot-main/`
deve ser mesclado na raiz instalada, normalmente `/home/ubuntu/bot`.
Conserve as configurações e o staging temporário de arquivamento.

Se instalar manualmente, faça backup dos arquivos substituídos antes de
mesclar e reinicie o controller:

```bash
sudo systemctl restart tts-bot.service
sudo systemctl is-active tts-bot.service
```

Somente se a automação do updater não tiver publicado o runtime, execute
na VPS usando o Python do serviço e a configuração padrão existente:

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

Preserve os caminhos personalizados se sua instalação usar outros locais.

## 2. Atualização no Termux

Após publicar na VPS:

```bash
python ~/.core-worker-runtime/current/phone_worker_bootstrap.py --check
python ~/testar-music-agent.py start
python ~/testar-music-agent.py status
```

O `--check` simples evita reaplicar uma release que o updater já ativou.
Use o auxiliar v2 já instalado. Confira `version: 0.3.88`,
`discord_ready: true`, `available: true`, `executor_mode: full` e
`voice_dependencies_ok: true`. Se estiver iniciando, aguarde alguns segundos
e consulte `status` novamente. A versão geral do Phone Worker é independente
da versão do Music Agent; ela pode continuar `1.11.42`.

O bootstrap informado na última coleta já está `succeeded` para a release
0.3.87. Se aparecer nova falha, guarde a saída de `--check`, consulte
`--status` e confira a versão efetivamente em execução antes de medir.

## 3. Confirmar o arquivamento dos dois casos

Mantenha o prewarm desligado. Capture os novos eventos no Termux:

```bash
mkdir -p ~/music-start-tests
tail -n 0 -F ~/phone-worker/music_agent.log \
  | grep --line-buffered -E 'archive_finished|archive_failed|archive_fallback_source|\[music-start\]' \
  | tee -a ~/music-start-tests/validacao-0.3.88.log
```

Procure `archive_finished` com `status='done'` para:

- `b6410f48db21fd1ebab3d3eb4f4fc211` — The Cure — Boys Don't Cry.
- `aeb8cedae8326bb90969f2397c9edccc` — yungatita — 7 Weeks & 3 Days.

O catálogo conserva a espera progressiva das falhas anteriores, limitada a
seis horas. A atualização não zera essa espera. A conta dona do bot pode
consultar o histórico no Discord, enviando cada comando separadamente:

```text
_musicarquivo auditoria b6410f48db21fd1ebab3d3eb4f4fc211
_musicarquivo auditoria aeb8cedae8326bb90969f2397c9edccc
_musicarquivo status
```

As contagens do catálogo podem incluir falhas anteriores e categorias
sobrepostas. Avalie os novos eventos e o estado de cada música. O comando
`reavaliar` não aceita entradas que já têm referência arquivada.

## 4. Medir a reprodução arquivada

Escolha uma faixa cujo arquivamento terminou e que já tenha o anexo
`archive-manifest.json` no post. Entre na call e envie separadamente:

```text
_stop
```

```text
_play LINK_DE_ORIGEM_DA_MUSICA_ARQUIVADA
```

Escute por cerca de vinte segundos, confira `_pause` e `_resume` e termine
com `_stop`. Verifique se o evento `music_first_packet_sent` indica
`source_mode: archive`. O tempo mede da chegada do pedido ao agente até o
primeiro pacote enviado ao Discord; a escuta verifica continuidade e qualidade.

Na aba de captura, pressione Ctrl+C e exporte:

```bash
python ~/testar-music-agent.py status
cp ~/music-start-tests/validacao-0.3.88.log \
   ~/storage/downloads/validacao-0.3.88.log
```

As quatro reproduções medidas na 0.3.87 ainda usaram fonte online. É preciso
obter uma reprodução arquivada antes de avaliar o ganho desse caminho. O
comparativo de prewarm permanece para uma etapa posterior.

## Rollback

Use o rollback do updater para restaurar os arquivos da 0.3.87 e publicar o
runtime anterior no telefone. Confira a versão pelo auxiliar e conserve os
dados e configurações. A 0.3.87 volta a ter a limitação de nomes documentada.
