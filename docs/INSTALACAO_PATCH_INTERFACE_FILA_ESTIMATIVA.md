# Interface — previsão de espera na fila

Patch incremental sobre a instalação com Music Agent **0.3.96**, mantendo as
pastas originais `tts-bot-main/`. A mudança é aplicada somente na **VPS**.
Music Agent permanece **0.3.96** e Phone Worker permanece **1.11.43**.

## O que aparece no Discord

A confirmação de uma inclusão individual passa a mostrar, por exemplo:

> 🎶 **Adicionada à fila: Labyrinth · `2:33`**  
> ⏳ Começa em aproximadamente **4:20**

`2:33` é a duração da música adicionada; `4:20` é a espera prevista antes de
ela começar. A espera soma o restante da música atual e as faixas que estão
na frente, considerando a velocidade de reprodução. A própria faixa nova
não entra nessa soma.

A confirmação guarda a estimativa do momento da inclusão. Ela não faz uma
contagem regressiva nem acompanha alterações posteriores da fila. Se o bot
completar um título provisório, a edição preserva essa estimativa original.
Nas inclusões de playlists, a confirmação mantém o resumo do lote.

Na **lista completa da fila**, cada música mostra sua duração e espera em
uma linha secundária. Abrir a lista, trocar de página ou usar seus controles
recalcula os valores com o estado disponível naquele momento. A prévia curta
da fila no cartão principal continua enxuta.

A linha de áudio do player fica mais curta:

- `Opus processado · 96 kbps` para saída processada confirmada.
- `Opus direto · 96 kbps` para cópia confirmada.

O complemento `fonte Opus 128 kbps` sai dessa linha; os metadados originais
continuam disponíveis no diagnóstico. O bitrate exibido é o da saída aplicada.

Em pausa, a previsão é condicional: **“Após retomar: aproximadamente…”**.
Live anterior, repetição da música atual, recuperação de reprodução, duração
desconhecida ou uma ordem virtual que não possa ser comprovada exibem
**“Previsão indisponível”**. Durações de playlists virtuais são aproveitadas
quando já estão disponíveis no cache compatível.

O cálculo não abre consultas extras ao worker, não resolve streams e não
cria temporizadores. A atualização e a navegação da fila continuam usando
seu fluxo normal. A previsão é aproximada: pulos, pausas, remoções, efeitos
e reorganizações posteriores podem alterar a espera real.

## 1. Instalar somente na VPS

Envie `patch-interface-fila-estimativa-incremental.zip` e seu `.sha256` para
o diretório pessoal da VPS. Os comandos abaixo pressupõem o bot em
`/home/ubuntu/bot`. Encerre a reprodução antes de reiniciar o serviço.

Confira o pacote:

```bash
cd "$HOME"
sha256sum -c patch-interface-fila-estimativa-incremental.sha256
unzip -l patch-interface-fila-estimativa-incremental.zip
```

Faça backup dos dois arquivos de produção existentes que serão substituídos:

```bash
fila_estimativa_backup="$HOME/backup-interface-fila-$(date +%Y%m%dT%H%M%S).tar.gz"
tar -czf "$fila_estimativa_backup" -C /home/ubuntu/bot \
  cogs/musica/interface/componentes.py \
  cogs/musica/comandos/tocar.py
printf '%s\n' "$fila_estimativa_backup"
```

Guarde o caminho impresso. Aplique o patch mesclando as pastas originais:

```bash
fila_estimativa_patch_dir=$(mktemp -d /tmp/patch-interface-fila.XXXXXX)
unzip -q "$HOME/patch-interface-fila-estimativa-incremental.zip" \
  -d "$fila_estimativa_patch_dir"
cp -a "$fila_estimativa_patch_dir/tts-bot-main/." /home/ubuntu/bot/

sudo systemctl restart tts-bot.service
sudo systemctl is-active tts-bot.service
sudo journalctl -u tts-bot.service -n 40 --no-pager
```

Esperado em `is-active`: **`active`**. Abra uma nova lista da fila e reproduza
uma música para conferir a interface após a reinicialização.

