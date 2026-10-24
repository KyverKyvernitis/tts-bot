# Music Agent 0.3.90 — Opus direto validado

Patch incremental sobre o Music Agent **0.3.89** e o Phone Worker **1.11.43**
entregues nesta conversa. O ZIP contém somente arquivos alterados ou novos,
mantendo as pastas originais em `tts-bot-main/`. O worker continua 1.11.43.

## Funcionamento e limites

O modo novo pode enviar os **pacotes Opus originais**, sem recodificação de
saída. Antes disso, valida o contêiner Ogg, CRC, sequência, cabeçalhos,
estéreo, ganho, duração dos pacotes e decodificação nativa. A validação
continua ao longo da faixa; o primeiro pacote não comprova o resto do fluxo.

O mixer continua disponível. Um decoder acompanha os pacotes para manter o
PCM pronto. Se volume, efeitos, pausa, TTS ou o limite de bitrate exigirem
processamento, o mesmo player passa a enviar PCM codificado em Opus. Não é
preciso reconectar ao canal ou reiniciar a faixa para esses controles.
Depois desse fallback, a faixa permanece em PCM até terminar, evitando
alternar repetidamente entre streams de encoders independentes.

**O modo é opcional e vem desligado.** O volume padrão continua 55%; o
patch não o aumenta. Para copiar pacotes, a música precisa estar em **100%**,
sem efeitos ou TTS ativo. Fontes não compatíveis continuam no caminho PCM.

Condições principais para cópia:

- Fonte Opus estéreo, com áudio a 48 kHz, sem ganho no cabeçalho e com mapping 0.
- Pacotes válidos de exatamente 20 ms, dentro do limite de tamanho e do
  orçamento do bitrate solicitado/canal. Há um limite conservador por pacote,
  então um pico VBR pode provocar fallback mesmo com bitrate médio menor.
- Sem seek, transmissão ao vivo ou arquivo de várias partes. Um arquivo de
  uma parte pode ser usado. O contêiner não pode conter streams encadeados.
- Volume 100%, sem efeitos, sem filtro FFmpeg personalizado e sem TTS pendente.

Pacotes válidos de 10/40 ms são decodificados e reformatados para PCM de
20 ms, sem cópia. Falhas de validação inicial usam o decoder PCM normal.
Corrupção encontrada depois do início não é encaminhada ao Discord e segue
a recuperação de stream existente; esse caso pode interromper a reprodução.

O bitrate e o FEC de um pacote copiado pertencem à fonte original. Alterar
os perfis 192/256 ou a proteção não altera esses pacotes: os ajustes controlam
o encoder de fallback e os limites da cópia. **FEC da fonte fica desconhecido**.
Para rede instável, o PCM padrão mantém a proteção configurada do encoder.

Opus cru não transporta as instruções de trim do contêiner. Aceitamos pre-skip
limitado a 960 amostras (o comum é 312, cerca de 6,5 ms) e padding final de
até um pacote. Os metadados expõem esses valores; não há promessa de trim
sample-exact ou reprodução gapless nesta entrega. Uma mudança de encoder
pode produzir um pequeno transiente no decoder do ouvinte. A regra de
fallback por faixa evita repetição, mas a audição real ainda é necessária.

## 1. Instalação na VPS

Pare a música no Discord. Aplique o ZIP pelo updater habitual, mesclando o
conteúdo de `tts-bot-main/` em `/home/ubuntu/bot`, e publique o runtime.
Não substitua a base inteira. Este patch não inclui `.env`, tokens, chaves
ou dados dos arquivamentos e não exige alterações de SSH/firewall.

Para instalação manual, coloque o ZIP em
`~/patch-musica-0.3.90-incremental.zip` e execute na **VPS**:

```bash
unzip -l ~/patch-musica-0.3.90-incremental.zip

opus_backup="$HOME/backup-musica-antes-0.3.90-$(date +%Y%m%dT%H%M%S).tar.gz"
tar -czf "$opus_backup" -C /home/ubuntu/bot \
  cogs/musica/runtime_telefone/agente/buffer_pcm.py \
  cogs/musica/runtime_telefone/agente/configuracao.py \
  cogs/musica/runtime_telefone/agente/estado.py \
  cogs/musica/runtime_telefone/agente/mixer_pcm.py \
  cogs/musica/runtime_telefone/agente/preparacao_audio.py \
  cogs/musica/runtime_telefone/agente/reproducao.py \
  cogs/musica/runtime_telefone/agente/servidor.py

opus_patch_dir=$(mktemp -d /tmp/patch-musica-0.3.90.XXXXXX)
unzip -q ~/patch-musica-0.3.90-incremental.zip -d "$opus_patch_dir"
cp -a "$opus_patch_dir/tts-bot-main/." /home/ubuntu/bot/
```

