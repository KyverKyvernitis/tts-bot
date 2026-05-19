package dev.core.worker;

import android.content.Context;
import android.content.pm.ApplicationInfo;
import android.os.Build;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/**
 * Executor nativo controlado do Core Linux.
 *
 * Esta classe não abre shell e não executa comandos enviados pela VPS. O APK carrega
 * libcoreworker_executor.so via JNI e expõe apenas uma allowlist local mínima para
 * validar a primeira peça nativa do Core Linux antes de rootfs/Box64/Bedrock reais.
 */
public final class CoreWorkerNativeExecutor {
    private static final long TEST_TIMEOUT_MS = 3500L;
    private static final int OUTPUT_LIMIT = 12 * 1024;
    private static final String LIBRARY_NAME = "coreworker_executor";
    private static final Set<String> ALLOWED_COMMANDS = new HashSet<>(Arrays.asList(
            "version", "echo", "env-info", "fs-probe", "native-ping"
    ));

    private static boolean nativeLoadAttempted = false;
    private static boolean nativeLoaded = false;
    private static String nativeLoadError = "";

    private CoreWorkerNativeExecutor() {}

    private static native String nativeRun(String command, String argument, String workDir);

    public static JSONObject snapshot(Context context, File coreLinuxDir, String action) {
        try {
            String safeAction = action == null || action.trim().isEmpty() ? "probe" : action.trim();
            File base = coreLinuxDir == null ? new File(context.getFilesDir(), "core-linux") : coreLinuxDir;
            File runtime = new File(base, "runtime");
            File logs = new File(base, "logs");
            File bin = new File(base, "bin");
            File staging = new File(base, "staging");
            runtime.mkdirs();
            logs.mkdirs();
            bin.mkdirs();
            staging.mkdirs();

            ApplicationInfo info = context.getApplicationInfo();
            File nativeDir = new File(info == null || info.nativeLibraryDir == null ? "" : info.nativeLibraryDir);
            File dataDir = new File(info == null || info.dataDir == null ? context.getFilesDir().getParent() : info.dataDir);
            long now = System.currentTimeMillis();

            boolean loaded = ensureNativeLoaded();
            JSONArray embeddedCandidates = new JSONArray();
            JSONObject executor = findEmbedded(nativeDir, "executor", new String[]{
                    "libcoreworker_executor.so"
            }, embeddedCandidates);
            JSONObject proot = findEmbedded(nativeDir, "proot", new String[]{
                    "libcoreworker_proot.so",
                    "libproot.so"
            }, embeddedCandidates);
            JSONObject busybox = findEmbedded(nativeDir, "busybox", new String[]{
                    "libcoreworker_busybox.so",
                    "libbusybox.so"
            }, embeddedCandidates);
            JSONObject box64 = findEmbedded(nativeDir, "box64", new String[]{
                    "libcoreworker_box64.so",
                    "libbox64.so"
            }, embeddedCandidates);

            JSONArray downloadedCandidates = new JSONArray();
            addDownloaded(downloadedCandidates, new File(bin, "core-runner"), dataDir);
            addDownloaded(downloadedCandidates, new File(bin, "proot"), dataDir);
            addDownloaded(downloadedCandidates, new File(bin, "busybox"), dataDir);
            addDownloaded(downloadedCandidates, new File(bin, "box64"), dataDir);
            addDownloaded(downloadedCandidates, new File(base, "box64/box64"), dataDir);

            boolean embeddedExecutorPresent = loaded || executor.optBoolean("present");
            boolean embeddedBox64Present = box64.optBoolean("present");
            boolean downloadedExecutableBlocked = hasPresent(downloadedCandidates);

            JSONArray blockers = new JSONArray();
            JSONArray warnings = new JSONArray();
            if (!embeddedExecutorPresent) blockers.put("executor nativo embutido pendente");
            if (embeddedExecutorPresent && !loaded) blockers.put("executor nativo detectado, mas JNI não carregou");
            if (!embeddedBox64Present) warnings.put("Box64 nativo embutido pendente para Bedrock");
            if (downloadedExecutableBlocked && Build.VERSION.SDK_INT >= 29) {
                warnings.put("binários em diretório gravável detectados, mas não executados por restrição Android 10+");
            }

            JSONObject previous = readJson(new File(runtime, "native-executor-state.json"));
            JSONObject test = new JSONObject();
            test.put("attempted", false);
            test.put("ok", false);
            test.put("summary", "teste não executado");
            test.put("exitCode", -1);
            if (previous != null && previous.optBoolean("readyForRootfs", false)) {
                JSONObject previousTest = previous.optJSONObject("test");
                if (previousTest != null && previousTest.optBoolean("ok", false)) {
                    test = previousTest;
                    test.put("reusedPreviousOk", true);
                }
            }

            boolean shouldTest = "test".equals(safeAction)
                    || "native_test".equals(safeAction)
                    || "executor_test".equals(safeAction)
                    || "repair".equals(safeAction);
            if (shouldTest) {
                test = runSelfTest(base);
                writeText(new File(logs, "native-executor-test.log"), test.toString(2) + "\n");
                if (!test.optBoolean("ok", false)) {
                    blockers.put("teste do executor nativo falhou");
                }
            }

            boolean testOk = test.optBoolean("ok", false);
            boolean testAttempted = test.optBoolean("attempted", false);
            String state;
            if (!embeddedExecutorPresent) {
                state = "executor_missing";
            } else if (!loaded || (testAttempted && !testOk)) {
                state = "executor_test_failed";
            } else if (testOk && shouldTest) {
                state = "executor_test_ok";
            } else if (testOk) {
                state = "ready_for_rootfs";
            } else {
                state = "executor_detected";
            }
            boolean readyForRootfs = embeddedExecutorPresent && loaded && testOk;

            JSONObject payload = new JSONObject();
            payload.put("ok", embeddedExecutorPresent && loaded && (!testAttempted || testOk));
            payload.put("state", state);
            payload.put("readinessState", readyForRootfs ? "ready_for_rootfs" : state);
            payload.put("action", safeAction);
            payload.put("readyForRootfs", readyForRootfs);
            payload.put("nativeLibraryDir", path(nativeDir));
            payload.put("coreLinuxDir", path(base));
            payload.put("androidSdk", Build.VERSION.SDK_INT);
            payload.put("primaryAbi", Build.SUPPORTED_ABIS != null && Build.SUPPORTED_ABIS.length > 0 ? Build.SUPPORTED_ABIS[0] : "");
            JSONArray abis = new JSONArray();
            if (Build.SUPPORTED_ABIS != null) {
                for (String abi : Build.SUPPORTED_ABIS) abis.put(abi);
            }
            payload.put("supportedAbis", abis);
            payload.put("embeddedExecutorPresent", embeddedExecutorPresent);
            payload.put("embeddedBox64Present", embeddedBox64Present);
            payload.put("executor", executor);
            payload.put("proot", proot);
            payload.put("busybox", busybox);
            payload.put("box64", box64);
            payload.put("embeddedCandidates", embeddedCandidates);
            payload.put("downloadedCandidates", downloadedCandidates);
            payload.put("downloadedExecutableBlocked", downloadedExecutableBlocked && Build.VERSION.SDK_INT >= 29);
            payload.put("nativeBridge", nativeBridgeSnapshot(loaded));
            payload.put("allowlist", new JSONArray().put("version").put("echo").put("env-info").put("fs-probe").put("native-ping"));
            payload.put("test", test);
            payload.put("blockers", blockers);
            payload.put("warnings", warnings);
            payload.put("updatedAt", now);
            payload.put("summary", summary(state, readyForRootfs, blockers, warnings));
            payload.put("safety", "JNI allowlist nativa; sem shell livre; sem ProcessBuilder para comando remoto; não executa binários baixados do app home");

            writeJson(new File(runtime, "native-executor-state.json"), payload);
            writeJson(new File(runtime, "native-runtime-state.json"), payload);
            return payload;
        } catch (Throwable exc) {
            JSONObject err = new JSONObject();
            try {
                err.put("ok", false);
                err.put("state", "executor_test_failed");
                err.put("summary", "falha no executor nativo: " + shortThrowable(exc));
                err.put("error", shortThrowable(exc));
            } catch (Throwable ignored) {}
            return err;
        }
    }

