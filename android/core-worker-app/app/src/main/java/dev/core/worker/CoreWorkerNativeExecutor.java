package dev.core.worker;

import android.content.Context;
import android.content.pm.ApplicationInfo;
import android.os.Build;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

/**
 * Executor nativo controlado do Core Linux.
 *
 * Não é shell livre: apenas localiza e testa binários nativos embutidos no APK
 * dentro de nativeLibraryDir. Binários baixados/criados em diretórios graváveis
 * do app são reportados, mas não são executados em Android 10+.
 */
public final class CoreWorkerNativeExecutor {
    private static final long TEST_TIMEOUT_MS = 3500L;
    private static final int OUTPUT_LIMIT = 12 * 1024;

    private CoreWorkerNativeExecutor() {}

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

            JSONArray embeddedCandidates = new JSONArray();
            JSONObject executor = findEmbedded(nativeDir, "executor", new String[]{
                    "libcoreworker_executor.so",
                    "libcoreworker_proot.so",
                    "libcoreworker_busybox.so",
                    "libproot.so",
                    "libbusybox.so"
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

            boolean embeddedExecutorPresent = executor.optBoolean("present") || proot.optBoolean("present") || busybox.optBoolean("present");
            boolean embeddedBox64Present = box64.optBoolean("present");
            boolean downloadedExecutableBlocked = hasPresent(downloadedCandidates);
            JSONArray blockers = new JSONArray();
            JSONArray warnings = new JSONArray();
            if (!embeddedExecutorPresent) blockers.put("executor nativo embutido pendente");
            if (!embeddedBox64Present) warnings.put("Box64 nativo embutido pendente para Bedrock");
            if (downloadedExecutableBlocked && Build.VERSION.SDK_INT >= 29) {
                warnings.put("binários em diretório gravável detectados, mas não executados por restrição Android 10+");
            }

            JSONObject test = new JSONObject();
            test.put("attempted", false);
            test.put("ok", false);
            test.put("summary", "teste não executado");
            test.put("exitCode", -1);
            if ("test".equals(safeAction) || "native_test".equals(safeAction) || "executor_test".equals(safeAction)) {
                test = runAllowedTest(executor, proot, busybox, box64, base);
                writeText(new File(logs, "native-executor-test.log"), test.toString(2) + "\n");
                if (!test.optBoolean("ok", false)) {
                    blockers.put("teste do executor nativo falhou");
                }
            }

            String state = embeddedExecutorPresent ? (test.optBoolean("ok", false) ? "executor_test_ok" : "executor_detected") : "executor_missing";
            if (!embeddedExecutorPresent && downloadedExecutableBlocked && Build.VERSION.SDK_INT >= 29) state = "android_execution_blocked";
            boolean readyForRootfs = embeddedExecutorPresent && (!test.optBoolean("attempted", false) || test.optBoolean("ok", false));

            JSONObject payload = new JSONObject();
            payload.put("ok", embeddedExecutorPresent && (!test.optBoolean("attempted", false) || test.optBoolean("ok", false)));
            payload.put("state", state);
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
            payload.put("test", test);
            payload.put("blockers", blockers);
            payload.put("warnings", warnings);
            payload.put("updatedAt", now);
            payload.put("summary", summary(state, blockers, warnings));
            payload.put("safety", "allowlist nativa; sem shell livre; não executa binários baixados do app home");

            writeJson(new File(runtime, "native-executor-state.json"), payload);
            writeJson(new File(runtime, "native-runtime-state.json"), payload);
            return payload;
        } catch (Throwable exc) {
            JSONObject err = new JSONObject();
            try {
                err.put("ok", false);
                err.put("state", "error");
                err.put("summary", "falha no executor nativo: " + shortThrowable(exc));
                err.put("error", shortThrowable(exc));
            } catch (Throwable ignored) {}
            return err;
        }
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

    private static JSONObject runAllowedTest(JSONObject executor, JSONObject proot, JSONObject busybox, JSONObject box64, File workDir) throws Exception {
        JSONObject candidate = firstPresent(busybox, executor, proot, box64);
        JSONObject out = new JSONObject();
        out.put("attempted", true);
        if (candidate == null || !candidate.optBoolean("present")) {
            out.put("ok", false);
            out.put("summary", "nenhum executor nativo embutido disponível para teste");
            out.put("exitCode", -1);
            return out;
        }
        String path = candidate.optString("path", "");
        String name = candidate.optString("name", "");
        List<String> cmd = new ArrayList<>();
        cmd.add(path);
        if (name.contains("box64")) {
            cmd.add("--version");
        } else if (name.contains("busybox")) {
            cmd.add("--help");
        } else {
            cmd.add("--help");
        }
        out.put("command", new JSONArray(cmd));
        long started = System.currentTimeMillis();
        Process process = null;
        try {
            ProcessBuilder builder = new ProcessBuilder(cmd);
            builder.directory(workDir);
            builder.redirectErrorStream(true);
            process = builder.start();
            boolean finished = process.waitFor(TEST_TIMEOUT_MS, TimeUnit.MILLISECONDS);
            String output = readLimit(process.getInputStream(), OUTPUT_LIMIT);
            if (!finished) {
                process.destroyForcibly();
                out.put("ok", false);
                out.put("summary", "teste do executor nativo excedeu timeout");
                out.put("exitCode", -1);
            } else {
                int exit = process.exitValue();
                out.put("ok", exit == 0 || exit == 1);
                out.put("summary", "executor nativo respondeu ao teste");
                out.put("exitCode", exit);
            }
            out.put("stdout", sanitize(output, OUTPUT_LIMIT));
        } catch (Throwable exc) {
            out.put("ok", false);
            out.put("summary", "falha ao testar executor nativo: " + shortThrowable(exc));
            out.put("error", shortThrowable(exc));
            out.put("exitCode", -1);
        } finally {
            if (process != null) {
                try { process.destroy(); } catch (Throwable ignored) {}
            }
        }
        out.put("durationMs", System.currentTimeMillis() - started);
        return out;
    }

    private static JSONObject firstPresent(JSONObject... objects) {
        for (JSONObject obj : objects) {
            if (obj != null && obj.optBoolean("present")) return obj;
        }
        return null;
    }

    private static String readLimit(InputStream input, int limit) throws Exception {
        if (input == null) return "";
        BufferedReader reader = new BufferedReader(new InputStreamReader(input, StandardCharsets.UTF_8));
        StringBuilder builder = new StringBuilder();
        char[] buf = new char[1024];
        int n;
        while ((n = reader.read(buf)) >= 0) {
            if (builder.length() < limit) {
                int allowed = Math.min(n, limit - builder.length());
                builder.append(buf, 0, allowed);
            }
        }
        return builder.toString();
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

    private static String summary(String state, JSONArray blockers, JSONArray warnings) {
        if ("executor_test_ok".equals(state)) return "executor nativo interno testado";
        if ("executor_detected".equals(state)) return "executor nativo interno detectado";
        if ("android_execution_blocked".equals(state)) return "executor baixado detectado, mas bloqueado pelo Android";
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
                .replace('\r', ' ');
        if (clean.length() > limit) clean = clean.substring(0, limit) + "…";
        return clean;
    }

    private static String shortThrowable(Throwable exc) {
        if (exc == null) return "erro desconhecido";
        String msg = exc.getMessage();
        return exc.getClass().getSimpleName() + (msg == null || msg.isEmpty() ? "" : ": " + sanitize(msg, 180));
    }
}
