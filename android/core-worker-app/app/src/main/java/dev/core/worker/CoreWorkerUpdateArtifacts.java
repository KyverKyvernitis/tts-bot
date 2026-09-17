package dev.core.worker;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.util.Locale;

/** Same-origin, size/hash checked APK download with replacement after verification. */
final class CoreWorkerUpdateArtifacts {
    static final long MAX_APK_BYTES = 256L * 1024 * 1024;
    static final int MAX_REDIRECTS = 5;
    interface Progress { void onProgress(long done, long total); }
    private CoreWorkerUpdateArtifacts() { }

    static String requireSha256(String hash) {
        if (hash == null || !hash.trim().matches("(?i)[a-f0-9]{64}")) {
            throw new IllegalArgumentException("manifesto sem SHA-256 válido");
        }
        return hash.trim().toLowerCase(Locale.ROOT);
    }

    static URL resolve(String serverUrl, String raw) throws IOException {
        if (raw == null || raw.trim().isEmpty()) throw new IOException("manifesto sem URL de APK");
        URL base = new URL(serverUrl);
        String clean = raw.trim();
        URL target = new URL(base, clean.startsWith("/") || clean.contains(":") ? clean : "/" + clean);
        requireSameOrigin(base, target);
        return target;
    }

    static void requireSameOrigin(URL base, URL target) throws IOException {
        String scheme = base.getProtocol();
        if (!("https".equalsIgnoreCase(scheme) || "http".equalsIgnoreCase(scheme))
                || !scheme.equalsIgnoreCase(target.getProtocol())
                || !base.getHost().equalsIgnoreCase(target.getHost())
                || port(base) != port(target) || target.getUserInfo() != null
                || base.getUserInfo() != null || target.getRef() != null) {
            throw new IOException("atualização deve permanecer na origem da VPS");
        }
    }

    private static int port(URL url) { return url.getPort() < 0 ? url.getDefaultPort() : url.getPort(); }

    static synchronized void download(String serverUrl, String raw, String hash, File target, Progress progress) throws Exception {
        download(serverUrl, raw, hash, target, progress, MAX_APK_BYTES);
    }

    // Explicit limit also permits small deterministic boundary tests.
    static synchronized void download(String serverUrl, String raw, String hash, File target, Progress progress, long limit) throws Exception {
        String expected = requireSha256(hash);
        URL base = new URL(serverUrl);
        URL url = resolve(serverUrl, raw);
        if (limit < 1 || limit > MAX_APK_BYTES) throw new IllegalArgumentException("limite de APK inválido");
        File parent = target.getAbsoluteFile().getParentFile();
        if (!parent.isDirectory() && !parent.mkdirs()) throw new IOException("diretório de update indisponível");
        File partial = new File(parent, target.getName() + ".download");
        try {
            for (int redirects = 0; ; redirects++) {
                requireSameOrigin(base, url);
                HttpURLConnection connection = (HttpURLConnection) url.openConnection();
                try {
                    connection.setInstanceFollowRedirects(false);
                    connection.setConnectTimeout(9000);
                    connection.setReadTimeout(30000);
                    connection.setRequestProperty("Accept", "application/vnd.android.package-archive,*/*");
                    int status = connection.getResponseCode();
                    if (status == 301 || status == 302 || status == 303 || status == 307 || status == 308) {
                        if (redirects >= MAX_REDIRECTS) throw new IOException("redirecionamentos demais");
                        String location = connection.getHeaderField("Location");
                        if (location == null || location.isEmpty()) throw new IOException("redirect sem destino");
                        url = new URL(url, location);
                        requireSameOrigin(base, url);
                        continue;
                    }
                    if (status != 200) throw new IOException("download APK: HTTP " + status);
                    long total = connection.getContentLengthLong();
                    if (total > limit) throw new IOException("APK grande demais");
                    MessageDigest digest = MessageDigest.getInstance("SHA-256");
                    long done = 0L, lastUi = 0L;
                    try (InputStream input = connection.getInputStream(); FileOutputStream output = new FileOutputStream(partial)) {
                        byte[] buffer = new byte[32768];
                        int count;
                        while ((count = input.read(buffer)) != -1) {
                            if (Thread.currentThread().isInterrupted()) throw new java.io.InterruptedIOException("download cancelado");
                            if (count > limit - done) throw new IOException("APK grande demais");
                            output.write(buffer, 0, count);
                            digest.update(buffer, 0, count);
                            done += count;
                            long now = System.currentTimeMillis();
                            if (progress != null && now - lastUi >= 700) { progress.onProgress(done, total); lastUi = now; }
                        }
                        if (done == 0 || total >= 0 && total != done) throw new IOException("download APK incompleto");
                        if (!expected.equals(hex(digest.digest()))) throw new IOException("SHA-256 divergente no APK baixado");
                        output.flush();
                        output.getFD().sync();
                    }
                    // Callback failure must leave the previously valid APK intact.
                    if (progress != null) progress.onProgress(done, total);
                    moveReplacing(partial, target);
                    return;
                } finally { connection.disconnect(); }
            }
        } finally { Files.deleteIfExists(partial.toPath()); }
    }

    static void moveReplacing(File from, File to) throws IOException {
        try { Files.move(from.toPath(), to.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING); }
        catch (AtomicMoveNotSupportedException error) {
            Files.move(from.toPath(), to.toPath(), StandardCopyOption.REPLACE_EXISTING);
        }
    }

    static String sha256(File file) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (InputStream input = new FileInputStream(file)) {
            byte[] buffer = new byte[16384];
            int count;
            while ((count = input.read(buffer)) != -1) digest.update(buffer, 0, count);
        }
        return hex(digest.digest());
    }

    private static String hex(byte[] hash) {
        StringBuilder out = new StringBuilder();
        for (byte value : hash) out.append(String.format(Locale.ROOT, "%02x", value & 255));
        return out.toString();
    }
}
