# Termux Kit v9.1

O kit importa MIDIs locais, prepara o CD e publica automaticamente na rota
autenticada da VPS pela VPN Tailscale existente. HTTPS também é suportado.
O catálogo é ativado sem reiniciar o bot. O updater
continua responsável pelas atualizações de código; novos CDs usam o serviço
de dados do catálogo.

## Instalação

Baixe o kit v9.1 em Downloads e encerre o importador antigo com Ctrl+C na sua
sessão. `pgrep -af import-midi.py` mostra os processos; `kill -INT PID` encerra
somente o importador escolhido. O Phone Worker continua separado.

```sh
pkg install -y python ffmpeg unzip python-pillow openssh
python -m pip install aiohttp yt-dlp
unzip -o "$HOME/storage/downloads/osaka-cd-termux-kit-v9.1.zip" -d "$HOME"
cd "$HOME/osaka-cd-termux-kit-v9.1"
```

A VPS precisa receber a atualização v9.1 uma vez e executar
`python3 scripts/configure-cd-server.py` dentro da pasta do bot. Siga
`docs/cd-v9.1-vpn.md` da atualização para instalar o arquivo de serviço e
reiniciar o bot nessa configuração inicial. O script gera na VPS o arquivo
privado `~/osaka-cd-client.json`, já com o endereço e o token correspondentes.
Novos CDs depois dessa configuração usam a rota de dados e não reiniciam o bot.

Sem `--url`, o script consulta o Tailscale ativo na VPS e a porta do serviço
do bot. Não exige domínio público. No telefone, mantenha o aplicativo Tailscale
conectado à mesma rede. Para descobrir o endereço já pareado com seu worker:

```sh
python deploy/ttv-cd/configure-cd.py --discover
```

No Termux, copie o arquivo por SSH. Execute `read` e, na linha seguinte,
digite o `vps_host` mostrado pela descoberta e pressione Enter:

```sh
mkdir -p "$HOME/.config/osaka-cd"
chmod 700 "$HOME/.config/osaka-cd"
read -r OSAKA_CD_VPS
```

Depois de preencher o endereço, execute:

```sh
scp "ubuntu@${OSAKA_CD_VPS}:osaka-cd-client.json" "$HOME/.config/osaka-cd/publish.json"
chmod 600 "$HOME/.config/osaka-cd/publish.json"
```

O SSH poderá solicitar a confirmação da chave da VPS e sua senha. A alternativa
é executar `python deploy/ttv-cd/configure-cd.py`: ele reutiliza a URL literal
do pareamento existente e pede o token de publicação com entrada oculta. O token
de publicação é próprio do catálogo; o token do Phone Worker não é reutilizado.
Não envie tokens ou o arquivo privado no Discord.

A configuração fica em `~/.config/osaka-cd/publish.json`, com permissão 600,
fora do diretório do kit. Instalar versões posteriores preserva a configuração.
O arquivo privado não entra em relatórios nem nos pacotes de CD.
O modo `tailscale` permite HTTP somente para IP literal da rede Tailscale,
com autenticação e sem redirecionamento ou proxy HTTP. O telefone não precisa
do comando `tailscale`; a conexão é fornecida pelo aplicativo Android.

Verifique a conexão e inicie o importador:

```sh
python deploy/ttv-cd/configure-cd.py --check
mkdir -p "$HOME/storage/downloads/osaka-cd-midi"
python deploy/ttv-cd/import-midi.py --watch
```

Coloque os novos arquivos `.mid` ou `.midi` na pasta
`Downloads/osaka-cd-midi`. O importador espera o MIDI e seu sidecar ficarem
estáveis por dois segundos. Não é necessário enviar o ZIP de cada música.
O ZIP continua sendo gerado localmente como artefato auditável e transporte
interno; os arquivos concluídos são verificados antes da publicação.

## Identificar a música

O nome `Artista - Música.mid` define artista e título. Para nomes diferentes,
faça um JSON com o mesmo nome-base ao lado do MIDI:

```json
{
  "id": "minha-musica",
  "title": "Minha Música",
  "artist": "Artista",
  "aliases": ["Título alternativo"],
  "artist_aliases": [],
  "max_parts": 3,
  "include_bass": true
}
```

A busca reutiliza a normalização do player, verifica os candidatos compatíveis
e compara suas contagens atuais. Covers, reações e versões diferentes são
excluídos quando não solicitados. O relatório informa o universo consultado;
a escolha não representa uma garantia sobre todos os vídeos do YouTube.

`max_parts` aceita de 1 a 3 partes. A seleção vocal permanece marcada como
ambígua quando o MIDI não oferece evidência suficiente. O arranjo completo
preserva a origem; o painel e a amostra usam um trecho comum entre as partes,
com até 60 segundos e 128 notas por parte. As harmonias vêm das notas reais do
MIDI, sem transposição ou harmonias inventadas automaticamente.

## Síntese e arquivos prontos

O backend UTAU do Phone Worker 1.11.41 é preservado. A síntese multipartes usa
o worker ativo autenticado, evitando abrir outro renderizador concorrente.
Não instala resamplers nem voicebanks. As partes são renderizadas em WAV/PCM,
alinhadas na mesma linha do tempo e misturadas antes da codificação MP3.

Uma alteração da capa atualiza thumbnail, cor e emoji, preservando a amostra.
Uma mudança na melodia, harmonia ou configuração de síntese invalida a amostra
correspondente. Uma amostra monofônica antiga só migra explicitamente para um
arranjo de uma parte com a mesma partitura; ela nunca representa um arranjo
novo de duas ou três partes.

## Estado e recuperação

A fila de publicação em `pending_publications.json` é gravada antes da rede.
Falhas de conexão não perdem pacotes. Jobs já recebidos são consultados sem
reenviar o ZIP. `publication_packages.json` registra o último ZIP validado de
cada música, inclusive quando a publicação ainda não foi configurada;
`published_publications.json` guarda as confirmações de instalação para evitar
reenviar pacotes concluídos depois de reiniciar. Os estados são distintos:

- `received`/`processing`: recebido ou em validação.
- `applied`: arquivos completos e consistentes gravados na VPS.
- `activated`: catálogo carregado pelo bot; `installed_on_vps: true`.
- `failed`: rejeição de validação ou conflito de versão, com código de motivo.

Conflitos não sobrescrevem uma versão mais nova automaticamente. Depois de
corrigir o MIDI ou o sidecar, pare o watch com Ctrl+C e execute:

```sh
python deploy/ttv-cd/import-midi.py --retry
python deploy/ttv-cd/import-midi.py --watch
```

`--retry` ignora o cooldown e permite repetir explicitamente uma publicação
rejeitada, consultando novamente a versão atual da VPS. `--force` reimporta os
arquivos já processados. Uma execução reserva o catálogo; abrir outro watch
retorna `importer_already_running`. Trocar arquivos Python durante a execução
encerra o importador e pede reinício para evitar mistura de versões.

Falhas de amostra permanentes aguardam correção de entrada ou configuração;
falhas temporárias usam cooldown mesmo quando o MIDI saiu da pasta. O watch
consulta arquivos a cada dois segundos e reserva um job de publicação por
rodada, com rotação entre músicas. Uploads têm prazo finito e podem levar mais
tempo em conexões lentas; esse tempo não é apresentado como síntese ou primeira
resposta do bot. Inventários do voicebank são verificados a cada 30 segundos
quando o kit está ocioso e integralmente antes de uma importação real.

O terminal mostra somente resultados úteis e um estado resumido periódico.
O relatório completo fica em `Downloads/osaka-cd-import-relatorio.json`; o
watch mantém `*-estado-atual.json` sem apagar a última auditoria concluída.
Use `--verbose-report` para imprimir também as notas e os candidatos.