    private static synchronized boolean ensureNativeLoaded() {
        if (nativeLoadAttempted) return nativeLoaded;
        nativeLoadAttempted = true;
        try {
            System.loadLibrary(LIBRARY_NAME);
            nativeLoaded = true;
            nativeLoadError = "";
        } catch (Throwable exc) {
            nativeLoaded = false;
            nativeLoadError = shortThrowable(exc);
        }
        return nativeLoaded;
    }

    private static JSONObject nativeBridgeSnapshot(boolean loaded) throws Exception {
        JSONObject bridge = new JSONObject();
        bridge.put("libraryName", LIBRARY_NAME);
        bridge.put("loaded", loaded);
        bridge.put("loadAttempted", nativeLoadAttempted);
        bridge.put("loadError", nativeLoadError == null ? "" : nativeLoadError);
        bridge.put("mode", "jni-shared-library");
        return bridge;
    }

    private static JSONObject runSelfTest(File workDir) throws Exception {
        JSONArray steps = new JSONArray();
        String[] commands = new String[]{"native-ping", "version", "env-info", "fs-probe", "echo"};
        boolean ok = true;
        int exitCode = 0;
        StringBuilder stdout = new StringBuilder();
        StringBuilder stderr = new StringBuilder();
        long started = System.currentTimeMillis();
        for (String command : commands) {
            String arg = "echo".equals(command) ? "core-worker-native-executor" : "";
            JSONObject step = runAllowedNative(command, arg, workDir, TEST_TIMEOUT_MS);
            steps.put(step);
            if (!step.optBoolean("ok", false)) {
                ok = false;
                if (exitCode == 0) exitCode = step.optInt("exitCode", 1);
            }
            String out = step.optString("stdout", "");
            String err = step.optString("stderr", "");
            if (!out.isEmpty()) stdout.append("[").append(command).append("] ").append(out).append('\n');
            if (!err.isEmpty()) stderr.append("[").append(command).append("] ").append(err).append('\n');
        }
        JSONObject result = new JSONObject();
        result.put("attempted", true);
        result.put("ok", ok);
        result.put("summary", ok ? "executor nativo respondeu à allowlist JNI" : "executor nativo falhou em pelo menos um teste allowlist");
        result.put("exitCode", ok ? 0 : exitCode);
        result.put("stdout", sanitize(stdout.toString(), OUTPUT_LIMIT));
        result.put("stderr", sanitize(stderr.toString(), OUTPUT_LIMIT));
        result.put("steps", steps);
        result.put("durationMs", System.currentTimeMillis() - started);
        return result;
    }

