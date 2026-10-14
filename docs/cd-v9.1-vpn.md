# Osaka CD v9.1 — usar a VPN Tailscale existente

Esta versão corrige a exigência de HTTPS público na instalação v9. O projeto
já usa a VPN Tailscale para comunicar o telefone com a VPS. A publicação de CDs
agora aceita esse endereço privado com autenticação própria e conserva HTTPS
como alternativa. Não altera o backend UTAU nem o Phone Worker 1.11.41.

Os pacotes abaixo contêm a versão v9 completa com essa correção. Não dependem
de ter instalado os ZIPs v9 anteriormente. O ZIP da VPS é incremental sobre
o código do bot; o kit do telefone é separado. Os dados locais do importador
continuam em `~/.local/share/osaka-ttv-cd`, fora da pasta de cada kit.

## Instalar na VPS uma vez

Aplique **osaka-cd-vps-v9.1.zip** pelo updater de código e aguarde a conclusão.
Depois, na VPS:

```sh
cd ~/bot
python3 scripts/configure-cd-server.py
```

O script descobre o IP da própria VPS pelo Tailscale ativo e lê somente as
configurações de endereço/porta do processo do bot. A saída mostra `publish_url`
e a origem da porta; não mostra o token. Se o processo não puder ser consultado,
usa a porta configurada no ambiente ou o padrão 10000 do projeto, e identifica
essa origem. `reachability_checked: false` significa que essa descoberta ainda
não verificou uma conexão do telefone.

Ele cria `~/osaka-cd-client.json`, `~/.config/osaka-cd/server.env` e o complemento
do serviço abaixo. Execuções posteriores preservam a credencial existente.

```sh
sudo install -m 0644 ~/.config/osaka-cd/60-cd-publication.conf /etc/systemd/system/tts-bot.service.d/60-cd-publication.conf
sudo systemctl daemon-reload
sudo systemctl restart tts-bot.service
systemctl --no-pager --full status tts-bot.service
```

Esse reinício aplica a configuração inicial. Novos CDs depois são publicados
pela rota de dados, validados e ativados sem enviar ZIP manualmente ao updater
e sem reiniciar o bot. Preserve `~/osaka-data/cd`, que contém gerações e rollback.

Se `tailscale_not_running` ou `tailscale_status_unavailable` aparecer, confira
a conexão Tailscale já instalada na VPS. Para ver apenas seu IP:

```sh
tailscale ip -4
```

Uma porta conhecida pode ser informada com `--port NUMERO`. Uma URL conhecida
pode ser informada com `--url http://IP_TAILSCALE:PORTA --transport tailscale`.
Não use o IP público nessa URL HTTP. HTTPS mantém o uso anterior com `--url`.

## Instalar o kit no Termux

Deixe o aplicativo Tailscale conectado à mesma rede da VPS. Encerre o importador
antigo com Ctrl+C na sessão dele. Se houver outra sessão, `pgrep -af import-midi.py`
mostra os PIDs; `kill -INT NUMERO_DO_PID` encerra somente o importador escolhido.

Baixe **osaka-cd-termux-kit-v9.1.zip** em Downloads e execute:

```sh
pkg install -y python ffmpeg unzip python-pillow openssh
python -m pip install aiohttp yt-dlp
unzip -o "$HOME/storage/downloads/osaka-cd-termux-kit-v9.1.zip" -d "$HOME"
cd "$HOME/osaka-cd-termux-kit-v9.1"
mkdir -p "$HOME/.config/osaka-cd"
chmod 700 "$HOME/.config/osaka-cd"
```

Não é necessário repetir `termux-setup-storage` se Downloads já está acessível.
O telefone usa a VPN do aplicativo Android; não exige instalar um comando
Tailscale no Termux nem substituir o worker.

Para ver o endereço já usado pelo Phone Worker, sem mostrar suas credenciais:

```sh
python deploy/ttv-cd/configure-cd.py --discover
```

Ele lê somente uma URL literal de `~/.phone-worker.env` ou do ambiente. Não
executa o arquivo, não o modifica e não reutiliza o token do worker. A saída
contém `vps_host`. Se a descoberta falhar, use o IP mostrado na saída da VPS.

Copie uma vez a configuração pareada por SSH. Substitua `IP_TAILSCALE_DA_VPS`
pelo IP encontrado:

```sh
scp ubuntu@IP_TAILSCALE_DA_VPS:osaka-cd-client.json "$HOME/.config/osaka-cd/publish.json"
chmod 600 "$HOME/.config/osaka-cd/publish.json"
python deploy/ttv-cd/configure-cd.py --check
```

O SSH usa a senha ou chave da VPS que você já possui. O arquivo transferido
contém a credencial privada de publicação; não compartilhe esse arquivo.

O `--check` deve retornar `ok: true`, `transport: tailscale` e
`server_available: true`. Esse resultado verifica o acesso autenticado à rota.
Se houver timeout, confira a VPN nos dois dispositivos e a porta indicada pela
descoberta. Um IP na faixa do Tailscale, sozinho, não prova um túnel ativo.

## Importar novos CDs automaticamente

```sh
mkdir -p "$HOME/storage/downloads/osaka-cd-midi"
python deploy/ttv-cd/import-midi.py --watch
```

Coloque MIDIs em **Downloads/osaka-cd-midi**, preferencialmente com o nome
`Artista - Música.mid`. O kit conserva o ZIP local para auditoria, publica pela
VPN e consulta a confirmação. `received`, `processing` e `applied` ainda não
confirmam carregamento; `activated` com `installed_on_vps: true` confirma que
o bot carregou a geração. Falhas conservam os artefatos e a fila para recuperação.

As harmonias, o cache de síntese, os limites e a interface são os mesmos da v9.
As instruções musicais estão em `deploy/ttv-cd/INSTRUCOES-IMPORTACAO.md`.

## Validação desta correção

Os testes isolados verificam a descoberta do Tailscale e da porta, configuração
privada, compatibilidade HTTPS, restrição de HTTP ao modo explícito da VPN,
autenticação, ausência de redirecionamentos e propagação do modo até a ativação.
O teste do cliente usa ZIP e MP3 reais, com a fronteira de rede simulada.
Não foi feita conexão com sua VPS ou seu telefone neste ambiente. O `--check`
acima e a confirmação `activated` verificam a instalação conectada.
