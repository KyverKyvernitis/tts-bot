package dev.core.worker;

import android.content.Context;
import android.os.Build;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.Comparator;
import java.util.List;

/**
 * Runtime mínimo do Core Linux sem Termux.
 *
 * Esta classe mantém a etapa v1 segura: cria/valida apenas um rootfs scaffold
 * controlado no armazenamento privado do app e compõe o smoke test com o executor
 * JNI allowlist. Não baixa binários, não abre shell, não inicia Bedrock e não
 * substitui o runner real por comandos arbitrários vindos da VPS.
 */
public final class CoreLinuxRuntimeManager {
    private static final String ROOTFS_MANIFEST_SCHEMA = "core-worker-rootfs-manifest-v1";
    private static final String ROOTFS_KIND = "core-worker-rootfs-scaffold";
    private static final String ROOTFS_REAL_KIND = "core-worker-rootfs-real";
    private static final long MIN_RECOMMENDED_FREE_BYTES = 512L * 1024L * 1024L;
    private static final int TEXT_LIMIT = 12 * 1024;

    private CoreLinuxRuntimeManager() {}

    public static JSONObject rootfsSnapshot(Context context, File coreLinuxDir, String action) {
        try {
            String safeAction = clean(action, 80);
            if (safeAction.isEmpty()) safeAction = "status";
            Layout layout = new Layout(resolveCoreLinuxDir(context, coreLinuxDir));
            ensureBase(layout);
            if ("clean_staging".equals(safeAction) || "cleanup_staging".equals(safeAction)) {
                removeTree(layout.staging);
                JSONObject state = status(layout, safeAction);
                state.put("summary", "Staging da rootfs limpo; rootfs ativa preservada");
                writeState(layout, state);
                appendLog(layout.rootfsLog, state.optString("summary"));
                return response(layout, state, safeAction);
            }
            if ("prepare".equals(safeAction) || "install".equals(safeAction) || "bootstrap".equals(safeAction)) {
                return response(layout, prepare(layout, false), safeAction);
            }
            if ("repair".equals(safeAction)) {
                return response(layout, prepare(layout, true), safeAction);
            }
            JSONObject state = status(layout, safeAction);
            if ("manifest".equals(safeAction)) {
                JSONObject existingManifest = state.optJSONObject("manifest");
                if (existingManifest == null || existingManifest.length() == 0) {
                    state.put("manifest", manifest(layout.rootfs, now(), "planned"));
                }
            }
            writeState(layout, state);
            appendLog(layout.validateLog, state.optString("summary"));
            return response(layout, state, safeAction);
        } catch (Throwable exc) {
            return error("core_linux_rootfs", exc);
        }
    }

    public static JSONObject runtimeSnapshot(Context context, File coreLinuxDir, String action, JSONObject nativeExecutor) {
        try {
            String safeAction = clean(action, 80);
            if (safeAction.isEmpty()) safeAction = "status";
            Layout layout = new Layout(resolveCoreLinuxDir(context, coreLinuxDir));
            ensureBase(layout);
            JSONObject rootfsState = status(layout, safeAction);
            JSONObject executor = nativeExecutor == null ? readJson(new File(layout.runtime, "native-executor-state.json")) : nativeExecutor;
            if (executor == null) executor = new JSONObject();
            boolean executorReady = executor.optBoolean("readyForRootfs", false);
            boolean rootfsReady = rootfsState.optBoolean("rootfsReady", false);
            boolean dirsReady = layout.core.isDirectory() && layout.runtime.isDirectory() && layout.logs.isDirectory() && layout.rootfs.exists();
            JSONArray blockers = new JSONArray();
            if (!executorReady) blockers.put("executor nativo ainda não testado");
            if (!rootfsReady) blockers.put("rootfs scaffold ainda não validado");
            if (!dirsReady) blockers.put("layout core-linux incompleto");
            JSONObject state = new JSONObject();
            state.put("ok", blockers.length() == 0);
            state.put("action", safeAction);
            state.put("state", blockers.length() == 0 ? "runtime_v1_ready" : "runtime_v1_pending");
            state.put("stage", "core-linux-runtime-v1");
            state.put("coreLinuxDir", path(layout.core));
            state.put("termuxRequired", false);
            state.put("bedrockStartAllowed", false);
            state.put("pythonRequired", false);
            state.put("rootfsReady", rootfsReady);
            state.put("executorReady", executorReady);
            state.put("dirsReady", dirsReady);
            state.put("androidSdk", Build.VERSION.SDK_INT);
            state.put("rootfs", rootfsState);
            state.put("nativeExecutor", executor);
            state.put("blockers", blockers);
            state.put("warnings", new JSONArray().put("v1 é smoke test seguro; Box64/Bedrock real continuam bloqueados"));
            state.put("updatedAt", now());
            state.put("summary", blockers.length() == 0
                    ? "Core Linux Runtime v1 pronto para próximos testes sem Termux"
                    : "Core Linux Runtime v1 pendente · " + blockers.optString(0));
            writeJson(new File(layout.runtime, "linux-runtime-state.json"), state);
            return state;
        } catch (Throwable exc) {
            return error("core_linux_runtime", exc);
        }
    }

