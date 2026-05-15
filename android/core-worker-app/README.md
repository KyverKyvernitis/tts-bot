# Core Worker APK privado

Este diretório é a base inicial do APK privado **Core Worker**.

Esta versão ainda é um **companion app**: ela facilita pareamento, status, bateria, rede e ping com a VPS. O worker pesado continua no Termux/phone-worker por enquanto. A próxima etapa será fazer o APK controlar melhor o agent e reduzir cada vez mais os comandos manuais.

## O que já faz

- testa a conexão com a VPS/orquestrador;
- pareia usando o código `CORE-XXXX` gerado no painel `workers` do Discord;
- salva o token localmente no app, sem hardcode no GitHub;
- envia heartbeat/status para `/core-worker/heartbeat`;
- reporta bateria usando Android `BatteryManager`;
- reporta rede e ping TCP até a VPS;
- permite abrir o Tailscale oficial pelo botão;
- tem perfis iniciais: `leve`, `midia`, `completo` e `bedrock`.

## O que ainda não faz

- ainda não substitui o Termux;
- ainda não executa jobs da VPS;
- ainda não inicia/para o phone-worker automaticamente;
- ainda não embute Tailscale/VPN;
- ainda não usa armazenamento criptografado avançado para o token.

## Fluxo de teste

1. No Discord, abra o painel `workers`.
2. Vá em **Adicionar celular → Gerar código**.
3. No APK, preencha:
   - URL da VPS, exemplo: `http://100.x.x.x:10000`;
   - código `CORE-XXXX`;
   - nome do celular;
   - perfil.
4. Toque em **Testar conexão com a VPS**.
5. Toque em **Parear com código**.
6. Volte no Discord e toque em **Atualizar**.

> Use HTTP apenas dentro da rede privada Tailscale. Se a VPS ficar exposta publicamente, use HTTPS antes de parear.

## Build pelo Android Studio

1. Abra `android/core-worker-app` no Android Studio.
2. Aguarde o Gradle sincronizar.
3. Selecione **Build > Build APK(s)**.
4. Instale o APK apenas nos seus celulares.

## Build por terminal

Em uma máquina com Android SDK/Gradle configurado:

```bash
cd android/core-worker-app
gradle :app:assembleDebug
```

O APK debug ficará em:

```text
app/build/outputs/apk/debug/app-debug.apk
```

## Segurança

- Não coloque token da VPS, token Discord ou segredo no código Android.
- O token de worker é gerado pela VPS no pareamento e salvo localmente no app.
- Não suba keystore privado para GitHub.
- O APK é privado para uso nos seus celulares.

## Futuro planejado

- tela de logs e saúde completa;
- controle start/stop/restart do agent;
- integração mais forte com Termux enquanto o runtime próprio não existe;
- QR code para pareamento;
- serviço foreground para heartbeat contínuo;
- armazenamento via Android Keystore;
- runtime próprio no APK ou Termux embutido/forkado em etapa futura;
- role Minecraft Bedrock sem assumir Java.