Guarde o caminho do backup. Se o updater não publicou o runtime, ou se fez
a instalação manual, publique usando o Python já instalado do serviço:

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

Esperado: publicação sem erro e serviço `active`. O hash do runtime muda;
a versão geral do worker continua **1.11.43**.

## 2. Atualizar o Termux e o auxiliar

Com a conexão online e a release publicada:

```bash
python ~/phone-worker/phone_worker_runtime/ssh_transport.py status
python ~/.core-worker-runtime/current/phone_worker_bootstrap.py --check
python ~/testar-music-agent.py start
python ~/testar-music-agent.py status
```

Confirme Music Agent **0.3.90**, `available: true`, `discord_ready: true` e
dependências de voz prontas. Se o arquivo foi atualizado, mas o processo
continua antigo, com a música parada reinicie uma vez:

```bash
python ~/testar-music-agent.py restart
python ~/testar-music-agent.py status
```

Se houver rollback, registre a saída antes dos testes. Não repita `--force`
para contornar a verificação. O auxiliar `testar-music-agent.py` é o v2 já
instalado nesta conversa.

Baixe o auxiliar **`testar-saida-musica-v2.py`** em Downloads e copie:

```bash
cp "$HOME/storage/downloads/testar-saida-musica-v2.py" "$HOME/testar-saida-musica.py"
python ~/testar-saida-musica.py status --guild 927002914449424404
```

O mesmo arquivo está em `scripts/testar-saida-musica.py` no ZIP. Ele lê o
token existente e faz apenas chamadas locais autenticadas, sem escrever `.env`.

## 3. Registrar uma referência em PCM

Mantenha a internet estável durante esta comparação. Use os mesmos fones,
volume, faixa e canal. Preferencialmente use uma música já arquivada em uma
única parte. Não compare uma fonte online diferente com um arquivo diferente.

Em outra sessão do **Termux**, capture somente linhas novas:

```bash
mkdir -p ~/music-start-tests
tail -n 0 -F ~/phone-worker/music_agent.log \
  | grep --line-buffered -E '\[music-start\]|audio_source_selected|opus_encoder_updated|opus_direct_|audio_quality|audio_output_' \
  | tee -a ~/music-start-tests/opus-0.3.90-agent.log
```

Na sessão principal, mantenha a cópia desligada e selecione um perfil de
comparação. Escolher 256 não ultrapassa o limite do canal:

```bash
python ~/testar-saida-musica.py direct --guild 927002914449424404 --mode off
python ~/testar-saida-musica.py profile --guild 927002914449424404 --profile quality_256
```

No **Discord**, anote o volume atual. Para a comparação em ganho unitário,
use `_volume 100`, desative os efeitos que estavam ligados e toque a faixa
pelo comando habitual. Isso aumenta o volume do bot em relação ao padrão de
55%; ajuste o volume de escuta do seu dispositivo conforme necessário.

Durante a reprodução, registre no Termux:

```bash
python ~/testar-saida-musica.py status --guild 927002914449424404 \
  | tee ~/music-start-tests/opus-referencia-pcm.json
```

## 4. Ativar Opus direto para o próximo início

Pare a música pelo comando habitual do Discord. Ative o modo no Termux:

```bash
python ~/testar-saida-musica.py direct --guild 927002914449424404 --mode auto
```

Toque novamente a mesma faixa desde o início, com volume 100 e efeitos
desligados. O modo novo não substitui à força uma faixa PCM já tocando:
nesse caso aparece `next_track_required`; inicie a faixa novamente.

Depois de alguns segundos:

```bash
python ~/testar-saida-musica.py status --guild 927002914449424404 \
  | tee ~/music-start-tests/opus-direto.json
```

Os ajustes são por servidor e duram até o Music Agent reiniciar. O default
global opcional `MUSIC_AGENT_OPUS_DIRECT=auto` existe, mas não é necessário
editar o ambiente para este teste. O default instalado é `off`.

## 5. Interpretar o resultado

Dentro de `audio_output`, confira:

| Campo | Resultado esperado durante cópia |
| --- | --- |
| `requested.opus_direct` | `auto` |
| `passthrough.copy_active` | `true` |
| `passthrough.state` | `copy` |
| `passthrough.copied_packets` | Aumenta durante a música |
| `passthrough.encoded_packets` | Zero antes de qualquer fallback |
| `passthrough.source_bitrate_kbps` | Média observada dos pacotes copiados |
| `passthrough.source` | Cabeçalho, pre-skip, padding e metadados validados |
| `applied.output_mode` | `opus_direct` |
| `applied.state` | `copied` |

Durante a cópia, `applied.bitrate_kbps`, `applied.fec` e
`applied.expected_packet_loss` ficam desconhecidos. Isso evita confundir a
fonte original com a configuração do encoder de fallback, disponível em
`passthrough.fallback_encoder`.

