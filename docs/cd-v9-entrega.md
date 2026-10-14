# Osaka CD v9.1 — instalação e uso

Para reutilizar a VPN existente, consulte também [cd-v9.1-vpn.md](cd-v9.1-vpn.md).

Esta entrega separa novos CDs das atualizações de código. A VPS recebe pacotes de dados pela rota autenticada, valida e ativa uma geração do catálogo sem reiniciar o bot. O Termux publica e acompanha o estado automaticamente. O ZIP local continua disponível para auditoria e recuperação.

O backend UTAU e os arquivos do Phone Worker 1.11.41 são preservados. O kit usa requisições de partitura monofônica compatíveis para sintetizar as partes e compõe o áudio alinhado. O painel mantém notas, oitavas, duração total, capa, seletor e botões; a duração individual das notas permanece removida.

## 1. Instalar a atualização da VPS uma vez

Envie **osaka-cd-vps-v9.1.zip** ao updater de código que você já utiliza e aguarde a conclusão. O pacote é incremental e inclui as correções anteriores necessárias. Não envie o ZIP do kit Termux ao updater da VPS.

Depois, na VPS:

```sh
cd ~/bot
python3 scripts/configure-cd-server.py
```

Sem pedir um endereço público, o script descobre o IP da VPN Tailscale ativa na VPS e a porta do serviço do bot. Ele gera uma credencial privada, conserva a mesma credencial em execuções posteriores e prepara os arquivos abaixo sem mostrar o token no terminal:

- `~/.config/osaka-cd/server.env`: configuração privada da VPS.
- `~/.config/osaka-cd/60-cd-publication.conf`: complemento de configuração do serviço.
- `~/osaka-cd-client.json`: configuração pareada para transferir uma vez ao telefone.
- `~/osaka-data/cd/`: catálogo dinâmico e cache.

Ative a configuração no serviço:

```sh
sudo install -m 0644 ~/.config/osaka-cd/60-cd-publication.conf /etc/systemd/system/tts-bot.service.d/60-cd-publication.conf
sudo systemctl daemon-reload
sudo systemctl restart tts-bot.service
systemctl --no-pager --full status tts-bot.service
```

Esse reinício é da instalação inicial. Adicionar CDs depois usa a API de dados e não reinicia o bot. Preserve `~/osaka-data/cd`: as gerações anteriores permitem rollback. O updater continua responsável por mudanças de código.

## 2. Instalar o kit no Termux

Baixe **osaka-cd-termux-kit-v9.1.zip** em Downloads. Encerre o importador antigo na sessão dele com Ctrl+C. Para localizar outra sessão:

```sh
pgrep -af import-midi.py
```

Se aparecer um importador antigo, use `kill -INT NUMERO_DO_PID`, substituindo pelo número mostrado. Isso encerra o importador escolhido; o Phone Worker permanece separado.

Instale e extraia:

```sh
pkg install -y python ffmpeg unzip python-pillow openssh
python -m pip install aiohttp yt-dlp
unzip -o "$HOME/storage/downloads/osaka-cd-termux-kit-v9.1.zip" -d "$HOME"
cd "$HOME/osaka-cd-termux-kit-v9.1"
mkdir -p "$HOME/.config/osaka-cd"
```

O acesso a Downloads já funciona no seu Termux. `termux-setup-storage` só é necessário se esse acesso deixar de existir.

## 3. Parear com a VPS uma vez

Mantenha o aplicativo Tailscale do telefone conectado à mesma rede. `python deploy/ttv-cd/configure-cd.py --discover` mostra o endereço já pareado em `~/.phone-worker.env`. Transfira o arquivo privado por SSH. No comando abaixo, substitua `SEU_IP` pelo IP da VPN encontrado ou pelo endereço que você usa para entrar na VPS:

```sh
scp ubuntu@SEU_IP:~/osaka-cd-client.json "$HOME/.config/osaka-cd/publish.json"
chmod 600 "$HOME/.config/osaka-cd/publish.json"
python deploy/ttv-cd/configure-cd.py --check
```

O SSH pode pedir a senha ou usar a chave já configurada. O arquivo contém a credencial de publicação; não envie esse arquivo pelo Discord ou junto com um CD.

Se preferir informar a URL e o token por entrada oculta, execute `python deploy/ttv-cd/configure-cd.py`. A transferência por SSH acima evita copiar o token no terminal.

O `--check` precisa retornar `ok: true` e `server_available: true`. Se a URL estiver errada, a rota não estiver habilitada ou a autenticação falhar, o kit informa o código da falha e mantém os artefatos para uma tentativa posterior.

## 4. Usar a importação automática

```sh
mkdir -p "$HOME/storage/downloads/osaka-cd-midi"
python deploy/ttv-cd/import-midi.py --watch
```

Coloque MIDIs em **Downloads/osaka-cd-midi**. O nome `Artista - Música.mid` fornece a identidade para a busca. Um JSON de mesmo nome pode informar título, artista, aliases, faixa preferida e versões autorizadas. Veja `deploy/ttv-cd/INSTRUCOES-IMPORTACAO.md` para as opções reais.

O kit espera o arquivo e o JSON estabilizarem, extrai a música, consulta candidatos com os critérios do player, prepara capa/amostra, publica e acompanha a confirmação. Artefatos prontos anteriores são reutilizados quando suas dependências continuam iguais. Os dados locais já existentes em `~/.local/share/osaka-ttv-cd` permanecem disponíveis ao kit v9; ele não exige apagar essa pasta.

