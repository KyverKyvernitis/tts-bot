package dev.core.worker;

import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.provider.OpenableColumns;
import android.system.Os;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.DigestInputStream;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.Comparator;
import java.util.Locale;
import java.util.zip.GZIPInputStream;

/**
 * Importador v1 de rootfs real para o Core Linux interno.
 *
 * Segurança do v1:
 * - só usa arquivo escolhido explicitamente pelo usuário via SAF;
 * - calcula SHA-256 do arquivo original selecionado;
 * - extrai sempre em staging;
 * - valida layout antes de promover;
 * - não executa binários importados;
 * - não abre shell livre;
 * - não inicia Bedrock/Box64.
 */
public final class CoreLinuxRootfsImportManager {
    private static final String ROOTFS_MANIFEST_SCHEMA = "core-worker-rootfs-manifest-v1";
    private static final String ROOTFS_REAL_KIND = "core-worker-rootfs-real";
    private static final long MIN_RECOMMENDED_FREE_BYTES = 512L * 1024L * 1024L;
    private static final long MAX_TOTAL_BYTES = 4L * 1024L * 1024L * 1024L;
    private static final long MAX_SINGLE_FILE_BYTES = 2L * 1024L * 1024L * 1024L;
    private static final int MAX_ENTRIES = 80000;
    private static final int TEXT_LIMIT = 64 * 1024;
    private static final String ROOTFS_IMPORT_STAGE_V17 = "core-linux-rootfs-staging-import-v17";
    private static final String ROOTFS_GLIBC_PREFLIGHT_STAGE_V16_2 = "core-linux-rootfs-glibc-intake-preflight-v16.2";

    private CoreLinuxRootfsImportManager() {}

    public static JSONObject status(Context context, File coreLinuxDir) {
        try {
            Layout layout = new Layout(resolveCoreLinuxDir(context, coreLinuxDir));
            ensureBase(layout);
            JSONObject importState = readJson(layout.importStateFile);
            JSONObject rootfsState = readJson(layout.rootfsStateFile);
            JSONObject out = new JSONObject();
            out.put("ok", true);
            out.put("component", "core_linux_rootfs_import");
            out.put("action", "status");
            out.put("state", firstNonEmpty(importState.optString("state", ""), rootfsState.optString("state", "rootfs_import_idle")));
            out.put("summary", firstNonEmpty(importState.optString("summary", ""), rootfsState.optString("summary", "Importação rootfs aguardando arquivo escolhido no APK")));
            out.put("rootfsReady", rootfsState.optBoolean("rootfsReady", false));
            out.put("distributionReady", rootfsState.optBoolean("distributionReady", false));
            out.put("validationLevel", rootfsState.optString("validationLevel", ""));
            out.put("rootfsDir", path(layout.rootfs));
            out.put("stagingDir", path(layout.importStaging));
            out.put("import", importState);
            out.put("rootfs", rootfsState);
            out.put("safety", safetySummary());
            return out;
        } catch (Throwable exc) {
            return error("core_linux_rootfs_import", exc);
        }
    }


    public static JSONObject glibcPreflight(Context context, File coreLinuxDir) {
        try {
            Layout layout = new Layout(resolveCoreLinuxDir(context, coreLinuxDir));
            ensureBase(layout);
            JSONObject validation = validateReal(layout.rootfs);
            JSONObject glibc = validation.optJSONObject("glibcRuntime");
            if (glibc == null) glibc = glibcRuntimeSnapshot(layout.rootfs);
            boolean ok = validation.optBoolean("ok", false) && glibc.optBoolean("ok", false);
            JSONObject rootfsState = readJson(layout.rootfsStateFile);
            JSONObject out = new JSONObject();
            out.put("ok", ok);
            out.put("component", "core_linux_rootfs_glibc_preflight");
            out.put("action", "glibc_preflight");
            out.put("stage", ROOTFS_GLIBC_PREFLIGHT_STAGE_V16_2);
            out.put("state", ok ? "rootfs_glibc_ready_for_box64" : "rootfs_glibc_preflight_missing_runtime");
            out.put("summary", ok
                    ? "Rootfs Linux arm64 com glibc validado · próximo passo pode executar smoke Box64"
                    : "Rootfs V16.2 pendente · falta runtime glibc arm64 para Box64");
            out.put("rootfsReady", ok);
            out.put("distributionReady", ok);
            out.put("validationLevel", ok ? "real-glibc" : validation.optString("validationLevel", "real"));
            out.put("readyForBox64Install", ok);
            out.put("readyForBox64Smoke", ok);
            out.put("readyForBedrockStart", false);
            out.put("rootfsDir", path(layout.rootfs));
            out.put("rootfsState", rootfsState);
            out.put("validation", validation);
            out.put("glibcRuntime", glibc);
            out.put("missing", validation.optJSONArray("missing") == null ? new JSONArray() : validation.optJSONArray("missing"));
            out.put("checks", validation.optJSONObject("checks") == null ? new JSONObject() : validation.optJSONObject("checks"));
            out.put("termuxTouched", false);
            out.put("pythonTouched", false);
            out.put("serviceStarted", false);
            out.put("bedrockStarted", false);
            out.put("box64Started", false);
            out.put("shellOpened", false);
            out.put("remoteCommandAllowed", false);
            out.put("safety", safetySummary());
            out.put("nextStep", ok ? "v17-box64-version-help-smoke" : "importar rootfs Linux arm64 com glibc pelo seletor do APK");
            out.put("updatedAt", now());
            writeJson(layout.importStateFile, out);
            appendLog(layout.importLog, out.optString("summary"));
            return out;
        } catch (Throwable exc) {
            return error("core_linux_rootfs_glibc_preflight", exc);
        }
    }

