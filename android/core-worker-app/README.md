# Core Worker APK privado

Este diretório contém o APK privado **Core Worker**.

O APK está evoluindo para o objetivo final:

```text
instalou o APK -> preparou o celular -> pareou -> virou worker da VPS
```

Hoje ele ainda é um **companion de onboarding**: guia Termux, Termux:API, Termux:Boot e Tailscale, fala com o phone-worker local em `127.0.0.1` e conecta o worker real à VPS. O controle pesado continua no Discord/VPS pelo painel `workers`.

## v0.4.5 — updater robusto e worker builder sem sujeira local

A versão `0.4.5` mantém a URL da VPS fixa na tela normal, reforça o banner único de atualização no topo e documenta que builds feitos por worker devem ser publicados/assinados pela VPS sem deixar artefatos locais no repositório.

A VPS só publica/sinaliza que existe uma versão nova, e o APK cuida da experiência humana:

- o APK consulta sempre a VPS principal configurada no app (`http://100.103.240.118:10000`) e seu `/core-worker/app/latest.json`;
- se houver versão nova, mostra um aviso no topo com botão **Atualizar**;
- quando possível, envia uma notificação local de atualização;
- ao tocar em **Atualizar**, o APK baixa o arquivo indicado no manifesto, valida SHA-256 quando informado e abre o instalador do Android;
- se não houver update, o topo fica limpo e não mostra botão extra.

A interface principal continua em passos:

- **Preparar este celular**;
- **Conectar à VPS**;
- **Perfil deste celular**;
- **Sistema do app**.

O pareamento continua correto:

```text
APK -> /local/pair -> Termux phone-worker -> /core-worker/pair na VPS
APK -> /local/heartbeat -> Termux phone-worker -> /core-worker/heartbeat na VPS
```

O APK não cria worker próprio, não salva token de worker e não envia heartbeat direto. No Android comum, a instalação ainda precisa de confirmação do usuário.

## Escopo correto do APK agora

O APK pode:

- detectar se Termux, Termux:API, Termux:Boot e Tailscale estão instalados;
- detectar o worker local em `http://127.0.0.1:8766/local/status`;
- testar conexão com a VPS principal pela rede privada atual, sem o usuário digitar IP/porta;
- parear este celular usando o código `CORE-XXXX` gerado no painel `workers`;
- passar o pareamento para o phone-worker real via `POST /local/pair`;
- editar o **perfil deste próprio celular** por `POST /local/profile`;
- pedir heartbeat/status básico ao worker local por `POST /local/heartbeat`;
- procurar atualização privada do APK na VPS;
- mostrar o botão **Atualizar** no topo apenas quando houver update;
- baixar o APK e abrir o instalador do Android pelo próprio app;
- abrir Termux/Tailscale quando o Android permitir.

O APK não deve virar, por enquanto:

- painel para gerenciar todos os workers;
- tela de fila completa de jobs;
- controle de failover;
- gerenciador de logs grandes;
- substituto do painel Discord;
- runtime completo que substitui o Termux;
- VPN embutida ainda.

## Relação com Termux, plugins e rede privada

Estado atual:

```text
Core Worker APK
  -> guia/prepara
  -> fala com o phone-worker local

Termux + phone_worker.py
  -> executa jobs reais
  -> heartbeat
  -> fila segura
  -> scripts/ffmpeg/diagnóstico

Termux:API
  -> bateria/recursos do Android para o worker

Termux:Boot
  -> boot automático do worker

Tailscale externo
  -> rede privada atual entre celular e VPS
```

Direção futura:

```text
Core Worker APK
  -> onboarding
  -> worker cada vez mais embutido
  -> plugins/recursos cada vez menos manuais
  -> rede privada própria estilo VPN/WireGuard/userspace
```

A VPS/Discord continua sendo o cérebro/orquestrador.

## Rotas locais esperadas no phone-worker

```text
GET  http://127.0.0.1:8766/local/status
POST http://127.0.0.1:8766/local/profile
POST http://127.0.0.1:8766/local/pair
POST http://127.0.0.1:8766/local/heartbeat
```

Essas rotas devem aceitar apenas localhost. Elas não expõem shell livre, token global, fila completa, controle pesado ou ações perigosas.

## Perfis disponíveis

- `leve`: diagnósticos e logs;
- `midia`: logs, ZIP, FFmpeg, FFprobe e TTS/cache;
- `completo`: mídia + manutenção;
- `bedrock`: perfil futuro para Minecraft Bedrock, sem assumir Java.

O APK altera apenas o perfil do celular onde ele está instalado.

## Fluxo de uso

1. Abra o app **Core Worker**.
2. Em **Preparar este celular**, toque em **Verificar este celular**.
3. Se faltar algo:
   - abra/instale Termux;
   - instale Termux:API;
   - instale Termux:Boot para boot automático;
   - conecte no Tailscale.
4. No Discord, abra o painel `workers`.
5. Vá em **Adicionar celular → Gerar código**.
6. No APK, preencha apenas:
   - código `CORE-XXXX`;
   - nome do celular.

   A URL da VPS é fixa no app e não aparece como escolha normal para o usuário.
7. Toque em **Testar conexão**.
8. Toque em **Conectar este celular à VPS**.
9. Escolha o perfil e toque em **Aplicar perfil**.
10. No Discord, toque em **Atualizar**.

> Use HTTP apenas dentro da rede privada Tailscale/rede privada equivalente. Se a VPS ficar exposta publicamente, use HTTPS antes de parear.

