// Core Worker Linux runner guard v1.
//
// This executable is intentionally tiny and safe-by-default.  It is built as a
// PIE ELF and packaged with a .so name so Android extracts it with native libs.
// It does not start Bedrock, Box64, proot, busybox or a free shell.  Future
// patches may add an allowlisted IPC/argv surface here after preflight passes.
#include <stdio.h>
#include <string.h>

static int print_version(void) {
    puts("core-worker-runner 0.1 safe-preflight-only");
    return 0;
}

static int print_probe(void) {
    puts("{\"ok\":true,\"runner\":\"core-worker-runner\",\"mode\":\"safe-preflight-only\",\"bedrockStarted\":false,\"box64Started\":false,\"shellOpened\":false}");
    return 0;
}

int main(int argc, char **argv) {
    if (argc <= 1) {
        return print_version();
    }
    if (strcmp(argv[1], "--version") == 0 || strcmp(argv[1], "version") == 0) {
        return print_version();
    }
    if (strcmp(argv[1], "--probe") == 0 || strcmp(argv[1], "probe") == 0) {
        return print_probe();
    }
    fputs("core-worker-runner: comando bloqueado no estágio safe-preflight-only\n", stderr);
    return 64;
}
