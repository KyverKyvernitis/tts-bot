# Executor de voz na VPS — opção 0.3.83

O controller pode enviar `/command` diretamente ao Music Agent na VPS. Isso remove
o caminho VPS → túnel/telefone → proxy local dos comandos de voz. O Phone Worker
continua atendendo resolução de metadados e arquivamento. O modo padrão permanece
`full` no telefone até esta opção ser configurada.

O perfil usa o mesmo código de reprodução, volume, efeitos e TTS. Não ativa
passthrough Opus: o volume 55%, a mistura de TTS e os efeitos precisam de PCM.
Os arquivos permanentes de música continuam no fórum; os buffers de reprodução
ficam na RAM e o arquivador mantém seu staging temporário.

## Preparar Linux com systemd

Copie o repositório atualizado para `/opt/tts-bot-main`. Antes de iniciar voz na
VPS, envie `_disconnect` em todas as guilds com sessão existente e configure
`MUSIC_AGENT_EXECUTOR_MODE=archive` no ambiente do agente do telefone. Reinicie
esse agente. Seu registro continuará disponível para metadados/arquivamento,
mas ele recusará comandos de voz. Nunca mantenha dois executores de voz ativos
com o mesmo token. O controller normal e o agente separado usam o mesmo token,
como na arquitetura atual; apenas o agente é responsável por voz.

Instale dependências e o serviço (Debian/Ubuntu):

```bash
sudo apt-get update
sudo apt-get install -y python3-venv ffmpeg libopus0 nodejs
sudo useradd --system --home-dir /var/lib/music-agent --shell /usr/sbin/nologin music-agent
sudo python3 -m venv /opt/music-agent-venv
sudo /opt/music-agent-venv/bin/pip install -r /opt/tts-bot-main/deploy/music-agent-vps/requirements-voice.txt
sudo install -d -m 700 /etc/music-agent
sudo install -m 600 /opt/tts-bot-main/deploy/music-agent-vps/voice.env.example /etc/music-agent/voice.env
sudo install -m 644 /opt/tts-bot-main/deploy/music-agent-vps/music-agent-voice.service /etc/systemd/system/music-agent-voice.service
```

Preencha os dois tokens em `/etc/music-agent/voice.env`. O token privado da API
deve ser aleatório e diferente do token Discord. O usuário `music-agent` precisa
ler o código em `/opt/tts-bot-main`; a unidade impede escrita no repositório.
Não execute `useradd` se esse usuário já existir. Para YouTube, use Node em versão
suportada pelo yt-dlp instalado; `nodejs` de distribuição antiga pode precisar
ser atualizado. `deno` também pode ser usado com a configuração correspondente.

Inicie e consulte o serviço:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now music-agent-voice
sudo journalctl -u music-agent-voice -n 40 --no-pager
```

Acrescente as três variáveis de `controller.env.example` no ambiente do bot
principal. O token precisa coincidir com `MUSIC_AGENT_TOKEN`. Reinicie o
controller depois do agente ficar `discord_ready=true` e com dependências
disponíveis. O controller valida versão >=0.3.83 e modo `voice`/`full`, sem
recorrer automaticamente a outro executor de voz quando a rota falha.

Para outra máquina, use HTTPS autenticado ou a interface privada Tailscale.
HTTP aceita loopback, redes privadas e endereços/MagicDNS do tailnet. A API exige
token; não exponha a porta na internet. O modelo loopback dispensa abrir porta.

Para voltar ao telefone: `_disconnect` nas sessões da VPS, pare/desabilite o
serviço de voz, desative `MUSIC_AGENT_DIRECT_API_ENABLED`, retorne o telefone a
`MUSIC_AGENT_EXECUTOR_MODE=full` e reinicie o controller. Não troque executor com
fila ativa; o vínculo da guild é mantido até desconexão explícita.

## Medir antes/depois

Colete pelo menos 20 reproduções de cada cenário: arquivo já conhecido com call
conectada, arquivo com call desconectada, playlist conhecida e música ainda não
arquivada. Mantenha região do Discord, qualidade e carga comparáveis. Salve as
linhas do journal com `[music-start]` e rode:

```bash
python3 scripts/benchmark-music-start.py antes.log --compare depois.log
```

O relatório mostra mediana e p95 das durações locais do controller e do agente
até o primeiro pacote musical enviado com sucesso. Não subtrai relógios de
máquinas diferentes nem soma etapas concorrentes. `queue_wait_ms` separa faixas
enfileiradas; o relatório de início imediato exclui esperas >100ms na fila.
O primeiro pacote enviado é uma aproximação operacional do início, não uma
medição do som recebido pelo cliente Discord. Latência de rede e decodificação
no cliente precisam de medição adicional. Não há porcentagem de ganho real
antes de executar esse comparativo no ambiente de produção.
