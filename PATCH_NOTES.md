# Patch: Core Worker app UI profissional + micro animações

Escopo: repaginação leve do APK Core Worker para reduzir poluição visual e deixar o uso normal mais profissional.

Alterações:
- APK bump para 0.5.61 / versionCode 76.
- Tela Core com textos mais curtos e foco em visão geral.
- Detalhes técnicos renomeados para Avançado.
- Bedrock mantém a ação principal visível e recolhe recursos técnicos em "Avançado do servidor".
- Importação/status rootfs, arquivos, logs, EULA e console ficam escondidos por padrão.
- Console do servidor só aparece quando o usuário toca em Abrir console.
- Botões ganharam micro animação de pressionamento.
- Troca Core ↔ Bedrock tem fade/slide leve.
- Expansão/recolhimento de Avançado usa animação curta.
- Limpeza segura de updates fica escondida no fluxo normal.

Não alterado:
- updater/ZIP/GitHub/rollback/redo;
- CallKeeper;
- Core Linux funcional;
- importação rootfs já validada;
- Bedrock start real;
- TTS runtime;
- player de música em execução.