    public static JSONObject smokeTest(Context context, File coreLinuxDir, JSONObject nativeExecutor) {
        try {
            Layout layout = new Layout(resolveCoreLinuxDir(context, coreLinuxDir));
            ensureBase(layout);
            JSONObject executor = nativeExecutor;
            if (executor == null || !executor.optBoolean("readyForRootfs", false)) {
                executor = CoreWorkerNativeExecutor.snapshot(context, layout.core, "test");
            }
            JSONObject rootfs = rootfsSnapshot(context, layout.core, "prepare");
            JSONObject rootfsState = rootfs.optJSONObject("rootfs");
            if (rootfsState == null) rootfsState = rootfs;
            JSONObject runtime = runtimeSnapshot(context, layout.core, "smoke_test", executor);
            JSONArray checks = new JSONArray()
                    .put(check("core-linux dir", layout.core.isDirectory(), path(layout.core)))
                    .put(check("executor JNI allowlist", executor.optBoolean("readyForRootfs", false), executor.optString("summary", "")))
                    .put(check("rootfs scaffold", rootfsState.optBoolean("rootfsReady", false), rootfsState.optString("summary", "")))
                    .put(check("runtime state", runtime.optBoolean("ok", false), runtime.optString("summary", "")))
                    .put(check("Bedrock bloqueado", true, "nenhum start real é feito nesta etapa"));
            boolean ok = true;
            for (int i = 0; i < checks.length(); i++) {
                JSONObject row = checks.optJSONObject(i);
                if (row != null && !row.optBoolean("ok", false)) ok = false;
            }
            JSONObject out = new JSONObject();
            out.put("ok", ok);
            out.put("type", "core_linux_runtime_smoke_test");
            out.put("state", ok ? "smoke_test_ok" : "smoke_test_pending");
            out.put("stage", "core-linux-runtime-v1");
            out.put("termuxTouched", false);
            out.put("pythonTouched", false);
            out.put("serviceStarted", false);
            out.put("bedrockStarted", false);
            out.put("checks", checks);
            out.put("nativeExecutor", executor);
            out.put("rootfs", rootfsState);
            out.put("runtime", runtime);
            out.put("updatedAt", now());
            out.put("summary", ok
                    ? "Smoke test Core Linux v1 ok · executor + rootfs sem Termux"
                    : "Smoke test Core Linux v1 pendente · revise executor/rootfs");
            writeJson(new File(layout.runtime, "core-linux-smoke-test.json"), out);
            appendLog(new File(layout.logs, "core-linux-smoke-test.log"), out.optString("summary"));
            return out;
        } catch (Throwable exc) {
            return error("core_linux_smoke_test", exc);
        }
    }