O patch acrescenta o helper de cálculo
`cogs/musica/reproducao/estimativa_fila.py`, além dos testes e deste guia.
Não é necessário publicar outro runtime, executar o bootstrap no Termux ou
reiniciar o Music Agent. Também não é necessário alterar SSH, `.env`, tokens,
arquivos de música ou configurações de áudio.

## 2. Testar no Discord

Use o mesmo fluxo habitual de música e mantenha o volume em um nível confortável.

1. Inicie uma música de duração conhecida e espere parte dela tocar.
2. Adicione uma segunda música com `_p <nome ou link>`. Confira título, duração
   e a linha **“Começa em aproximadamente…”**. A espera deve ficar próxima do
   tempo restante da primeira música.
3. Adicione uma terceira música. Sua espera deve incluir o restante da atual
   e a duração da segunda; a duração da terceira não deve entrar na espera dela.
4. Abra a **Fila**. Cada item deve ter duração e previsão. Feche e abra de novo
   após alguns segundos: a nova lista deve refletir o tempo transcorrido,
   enquanto as confirmações antigas continuam com os valores originais.
5. Pause a música atual e abra a fila. A previsão deve indicar **“Após retomar”**,
   sem fingir que a música está avançando durante a pausa.
6. Retome, pule uma faixa ou reorganize a fila e abra a lista novamente. Confira
   se as previsões seguem a nova ordem. As confirmações de inclusão continuam
   representando a estimativa inicial.
7. Se usar Nightcore ou Slowed + Reverb, abra a lista após mudar a velocidade.
   A espera deve acompanhar a velocidade atual; volume e Bassboost não devem
   alterar a previsão por si sós.
8. Teste uma inclusão por busca/seleção e uma cujo título seja completado depois.
   A confirmação final deve preservar a previsão calculada ao adicionar.
9. Confira a lista com títulos longos no celular, inclusive ao mudar de página.
   A prévia curta do player e os controles devem continuar funcionando.

Exemplo aritmético: se restam **1:10** da música atual e há uma faixa de
**3:10** antes da sua, a previsão é aproximadamente **4:20** na velocidade normal.
O bot não promete uma hora exata de início nem inclui um tempo fixo inventado
para preparação de áudio ou rede.

Se houver live ou duração desconhecida antes da faixa, confira
**“Previsão indisponível”**. Em uma playlist virtual, esse resultado também é
válido enquanto as durações necessárias não estiverem no cache; o cálculo
não deve forçar o carregamento das músicas apenas para preencher a previsão.

Confira também o player em Opus processado e em Opus direto. A linha deve
mostrar modo e bitrate, sem o complemento da fonte. O diagnóstico de áudio
continua mostrando os dados completos quando consultado pelo auxiliar existente.

## 3. Executar os testes locais

Na VPS, usando o Python do bot:

```bash
cd /home/ubuntu/bot
/home/ubuntu/bot-update-staging/candidates/python-runtimes/current/venv/bin/python -m unittest \
  tests.test_music_queue_wait_estimate \
  tests.test_music_queue_wait_ui \
  tests.test_music_queue_wait_messages \
  tests.test_music_opus_panel \
  tests.test_music_opus_processed_panel -v
```

Esses testes devem verificar a aritmética, identidade das entradas, estados
sem previsão confiável, preservação da estimativa durante a edição do título,
renderização da fila e rótulos compactos de áudio. As fronteiras de Discord
e transporte são adaptadas; esse comando não mede a espera real no telefone.

## 4. Reverter

Restaure os dois arquivos de produção usando o caminho real do backup:

```bash
tar -xzf "$HOME/backup-interface-fila-AAAAMMDDTHHMMSS.tar.gz" -C /home/ubuntu/bot
sudo systemctl restart tts-bot.service
sudo systemctl is-active tts-bot.service
```

O helper novo, os testes e a documentação podem permanecer no disco: os
arquivos antigos restaurados não importam o helper de estimativa. Abra uma
nova lista da fila para conferir o comportamento anterior. A reversão não
exige alteração no telefone nem restauração de músicas ou preferências.