    public static JSONObject validateActive(Context context, File coreLinuxDir) {
        try {
            Layout layout = new Layout(resolveCoreLinuxDir(context, coreLinuxDir));
            ensureBase(layout);
            JSONObject validation = validateReal(layout.rootfs);
            JSONObject out = new JSONObject();
            out.put("ok", validation.optBoolean("ok", false));
            out.put("component", "core_linux_rootfs_import");
            out.put("action", "validate_active");
            out.put("stage", ROOTFS_IMPORT_STAGE_V17);
            out.put("state", validation.optBoolean("ok", false) ? "rootfs_glibc_ready_for_box64" : "rootfs_real_validation_failed");
            out.put("summary", validation.optBoolean("ok", false)
                    ? "Rootfs real com glibc validado em staging/ativo · próximo passo pode preparar smoke Box64"
                    : "Rootfs real não passou na validação glibc V17");
            out.put("validation", validation);
            out.put("rootfsDir", path(layout.rootfs));
            out.put("safety", safetySummary());
            if (validation.optBoolean("ok", false)) {
                JSONObject state = activeState(layout, validation, readJson(new File(layout.rootfs, ".core-worker-rootfs-manifest.json")));
                writeJson(layout.rootfsStateFile, state);
                out.put("rootfs", state);
            }
            writeJson(layout.importStateFile, out);
            appendLog(layout.importLog, out.optString("summary"));
            return out;
        } catch (Throwable exc) {
            return error("core_linux_rootfs_import_validate", exc);
        }
    }

    public static JSONObject abort(Context context, File coreLinuxDir) {
        try {
            Layout layout = new Layout(resolveCoreLinuxDir(context, coreLinuxDir));
            ensureBase(layout);
            removeTree(layout.importStaging);
            JSONObject state = new JSONObject();
            state.put("ok", true);
            state.put("component", "core_linux_rootfs_import");
            state.put("action", "abort");
            state.put("stage", ROOTFS_IMPORT_STAGE_V17);
            state.put("state", "rootfs_import_aborted");
            state.put("summary", "Importação rootfs cancelada; rootfs ativa preservada");
            state.put("updatedAt", now());
            state.put("safety", safetySummary());
            writeJson(layout.importStateFile, state);
            appendLog(layout.importLog, state.optString("summary"));
            return state;
        } catch (Throwable exc) {
            return error("core_linux_rootfs_import_abort", exc);
        }
    }