    private static JSONObject prepare(Layout layout, boolean repair) throws Exception {
        if (hasRealExistingRootfs(layout.rootfs)) {
            JSONObject state = status(layout, repair ? "repair_blocked_real_rootfs" : "prepare_blocked_real_rootfs");
            state.put("ok", true);
            state.put("state", "rootfs_real_validated");
            state.put("summary", "Rootfs real já importado; scaffold não sobrescrito");
            state.put("blockers", new JSONArray());
            writeState(layout, state);
            appendLog(layout.rootfsLog, state.optString("summary"));
            return state;
        }
        if (hasUnknownExistingRootfs(layout.rootfs)) {
            JSONObject state = status(layout, repair ? "repair_blocked_unknown_rootfs" : "prepare_blocked_unknown_rootfs");
            state.put("ok", false);
            state.put("state", "rootfs_repair_needed");
            state.put("summary", "Rootfs existente desconhecido; não sobrescrevi automaticamente");
            state.put("blockers", new JSONArray().put("rootfs existente não foi criada pelo Core Worker"));
            writeState(layout, state);
            appendLog(layout.rootfsLog, state.optString("summary"));
            return state;
        }
        removeTree(layout.staging);
        layout.staging.mkdirs();
        appendLog(layout.rootfsLog, "criando rootfs staging controlado");
        JSONObject manifest = createScaffold(layout.staging, repair ? "internal-scaffold-repair" : "internal-scaffold");
        JSONObject validation = validate(layout.staging);
        if (!validation.optBoolean("ok", false)) {
            JSONObject state = status(layout, repair ? "repair_failed" : "prepare_failed");
            state.put("ok", false);
            state.put("state", "rootfs_validation_failed");
            state.put("summary", "Rootfs staging falhou na validação");
            state.put("validation", validation);
            state.put("blockers", validation.optJSONArray("missing"));
            writeState(layout, state);
            appendLog(layout.rootfsLog, state.optString("summary"));
            return state;
        }
        if (layout.rootfs.exists()) removeTree(layout.rootfs);
        layout.rootfs.getParentFile().mkdirs();
        if (!layout.staging.renameTo(layout.rootfs)) {
            copyTree(layout.staging, layout.rootfs);
            removeTree(layout.staging);
        }
        JSONObject state = status(layout, repair ? "repair" : "prepare");
        state.put("ok", true);
        state.put("state", "rootfs_validated");
        state.put("summary", "Rootfs scaffold validado · pronto para smoke test Core Linux v1");
        state.put("manifest", manifest);
        state.put("preparedAt", now());
        writeState(layout, state);
        appendLog(layout.rootfsLog, state.optString("summary"));
        return state;
    }

    private static JSONObject status(Layout layout, String action) throws Exception {
        JSONObject validation = validate(layout.rootfs);
        boolean ok = validation.optBoolean("ok", false);
        JSONObject previous = readJson(new File(layout.runtime, "rootfs-state.json"));
        JSONObject state = previous == null ? new JSONObject() : previous;
        state.put("schema", "core-worker-rootfs-state-v1");
        state.put("ok", ok);
        state.put("action", action == null ? "status" : action);
        state.put("state", layout.rootfs.exists() ? validation.optString("state", "rootfs_validation_failed") : "rootfs_missing");
        state.put("rootfsReady", ok);
        String level = validation.optString("validationLevel", "scaffold");
        boolean distributionReady = validation.optBoolean("distributionReady", false);
        state.put("validationLevel", level);
        state.put("distributionReady", distributionReady);
        state.put("readyForBox64Install", ok);
        state.put("readyForBedrockStart", false);
        state.put("rootfsDir", path(layout.rootfs));
        state.put("stagingDir", path(layout.staging));
        state.put("freeBytes", Math.max(0L, layout.core.getUsableSpace()));
        state.put("storageOk", layout.core.getUsableSpace() <= 0L || layout.core.getUsableSpace() >= MIN_RECOMMENDED_FREE_BYTES);
        state.put("recommendedFreeBytes", MIN_RECOMMENDED_FREE_BYTES);
        state.put("manifest", validation.optJSONObject("manifest") == null ? new JSONObject() : validation.optJSONObject("manifest"));
        state.put("validation", validation);
        state.put("blockers", ok ? new JSONArray() : new JSONArray().put("real".equals(level) ? "rootfs real pendente/invalidado" : "rootfs scaffold pendente/invalidado"));
        state.put("warnings", "real".equals(level)
                ? new JSONArray().put("rootfs real validado; execução de binários importados segue bloqueada nesta etapa").put("Bedrock/Box64/shell livre continuam bloqueados")
                : new JSONArray().put("rootfs atual é scaffold controlado; Ubuntu/Box64/Bedrock ficam para etapas futuras"));
        state.put("updatedAt", now());
        state.put("summary", ok
                ? ("real".equals(level) ? "Rootfs real validado · runner real ainda bloqueado" : "Rootfs scaffold validado")
                : ("real".equals(level) ? "Rootfs real pendente · importar/validar no APK" : "Rootfs scaffold pendente · preparar/validar no APK"));
        return state;
    }

