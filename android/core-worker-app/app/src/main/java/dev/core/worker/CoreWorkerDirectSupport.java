package dev.core.worker;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Base64;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;

/** Shared dependencies and bounded helpers for direct task domains. */
class CoreWorkerDirectSupport {
    static final String PREFS = "core_worker_private";
    static final int MAX_BODY_BYTES = 32 * 1024 * 1024;
    static final int MAX_OUTPUT_BYTES = 32 * 1024 * 1024;
    static final int MAX_TEXT_BYTES = 8 * 1024 * 1024;
    static final int MAX_ZIP_ENTRIES = 2000;
    static final int MAX_LOG_LINES = 500;

    final Context context;
    final SharedPreferences prefs;
    final NativeTtsManager tts;

    CoreWorkerDirectSupport(Context context, SharedPreferences prefs, NativeTtsManager tts) {
        this.context = context.getApplicationContext();
        this.prefs = prefs == null ? this.context.getSharedPreferences(PREFS, Context.MODE_PRIVATE) : prefs;
        this.tts = tts;
    }

    byte[] readLimited(InputStream input, int limitBytes) throws Exception {
        if (input == null) return new byte[0];
        try (InputStream source = input; ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[16 * 1024];
            int total = 0;
            int read;
            while ((read = source.read(buffer)) >= 0) {
                if (read == 0) continue;
                total += read;
                if (total > limitBytes) throw new IllegalArgumentException("emoji grande demais");
                output.write(buffer, 0, read);
            }
            return output.toByteArray();
        }
    }

    long directorySize(File root, int maxEntries) {
        if (root == null || !root.exists()) return 0L;
        long total = 0L;
        int visited = 0;
        List<File> pending = new ArrayList<>();
        pending.add(root);
        while (!pending.isEmpty() && visited < maxEntries) {
            File current = pending.remove(pending.size() - 1);
            visited++;
            if (current.isFile()) {
                total += Math.max(0L, current.length());
                continue;
            }
            File[] children = current.listFiles();
            if (children != null) Collections.addAll(pending, children);
        }
        return total;
    }

    File findBinary(String name) {
        List<File> candidates = new ArrayList<>();
        String nativeDir = context.getApplicationInfo().nativeLibraryDir;
        if (nativeDir != null) {
            candidates.add(new File(nativeDir, "libcoreworker_" + name + ".so"));
            candidates.add(new File(nativeDir, "lib" + name + ".so"));
        }
        candidates.add(new File(context.getFilesDir(), "core-linux/bin/" + name));
        candidates.add(new File(context.getFilesDir(), "core-linux/rootfs/usr/bin/" + name));
        candidates.add(new File(context.getFilesDir(), "bin/" + name));
        for (File file : candidates) {
            if (file.isFile() && file.canRead()) {
                if (!file.canExecute()) file.setExecutable(true, true);
                if (file.canExecute()) return file;
            }
        }
        return null;
    }

    File requireBinary(String name) {
        File binary = findBinary(name);
        if (binary == null) throw new IllegalStateException(name + " não encontrado nos binários privados do APK");
        return binary;
    }

    ProcessResult runProcess(String[] command, File directory, long timeoutMs, int maxOutput) throws Exception {
        ProcessBuilder builder = new ProcessBuilder(command);
        if (directory != null) builder.directory(directory);
        Map<String, String> env = builder.environment();
        String nativeDir = context.getApplicationInfo().nativeLibraryDir;
        if (nativeDir != null && !nativeDir.isEmpty()) env.put("LD_LIBRARY_PATH", nativeDir);
        CoreWorkerProcessRunner.Result result = CoreWorkerProcessRunner.run(builder, timeoutMs, maxOutput);
        return new ProcessResult(result.exitCode, result.stdout, result.stderr);
    }

    byte[] decodeBodyData(JSONObject body) {
        String b64 = body.optString("data_b64", "");
        if (!b64.isEmpty()) {
            byte[] data = Base64.decode(b64, Base64.DEFAULT);
            if (data.length > MAX_BODY_BYTES) throw new IllegalArgumentException("entrada grande demais");
            return data;
        }
        String text = body.optString("text", "");
        byte[] data = text.getBytes(StandardCharsets.UTF_8);
        if (data.length > MAX_BODY_BYTES) throw new IllegalArgumentException("entrada grande demais");
        return data;
    }

    String limitedText(String text) {
        String value = text == null ? "" : text;
        if (value.getBytes(StandardCharsets.UTF_8).length > MAX_TEXT_BYTES) throw new IllegalArgumentException("texto grande demais");
        return value;
    }

    File tempDir(String prefix) {
        File dir = new File(context.getCacheDir(), "direct-" + prefix + "-" + UUID.randomUUID());
        if (!dir.mkdirs()) throw new IllegalStateException("não consegui criar diretório temporário");
        return dir;
    }

    JSONObject readJson(File file) {
        try { return new JSONObject(new String(readBytes(file, 1024 * 1024), StandardCharsets.UTF_8)); }
        catch (Throwable ignored) { return new JSONObject(); }
    }