    public static JSONObject importFromUri(Context context, File coreLinuxDir, Uri uri, String expectedSha256) {
        Layout layout = new Layout(resolveCoreLinuxDir(context, coreLinuxDir));
        String displayName = displayName(context, uri);
        String expected = normalizeSha256(expectedSha256);
        long started = now();
        try {
            ensureBase(layout);
            if (uri == null) {
                JSONObject out = failure(layout, "rootfs_import_missing_uri", "Nenhum arquivo rootfs foi escolhido", null);
                return out;
            }
            if (!looksLikeTar(displayName)) {
                String lowerName = String.valueOf(displayName == null ? "" : displayName).toLowerCase(Locale.ROOT);
                String summary = (lowerName.endsWith(".tar.xz") || lowerName.endsWith(".txz") || lowerName.endsWith(".tar.zst") || lowerName.endsWith(".tzst"))
                        ? "Formato detectado, mas esta etapa V17 aceita import real somente .tar, .tar.gz ou .tgz; .xz/.zst ficam bloqueados até decompressor auditado"
                        : "Formato não suportado: use .tar, .tar.gz ou .tgz";
                return failure(layout, "rootfs_import_unsupported_format", summary, null);
            }
            if (layout.core.getUsableSpace() > 0L && layout.core.getUsableSpace() < MIN_RECOMMENDED_FREE_BYTES) {
                return failure(layout, "rootfs_import_low_storage", "Espaço livre insuficiente para importar rootfs com segurança", null);
            }

            removeTree(layout.importStaging);
            layout.importStaging.mkdirs();
            JSONObject start = new JSONObject();
            start.put("ok", true);
            start.put("component", "core_linux_rootfs_import");
            start.put("action", "import");
            start.put("stage", ROOTFS_IMPORT_STAGE_V17);
            start.put("state", "rootfs_import_extracting_staging");
            start.put("summary", "Importando rootfs Linux ARM64 com glibc em staging V17");
            start.put("fileName", clean(displayName, 240));
            start.put("expectedSha256Provided", !expected.isEmpty());
            start.put("startedAt", started);
            start.put("safety", safetySummary());
            writeJson(layout.importStateFile, start);
            appendLog(layout.importLog, "iniciando import rootfs: " + displayName);

            writeImportProgress(layout, "rootfs_import_reading", "Lendo arquivo e calculando SHA-256", displayName, null);
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            CoreLinuxRootfsTarExtractor.Stats stats;
            try (InputStream raw = context.getContentResolver().openInputStream(uri)) {
                if (raw == null) {
                    return failure(layout, "rootfs_import_open_failed", "Não consegui abrir o arquivo escolhido", null);
                }
                DigestInputStream digestInput = new DigestInputStream(new BufferedInputStream(raw, 64 * 1024), digest);
                InputStream tarInput = isGzipName(displayName) ? new GZIPInputStream(digestInput, 64 * 1024) : digestInput;
                stats = CoreLinuxRootfsTarExtractor.extractTar(tarInput, layout.importStaging);
                drain(digestInput);
            }
            String actualSha = hex(digest.digest());
            writeImportProgress(layout, "rootfs_import_hash_ready", "SHA-256 calculado; validando arquivo", displayName, actualSha);
            boolean shaVerified = !expected.isEmpty() && expected.equalsIgnoreCase(actualSha);
            if (!expected.isEmpty() && !shaVerified) {
                JSONObject details = new JSONObject().put("expectedSha256", expected).put("actualSha256", actualSha);
                return failure(layout, "rootfs_import_sha256_mismatch", "SHA-256 diferente do esperado; rootfs ativa preservada", details);
            }

            writeImportProgress(layout, "rootfs_import_validating", "Extração concluída; validando layout do rootfs", displayName, actualSha);
            postProcessImportedRootfs(layout.importStaging, displayName, actualSha, !expected.isEmpty(), shaVerified, stats, started);
            JSONObject validation = validateReal(layout.importStaging);
            if (!validation.optBoolean("ok", false)) {
                JSONObject details = new JSONObject().put("validation", validation).put("sha256", actualSha).put("stats", stats.toJson());
                JSONObject out = failure(layout, "rootfs_import_validation_failed", "Rootfs importado não passou na validação; rootfs ativa preservada", details);
                out.put("stagingPreserved", true);
                writeJson(layout.importStateFile, out);
                return out;
            }

            writeImportProgress(layout, "rootfs_import_promoting", "Rootfs validado; promovendo staging para ativo", displayName, actualSha);
            promote(layout);
            JSONObject manifest = readJson(new File(layout.rootfs, ".core-worker-rootfs-manifest.json"));
            JSONObject active = activeState(layout, validation, manifest);
            active.put("sha256", actualSha);
            active.put("sha256Verified", shaVerified);
            active.put("expectedSha256Provided", !expected.isEmpty());
            active.put("stats", stats.toJson());
            writeJson(layout.rootfsStateFile, active);

            JSONObject out = new JSONObject();
            out.put("ok", true);
            out.put("component", "core_linux_rootfs_import");
            out.put("action", "import");
            out.put("stage", ROOTFS_IMPORT_STAGE_V17);
            out.put("state", "rootfs_glibc_ready_for_box64");
            out.put("summary", "Rootfs Linux ARM64 com glibc importado em staging, validado e promovido · pronto para preparar smoke Box64");
            out.put("fileName", clean(displayName, 240));
            out.put("sha256", actualSha);
            out.put("sha256Verified", shaVerified);
            out.put("expectedSha256Provided", !expected.isEmpty());
            out.put("validation", validation);
            out.put("rootfs", active);
            out.put("stats", stats.toJson());
            out.put("rootfsDir", path(layout.rootfs));
            out.put("durationMs", Math.max(0L, now() - started));
            out.put("termuxTouched", false);
            out.put("pythonTouched", false);
            out.put("serviceStarted", false);
            out.put("bedrockStarted", false);
            out.put("safety", safetySummary());
            writeJson(layout.importStateFile, out);
            appendLog(layout.importLog, out.optString("summary") + " sha256=" + actualSha);
            return out;
        } catch (Throwable exc) {
            try {
                JSONObject out = failure(layout, "rootfs_import_error", "Falha ao importar rootfs: " + shortThrowable(exc), null);
                out.put("exception", shortThrowable(exc));
                return out;
            } catch (Throwable ignored) {
                return error("core_linux_rootfs_import", exc);
            }
        }
    }