    private static JSONObject validate(File rootfs) throws Exception {
        JSONObject manifest = readJson(new File(rootfs, ".core-worker-rootfs-manifest.json"));
        if (manifest == null) manifest = new JSONObject();
        JSONObject checks = new JSONObject();
        checks.put("rootfsDir", rootfs.exists() && rootfs.isDirectory());
        checks.put("readyMarker", new File(rootfs, ".core-worker-rootfs-ready").exists());
        checks.put("manifest", manifest.length() > 0);
        checks.put("manifestSchema", ROOTFS_MANIFEST_SCHEMA.equals(manifest.optString("schema", "")));
        String kind = manifest.optString("kind", "");
        boolean realRootfs = ROOTFS_REAL_KIND.equals(kind);
        boolean scaffoldRootfs = ROOTFS_KIND.equals(kind);
        checks.put("manifestKind", scaffoldRootfs || realRootfs);
        checks.put("etcOsRelease", new File(rootfs, "etc/os-release").exists());
        if (realRootfs) {
            checks.put("binOrUsrBin", new File(rootfs, "bin").exists() || new File(rootfs, "usr/bin").isDirectory());
        } else {
            checks.put("binDir", new File(rootfs, "bin").isDirectory());
            checks.put("binShMarker", new File(rootfs, "bin/sh").exists());
            checks.put("usrBinDir", new File(rootfs, "usr/bin").isDirectory());
        }
        checks.put("tmpDir", new File(rootfs, "tmp").isDirectory());
        checks.put("homeCoreDir", new File(rootfs, "home/core").isDirectory());
        checks.put("varLogDir", new File(rootfs, "var/log").isDirectory());
        checks.put("policy", new File(rootfs, "opt/core-worker/rootfs-policy.json").exists());
        JSONArray missing = new JSONArray();
        JSONArray names = checks.names();
        if (names != null) {
            for (int i = 0; i < names.length(); i++) {
                String key = names.optString(i, "");
                if (!checks.optBoolean(key, false)) missing.put(key);
            }
        }
        boolean ok = missing.length() == 0;
        JSONObject out = new JSONObject();
        out.put("ok", ok);
        out.put("rootfsReady", ok);
        out.put("state", ok ? "rootfs_validated" : "rootfs_validation_failed");
        String level = ROOTFS_REAL_KIND.equals(manifest.optString("kind", "")) ? "real" : "scaffold";
        out.put("validationLevel", level);
        out.put("distributionReady", ok && "real".equals(level));
        out.put("readyForBox64Install", ok);
        out.put("readyForBedrockStart", false);
        out.put("checks", checks);
        out.put("missing", missing);
        out.put("manifest", manifest);
        return out;
    }

