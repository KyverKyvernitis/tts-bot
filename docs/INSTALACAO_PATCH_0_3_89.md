# Music Agent 0.3.89 — saída de áudio

Patch incremental para a base desta conversa: Music Agent **0.3.88** e
Phone Worker **1.11.43**. O ZIP contém somente arquivos alterados ou novos,
mantendo as pastas originais dentro de `tts-bot-main/`.

## O que foi implementado

- Correção do bitrate que podia permanecer no limite antigo do canal: se a
  reprodução começasse em 64 kbps e o Discord aumentasse o canal depois, o
  encoder podia continuar em 64. Agora o evento do canal sincroniza o encoder
  durante a reprodução, na própria thread de áudio, sem reiniciar a faixa.
- Revalidação do perfil e do canal após a preparação do PCM. Isso cobre
  mudanças enquanto FFmpeg ou a rede ainda estão preparando o primeiro áudio.
- Perfis de saída por servidor, com troca local durante a reprodução:

| Perfil | Bitrate solicitado antes dos limites |
| --- | --- |
| `balanced` | Regra adaptativa existente: bitrate da fonte + margem configurada; 160 kbps quando desconhecido |
| `quality_192` | 192 kbps |
| `quality_256` | 256 kbps |

Todos respeitam o limite do canal e o máximo já configurado no agente. No
perfil padrão, os valores existentes continuam: piso de 96 kbps, margem de
32 kbps e máximo de 256 kbps; um canal com capacidade menor continua sendo
respeitado. Fontes de baixa qualidade não recuperam detalhes perdidos por
receber um bitrate de saída maior.

- FEC e expectativa de perda ficaram explícitos e consultáveis. Os padrões
  continuam **FEC ligado e perda esperada de 15%**, como na configuração
  anterior do encoder. Essa perda é uma hipótese de configuração, não uma
  medição da conexão.
- Diagnóstico distingue perfil solicitado, bitrate limitado pelo canal e
  parâmetros efetivamente aplicados; informa versões de discord.py/libopus
  quando disponíveis, controles suportados e métricas do mixer.
- Auxiliar local `scripts/testar-saida-musica.py`, usando somente Python
  padrão, sem importar o worker nem gerar tokens.

O caminho padrão permanece PCM estéreo de 48 kHz, frames de 20 ms e encoder
Opus para música. Volume, efeitos, TTS simultâneo, ducking e proteção existente
contra clipping permanecem. A proteção do mixer já existia; este patch não
adiciona um segundo limitador ou normalização de volume.

O modo de cópia direta de Opus ficou somente como diagnóstico de
elegibilidade. Metadados de codec/rate/canais não comprovam frames de 20 ms;
ativar a cópia sem essa validação poderia quebrar reprodução, volume e TTS.
O patch não ativa esse caminho. Complexity e VBR aparecem como desconhecidos
quando a API pública não os informa; não há promessa de configuração oculta.

## 1. Instalar na VPS

Pare a música no Discord e aplique o ZIP pelo updater habitual. Mescle o
conteúdo de `tts-bot-main/` em `/home/ubuntu/bot`.

Para instalação manual, coloque o ZIP em
`~/patch-musica-0.3.89-incremental.zip` e execute na **VPS**:

```bash
unzip -l ~/patch-musica-0.3.89-incremental.zip

audio_backup="$HOME/backup-musica-antes-0.3.89-$(date +%Y%m%dT%H%M%S).tar.gz"
tar -czf "$audio_backup" -C /home/ubuntu/bot \
  cogs/musica/runtime_telefone/agente/configuracao.py \
  cogs/musica/runtime_telefone/agente/estado.py \
  cogs/musica/runtime_telefone/agente/reproducao.py \
  cogs/musica/runtime_telefone/agente/servidor.py

audio_patch_dir=$(mktemp -d /tmp/patch-musica-0.3.89.XXXXXX)
unzip -q ~/patch-musica-0.3.89-incremental.zip -d "$audio_patch_dir"
cp -a "$audio_patch_dir/tts-bot-main/." /home/ubuntu/bot/
```

Guarde o caminho de `audio_backup` para eventual reversão. O patch não inclui
`.env`, tokens, chaves SSH, dados do arquivo ou configurações do telefone.
O Phone Worker continua **1.11.43**. Não é necessário repetir `ssh refresh`
nem mudar o firewall para instalar este patch de áudio.

Se o updater não publicou o runtime, ou se instalou manualmente, publique
usando o Python do serviço, que já tem as dependências instaladas:

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