    private static void promote(Layout layout) throws Exception {
        CoreLinuxRootfsFilesystem.promote(layout.rootfs, layout.importStaging, layout.previousRootfs);
        appendLog(layout.importLog, "rootfs staging promovida para ativa");
    }

    private static void postProcessImportedRootfs(File rootfs, String fileName, String sha, boolean expectedProvided, boolean shaVerified, CoreLinuxRootfsTarExtractor.Stats stats, long started) throws Exception {
        new File(rootfs, "tmp").mkdirs();
        new File(rootfs, "var/log").mkdirs();
        new File(rootfs, "home/core").mkdirs();
        new File(rootfs, "opt/core-worker").mkdirs();
        writeJson(new File(rootfs, ".core-worker-rootfs-manifest.json"), realManifest(rootfs, fileName, sha, expectedProvided, shaVerified, stats, started));
        writeJson(new File(rootfs, "opt/core-worker/rootfs-policy.json"), rootfsPolicy());
        writeText(new File(rootfs, ".core-worker-rootfs-ready"), "readyAt=" + now() + "\nkind=" + ROOTFS_REAL_KIND + "\nstage=core-linux-rootfs-staging-import-v17\n");
        writeText(new File(rootfs, "opt/core-worker/README.imported-rootfs.txt"), "Rootfs real importado pelo Core Worker. Binários importados NÃO são executados nesta etapa; Bedrock/Box64/shell livre continuam bloqueados.\n");
    }

    private static JSONObject validateReal(File rootfs) throws Exception {
        JSONObject manifest = readJson(new File(rootfs, ".core-worker-rootfs-manifest.json"));
        if (manifest == null) manifest = new JSONObject();
        JSONObject checks = new JSONObject();
        checks.put("rootfsDir", rootfs.exists() && rootfs.isDirectory());
        checks.put("readyMarker", new File(rootfs, ".core-worker-rootfs-ready").exists());
        checks.put("manifest", manifest.length() > 0);
        checks.put("manifestSchema", ROOTFS_MANIFEST_SCHEMA.equals(manifest.optString("schema", "")));
        checks.put("manifestKind", ROOTFS_REAL_KIND.equals(manifest.optString("kind", "")));
        checks.put("etcOsRelease", new File(rootfs, "etc/os-release").exists());
        checks.put("binOrUsrBin", new File(rootfs, "bin").exists() || new File(rootfs, "usr/bin").isDirectory());
        checks.put("tmpDir", new File(rootfs, "tmp").isDirectory());
        checks.put("homeCoreDir", new File(rootfs, "home/core").isDirectory());
        checks.put("varLogDir", new File(rootfs, "var/log").isDirectory());
        checks.put("policy", new File(rootfs, "opt/core-worker/rootfs-policy.json").exists());
        checks.put("binSh", new File(rootfs, "bin/sh").exists() || new File(rootfs, "usr/bin/sh").exists());
        JSONObject glibc = glibcRuntimeSnapshot(rootfs);
        checks.put("glibcRuntime", glibc.optBoolean("ok", false));
        JSONArray missing = new JSONArray();
        JSONArray names = checks.names();
        if (names != null) {
            for (int i = 0; i < names.length(); i++) {
                String key = names.optString(i, "");
                if (!checks.optBoolean(key, false)) missing.put(key);
            }
        }
        JSONArray glibcMissing = glibc.optJSONArray("missing");
        if (glibcMissing != null) {
            for (int i = 0; i < glibcMissing.length(); i++) {
                String item = glibcMissing.optString(i, "");
                if (!item.isEmpty()) missing.put(item);
            }
        }
        boolean ok = missing.length() == 0;
        return new JSONObject()
                .put("ok", ok)
                .put("rootfsReady", ok)
                .put("stage", ROOTFS_IMPORT_STAGE_V17)
                .put("state", ok ? "rootfs_glibc_ready_for_box64" : "rootfs_real_validation_failed")
                .put("validationLevel", ok ? "real-glibc" : "real")
                .put("distributionReady", ok)
                .put("readyForBox64Install", ok)
                .put("readyForBox64Smoke", ok)
                .put("readyForBedrockStart", false)
                .put("checks", checks)
                .put("glibcRuntime", glibc)
                .put("missing", missing)
                .put("manifest", manifest);
    }

