# Smoke test / checklist de regressão

Use este checklist depois de mudanças em:
- `tts_voice.py`
- `tts_audio.py`
- `utility.py`
- `tts_toggle.py`
- `bot.py`
- comandos slash / prefixo
- painéis / toggles / auto-leave / self-deaf

Objetivo: detectar rápido se o bot subiu, se o TTS ainda funciona e se alguma regressão óbvia apareceu.

---

## 1) Pré-check rápido na VPS

Rodar:

```bash
sudo systemctl restart tts-bot
sudo systemctl status tts-bot --no-pager
journalctl -u tts-bot -n 120 --no-pager
curl -s http://127.0.0.1:10000/health | python3 -m json.tool
```

### Esperado
- `tts-bot.service` **active (running)**
- sem traceback novo no `journalctl`
- `/health` com:
  - `healthy: true`
  - `status: "ok"`
  - `discord_ready: true`
  - `discord_closed: false`
  - `mongo_ok: true`
  - `starting: false`

Se `/health` estiver `false`, parar aqui e corrigir antes de testar no Discord.

---

## 2) Slash commands críticos

### 2.1 `/help`
Abrir `/help`.

### Esperado
- abre a central de ajuda
- paginação funciona
- botões desativam no início/fim corretamente
- `⏪` e `⏩` funcionam
- apenas quem abriu consegue trocar de página
- se a view expirar, a resposta é amigável e não quebra feio

### 2.2 `/health`
Testar **somente** na guild `927002914449424404`.

### Esperado
- o comando aparece **só** nessa guild
- mostra embed bonito de saúde geral do bot
- **não** aparece como `/tts perf`
- `/tts perf` não deve existir mais

### 2.3 TTS slash principais
Testar os slash que você usa no dia a dia, pelo menos:
- `/tts`
- `/tts status`
- `/tts usuario`
- `/tts servidor`
- `/tts voices`

### Esperado
- sem erro de `TransformerError`
- sem comando ficando carregando infinitamente
- sem mensagem de comando inexistente

---

## 3) Prefixos e aliases

Usar o prefixo configurado da guild (ex.: `_`).

### 3.1 Help
- `_help`

### 3.2 Painéis
- `_panel`
- `_p`
- `_server_panel`
- `_sp`
- `_toggle_panel`
- `_tp`

### 3.3 Controles básicos
- `_join`
- `_leave`
- `_clear`
- `_reset`

### Esperado
- todos respondem
- aliases curtos funcionam igual ao comando principal
- nenhum vira fala TTS por engano
- não aparece no log `Command "help" is not found`, `panel`, `join`, `leave`, `reset` etc.

---

## 4) Painel pessoal

Abrir painel pessoal (`_panel` ou `_p`).

### Testar
- engine
- voz
- velocidade
- tom
- autor antes da frase
- apelido falado / nickname falado

### Esperado
- mudanças aplicam sem precisar restart
- nada edita a mensagem errada
- status do usuário reflete o valor alterado
- sem erro nos botões/menus

---

## 5) Painel do servidor

Abrir painel do servidor (`_server_panel` ou `_sp`).

### Testar
- idioma do servidor
- engine padrão do servidor
- opções gerais do TTS de servidor

### Esperado
- alterações persistem
- refletem no comportamento das falas seguintes
- sem erro de permissão indevida

---

## 6) Painel de toggles

Abrir painel de toggles (`_toggle_panel` ou `_tp`).

### Testar
- Auto leave
- self-deaf relacionado ao fluxo atual
- outras toggles disponíveis no painel

### Esperado
- toggle muda de estado corretamente
- embed/status do painel reflete a mudança
- não some opção do painel
- nada para de responder depois da mudança

---

## 7) Fluxo de call / voz

### 7.1 Join e fala básica
1. entrar em uma call
2. mandar uma frase simples

### Esperado
- bot entra na call
- fala a mensagem
- sem travar fila

### 7.2 Permanência na call com Auto leave desligado
1. desligar `Auto leave`
2. deixar o bot ocioso na call