sudo systemctl restart tts-bot.service
sudo systemctl is-active tts-bot.service
```

Esperado: publicação concluída sem erro e serviço `active`. O hash do runtime
muda mesmo com a versão geral do worker permanecendo 1.11.43.
Para este pacote, o `source_hash` esperado é
`d36f14ac4708c8bd808d2becd875a15c16000cd8fb6b010bae1c26214ca44d8b`.

## 2. Atualizar e conferir no Termux

Com o transporte conectado e a release publicada:

```bash
python ~/phone-worker/phone_worker_runtime/ssh_transport.py status
python ~/.core-worker-runtime/current/phone_worker_bootstrap.py --check
python ~/testar-music-agent.py start
python ~/testar-music-agent.py status
```

Esperado: transporte conectado, Music Agent **0.3.89**, `available: true`,
`discord_ready: true` e dependências de voz prontas. Use o auxiliar v2 já
instalado nesta conversa. Se o arquivo novo foi instalado, mas o processo
ainda anunciar 0.3.88, com a música parada execute uma única vez:

```bash
python ~/testar-music-agent.py restart
python ~/testar-music-agent.py status
```

Se o bootstrap indicar rollback ou o agente ficar indisponível, guarde a
saída e o log; conclua a atualização antes da comparação de áudio. Não use
`--force` repetidamente para contornar uma falha de verificação.

Baixe também o novo `testar-saida-musica.py` em Downloads e copie no **Termux**:

```bash
cp "$HOME/storage/downloads/testar-saida-musica.py" "$HOME/testar-saida-musica.py"
python ~/testar-saida-musica.py status --guild 927002914449424404
```

Também é possível obter o mesmo arquivo em `scripts/testar-saida-musica.py`
do patch. O auxiliar lê os arquivos de ambiente já instalados, usa o token
existente e aceita somente o endereço local do Music Agent.

## 3. Comparar a saída sem trocar a fonte

Mantenha a internet ligada e o worker online durante os testes de qualidade.
Use a mesma música, preferencialmente já arquivada, os mesmos fones e o mesmo
volume. Evite mudar os efeitos durante a primeira comparação. Comece a tocar
a música pelo comando habitual do Discord.

Abra outra sessão do **Termux** para capturar somente linhas novas:

```bash
mkdir -p ~/music-start-tests
tail -n 0 -F ~/phone-worker/music_agent.log \
  | grep --line-buffered -E '\[music-start\]|audio_source_selected|opus_encoder_updated|audio_output_|opus_encoder_kwargs_unsupported|audio_quality' \
  | tee -a ~/music-start-tests/saida-0.3.89-agent.log
```

Na sessão principal do Termux, primeiro registre o padrão:

```bash
python ~/testar-saida-musica.py reset --guild 927002914449424404
python ~/testar-saida-musica.py status --guild 927002914449424404 \
  | tee ~/music-start-tests/saida-balanced.json
```

Escute um trecho com pratos de bateria, vozes e instrumentos simultâneos.
Troque para 192 kbps durante a mesma reprodução:

```bash
python ~/testar-saida-musica.py profile --guild 927002914449424404 --profile quality_192
```

Aguarde cerca de dois segundos e consulte novamente:

```bash
python ~/testar-saida-musica.py status --guild 927002914449424404 \
  | tee ~/music-start-tests/saida-192.json
```

Repita com 256 kbps e registre depois de alguns segundos:

```bash
python ~/testar-saida-musica.py profile --guild 927002914449424404 --profile quality_256
```

```bash
python ~/testar-saida-musica.py status --guild 927002914449424404 \
  | tee ~/music-start-tests/saida-256.json