    private static JSONObject activeState(Layout layout, JSONObject validation, JSONObject manifest) throws Exception {
        boolean ok = validation.optBoolean("ok", false);
        return new JSONObject()
                .put("schema", "core-worker-rootfs-state-v1")
                .put("ok", ok)
                .put("action", "import")
                .put("stage", ROOTFS_IMPORT_STAGE_V17)
                .put("state", ok ? "rootfs_glibc_ready_for_box64" : "rootfs_real_validation_failed")
                .put("rootfsReady", ok)
                .put("validationLevel", ok ? "real-glibc" : "real")
                .put("distributionReady", ok)
                .put("readyForBox64Install", ok)
                .put("readyForBox64Smoke", ok)
                .put("readyForBedrockStart", false)
                .put("termuxRequired", false)
                .put("runnerBlocked", true)
                .put("bedrockStartAllowed", false)
                .put("rootfsDir", path(layout.rootfs))
                .put("stagingDir", path(layout.importStaging))
                .put("freeBytes", Math.max(0L, layout.core.getUsableSpace()))
                .put("storageOk", layout.core.getUsableSpace() <= 0L || layout.core.getUsableSpace() >= MIN_RECOMMENDED_FREE_BYTES)
                .put("recommendedFreeBytes", MIN_RECOMMENDED_FREE_BYTES)
                .put("manifest", manifest == null ? new JSONObject() : manifest)
                .put("validation", validation)
                .put("blockers", ok ? new JSONArray() : validation.optJSONArray("missing"))
                .put("warnings", new JSONArray()
                        .put("rootfs real com glibc validado; execução de binários importados segue bloqueada nesta etapa")
                        .put("Bedrock/Box64/shell livre continuam bloqueados"))
                .put("updatedAt", now())
                .put("summary", ok ? "Rootfs real com glibc validado · pronto para preparar smoke Box64 em etapa futura" : "Rootfs real falhou na validação glibc V17");
    }

    private static JSONObject realManifest(File rootfs, String fileName, String sha, boolean expectedProvided, boolean shaVerified, CoreLinuxRootfsTarExtractor.Stats stats, long started) throws Exception {
        JSONObject layout = new JSONObject();
        for (String item : Arrays.asList("bin", "usr/bin", "etc", "tmp", "home/core", "var/log", "opt/core-worker")) {
            layout.put(item, new File(rootfs, item).exists());
        }
        return new JSONObject()
                .put("schema", ROOTFS_MANIFEST_SCHEMA)
                .put("kind", ROOTFS_REAL_KIND)
                .put("stage", ROOTFS_IMPORT_STAGE_V17)
                .put("version", 1)
                .put("name", "Core Linux imported rootfs")
                .put("source", "user-selected-document")
                .put("fileName", clean(fileName, 240))
                .put("createdAt", started)
                .put("updatedAt", now())
                .put("arch", "aarch64")
                .put("distribution", readDistribution(rootfs))
                .put("distributionReady", true)
                .put("readyForBox64Install", true)
                .put("readyForBox64Smoke", true)
                .put("readyForBedrockStart", false)
                .put("sha256", sha == null ? "" : sha)
                .put("expectedSha256Provided", expectedProvided)
                .put("sha256Verified", shaVerified)
                .put("path", path(rootfs))
                .put("policy", rootfsPolicy())
                .put("layout", layout)
                .put("stats", stats == null ? new JSONObject() : stats.toJson())
                .put("notes", new JSONArray()
                        .put("Rootfs real importado e validado em staging antes da promoção.")
                        .put("Nenhum binário importado é executado nesta etapa.")
                        .put("Bedrock, Box64 e shell livre continuam bloqueados para patches futuros."));
    }