    private static JSONObject runAllowedNative(final String command, final String argument, final File workDir, long timeoutMs) throws Exception {
        JSONObject out = new JSONObject();
        long started = System.currentTimeMillis();
        out.put("command", command);
        out.put("attempted", true);
        out.put("timeoutMs", timeoutMs);
        if (!ALLOWED_COMMANDS.contains(command)) {
            out.put("ok", false);
            out.put("exitCode", 126);
            out.put("summary", "comando bloqueado pela allowlist Java");
            out.put("stdout", "");
            out.put("stderr", "comando não permitido");
            out.put("durationMs", System.currentTimeMillis() - started);
            return out;
        }
        if (!ensureNativeLoaded()) {
            out.put("ok", false);
            out.put("exitCode", 127);
            out.put("summary", "biblioteca JNI não carregada");
            out.put("stdout", "");
            out.put("stderr", nativeLoadError == null ? "" : nativeLoadError);
            out.put("durationMs", System.currentTimeMillis() - started);
            return out;
        }

        ExecutorService executor = Executors.newSingleThreadExecutor();
        Future<String> future = executor.submit(new Callable<String>() {
            @Override
            public String call() {
                return nativeRun(command, argument == null ? "" : argument, workDir == null ? "" : workDir.getAbsolutePath());
            }
        });
        try {
            String raw = future.get(timeoutMs, TimeUnit.MILLISECONDS);
            JSONObject nativeResult = new JSONObject(raw == null || raw.trim().isEmpty() ? "{}" : raw);
            out.put("ok", nativeResult.optBoolean("ok", false));
            out.put("exitCode", nativeResult.optInt("exitCode", nativeResult.optBoolean("ok", false) ? 0 : 1));
            out.put("summary", nativeResult.optBoolean("ok", false) ? "comando allowlist JNI executado" : "comando allowlist JNI falhou");
            out.put("stdout", sanitize(nativeResult.optString("stdout", ""), OUTPUT_LIMIT));
            out.put("stderr", sanitize(nativeResult.optString("stderr", ""), OUTPUT_LIMIT));
            out.put("native", nativeResult);
        } catch (TimeoutException exc) {
            future.cancel(true);
            out.put("ok", false);
            out.put("exitCode", -1);
            out.put("summary", "timeout no comando allowlist JNI");
            out.put("stdout", "");
            out.put("stderr", "timeout após " + timeoutMs + "ms");
        } catch (Throwable exc) {
            out.put("ok", false);
            out.put("exitCode", -1);
            out.put("summary", "falha no comando allowlist JNI: " + shortThrowable(exc));
            out.put("stdout", "");
            out.put("stderr", shortThrowable(exc));
        } finally {
            executor.shutdownNow();
        }
        out.put("durationMs", System.currentTimeMillis() - started);
        return out;
    }

