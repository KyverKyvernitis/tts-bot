package dev.core.worker;

import org.json.JSONObject;

/**
 * Loader seguro do core-runner embutido.
 *
 * Esta classe só tenta carregar a biblioteca nativa própria do APK e expõe um
 * snapshot de presença para o preflight. Não chama funções nativas, não abre
 * shell e não inicia Bedrock/proot/box64/busybox.
 */
public final class CoreWorkerRunnerGuard {
    private static final String LIBRARY_NAME = "coreworker_runner";
    private static boolean loadAttempted = false;
    private static boolean loaded = false;
    private static String loadError = "";

    private CoreWorkerRunnerGuard() {}

    public static synchronized boolean ensureLoaded() {
        if (loadAttempted) return loaded;
        loadAttempted = true;
        try {
            System.loadLibrary(LIBRARY_NAME);
            loaded = true;
            loadError = "";
        } catch (Throwable exc) {
            loaded = false;
            loadError = shortThrowable(exc);
        }
        return loaded;
    }

    public static synchronized JSONObject snapshot() {
        boolean ok = ensureLoaded();
        JSONObject out = new JSONObject();
        try {
            out.put("libraryName", LIBRARY_NAME);
            out.put("loaded", ok);
            out.put("loadAttempted", loadAttempted);
            out.put("loadError", loadError == null ? "" : loadError);
            out.put("mode", "jni-shared-library-safe-probe");
            out.put("stage", "core-runner-guard-v1");
            out.put("startsBedrock", false);
            out.put("opensShell", false);
            out.put("runsExternalBinary", false);
        } catch (Throwable ignored) {}
        return out;
    }

    private static String shortThrowable(Throwable exc) {
        if (exc == null) return "erro desconhecido";
        String msg = exc.getMessage();
        String out = exc.getClass().getSimpleName() + (msg == null || msg.isEmpty() ? "" : ": " + msg);
        out = out.replace('\n', ' ').replace('\r', ' ').trim();
        return out.length() > 220 ? out.substring(0, 219) + "…" : out;
    }
}
