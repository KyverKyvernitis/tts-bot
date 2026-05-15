# Core Worker APK privado

Este diretório contém o APK privado **Core Worker**.

O APK é um **companion leve de onboarding**, não um painel completo de administração. Ele serve para facilitar o pareamento e fazer este celular aparecer como worker com o perfil correto. O controle pesado continua no Discord/VPS pelo painel `workers`.

## Escopo correto do APK

O APK pode:

- testar conexão com a VPS pela rede Tailscale;
- parear este celular usando o código `CORE-XXXX` gerado no painel `workers`;
- salvar o token localmente no app, sem hardcode no GitHub;
- mostrar/enviar status básico do próprio celular;
- reportar bateria via Android `BatteryManager`;
- reportar rede e ping TCP até a VPS;
- abrir o app Tailscale;
- editar o **perfil deste próprio celular** e reenviar roles/capabilities para a VPS.

O APK não deve virar, por enquanto:

- painel para gerenciar todos os workers;
- tela de fila completa de jobs;
- controle de failover;
- gerenciador de logs grandes;
- substituto do painel Discord;
- runtime completo que substitui o Termux.

## Perfis disponíveis

- `leve`: diagnósticos e logs;
- `midia`: logs, ZIP, FFmpeg, FFprobe e TTS/cache;
- `completo`: mídia + manutenção;
- `bedrock`: perfil futuro para Minecraft Bedrock, sem assumir Java.

O APK altera apenas o perfil do celular onde ele está instalado.

## Fluxo de teste

1. No Discord, abra o painel `workers`.
2. Vá em **Adicionar celular → Gerar código**.
3. No APK, preencha:
   - URL da VPS, exemplo: `http://100.x.x.x:10000`;
   - código `CORE-XXXX`;
   - nome do celular;
   - perfil.
4. Toque em **Testar VPS**.
5. Toque em **Conectar / parear celular**.
6. Para mudar o perfil depois, escolha outro perfil e toque em **Salvar perfil deste celular**.
7. Volte no Discord e toque em **Atualizar**.

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

- QR code para pareamento;
- detecção mais amigável de Tailscale/Termux;
- integração para facilitar instalar/iniciar o agent Termux;
- armazenamento via Android Keystore;
- tornar o processo “instalar APK → tocar conectar → virar worker” o mais automático possível;
- manter o controle pesado no Discord/VPS.
