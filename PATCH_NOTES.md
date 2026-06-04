# patch-core-worker-app-polish-rootfs-reporting-and-bedrock-prep-20260603

Patch grande de polimento do Core Worker APK + reporting do rootfs real.

## Incluído

- APK bump para `0.5.62` / `versionCode 77`.
- Tela Core passa a destacar o estado principal do rootfs real.
- Tela Bedrock mostra rootfs e runner em linguagem mais limpa.
- Avançado do servidor recebe status detalhado do rootfs real.
- Botões novos no avançado:
  - Validar rootfs ativo.
  - Cancelar importação pendente.
- Importação rootfs ganha estados intermediários persistidos:
  - lendo/calculando SHA-256;
  - hash pronto;
  - validando layout;
  - promovendo staging;
  - concluído.
- Status do rootfs mostra estatísticas compactas quando disponíveis.
- `coreLinuxState`/`coreLinuxSummary` passam a promover `rootfs_real_validated` para o resumo principal.
- `runtime-summary` da VPS também promove rootfs real validado para o top-level do Core Linux.
- Painel workers passa a mostrar `Rootfs real validado` e `runner bloqueado` quando aplicável.
- Importador tar aceita melhor PAX/GNU long path (`L`, `K`, `x`) sem abrir execução de binários.

## Mantido bloqueado

- Bedrock start real.
- Box64 start.
- Runner real.
- Shell livre.
- Comando remoto arbitrário.
- Execução de binários importados no rootfs.

## Não alterado

- Updater / ZIP update / GitHub / rollback / redo.
- CallKeeper.
- Player de música em execução.
- TTS runtime.
- Core Linux funcional já validado.
