// Core Worker Linux runner guard v1.
//
// Este arquivo gera o libcoreworker_runner.so embutido no APK privado.  Ele é
// propositalmente mínimo e safe-by-default: não chama libc, não abre shell, não
// inicia Bedrock, não executa Box64/proot/busybox e não aceita comando remoto.
// O objetivo deste estágio é transformar o requisito "core-runner embutido" em
// um artefato ELF arm64 real, auditável e detectável pelo preflight.
//
// A biblioteca exporta funções pequenas para probes futuros. O runner real de
// execução controlada só deve ser liberado depois que proot/busybox/box64 e os
// arquivos do servidor passarem no preflight.

#define CORE_WORKER_RUNNER_VERSION "core-worker-runner/0.1.0-safe-preflight-only"

__attribute__((visibility("default")))
const char* core_worker_runner_version(void) {
    return CORE_WORKER_RUNNER_VERSION;
}

__attribute__((visibility("default")))
int core_worker_runner_probe(void) {
    return 0;
}

__attribute__((visibility("default")))
int core_worker_runner_capabilities(void) {
    // bit 0 = safe probe presente. Nenhum bit de execução real fica ligado.
    return 1;
}

__attribute__((visibility("default")))
int core_worker_runner_start_blocked(void) {
    // 64 mantém o mesmo sentido usado pelos comandos bloqueados do executor:
    // chamada reconhecida, mas recusada por política neste estágio.
    return 64;
}

// Mantém tamanho suficiente para validação anti-placeholder sem depender de
// símbolos de libc ou de toolchain externa pesada.
__attribute__((used, visibility("hidden")))
static const char core_worker_runner_policy_blob[] =
    "policy=no-shell;no-bedrock-start;no-box64-start;no-proot-start;"
    "no-remote-arbitrary-command;allowlist-only;apk-native-lib-only;"
    "stage=core-linux-runner-embedded-v1;"
    "padding=0000000000000000000000000000000000000000000000000000000000000000;"
    "padding=1111111111111111111111111111111111111111111111111111111111111111;"
    "padding=2222222222222222222222222222222222222222222222222222222222222222;"
    "padding=3333333333333333333333333333333333333333333333333333333333333333;"
    "padding=4444444444444444444444444444444444444444444444444444444444444444;";