    static String normalizeTask(String value) {
        return (value == null ? "" : value.trim().toLowerCase(Locale.ROOT).replace('-', '_')).replaceAll("[^a-z0-9_]+", "_");
    }

    String workerId() {
        String value = CoreWorkerRuntimeIdentity.runtimeWorkerId(context);
        if (!value.isEmpty()) return value;
        return "apk-" + prefs.getString("install_id", "unknown").replace("-", "");
    }

    String normalizedServerUrl() {
        String value = prefs.getString("server_url", "").trim();
        if (value.isEmpty()) value = BuildConfig.CORE_WORKER_VPS_URL == null ? "" : BuildConfig.CORE_WORKER_VPS_URL.trim();
        return value.replaceAll("/+$", "");
    }

    static String safeName(String value) {
        String clean = value == null ? "" : value.replace('\\', '_').replace('/', '_').replaceAll("[^a-zA-Z0-9._ -]+", "_").trim();
        return clean.isEmpty() ? "file.bin" : limit(clean, 120);
    }

    static String safeZipPath(String value) {
        String clean = value == null ? "" : value.replace('\\', '/').replaceAll("^/+", "");
        if (isSuspiciousZipPath(clean)) throw new IllegalArgumentException("caminho ZIP suspeito");
        return clean.isEmpty() ? "file.bin" : limit(clean, 180);
    }

    static boolean isSuspiciousZipPath(String value) {
        String clean = value == null ? "" : value.replace('\\', '/');
        return clean.startsWith("/") || clean.matches("^[A-Za-z]:.*") || clean.contains("../") || clean.equals("..") || clean.indexOf('\0') >= 0;
    }

    static String safeExtension(String value, String fallback) {
        String clean = value == null ? "" : value.toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9]+", "");
        return clean.isEmpty() ? fallback : limit(clean, 12);
    }

    static boolean isSafeFfmpegArg(String arg) {
        if (arg == null || arg.isEmpty() || arg.length() > 120) return false;
        if (arg.contains(";") || arg.contains("&&") || arg.contains("||") || arg.contains("`") || arg.contains("\n") || arg.contains("\r")) return false;
        if (arg.startsWith("file:") || arg.startsWith("http:") || arg.startsWith("https:") || arg.startsWith("tcp:") || arg.startsWith("udp:")) return false;
        return arg.matches("[a-zA-Z0-9_+.,:=/@%\\-]+") && !arg.contains("../");
    }

    static String sha256(byte[] data) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        byte[] hash = digest.digest(data == null ? new byte[0] : data);
        StringBuilder out = new StringBuilder();
        for (byte value : hash) out.append(String.format(Locale.ROOT, "%02x", value & 0xff));
        return out.toString();
    }

    static void writeBytes(File file, byte[] data) throws Exception {
        File parent = file.getParentFile();
        if (parent != null && !parent.exists()) parent.mkdirs();
        try (FileOutputStream output = new FileOutputStream(file, false)) { output.write(data); output.flush(); }
    }

    static void writeText(File file, String text) throws Exception {
        writeBytes(file, (text == null ? "" : text).getBytes(StandardCharsets.UTF_8));
    }

    static byte[] readBytes(File file, int maxBytes) throws Exception {
        if (file == null || !file.isFile()) throw new IllegalArgumentException("arquivo ausente");
        if (file.length() > maxBytes) throw new IllegalArgumentException("arquivo grande demais");
        try (FileInputStream input = new FileInputStream(file); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[64 * 1024];
            int read;
            while ((read = input.read(buffer)) >= 0) {
                if (read == 0) continue;
                if (output.size() + read > maxBytes) throw new IllegalArgumentException("arquivo grande demais");
                output.write(buffer, 0, read);
            }
            return output.toByteArray();
        }
    }

    static long directorySize(File file) {
        if (file == null || !file.exists()) return 0L;
        if (file.isFile()) return file.length();
        long total = 0L;
        File[] children = file.listFiles();
        if (children != null) for (File child : children) total += directorySize(child);
        return total;
    }

    static void deleteTree(File file) {
        if (file == null || !file.exists()) return;
        File[] children = file.listFiles();
        if (children != null) for (File child : children) deleteTree(child);
        file.delete();
    }

    static int clamp(int value, int min, int max) { return Math.max(min, Math.min(max, value)); }

    static String firstLine(String value) { String[] lines = (value == null ? "" : value).split("\\R", 2); return lines.length == 0 ? "" : limit(lines[0], 240); }

    static String limit(String value, int max) { String clean = value == null ? "" : value; return clean.length() <= max ? clean : clean.substring(0, max); }

    static String shortThrowable(Throwable error) { return error == null ? "erro desconhecido" : limit(error.getClass().getSimpleName() + (error.getMessage() == null ? "" : ": " + error.getMessage()), 240); }

    static long elapsedMs(long startedNanos) { return Math.max(0L, Math.round((System.nanoTime() - startedNanos) / 1_000_000.0)); }

    static final class ProcessResult {
        final int exitCode;
        final String stdout;
        final String stderr;
        ProcessResult(int exitCode, String stdout, String stderr) { this.exitCode = exitCode; this.stdout = stdout == null ? "" : stdout; this.stderr = stderr == null ? "" : stderr; }
    }

}