    private static JSONObject findEmbedded(File nativeDir, String kind, String[] names, JSONArray all) throws Exception {
        JSONObject obj = new JSONObject();
        obj.put("kind", kind);
        obj.put("present", false);
        JSONArray expected = new JSONArray();
        for (String name : names) {
            expected.put(name);
            File f = nativeDir == null ? new File(name) : new File(nativeDir, name);
            JSONObject row = new JSONObject();
            row.put("kind", kind);
            row.put("name", name);
            row.put("path", path(f));
            row.put("exists", f.exists());
            row.put("size", f.exists() ? f.length() : 0L);
            row.put("nativeLibraryDir", nativeDir == null ? "" : path(nativeDir));
            all.put(row);
            if (!obj.optBoolean("present") && f.exists()) {
                obj.put("present", true);
                obj.put("name", name);
                obj.put("path", path(f));
                obj.put("size", f.length());
                obj.put("canExecute", f.canExecute());
            }
        }
        obj.put("expectedNames", expected);
        if (!obj.has("name")) obj.put("name", "");
        if (!obj.has("path")) obj.put("path", "");
        return obj;
    }

    private static void addDownloaded(JSONArray arr, File file, File dataDir) throws Exception {
        JSONObject obj = new JSONObject();
        obj.put("name", file.getName());
        obj.put("path", path(file));
        obj.put("exists", file.exists());
        obj.put("size", file.exists() ? file.length() : 0L);
        obj.put("canExecute", file.exists() && file.canExecute());
        obj.put("writableAppHome", isInside(file, dataDir));
        obj.put("blockedByAndroid10Policy", file.exists() && isInside(file, dataDir) && Build.VERSION.SDK_INT >= 29);
        arr.put(obj);
    }

    private static boolean hasPresent(JSONArray arr) {
        for (int i = 0; i < arr.length(); i++) {
            if (arr.optJSONObject(i) != null && arr.optJSONObject(i).optBoolean("exists")) return true;
        }
        return false;
    }

    private static JSONObject readJson(File file) {
        try {
            if (file == null || !file.exists()) return null;
            byte[] raw = java.nio.file.Files.readAllBytes(file.toPath());
            return new JSONObject(new String(raw, StandardCharsets.UTF_8));
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static void writeJson(File file, JSONObject obj) throws Exception {
        writeText(file, obj.toString(2));
    }

    private static void writeText(File file, String value) throws Exception {
        File parent = file.getParentFile();
        if (parent != null) parent.mkdirs();
        FileOutputStream out = new FileOutputStream(file);
        out.write((value == null ? "" : value).getBytes(StandardCharsets.UTF_8));
        out.close();
    }

    private static String summary(String state, boolean readyForRootfs, JSONArray blockers, JSONArray warnings) {
        if (readyForRootfs) return "executor nativo interno pronto para rootfs";
        if ("executor_test_ok".equals(state)) return "executor nativo interno testado";
        if ("executor_detected".equals(state)) return "executor nativo interno detectado; teste pendente";
        if ("executor_test_failed".equals(state)) return "executor nativo interno falhou no teste";
        if (blockers != null && blockers.length() > 0) return "executor nativo interno pendente · " + blockers.optString(0);
        if (warnings != null && warnings.length() > 0) return "executor nativo interno parcial · " + warnings.optString(0);
        return "executor nativo interno atualizado";
    }

    private static boolean isInside(File file, File parent) {
        try {
            if (file == null || parent == null) return false;
            String f = file.getCanonicalPath();
            String p = parent.getCanonicalPath();
            return f.equals(p) || f.startsWith(p + File.separator);
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static String path(File file) {
        try {
            return file == null ? "" : file.getAbsolutePath();
        } catch (Throwable ignored) {
            return "";
        }
    }

    private static String sanitize(String value, int limit) {
        String clean = (value == null ? "" : value)
                .replaceAll("(?i)(token|authorization|bearer|secret|password|passwd|firebase|fcm)[=: ]+[^\\s]+", "$1=[redacted]")
                .replaceAll("([0-9]{1,3}\\.){3}[0-9]{1,3}", "[ip-redacted]")
                .replace('\r', ' ')
                .replace((char) 0, ' ');
        if (clean.length() > limit) clean = clean.substring(0, limit) + "…";
        return clean;
    }

    private static String shortThrowable(Throwable exc) {
        if (exc == null) return "erro desconhecido";
        String msg = exc.getMessage();
        return exc.getClass().getSimpleName() + (msg == null || msg.isEmpty() ? "" : ": " + sanitize(msg, 180));
    }
}