## Atualização privada pela VPS

O APK procura atualização aqui:

```text
GET /core-worker/app/latest.json
```

E baixa o APK pelo `apkUrl` indicado no manifesto quando o usuário toca em **Atualizar** no topo do app.

### Endpoint na VPS

`webserver.py` serve arquivos a partir de:

```text
CORE_WORKER_APK_DIR
```

Se a variável não existir, o padrão é:

```text
/home/ubuntu/bot/android/core-worker-app/releases
```

considerando que o bot rode a partir de `/home/ubuntu/bot`.

### Publicar uma versão nova

Depois de buildar o APK na VPS:

```bash
cd /home/ubuntu/bot/android/core-worker-app
mkdir -p releases
cp app/build/outputs/apk/debug/app-debug.apk releases/CoreWorker-v0.4.5-debug.apk
sha256sum releases/CoreWorker-v0.4.5-debug.apk
```

Crie o manifesto:

```bash
cat > releases/latest.json <<'JSON'
{
  "versionName": "0.4.5",
  "versionCode": 9,
  "apkUrl": "/core-worker/app/CoreWorker-v0.4.5-debug.apk",
  "sha256": "COLE_AQUI_O_SHA256",
  "requiredAgentVersion": "1.6.5",
  "changelog": [
    "Botão Atualizar no topo apenas quando houver versão nova",
    "Notificação local quando a VPS publica atualização",
    "Atualização do APK feita pelo próprio app"
  ]
}
JSON
```

Reinicie o bot/webserver se necessário. O APK consulta a VPS, mostra uma notificação local quando houver versão nova e exibe o botão **Atualizar** no topo apenas nesse caso. O painel `workers` não precisa controlar atualização do APK.

## Build por terminal na VPS

Em uma VPS fraca, use swap de 4 GB e compile com baixa prioridade. Evite parar o bot se o callkeeper/monitor for religá-lo durante a build:

```bash
cd /home/ubuntu/bot/android/core-worker-app
nice -n 19 ionice -c3 gradle assembleDebug --no-daemon --max-workers=1
```

O APK debug ficará em:

```text
app/build/outputs/apk/debug/app-debug.apk
```

## Build pelo Android Studio

1. Abra `android/core-worker-app` no Android Studio.
2. Aguarde o Gradle sincronizar.
3. Selecione **Build > Build APK(s)**.
4. Instale o APK apenas nos seus celulares.

## Segurança

- Não coloque token da VPS, token Discord ou segredo no código Android.
- O APK não salva token de worker; o token real fica no `~/.phone-worker.env` do Termux.
- Não suba keystore privado para GitHub.
- O APK é privado para uso nos seus celulares.
- Atualização pela VPS deve ser usada pela rede privada.
- Sempre valide SHA-256 no `latest.json`.
- As rotas locais do phone-worker devem aceitar apenas `127.0.0.1`/localhost.

## Futuro planejado

Fases esperadas:

1. APK companion com Termux/Tailscale existentes.
2. APK guiando e automatizando cada vez mais setup, validação, atualização e recuperação.
3. APK reduzindo dependência manual de Termux/plugins.
4. Futuro grande: rede privada embutida/própria estilo VPN, provavelmente WireGuard/userspace ou equivalente.

Mesmo no futuro, a VPS/Discord deve continuar como cérebro/orquestrador e segredos devem continuar fora do GitHub.


## APK compilado por worker builder

Para evitar travar a VPS Oracle de 1 GB RAM, o painel `workers` pode enviar o job **Buildar APK** para um celular com perfil/função `builder` (`apk-builder`).

O fluxo é:

```text
VPS empacota android/core-worker-app em source-core-worker-app.zip
worker builder baixa o source
worker compila o APK
worker envia APK + sha256 para a VPS
VPS re-assina o APK com chave fixa local
VPS atualiza latest.json com o SHA-256 do APK assinado
Core Worker APK mostra Atualizar no topo quando houver versão nova
```

A VPS continua só como orquestradora/publicadora. O build pesado fica no worker.

No Android comum, a instalação ainda exige confirmação do usuário.


## Assinatura fixa ao publicar APK de worker builder

Quando um worker builder compila o APK, ele pode assinar com a chave debug do próprio Termux. Isso causa conflito no Android se o APK instalado foi assinado por outra chave. Por isso o endpoint `POST /core-worker/app/publish` agora deve re-assinar o APK na VPS antes de publicar.

Configuração recomendada na VPS:

```env
CORE_WORKER_APK_SIGNING_MODE=debug
CORE_WORKER_APK_KEYSTORE=/home/ubuntu/.android/debug.keystore
CORE_WORKER_APK_KEY_ALIAS=androiddebugkey
CORE_WORKER_APK_KEYSTORE_PASSWORD=android
CORE_WORKER_APK_KEY_PASSWORD=android
```

Para produção privada futura, troque por uma keystore release local da VPS:

```env
CORE_WORKER_APK_SIGNING_MODE=release
CORE_WORKER_APK_KEYSTORE=/home/ubuntu/secrets/core-worker-release.jks
CORE_WORKER_APK_KEY_ALIAS=core-worker
CORE_WORKER_APK_KEYSTORE_PASSWORD=...
CORE_WORKER_APK_KEY_PASSWORD=...
```

Nunca envie keystore ou senhas para GitHub. O APK publicado no `latest.json` deve ser o APK já assinado pela VPS, não o APK assinado pelo worker.