```

Os ajustes valem **somente para esse servidor e até o Music Agent reiniciar**.
A troca não reinicia a faixa. O retorno imediato pode indicar `queued`:
o controle só é aplicado antes da próxima leitura de áudio. Se o player
estiver inteiramente pausado, confira após retomar. Em um caminho já
codificado, o ajuste fica adiado para a próxima faixa.

## 4. Interpretar o status

Dentro de `audio_output`, confira:

| Campo | Significado |
| --- | --- |
| `active` | Há música e uma sessão de voz conectada |
| `requested.profile` | Perfil escolhido |
| `requested.target_bitrate_kbps` | Alvo antes dos limites |
| `requested.channel_bitrate_kbps` | Capacidade atual do canal |
| `requested.bitrate_kbps` | Alvo depois dos limites do canal e da configuração |
| `applied.state` | `applied` confirma aplicação; `partial` ou `unsupported` pedem inspeção |
| `applied.bitrate_kbps` | Bitrate aplicado ao encoder; vazio/desconhecido não confirma aplicação |
| `applied.fec`, `applied.expected_packet_loss` | Proteção aplicada |
| `runtime` | Versões disponíveis e controles públicos suportados |
| `metrics` | Contadores de frames, atrasos e atuação da proteção do mixer |

Exemplo: no perfil 256, canal de 96 kbps implica alvo de 256 e aplicação de
96 kbps. Isso é esperado. Para comparar 192 com 256, o canal precisa permitir
esses valores. Aumentar o perfil não ultrapassa as limitações do Discord.

Se o canal mudar de 64 para 96/192/256 durante a faixa, procure
`opus_encoder_updated` com o limite novo e confira `applied` novamente.
Um contador `mix_limited_frames` maior que zero indica atuação da proteção
existente; sozinho não comprova distorção. Os contadores não medem perda de
pacotes recebidos por cada ouvinte nem demonstram qualidade perceptiva.

## 5. Verificar controles, TTS e primeiro áudio

Em cada perfil, faça pausa/retomada, skip e uma fala TTS sobre a música.
Confira ausência de cortes, distorção e alterações inesperadas no volume.
Depois pare a música/desconecte pelo comando habitual e toque novamente a
mesma faixa. Repita algumas vezes no mesmo cenário de cache/conexão.

No log, compare `music_first_packet_sent` e `music_start_outcome`. O resultado
esperado é `success`. Compare `agent_to_first_packet_ms` em condições iguais:
arquivo confirmado com arquivo confirmado; fonte online com fonte online;
conexão fria com conexão fria. Os spans de conexão e resolução podem ocorrer
em paralelo e não devem ser somados como se fossem etapas sequenciais.

Este patch não acrescenta consultas de rede ao caminho normal do início.
Os testes locais verificam as corridas de preparação e preservação do PCM;
o tempo e a estabilidade no seu telefone precisam ser confirmados aqui.

Opcionalmente, em uma rede comprovadamente estável, compare uma hipótese de
perda de 5%, mantendo FEC ligado:

```bash
python ~/testar-saida-musica.py protection --guild 927002914449424404 --fec on --loss 0.05
```

Esse teste não mede a perda real. Não é necessário para ativar os perfis.
Para restaurar a proteção anterior sem trocar o perfil:

```bash
python ~/testar-saida-musica.py protection --guild 927002914449424404 --fec on --loss 0.15
```

Para remover todos os ajustes de teste desse servidor e voltar aos defaults
instalados:

```bash
python ~/testar-saida-musica.py reset --guild 927002914449424404
```

Defaults opcionais para uma instalação futura são
`MUSIC_AGENT_AUDIO_OUTPUT_PROFILE`, `MUSIC_AGENT_DISCORD_OPUS_FEC` e
`MUSIC_AGENT_DISCORD_OPUS_EXPECTED_PACKET_LOSS`. Eles afetam todos os
servidores sem ajuste local e exigem reinício. Não é preciso editar o `.env`
para fazer os testes acima.

Encerre a captura com Ctrl+C e copie os resultados para Downloads:

```bash
cp ~/music-start-tests/saida-0.3.89-agent.log ~/storage/downloads/
cp ~/music-start-tests/saida-balanced.json ~/storage/downloads/
cp ~/music-start-tests/saida-192.json ~/storage/downloads/
cp ~/music-start-tests/saida-256.json ~/storage/downloads/
```

## Reversão

Para desfazer apenas os ajustes, use `reset`; não é necessário reinstalar.
Para reverter o código, pare a música, restaure os quatro arquivos do backup
na VPS, publique novamente com o comando da seção 1 e execute o bootstrap
no telefone. Reinicie o Music Agent parado se ele ainda anunciar 0.3.89.
Isso restaura o Music Agent anterior sem desfazer o patch SSH do worker
1.11.43. Após a reversão, o endpoint novo deixa de existir.

## Limites da validação local

Os testes novos executam as funções de produção de política, sincronização,
mixer e API, com fronteiras Discord simuladas. Há também integração com
libopus nativa: controles de bitrate/FEC/perda e codificação/decodificação
de frames de 20 ms, sem substituir o encoder durante a troca de perfil.
Isso valida compatibilidade, não ganho audível. O ambiente local não tem
discord.py e pytest instalados; não foi executada uma sessão Discord real
nem a suíte completa do bot. O pacote inclui os testes para execução no
ambiente do projeto.

Resultado local desta entrega: **80 testes novos passaram**, incluindo dois
com libopus 1.5.2, e **75 testes existentes do transporte/política SSH
passaram**. Outros **64 checks existentes de qualidade, primeiro áudio,
telemetria e DSP passaram por adapters stdlib**; dois casos com executor de
threads não foram validados porque bloquearam nesta sandbox. Esses adapters
não equivalem à execução pelo pytest.

Para repetir os testes novos na raiz do projeto, sem pytest:

```bash
python -m unittest \
  cogs.musica.testes.runtime_telefone.test_output_profiles \
  cogs.musica.testes.runtime_telefone.test_output_bitrate_sync \
  tests.test_music_output_api \
  tests.test_music_output_helper \
  tests.test_music_output_native_opus -v
```

Os testes nativos são pulados se libopus não estiver disponível. A publicação
real do runtime foi verificada em diretório temporário: 63 arquivos, 64
membros no ZIP, hashes/CRC/permissões e extração aceitos pelos bootstraps
1.0.0 e 1.0.1. A sintaxe dos arquivos Python alterados e do runtime foi
validada para Python 3.10.
