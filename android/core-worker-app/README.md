# Core Worker APK privado

Este diretório contém o APK privado **Core Worker**.

O APK é um **companion leve de onboarding**. Ele não é o painel completo de administração: serve para facilitar o pareamento, mostrar um status simples, escolher o perfil deste celular e conversar com o worker local do Termux. O controle pesado continua no Discord/VPS pelo painel `workers`.

## Escopo correto do APK

O APK pode:

- testar conexão com a VPS pela rede privada atual;
- parear este celular usando o código `CORE-XXXX` gerado no painel `workers`;
- salvar o token localmente no app, sem hardcode no GitHub;
- mostrar/enviar status básico do próprio celular;
- reportar bateria via Android `BatteryManager`;
- reportar rede e ping TCP até a VPS;
- abrir o app Tailscale;
- abrir o Termux ou orientar o comando de início;
- detectar o agent local em `http://127.0.0.1:8766/local/status`;
- editar o **perfil deste próprio celular** e tentar sincronizar com o phone-worker real por `POST /local/profile`.

O APK não deve virar, por enquanto:

- painel para gerenciar todos os workers;
- tela de fila completa de jobs;
- controle de failover;
- gerenciador de logs grandes;
- substituto do painel Discord;
- runtime completo que substitui o Termux.

## v0.3.0 — integração leve com o Termux

A versão `0.3.0` adiciona a ponte local APK ↔ phone-worker:

- checklist simples de rede/Tailscale, VPS, worker local e pareamento;
- detecção do worker local via `GET /local/status` em `127.0.0.1:8766`;
- exibição da versão/perfil do agent local quando ele responde;
- botão **Abrir Termux** para facilitar iniciar o worker;
- ao salvar o perfil, o APK tenta enviar o perfil para o worker local;
- se o worker local estiver offline, o perfil fica salvo no APK e o usuário recebe aviso simples.

As rotas locais existem só para o próprio celular. Elas não expõem shell livre, token global, fila completa, controle pesado ou ações perigosas.

## Perfis disponíveis

- `leve`: diagnósticos e logs;
- `midia`: logs, ZIP, FFmpeg, FFprobe e TTS/cache;
- `completo`: mídia + manutenção;
- `bedrock`: perfil futuro para Minecraft Bedrock, sem assumir Java.

O APK altera apenas o perfil do celular onde ele está instalado.

## Fluxo de teste

1. No Termux, deixe o phone-worker atualizado e rodando.
2. No Discord, abra o painel `workers`.
3. Vá em **Adicionar celular → Gerar código**.
4. No APK, preencha:
   - URL da VPS, exemplo: `http://100.x.x.x:10000`;
   - código `CORE-XXXX`;
   - nome do celular;
   - perfil.
5. Toque em **Verificar worker local**.
6. Toque em **Testar VPS**.
7. Toque em **Conectar / parear celular**.
8. Para mudar o perfil depois, escolha outro perfil e toque em **Salvar perfil deste celular**.
9. Volte no Discord e toque em **Atualizar**.

> Use HTTP apenas dentro da rede privada Tailscale/rede privada equivalente. Se a VPS ficar exposta publicamente, use HTTPS antes de parear.

## Relação com o phone-worker

Hoje o Termux worker ainda executa os jobs reais. O APK apenas ajuda a conectar, verificar, parear e ajustar o próprio perfil do celular.

A ponte local esperada é:

```text
APK Core Worker -> http://127.0.0.1:8766/local/status
APK Core Worker -> http://127.0.0.1:8766/local/profile
```

O Discord/VPS continua sendo o cérebro/orquestrador. O APK não deve gerenciar outros celulares.

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
- As rotas locais do phone-worker devem aceitar apenas chamadas vindas de `127.0.0.1`/localhost.

## Futuro planejado

Objetivo final: **instalou o APK → pareou → o celular virou worker da VPS**.

Fases esperadas:

1. APK companion leve com Termux/Tailscale existentes.
2. APK ajudando cada vez mais o setup, validação e recuperação do worker.
3. Futuro: reduzir ou substituir dependências manuais, incluindo uma rede privada embutida/própria estilo VPN, provavelmente baseada em WireGuard/userspace ou equivalente.

Mesmo no futuro, a VPS/Discord deve continuar como cérebro/orquestrador e segredos devem continuar fora do GitHub.