    private static JSONObject rootfsPolicy() throws Exception {
        return new JSONObject()
                .put("noFreeShell", true)
                .put("noRemoteArbitraryCommand", true)
                .put("noAutoDownload", true)
                .put("noBedrockStart", true)
                .put("appSpecificStorage", true)
                .put("termuxFallbackOnly", true)
                .put("rootfsImportV1", true)
                .put("rootfsStagingImportV17", true)
                .put("glibcRuntimeRequiredForBox64", true)
                .put("runnerBlocked", true);
    }

    private static boolean looksLikeTar(String name) {
        String n = String.valueOf(name == null ? "" : name).toLowerCase(Locale.ROOT);
        return n.endsWith(".tar") || n.endsWith(".tar.gz") || n.endsWith(".tgz");
    }

    private static boolean isGzipName(String name) {
        String n = String.valueOf(name == null ? "" : name).toLowerCase(Locale.ROOT);
        return n.endsWith(".tar.gz") || n.endsWith(".tgz");
    }

    private static String displayName(Context context, Uri uri) {
        String fallback = uri == null ? "rootfs.tar" : String.valueOf(uri.getLastPathSegment());
        try (Cursor cursor = context.getContentResolver().query(uri, null, null, null, null)) {
            if (cursor != null && cursor.moveToFirst()) {
                int idx = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (idx >= 0) {
                    String name = cursor.getString(idx);
                    if (name != null && !name.trim().isEmpty()) return name.trim();
                }
            }
        } catch (Throwable ignored) {
        }
        return fallback == null || fallback.trim().isEmpty() ? "rootfs.tar" : fallback.trim();
    }

    private static void writeImportProgress(Layout layout, String state, String summary, String fileName, String sha256) {
        try {
            JSONObject out = new JSONObject();
            out.put("ok", true);
            out.put("component", "core_linux_rootfs_import");
            out.put("action", "progress");
            out.put("stage", ROOTFS_IMPORT_STAGE_V17);
            out.put("state", state == null ? "rootfs_import_progress" : state);
            out.put("summary", summary == null ? "Importação rootfs em andamento" : summary);
            out.put("fileName", clean(fileName, 240));
            out.put("sha256", sha256 == null ? "" : clean(sha256, 80));
            out.put("updatedAt", now());
            out.put("safety", safetySummary());
            writeJson(layout.importStateFile, out);
            appendLog(layout.importLog, out.optString("summary"));
        } catch (Throwable ignored) {
        }
    }

    private static JSONObject failure(Layout layout, String state, String summary, JSONObject details) throws Exception {
        JSONObject out = new JSONObject();
        out.put("ok", false);
        out.put("component", "core_linux_rootfs_import");
        out.put("action", "import");
        out.put("stage", ROOTFS_IMPORT_STAGE_V17);
        out.put("state", state);
        out.put("summary", summary);
        out.put("details", details == null ? new JSONObject() : details);
        out.put("rootfsDir", path(layout.rootfs));
        out.put("stagingDir", path(layout.importStaging));
        out.put("termuxTouched", false);
        out.put("pythonTouched", false);
        out.put("serviceStarted", false);
        out.put("bedrockStarted", false);
        out.put("safety", safetySummary());
        out.put("updatedAt", now());
        writeJson(layout.importStateFile, out);
        appendLog(layout.importLog, summary);
        return out;
    }

    private static void ensureBase(Layout layout) throws IOException {
        layout.core.mkdirs();
        layout.runtime.mkdirs();
        layout.logs.mkdirs();
        layout.manifests.mkdirs();
        layout.importStaging.getParentFile().mkdirs();
        CoreLinuxRootfsFilesystem.recover(layout.rootfs, layout.previousRootfs);
    }



    private static void removeTree(File file) throws IOException {
        CoreLinuxRootfsFilesystem.removeTree(file);
    }

    private static void drain(InputStream input) throws IOException {
        byte[] buf = new byte[8192]; int count; long bytes = 0;
        while ((count = input.read(buf)) != -1) {
            bytes += count;
            if (bytes > 1024 * 1024) throw new IOException("dados residuais demais após TAR");
        }
    }