    private static JSONObject createScaffold(File rootfs, String source) throws Exception {
        long ts = now();
        List<String> dirs = Arrays.asList(
                "bin", "usr/bin", "etc", "tmp", "home/core", "var/log", "run", "opt/core-worker"
        );
        for (String dir : dirs) new File(rootfs, dir).mkdirs();
        JSONObject manifest = manifest(rootfs, ts, source);
        writeJson(new File(rootfs, ".core-worker-rootfs-manifest.json"), manifest);
        writeJson(new File(rootfs, "opt/core-worker/rootfs-policy.json"), manifest.optJSONObject("policy"));
        writeText(new File(rootfs, "etc/os-release"), "NAME=\"Core Worker Internal Rootfs Scaffold\"\nID=core-worker-rootfs\nVERSION_ID=\"0.2\"\nPRETTY_NAME=\"Core Worker Internal Rootfs Scaffold 0.2\"\n");
        writeText(new File(rootfs, "bin/sh"), "Core Worker rootfs marker. This is not an executable Android shell.\n");
        writeText(new File(rootfs, "usr/bin/env"), "Core Worker rootfs marker. This is not an executable Android binary.\n");
        writeText(new File(rootfs, "README.core-worker-rootfs.txt"), "Rootfs scaffold do Core Linux Runtime v1. Valida layout/estado sem Termux, sem shell livre e sem iniciar Bedrock.\n");
        writeText(new File(rootfs, ".core-worker-rootfs-ready"), "readyAt=" + ts + "\nkind=" + ROOTFS_KIND + "\nstage=core-linux-runtime-v1\n");
        return manifest;
    }

    private static JSONObject manifest(File rootfs, long ts, String source) throws Exception {
        JSONObject policy = new JSONObject();
        policy.put("noFreeShell", true);
        policy.put("noRemoteArbitraryCommand", true);
        policy.put("noAutoDownload", true);
        policy.put("noBedrockStart", true);
        policy.put("appSpecificStorage", true);
        policy.put("termuxFallbackOnly", true);
        policy.put("runtimeV1SmokeOnly", true);
        JSONObject layout = new JSONObject();
        for (String item : Arrays.asList("bin", "usr/bin", "etc", "tmp", "home/core", "var/log", "run", "opt/core-worker")) {
            layout.put(item, true);
        }
        return new JSONObject()
                .put("schema", ROOTFS_MANIFEST_SCHEMA)
                .put("kind", ROOTFS_KIND)
                .put("version", 2)
                .put("name", "Core Linux internal rootfs scaffold")
                .put("source", source == null ? "internal-scaffold" : source)
                .put("createdAt", ts)
                .put("updatedAt", ts)
                .put("arch", "aarch64")
                .put("distribution", "core-worker-scaffold")
                .put("distributionReady", false)
                .put("readyForBox64Install", true)
                .put("readyForBedrockStart", false)
                .put("path", path(rootfs))
                .put("policy", policy)
                .put("layout", layout)
                .put("notes", new JSONArray()
                        .put("Scaffold validado para provar runtime interno sem Termux.")
                        .put("Box64, Ubuntu real e Bedrock ficam bloqueados para patches futuros."));
    }

    private static JSONObject response(Layout layout, JSONObject state, String action) throws Exception {
        JSONObject out = new JSONObject();
        out.put("ok", state.optBoolean("ok", false));
        out.put("component", "core_linux_rootfs");
        out.put("summary", state.optString("summary", "Rootfs interno atualizado"));
        out.put("state", state.optString("state", "unknown"));
        out.put("action", action == null ? "status" : action);
        out.put("rootfsReady", state.optBoolean("rootfsReady", false));
        out.put("readyForBox64Install", state.optBoolean("readyForBox64Install", false));
        out.put("readyForBedrockStart", false);
        out.put("termuxTouched", false);
        out.put("pythonTouched", false);
        out.put("serviceStarted", false);
        out.put("rootfs", state);
        out.put("rootfsDir", path(layout.rootfs));
        out.put("statePath", path(new File(layout.runtime, "rootfs-state.json")));
        out.put("manifestPath", path(new File(layout.manifests, "rootfs-manifest.json")));
        out.put("logs", new JSONObject().put("install", path(layout.rootfsLog)).put("validate", path(layout.validateLog)));
        out.put("size", dirSize(layout.core, 900));
        out.put("validationLevel", state.optString("validationLevel", ""));
        out.put("distributionReady", state.optBoolean("distributionReady", false));
        out.put("safety", "rootfs app-specific; sem shell livre, sem executar binários importados, sem iniciar Bedrock");
        return out;
    }

