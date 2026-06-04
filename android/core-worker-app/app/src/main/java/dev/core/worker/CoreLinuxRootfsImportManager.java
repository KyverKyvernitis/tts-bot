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

    public static JSONObject validateActive(Context context, File coreLinuxDir) {
        try {
            Layout layout = new Layout(resolveCoreLinuxDir(context, coreLinuxDir));
            ensureBase(layout);
            JSONObject validation = validateReal(layout.rootfs);
            JSONObject out = new JSONObject();
            out.put("ok", validation.optBoolean("ok", false));
            out.put("component", "core_linux_rootfs_import");
            out.put("action", "validate_active");
            out.put("state", validation.optBoolean("ok", false) ? "rootfs_real_validated" : "rootfs_real_validation_failed");
            out.put("summary", validation.optBoolean("ok", false)
                    ? "Rootfs real validado · runner real ainda bloqueado"
                    : "Rootfs real não passou na validação");
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
                return failure(layout, "rootfs_import_unsupported_format", "Formato não suportado: use .tar, .tar.gz ou .tgz", null);
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
            start.put("state", "rootfs_import_extracting");
            start.put("summary", "Importando rootfs real em staging");
            start.put("fileName", clean(displayName, 240));
            start.put("expectedSha256Provided", !expected.isEmpty());
            start.put("startedAt", started);
            start.put("safety", safetySummary());
            writeJson(layout.importStateFile, start);
            appendLog(layout.importLog, "iniciando import rootfs: " + displayName);

            writeImportProgress(layout, "rootfs_import_reading", "Lendo arquivo e calculando SHA-256", displayName, null);
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            TarStats stats;
            try (InputStream raw = context.getContentResolver().openInputStream(uri)) {
                if (raw == null) {
                    return failure(layout, "rootfs_import_open_failed", "Não consegui abrir o arquivo escolhido", null);
                }
                DigestInputStream digestInput = new DigestInputStream(new BufferedInputStream(raw, 64 * 1024), digest);
                InputStream tarInput = isGzipName(displayName) ? new GZIPInputStream(digestInput, 64 * 1024) : digestInput;
                stats = extractTar(tarInput, layout.importStaging);
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
            out.put("state", "rootfs_real_validated");
            out.put("summary", "Rootfs real importado e validado · runner real ainda bloqueado");
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
        removeTree(layout.previousRootfs);
        if (layout.rootfs.exists()) {
            if (!layout.rootfs.renameTo(layout.previousRootfs)) {
                copyTree(layout.rootfs, layout.previousRootfs);
                removeTree(layout.rootfs);
            }
        }
        boolean promoted = layout.importStaging.renameTo(layout.rootfs);
        if (!promoted) {
            try {
                copyTree(layout.importStaging, layout.rootfs);
                removeTree(layout.importStaging);
                promoted = true;
            } catch (Throwable exc) {
                removeTree(layout.rootfs);
                if (layout.previousRootfs.exists()) {
                    //noinspection ResultOfMethodCallIgnored
                    layout.previousRootfs.renameTo(layout.rootfs);
                }
                throw exc;
            }
        }
        if (promoted) {
            appendLog(layout.importLog, "rootfs staging promovida para ativa");
        }
    }

    private static void postProcessImportedRootfs(File rootfs, String fileName, String sha, boolean expectedProvided, boolean shaVerified, TarStats stats, long started) throws Exception {
        new File(rootfs, "tmp").mkdirs();
        new File(rootfs, "var/log").mkdirs();
        new File(rootfs, "home/core").mkdirs();
        new File(rootfs, "opt/core-worker").mkdirs();
        writeJson(new File(rootfs, ".core-worker-rootfs-manifest.json"), realManifest(rootfs, fileName, sha, expectedProvided, shaVerified, stats, started));
        writeJson(new File(rootfs, "opt/core-worker/rootfs-policy.json"), rootfsPolicy());
        writeText(new File(rootfs, ".core-worker-rootfs-ready"), "readyAt=" + now() + "\nkind=" + ROOTFS_REAL_KIND + "\nstage=core-linux-rootfs-import-v1\n");
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
        JSONArray missing = new JSONArray();
        JSONArray names = checks.names();
        if (names != null) {
            for (int i = 0; i < names.length(); i++) {
                String key = names.optString(i, "");
                if (!checks.optBoolean(key, false)) missing.put(key);
            }
        }
        boolean ok = missing.length() == 0;
        return new JSONObject()
                .put("ok", ok)
                .put("rootfsReady", ok)
                .put("state", ok ? "rootfs_real_validated" : "rootfs_real_validation_failed")
                .put("validationLevel", "real")
                .put("distributionReady", ok)
                .put("readyForBox64Install", ok)
                .put("readyForBedrockStart", false)
                .put("checks", checks)
                .put("missing", missing)
                .put("manifest", manifest);
    }

    private static JSONObject activeState(Layout layout, JSONObject validation, JSONObject manifest) throws Exception {
        boolean ok = validation.optBoolean("ok", false);
        return new JSONObject()
                .put("schema", "core-worker-rootfs-state-v1")
                .put("ok", ok)
                .put("action", "import")
                .put("state", ok ? "rootfs_real_validated" : "rootfs_real_validation_failed")
                .put("rootfsReady", ok)
                .put("validationLevel", "real")
                .put("distributionReady", ok)
                .put("readyForBox64Install", ok)
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
                        .put("rootfs real importado; execução de binários importados segue bloqueada nesta etapa")
                        .put("Bedrock/Box64/shell livre continuam bloqueados"))
                .put("updatedAt", now())
                .put("summary", ok ? "Rootfs real validado · runner real ainda bloqueado" : "Rootfs real falhou na validação");
    }

    private static JSONObject realManifest(File rootfs, String fileName, String sha, boolean expectedProvided, boolean shaVerified, TarStats stats, long started) throws Exception {
        JSONObject layout = new JSONObject();
        for (String item : Arrays.asList("bin", "usr/bin", "etc", "tmp", "home/core", "var/log", "opt/core-worker")) {
            layout.put(item, new File(rootfs, item).exists());
        }
        return new JSONObject()
                .put("schema", ROOTFS_MANIFEST_SCHEMA)
                .put("kind", ROOTFS_REAL_KIND)
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
                .put("runnerBlocked", true);
    }

    private static TarStats extractTar(InputStream input, File staging) throws Exception {
        TarStats stats = new TarStats();
        byte[] header = new byte[512];
        String base = staging.getCanonicalPath();
        String pendingLongName = null;
        String pendingLongLink = null;
        while (true) {
            int read = readBlock(input, header);
            if (read == 0) break;
            if (read < 512) throw new IOException("tar header incompleto");
            if (isZeroBlock(header)) break;
            String name = tarString(header, 0, 100);
            String prefix = tarString(header, 345, 155);
            if (!prefix.isEmpty()) name = prefix + "/" + name;
            long size = tarOctal(header, 124, 12);
            char type = (char) header[156];
            String linkName = tarString(header, 157, 100);

            if (type == 'L') {
                pendingLongName = readEntryText(input, size, 8192);
                stats.meta += 1;
                continue;
            }
            if (type == 'K') {
                pendingLongLink = readEntryText(input, size, 8192);
                stats.meta += 1;
                continue;
            }
            if (type == 'x') {
                String pax = readEntryText(input, size, 64 * 1024);
                String paxPath = parsePaxValue(pax, "path");
                String paxLink = parsePaxValue(pax, "linkpath");
                if (!paxPath.isEmpty()) pendingLongName = paxPath;
                if (!paxLink.isEmpty()) pendingLongLink = paxLink;
                stats.meta += 1;
                continue;
            }
            if (type == 'g') {
                skipEntry(input, size);
                stats.meta += 1;
                continue;
            }

            if (pendingLongName != null && !pendingLongName.trim().isEmpty()) {
                name = pendingLongName.trim();
                pendingLongName = null;
            }
            if (pendingLongLink != null && !pendingLongLink.trim().isEmpty()) {
                linkName = pendingLongLink.trim();
                pendingLongLink = null;
            }
            name = cleanTarPath(name);
            if (name.isEmpty()) {
                skipEntry(input, size);
                continue;
            }
            stats.entries += 1;
            if (stats.entries > MAX_ENTRIES) throw new IOException("rootfs tem arquivos demais para import v1");
            if (size < 0L || size > MAX_SINGLE_FILE_BYTES) throw new IOException("arquivo muito grande no rootfs: " + name);
            stats.bytes += Math.max(0L, size);
            if (stats.bytes > MAX_TOTAL_BYTES) throw new IOException("rootfs excede limite seguro v1");
            File target = safeTarget(staging, base, name);
            if (type == '5') {
                target.mkdirs();
                skipEntry(input, size);
                stats.dirs += 1;
            } else if (type == '0' || type == 0) {
                File parent = target.getParentFile();
                if (parent != null) parent.mkdirs();
                try (FileOutputStream out = new FileOutputStream(target, false)) {
                    copyExactly(input, out, size);
                }
                skipPadding(input, size);
                stats.files += 1;
            } else if (type == '2') {
                createSafeSymlink(staging, base, target, linkName);
                skipEntry(input, size);
                stats.symlinks += 1;
            } else if (type == '1') {
                throw new IOException("hardlink não suportado no import v1: " + name);
            } else {
                throw new IOException("tipo tar não suportado no import v1: " + String.valueOf(type) + " em " + name);
            }
        }
        return stats;
    }

    private static String readEntryText(InputStream input, long size, int limit) throws Exception {
        int max = (int) Math.max(0L, Math.min(size, Math.max(1, limit)));
        byte[] data = new byte[max];
        int off = 0;
        while (off < max) {
            int n = input.read(data, off, max - off);
            if (n < 0) break;
            off += n;
        }
        if (size > max) skipFully(input, size - max);
        skipPadding(input, size);
        String text = new String(data, 0, off, StandardCharsets.UTF_8);
        int zero = text.indexOf('\0');
        return zero >= 0 ? text.substring(0, zero) : text.trim();
    }

    private static String parsePaxValue(String pax, String key) {
        if (pax == null || key == null) return "";
        String needle = key + "=";
        for (String line : pax.split("\n")) {
            int idx = line.indexOf(needle);
            if (idx >= 0) {
                return line.substring(idx + needle.length()).trim();
            }
        }
        return "";
    }

    private static void createSafeSymlink(File staging, String base, File target, String linkName) throws Exception {
        String link = clean(linkName, 512).replace('\\', '/');
        if (link.isEmpty()) throw new IOException("symlink vazio em " + target.getName());
        if (link.startsWith("/")) throw new IOException("symlink absoluto bloqueado: " + link);
        File parent = target.getParentFile();
        if (parent != null) parent.mkdirs();
        File resolved = new File(parent == null ? staging : parent, link);
        String resolvedPath = resolved.getCanonicalPath();
        if (!resolvedPath.equals(base) && !resolvedPath.startsWith(base + File.separator)) {
            throw new IOException("symlink escapando do staging: " + link);
        }
        if (target.exists()) removeTree(target);
        try {
            Os.symlink(link, target.getAbsolutePath());
        } catch (Throwable exc) {
            // Alguns aparelhos podem bloquear symlink em storage privado. Preserve um marcador
            // para diagnóstico e deixe a validação decidir se esse rootfs ainda é aceitável.
            writeText(new File(target.getAbsolutePath() + ".core-worker-symlink.txt"), "symlink=" + link + "\nerror=" + shortThrowable(exc) + "\n");
        }
    }

    private static File safeTarget(File root, String base, String name) throws Exception {
        if (name.startsWith("/") || name.contains("\u0000")) throw new IOException("path inseguro no tar: " + clean(name, 120));
        File target = new File(root, name);
        String path = target.getCanonicalPath();
        if (!path.equals(base) && !path.startsWith(base + File.separator)) {
            throw new IOException("path escapando do staging: " + clean(name, 120));
        }
        return target;
    }

    private static String cleanTarPath(String name) throws IOException {
        String value = String.valueOf(name == null ? "" : name).replace('\\', '/').trim();
        while (value.startsWith("./")) value = value.substring(2);
        while (value.startsWith("/")) throw new IOException("path absoluto bloqueado: " + clean(value, 120));
        if (value.equals(".") || value.equals("./")) return "";
        String[] parts = value.split("/");
        for (String part : parts) {
            if (part.equals("..")) throw new IOException("path com .. bloqueado: " + clean(value, 120));
        }
        return value;
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

    private static void ensureBase(Layout layout) {
        layout.core.mkdirs();
        layout.runtime.mkdirs();
        layout.logs.mkdirs();
        layout.manifests.mkdirs();
        layout.importStaging.getParentFile().mkdirs();
    }

    private static void copyTree(File src, File dst) throws Exception {
        if (src.isDirectory()) {
            if (!dst.exists()) dst.mkdirs();
            File[] files = src.listFiles();
            if (files != null) for (File child : files) copyTree(child, new File(dst, child.getName()));
        } else {
            File parent = dst.getParentFile();
            if (parent != null) parent.mkdirs();
            try (FileInputStream in = new FileInputStream(src); FileOutputStream out = new FileOutputStream(dst)) {
                byte[] buf = new byte[64 * 1024];
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

    private static int readBlock(InputStream input, byte[] block) throws Exception {
        int off = 0;
        while (off < block.length) {
            int n = input.read(block, off, block.length - off);
            if (n < 0) break;
            off += n;
        }
        return off;
    }

    private static boolean isZeroBlock(byte[] block) {
        for (byte b : block) if (b != 0) return false;
        return true;
    }

    private static String tarString(byte[] block, int offset, int len) {
        int end = offset;
        int max = Math.min(block.length, offset + len);
        while (end < max && block[end] != 0) end++;
        return new String(block, offset, Math.max(0, end - offset), StandardCharsets.UTF_8).trim();
    }

    private static long tarOctal(byte[] block, int offset, int len) {
        String raw = tarString(block, offset, len).trim();
        if (raw.isEmpty()) return 0L;
        long value = 0L;
        for (int i = 0; i < raw.length(); i++) {
            char c = raw.charAt(i);
            if (c < '0' || c > '7') continue;
            value = (value << 3) + (c - '0');
        }
        return value;
    }

    private static void copyExactly(InputStream input, FileOutputStream output, long size) throws Exception {
        byte[] buf = new byte[64 * 1024];
        long remaining = size;
        while (remaining > 0L) {
            int n = input.read(buf, 0, (int) Math.min(buf.length, remaining));
            if (n < 0) throw new IOException("fim inesperado do tar");
            output.write(buf, 0, n);
            remaining -= n;
        }
    }

    private static void skipEntry(InputStream input, long size) throws Exception {
        skipFully(input, size);
        skipPadding(input, size);
    }

    private static void skipPadding(InputStream input, long size) throws Exception {
        long pad = (512L - (size % 512L)) % 512L;
        skipFully(input, pad);
    }

    private static void skipFully(InputStream input, long amount) throws Exception {
        long remaining = amount;
        byte[] buf = new byte[8192];
        while (remaining > 0L) {
            long skipped = input.skip(remaining);
            if (skipped <= 0L) {
                int n = input.read(buf, 0, (int) Math.min(buf.length, remaining));
                if (n < 0) throw new IOException("fim inesperado ao pular tar");
                skipped = n;
            }
            remaining -= skipped;
        }
    }

    private static void drain(InputStream input) {
        try {
            byte[] buf = new byte[8192];
            while (input.read(buf) >= 0) {}
        } catch (Throwable ignored) {
        }
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
            if (file == null || !file.exists()) return new JSONObject();
            return new JSONObject(new String(readBytes(file, TEXT_LIMIT), StandardCharsets.UTF_8));
        } catch (Throwable ignored) {
            return new JSONObject();
        }
    }

    private static byte[] readBytes(File file, int limit) throws Exception {
        try (FileInputStream in = new FileInputStream(file)) {
            byte[] buf = new byte[Math.max(1, Math.min(limit, (int) Math.max(1L, file.length())) )];
            int n = in.read(buf);
            if (n <= 0) return new byte[0];
            return n == buf.length ? buf : Arrays.copyOf(buf, n);
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
        return "importação rootfs v1: arquivo escolhido pelo usuário, staging seguro, SHA-256 calculado, sem shell livre, sem executar binários, sem iniciar Bedrock";
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

    private static final class TarStats {
        long entries = 0L;
        long files = 0L;
        long dirs = 0L;
        long symlinks = 0L;
        long meta = 0L;
        long bytes = 0L;

        JSONObject toJson() throws Exception {
            return new JSONObject()
                    .put("entries", entries)
                    .put("files", files)
                    .put("dirs", dirs)
                    .put("symlinks", symlinks)
                    .put("meta", meta)
                    .put("bytes", bytes);
        }
    }
}