    private static JSONObject glibcRuntimeSnapshot(File rootfsDir) {
        JSONObject out = new JSONObject();
        try {
            JSONObject loader = fileStatusRelative(rootfsDir, "lib/ld-linux-aarch64.so.1");
            JSONObject libc = firstExistingRelative(rootfsDir,
                    "lib/aarch64-linux-gnu/libc.so.6",
                    "usr/lib/aarch64-linux-gnu/libc.so.6",
                    "lib/libc.so.6");
            JSONObject libm = firstExistingRelative(rootfsDir,
                    "lib/aarch64-linux-gnu/libm.so.6",
                    "usr/lib/aarch64-linux-gnu/libm.so.6",
                    "lib/libm.so.6");
            JSONObject libresolv = firstExistingRelative(rootfsDir,
                    "lib/aarch64-linux-gnu/libresolv.so.2",
                    "usr/lib/aarch64-linux-gnu/libresolv.so.2",
                    "lib/libresolv.so.2");
            boolean ok = loader.optBoolean("exists", false)
                    && libc.optBoolean("exists", false)
                    && libm.optBoolean("exists", false)
                    && libresolv.optBoolean("exists", false);
            JSONArray missing = new JSONArray();
            if (!loader.optBoolean("exists", false)) missing.put("/lib/ld-linux-aarch64.so.1");
            if (!libc.optBoolean("exists", false)) missing.put("libc.so.6");
            if (!libm.optBoolean("exists", false)) missing.put("libm.so.6");
            if (!libresolv.optBoolean("exists", false)) missing.put("libresolv.so.2");
            out.put("ok", ok);
            out.put("stage", ROOTFS_GLIBC_PREFLIGHT_STAGE_V16_2);
            out.put("rootfsDir", path(rootfsDir));
            out.put("loader", loader);
            out.put("libc", libc);
            out.put("libm", libm);
            out.put("libresolv", libresolv);
            out.put("missing", missing);
            out.put("required", new JSONArray()
                    .put("/lib/ld-linux-aarch64.so.1")
                    .put("libc.so.6")
                    .put("libm.so.6")
                    .put("libresolv.so.2"));
            out.put("summary", ok ? "runtime glibc arm64 presente no rootfs" : "runtime glibc arm64 ausente/incompleto no rootfs");
            return out;
        } catch (Throwable exc) {
            try {
                out.put("ok", false);
                out.put("error", shortThrowable(exc));
                out.put("missing", new JSONArray().put("glibc_runtime_probe_error"));
            } catch (Throwable ignored) {}
            return out;
        }
    }

    private static JSONObject firstExistingRelative(File rootfsDir, String... rels) {
        JSONArray checked = new JSONArray();
        JSONObject first = new JSONObject();
        try {
            if (rels != null) {
                for (String rel : rels) {
                    JSONObject row = fileStatusRelative(rootfsDir, rel);
                    checked.put(row);
                    if (row.optBoolean("exists", false) && first.length() == 0) first = row;
                }
            }
            if (first.length() == 0) {
                first.put("exists", false);
                first.put("path", "");
                first.put("relativePath", rels != null && rels.length > 0 ? rels[0] : "");
            }
            first.put("checked", checked);
        } catch (Throwable exc) {
            try { first.put("exists", false).put("error", shortThrowable(exc)).put("checked", checked); } catch (Throwable ignored) {}
        }
        return first;
    }

    private static JSONObject fileStatusRelative(File rootfsDir, String rel) {
        JSONObject out = new JSONObject();
        try {
            String safeRel = String.valueOf(rel == null ? "" : rel).replace('\\', '/');
            while (safeRel.startsWith("/")) safeRel = safeRel.substring(1);
            File file = new File(rootfsDir, safeRel);
            boolean exists = file.exists();
            out.put("relativePath", safeRel);
            out.put("path", path(file));
            out.put("exists", exists);
            out.put("isFile", exists && file.isFile());
            out.put("isDirectory", exists && file.isDirectory());
            out.put("canRead", exists && file.canRead());
            out.put("size", exists && file.isFile() ? Math.max(0L, file.length()) : 0L);
        } catch (Throwable exc) {
            try { out.put("relativePath", clean(rel, 180)).put("exists", false).put("error", shortThrowable(exc)); } catch (Throwable ignored) {}
        }
        return out;
    }