    private static JSONObject check(String label, boolean ok, String detail) throws Exception {
        return new JSONObject().put("label", label).put("ok", ok).put("detail", clean(detail, 240));
    }

    private static void writeState(Layout layout, JSONObject state) throws Exception {
        writeJson(new File(layout.runtime, "rootfs-state.json"), state);
        JSONObject manifest = state.optJSONObject("manifest");
        if (manifest == null) manifest = new JSONObject();
        writeJson(new File(layout.manifests, "rootfs-manifest.json"), manifest);
    }

    private static boolean hasRealExistingRootfs(File rootfs) {
        if (rootfs == null || !rootfs.exists()) return false;
        JSONObject manifest = readJson(new File(rootfs, ".core-worker-rootfs-manifest.json"));
        return manifest != null && ROOTFS_REAL_KIND.equals(manifest.optString("kind", ""));
    }

    private static boolean hasUnknownExistingRootfs(File rootfs) {
        if (!rootfs.exists()) return false;
        File[] files = rootfs.listFiles();
        if (files == null || files.length == 0) return false;
        JSONObject manifest = readJson(new File(rootfs, ".core-worker-rootfs-manifest.json"));
        if (manifest != null && (ROOTFS_KIND.equals(manifest.optString("kind", "")) || ROOTFS_REAL_KIND.equals(manifest.optString("kind", "")))) return false;
        return !(new File(rootfs, ".core-worker-rootfs-ready").exists()
                && new File(rootfs, "README.core-worker-rootfs.txt").exists());
    }

    private static File resolveCoreLinuxDir(Context context, File provided) {
        if (provided != null) return provided;
        return new File(context.getFilesDir(), "core-linux");
    }

    private static void ensureBase(Layout layout) {
        layout.core.mkdirs();
        layout.runtime.mkdirs();
        layout.logs.mkdirs();
        layout.manifests.mkdirs();
        layout.downloads.mkdirs();
        layout.bedrock.mkdirs();
        layout.bin.mkdirs();
        layout.scripts.mkdirs();
        try {
            writeJson(new File(layout.core, "runtime-marker.json"), new JSONObject()
                    .put("schema", "core-linux-runtime-marker-v1")
                    .put("stage", "core-linux-runtime-v1")
                    .put("termuxRequired", false)
                    .put("updatedAt", now()));
        } catch (Throwable ignored) {
        }
    }

    private static void copyTree(File src, File dst) throws Exception {
        if (src.isDirectory()) {
            if (!dst.exists()) dst.mkdirs();
            File[] files = src.listFiles();
            if (files != null) {
                for (File child : files) copyTree(child, new File(dst, child.getName()));
            }
        } else {
            File parent = dst.getParentFile();
            if (parent != null) parent.mkdirs();
            try (FileInputStream in = new FileInputStream(src); FileOutputStream out = new FileOutputStream(dst)) {
                byte[] buf = new byte[8192];
                int n;
                while ((n = in.read(buf)) >= 0) out.write(buf, 0, n);
            }
        }
    }

    private static void removeTree(File file) {
        if (file == null || !file.exists()) return;
        File[] files = file.listFiles();
        if (files != null) {
            Arrays.sort(files, Comparator.comparing(File::getAbsolutePath).reversed());
            for (File child : files) removeTree(child);
        }
        //noinspection ResultOfMethodCallIgnored
        file.delete();
    }