Uma tentativa manual imediata:

```sh
python deploy/ttv-cd/import-midi.py --retry
```

Para refazer uma importação já processada e consultar metadados novamente:

```sh
python deploy/ttv-cd/import-midi.py --force
```

Use esses comandos após encerrar o `--watch` com Ctrl+C. O guard impede dois importadores de escreverem o mesmo catálogo.

`received`, `processing` e `applied` ainda não são confirmação de que o bot carregou o CD. `activated` e `installed_on_vps: true` confirmam a geração carregada. O estado de capa, amostra e emoji continua separado: uma versão pode ser ativada com artefatos explicitamente pendentes.

O watcher verifica arquivos a cada dois segundos quando está disponível e consulta recursos do telefone de forma limitada. Processamento e upload são sequenciais; uma transferência pode ocupar até 120 segundos na rede móvel. A publicação tem fila, cooldown e recuperação, sem prometer um tempo fixo para busca ou síntese fria.

## 5. Harmonias e músicas completas

O arranjo conserva o MIDI completo e a proveniência. A seleção separa até três partes com continuidade musical, registro e evidências da faixa. Harmonias precisam ter suporte no arquivo; a extração não inventa uma transcrição vocal confirmada.

Cada parte usa chamadas monofônicas limitadas do UTAU existente. Os trechos compartilham o mesmo relógio, são alinhados pelo preroll das consoantes, somados em PCM com margem de volume e codificados para o áudio final. Cache por parte permite mudar ganho/panorâmica sem repetir síntese.

A amostra usa uma janela comum às partes. O canto de uma mensagem utiliza a letra fornecida na melodia principal e guias de vogais nas harmonias, salvo letras MIDI explicitamente selecionadas para essas partes. Letras insuficientes recebem orientação; não desativam a saúde do worker.

Os limites do arranjo desta versão são dez minutos, três partes, 24.000 notas cantáveis e 60.000 notas originais. Cortes necessários para chamadas físicas são registrados, incluindo notas sustentadas que precisem de novo ataque na junção; não se afirma continuidade fonética perfeita nesses casos.

**Limite real preservado:** o gateway de TTS do telefone e a rota remota de música têm teto de 90 segundos por reprodução. Músicas completas maiores podem ser compostas e reproduzidas pela rota local da VPS. O sistema impede enviar uma composição longa à rota remota que a cortaria; não muda a posse da conexão de voz para contornar esse limite. Amostras de até 60 segundos permanecem dentro dele. Ampliar reprodução remota exige uma extensão específica desse gateway, separada do backend UTAU.

## 6. Verificar a instalação

No Termux, consulte `Downloads/osaka-cd-import-relatorio.json` e o arquivo `osaka-cd-import-relatorio-estado-atual.json`. O relatório inclui arquivos absolutos, hashes, critérios de seleção, arranjo, validação e confirmação da publicação. Rodadas ociosas preservam o último relatório útil.

No Discord:

1. Abra `_cd` e confira o novo CD, capa e notas.
2. Clique em **Ouvir amostra** e confira o áudio até o fim.
3. Selecione o CD e teste canto com letra suficiente para as frases indicadas.
4. Confirme que outros comandos e painéis continuam respondendo durante a importação.

Na VPS, para consultar os logs sem precisar de aspas:

```sh
sudo journalctl -u tts-bot.service --since=-15min --no-pager -n 150
```

O diagnóstico de capas anterior continua disponível em `scripts/diagnose-cd-assets.py`; ele informa se Pillow existe no Python usado. O Python do sistema e o do serviço podem ser diferentes. A confirmação via API e os testes do painel usam o catálogo runtime da instalação.

## 7. Evidências e limites dos testes

Os testes isolados conferem ZIP, JSON, hashes, duração/decodificação MP3, JPEG, transações SQLite, concorrência, retomada, CAS, rollback, catálogo por geração, cache, MIDI original e composição real PCM/FFmpeg. O MIDI original de Rolling Girl preserva 188,663 segundos e 2.880 notas de origem; a seleção de três partes contém 1.824 notas em 11 trechos comuns. A versão simplificada conserva 72 notas e 13,413 segundos.

O MP3 de Rolling Girl fornecido anteriormente foi validado e preservado byte a byte na migração. Os testes de composição usam tons PCM controlados; eles verificam tempo, mistura e arquivo final, sem afirmar que sintetizaram uma nova gravação física de Teto.

Este ambiente não possui a voicebank física do seu telefone, uma sessão Discord, acesso à VPS ou os SDKs Flask/Discord/pytest instalados. Casos HTTP/TCP indisponíveis são identificados como ignorados, e testes de handlers usam um adaptador explícito de fronteira. A qualidade vocal e a instalação conectada precisam da verificação do passo 6.

Os benchmarks são locais. A medição desta máquina com dez rodadas encontrou p50 aproximado de 143 ms para validação/commit de uma fixture curta e 1,2 ms para reutilizar uma composição em cache. Eles não representam tempo de canto no telefone nem latência audível no Discord.

Os artefatos `CD-v9-validation.json`, `CD-v9-test-results.json`, `CD-v9-termux-validation.json`, `music-v9-review.json` e `CD-v9-local-benchmark.json` registram escopo, resultados e verificações dos pacotes entregues.