### Esperado
- o bot **não sai** sozinho

### 7.3 Auto leave ligado
1. ligar `Auto leave`
2. sair da call / deixar bot sozinho conforme seu fluxo normal

### Esperado
- o bot sai quando deveria

### 7.4 Mover de canal
1. mover o bot para outro canal de voz

### Esperado
- continua conectado
- continua funcionando
- permanece **self-deaf** depois de mover

### 7.5 Leave manual
- usar `_leave`

### Esperado
- sai da call sem deixar worker preso

---

## 8) TTS real / cache / fila

### 8.1 Fala simples
Mandar 1 frase curta.

### Esperado
- fala sai normal
- sem demora absurda

### 8.2 Repetição da mesma frase
Mandar a mesma frase novamente.

### Esperado
- tende a usar cache
- `/health` deve mostrar `cache_hits` subindo

### 8.3 Mensagens idênticas em sequência
Mandar duas mensagens idênticas em sequência bem rápida.

### Esperado
- deduplicação não quebra a fila
- o bot não sintetiza trabalho repetido desnecessário

### 8.4 Fila curta
Mandar 3 a 5 mensagens curtas.

### Esperado
- fila anda
- não trava
- `queued_items_current` volta para `0` depois

---

## 9) Health / métricas TTS

Depois de testar falas, rodar de novo:

```bash
curl -s http://127.0.0.1:10000/health | python3 -m json.tool
```

### Conferir
- `tts_metrics` existe
- `cache_hits` / `cache_misses` fazem sentido
- `queue_enqueued` subiu
- `queued_items_current` volta a `0`
- engines mostram métricas coerentes
- sem `last_error` inesperado

---

## 10) Logs que não devem aparecer

Depois do teste, verificar:

```bash
journalctl -u tts-bot -n 200 --no-pager
```

### Não deveria aparecer com frequência
- `Command "help" is not found`
- `Command "panel" is not found`
- `Command "join" is not found`
- `Command "leave" is not found`
- `Command "reset" is not found`
- `TransformerError`
- traceback novo de import/syntax

---

## 11) Checklist curto de release

Use este mini-check depois de qualquer alteração pequena:

- [ ] bot sobe sem traceback
- [ ] `/health` da VPS está `healthy: true`
- [ ] `/help` funciona
- [ ] `_help` funciona
- [ ] `_panel` / `_p` funcionam
- [ ] `_sp` funciona
- [ ] `_tp` funciona
- [ ] bot entra em call e fala
- [ ] `Auto leave` desligado não faz o bot sair
- [ ] mover de canal mantém `self_deaf`
- [ ] `/health` slash aparece só na guild correta
- [ ] `/tts perf` não existe mais

---

## 12) Quando uma mudança mexer em `tts_audio.py`

Testar obrigatoriamente:
- [ ] fala curta
- [ ] fala repetida (cache)
- [ ] fila com várias mensagens
- [ ] mover canal
- [ ] `tmp_audio` não cresce absurdamente

Comandos úteis:

```bash
du -sh /home/ubuntu/bot/tmp_audio
find /home/ubuntu/bot/tmp_audio -type f | wc -l
```

---

## 13) Quando uma mudança mexer em `bot.py` / slash sync

Testar obrigatoriamente:
- [ ] bot carregou todos os cogs
- [ ] helpers novos não foram tratados como cog por engano
- [ ] slash commands sincronizaram
- [ ] `/health` aparece só na guild correta

Comando útil:

```bash
journalctl -u tts-bot -n 200 --no-pager | grep -i SYNC
```

---

## 14) Se der problema, coletar isso antes de mexer mais

```bash
sudo systemctl status tts-bot --no-pager
journalctl -u tts-bot -n 200 --no-pager
curl -s http://127.0.0.1:10000/health | python3 -m json.tool
```

Se o problema for de update:

```bash
tail -n 80 /home/ubuntu/bot/update.log
cat /home/ubuntu/bot/.update_last_failed_remote_hash 2>/dev/null || true
```