    private static JSONObject readJson(File file) {
        try {
            if (file == null || !file.exists()) return null;
            byte[] raw = readBytes(file, TEXT_LIMIT * 4);
            return new JSONObject(new String(raw, StandardCharsets.UTF_8));
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static byte[] readBytes(File file, int limit) throws Exception {
        try (FileInputStream in = new FileInputStream(file)) {
            byte[] buf = new byte[Math.max(1, Math.min(limit, (int) Math.max(1, file.length())) )];
            int n = in.read(buf);
            if (n <= 0) return new byte[0];
            if (n == buf.length) return buf;
            return Arrays.copyOf(buf, n);
        }
    }

    private static void writeJson(File file, JSONObject obj) throws Exception {
        writeText(file, obj == null ? "{}" : obj.toString(2));
    }

    private static void writeText(File file, String value) throws Exception {
        File parent = file.getParentFile();
        if (parent != null) parent.mkdirs();
        try (FileOutputStream out = new FileOutputStream(file, false)) {
            out.write(String.valueOf(value == null ? "" : value).getBytes(StandardCharsets.UTF_8));
        }
    }

    private static void appendLog(File file, String line) {
        try {
            File parent = file.getParentFile();
            if (parent != null) parent.mkdirs();
            try (FileOutputStream out = new FileOutputStream(file, true)) {
                String text = "[" + now() + "] " + clean(line, 1200) + "\n";
                out.write(text.getBytes(StandardCharsets.UTF_8));
            }
        } catch (Throwable ignored) {
        }
    }

    private static JSONObject dirSize(File dir, int maxFiles) throws Exception {
        long[] acc = new long[]{0L, 0L};
        accumulate(dir, acc, Math.max(1, maxFiles));
        return new JSONObject().put("bytes", acc[0]).put("files", acc[1]).put("limited", acc[1] >= Math.max(1, maxFiles));
    }

    private static void accumulate(File f, long[] acc, int maxFiles) {
        if (f == null || !f.exists() || acc[1] >= maxFiles) return;
        if (f.isFile()) {
            acc[0] += Math.max(0L, f.length());
            acc[1] += 1L;
            return;
        }
        File[] files = f.listFiles();
        if (files == null) return;
        for (File child : files) {
            if (acc[1] >= maxFiles) break;
            accumulate(child, acc, maxFiles);
        }
    }

    private static JSONObject error(String component, Throwable exc) {
        JSONObject out = new JSONObject();
        try {
            out.put("ok", false);
            out.put("component", component);
            out.put("state", "error");
            out.put("summary", "falha no " + component + ": " + shortThrowable(exc));
            out.put("error", shortThrowable(exc));
            out.put("termuxTouched", false);
            out.put("serviceStarted", false);
        } catch (Throwable ignored) {
        }
        return out;
    }

    private static String clean(String value, int limit) {
        String text = String.valueOf(value == null ? "" : value)
                .replace((char) 0, ' ')
                .replace('\r', ' ')
                .trim();
        text = text.replaceAll("(?i)(token|authorization|bearer|secret|password|passwd)[=: ]+[^\\s]+", "$1=[redacted]");
        if (text.length() > limit) text = text.substring(0, Math.max(0, limit)) + "…";
        return text;
    }

    private static String shortThrowable(Throwable exc) {
        if (exc == null) return "erro desconhecido";
        String msg = exc.getMessage();
        return exc.getClass().getSimpleName() + (msg == null || msg.isEmpty() ? "" : ": " + clean(msg, 180));
    }

    private static long now() {
        return System.currentTimeMillis();
    }

    private static String path(File file) {
        try {
            return file == null ? "" : file.getAbsolutePath();
        } catch (Throwable ignored) {
            return "";
        }
    }

    private static final class Layout {
        final File core;
        final File rootfs;
        final File staging;
        final File runtime;
        final File logs;
        final File manifests;
        final File downloads;
        final File bedrock;
        final File bin;
        final File scripts;
        final File rootfsLog;
        final File validateLog;

        Layout(File core) {
            this.core = core;
            this.rootfs = new File(core, "rootfs");
            this.staging = new File(new File(core, "staging"), "rootfs-next");
            this.runtime = new File(core, "runtime");
            this.logs = new File(core, "logs");
            this.manifests = new File(core, "manifests");
            this.downloads = new File(core, "downloads");
            this.bedrock = new File(core, "bedrock");
            this.bin = new File(core, "bin");
            this.scripts = new File(core, "scripts");
            this.rootfsLog = new File(logs, "rootfs-install.log");
            this.validateLog = new File(logs, "rootfs-validate.log");
        }
    }
}