Se `copy_active` estiver falso, consulte `reason` e `reasons`. Volume menor
que 100, efeitos, TTS, seek, fonte incompatível ou vários segmentos pedem
PCM. `packet_bitrate_limit` significa que um pacote ultrapassou o orçamento
conservador do perfil/canal. Não aumentamos o canal nem reduzimos a fonte
automaticamente para forçar a cópia. Em um canal de 96 kbps, uma fonte Opus
de 128 kbps pode continuar em PCM mesmo no perfil 256.

O primeiro início após ativar pode descartar um PCM antecipado e abrir a
leitura Opus. Esse cenário pode ter trabalho extra de preparação; compare
os tempos em condições iguais e não conclua ganho de velocidade apenas
pela ausência de encode de saída.

## 6. Testar volume, TTS, pausa e efeitos

Com `copy_active: true`, use no Discord `_volume 90` ou envie uma fala TTS
sobre a música. Consulte novamente no Termux:

```bash
python ~/testar-saida-musica.py status --guild 927002914449424404 \
  | tee ~/music-start-tests/opus-fallback.json
```

Esperado: música continua, `copy_active: false`, `state: pcm_fallback`,
`fallback_latched: true` e `encoded_packets` aumentando. Voltar a 100% ou
terminar o TTS não reativa a cópia no meio dessa faixa. Uma próxima faixa
compatível pode voltar ao modo direto.

Repita com pausa/retomada e bassboost. Nightcore/reverb usam a troca PCM já
existente. Confira volume correto, TTS audível, ducking/restauração, skip e
ausência de estalos perceptíveis. Um stream tecnicamente válido não comprova
ausência de transiente audível ao trocar de encoder.

Compare também `agent_to_first_packet_ms` e `music_start_outcome` no log,
separando conexão fria/quente, cache e arquivo confirmado/fonte online.
Os spans paralelos de conexão e resolução não devem ser somados.

Para desligar a cópia imediatamente e restaurar o perfil adaptativo:

```bash
python ~/testar-saida-musica.py direct --guild 927002914449424404 --mode off
python ~/testar-saida-musica.py reset --guild 927002914449424404
```

Restaure também o volume anterior no Discord (`_volume 55` se era o padrão).
O reset de qualidade não altera volume ou efeitos. Para restaurar o código,
pare a música, restaure os sete arquivos do backup na VPS, publique o runtime
e execute o bootstrap no telefone. Isso preserva o patch SSH do worker 1.11.43.

Encerre a captura com Ctrl+C e copie os resultados:

```bash
cp ~/music-start-tests/opus-0.3.90-agent.log ~/storage/downloads/
cp ~/music-start-tests/opus-referencia-pcm.json ~/storage/downloads/
cp ~/music-start-tests/opus-direto.json ~/storage/downloads/
cp ~/music-start-tests/opus-fallback.json ~/storage/downloads/
```

## Verificações locais

Os testes incluem FFmpeg e libopus reais: pacotes idênticos à origem, parser,
buffer, mixer híbrido, encoder e decoder de saída; frames de 20 ms; fallback
de volume/TTS e permanência do encoder. Testes de API/preparação usam métodos
reais de produção com fronteiras Discord simuladas. Não foi executada uma
sessão Discord real, medição perceptiva, teste de rede móvel ou benchmark de
CPU no telefone. O ambiente local não tem pytest/discord.py instalados.

Para repetir os testes de saída na raiz do projeto, com a biblioteca padrão:

```bash
python -m unittest \
  cogs.musica.testes.runtime_telefone.test_output_profiles \
  cogs.musica.testes.runtime_telefone.test_output_bitrate_sync \
  tests.test_music_output_api \
  tests.test_music_output_helper \
  tests.test_music_output_native_opus \
  tests.test_music_opus_packets \
  tests.test_music_opus_hybrid \
  tests.test_music_opus_preparation \
  tests.test_music_opus_native_pipeline -v
```

Os testes nativos são pulados quando FFmpeg/libopus não estão disponíveis.
O pacote do runtime continua limitado a 63 arquivos mais o manifesto, para
preservar compatibilidade com os bootstraps existentes.

Nesta entrega, **168 testes de saída passaram**, incluindo os codecs nativos,
e **75 testes de transporte/política SSH passaram**. Outros 64 checks
existentes de qualidade, primeiro áudio, telemetria e DSP passaram por
adapters stdlib; dois casos com executor de threads não foram validados
porque bloquearam nesta sandbox. Esses adapters não equivalem à execução
com pytest. A publicação do runtime foi testada em diretório temporário:
CRC, hashes, permissões e extração pelos bootstraps 1.0.0 e 1.0.1. A sintaxe
dos arquivos alterados e do runtime foi validada para Python 3.10.