    private static String readDistribution(File rootfs) {
        try {
            byte[] raw = readBytes(new File(rootfs, "etc/os-release"), 8192);
            String text = new String(raw, StandardCharsets.UTF_8);
            for (String line : text.split("\\n")) {
                if (line.startsWith("PRETTY_NAME=")) return clean(line.substring("PRETTY_NAME=".length()).replace('"', ' '), 120);
            }
            for (String line : text.split("\\n")) {
                if (line.startsWith("ID=")) return clean(line.substring(3).replace('"', ' '), 80);
            }
        } catch (Throwable ignored) {
        }
        return "imported-rootfs";
    }

    private static JSONObject readJson(File file) {
        try {
            if (file == null) return new JSONObject();
            return new JSONObject(new String(CoreWorkerAtomicTextFiles.read(file, TEXT_LIMIT), StandardCharsets.UTF_8));
        } catch (Throwable ignored) {
            return new JSONObject();
        }
    }

    private static byte[] readBytes(File file, int limit) throws Exception {
        return CoreWorkerAtomicTextFiles.readPrefix(file, limit);
    }

    private static void writeJson(File file, JSONObject obj) throws Exception {
        writeText(file, obj == null ? "{}" : obj.toString(2));
    }

    private static void writeText(File file, String value) throws Exception {
        CoreWorkerAtomicTextFiles.write(file, value);
    }

    private static void appendLog(File file, String line) {
        try {
            File parent = file.getParentFile();
            if (parent != null) parent.mkdirs();
            try (FileOutputStream out = new FileOutputStream(file, true)) {
                out.write(("[" + now() + "] " + clean(line, 1200) + "\n").getBytes(StandardCharsets.UTF_8));
            }
        } catch (Throwable ignored) {
        }
    }

    private static String normalizeSha256(String value) {
        String text = String.valueOf(value == null ? "" : value).trim().toLowerCase(Locale.ROOT).replace(" ", "");
        if (text.matches("^[0-9a-f]{64}$")) return text;
        return "";
    }

    private static String hex(byte[] raw) {
        StringBuilder sb = new StringBuilder();
        for (byte b : raw) sb.append(String.format(Locale.ROOT, "%02x", b & 0xff));
        return sb.toString();
    }

    private static String firstNonEmpty(String... values) {
        if (values != null) for (String value : values) if (value != null && !value.trim().isEmpty()) return value.trim();
        return "";
    }

    private static String safetySummary() {
        return "importação rootfs v17: arquivo escolhido pelo usuário, staging seguro, SHA-256 calculado, valida glibc arm64, sem shell livre, sem executar binários, sem iniciar Bedrock";
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
            out.put("bedrockStarted", false);
        } catch (Throwable ignored) {
        }
        return out;
    }

    private static String shortThrowable(Throwable exc) {
        if (exc == null) return "erro desconhecido";
        String msg = exc.getMessage();
        return exc.getClass().getSimpleName() + (msg == null || msg.isEmpty() ? "" : ": " + clean(msg, 180));
    }

    private static String clean(String value, int limit) {
        String text = String.valueOf(value == null ? "" : value).replace((char) 0, ' ').replace('\r', ' ').trim();
        text = text.replaceAll("(?i)(token|authorization|bearer|secret|password|passwd)[=: ]+[^\\s]+", "$1=[redacted]");
        if (text.length() > limit) text = text.substring(0, Math.max(0, limit)) + "…";
        return text;
    }

    private static long now() {
        return System.currentTimeMillis();
    }

    private static String path(File file) {
        try { return file == null ? "" : file.getAbsolutePath(); } catch (Throwable ignored) { return ""; }
    }

    private static File resolveCoreLinuxDir(Context context, File provided) {
        if (provided != null) return provided;
        return new File(context.getFilesDir(), "core-linux");
    }

    private static final class Layout {
        final File core;
        final File rootfs;
        final File previousRootfs;
        final File runtime;
        final File logs;
        final File manifests;
        final File importStaging;
        final File importLog;
        final File importStateFile;
        final File rootfsStateFile;

        Layout(File core) {
            this.core = core;
            this.rootfs = new File(core, "rootfs");
            this.previousRootfs = new File(new File(core, "staging"), "rootfs-previous");
            this.runtime = new File(core, "runtime");
            this.logs = new File(core, "logs");
            this.manifests = new File(core, "manifests");
            this.importStaging = new File(new File(core, "staging"), "rootfs-import-next");
            this.importLog = new File(logs, "rootfs-import.log");
            this.importStateFile = new File(runtime, "rootfs-import-state.json");
            this.rootfsStateFile = new File(runtime, "rootfs-state.json");
        }
    }

}
