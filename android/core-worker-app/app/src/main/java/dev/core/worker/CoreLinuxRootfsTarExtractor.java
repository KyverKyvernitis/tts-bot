package dev.core.worker;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import org.json.JSONObject;

/** Bounded USTAR/PAX/GNU-name reader for staged rootfs imports. */
final class CoreLinuxRootfsTarExtractor {
    private CoreLinuxRootfsTarExtractor() { }
    static Stats extractTar(InputStream input, File staging) throws Exception {
        return extractTar(input, staging, new Limits(80000, 4L * 1024 * 1024 * 1024, 2L * 1024 * 1024 * 1024),
                (target, link) -> Files.createSymbolicLink(target, link));
    }

    static Stats extractTar(InputStream input, File staging, Limits limits, LinkCreator links) throws Exception {
        CoreLinuxRootfsFilesystem.requireDirectory(staging);
        Stats stats = new Stats();
        byte[] header = new byte[512];
        String base = staging.getCanonicalPath();
        String pendingLongName = null;
        String pendingLongLink = null;
        Long pendingSize = null;
        while (true) {
            int read = readBlock(input, header);
            if (read == 0) throw new IOException("TAR sem bloco final");
            if (read < 512) throw new IOException("tar header incompleto");
            if (isZeroBlock(header)) {
                if (pendingLongName != null || pendingLongLink != null || pendingSize != null) throw new IOException("metadata TAR sem entrada");
                // Read through gzip trailer as well; truncated/corrupt streams must fail.
                byte[] trailing = new byte[8192]; int count; long padding = 0;
                while ((count = input.read(trailing)) != -1) {
                    padding += count;
                    if (padding > 1024 * 1024) throw new IOException("padding TAR grande demais");
                    for (int i = 0; i < count; i++) if (trailing[i] != 0) throw new IOException("dados após fim do TAR");
                }
                break;
            }
            verifyChecksum(header);
            String name = tarString(header, 0, 100);
            String prefix = tarString(header, 345, 155);
            if (!prefix.isEmpty()) name = prefix + "/" + name;
            long size = tarOctal(header, 124, 12);
            char type = (char) header[156];
            String linkName = tarString(header, 157, 100);
            boolean metadata = type == 'L' || type == 'K' || type == 'x' || type == 'g';
            if (!metadata && pendingSize != null) { size = pendingSize; pendingSize = null; }
            if (++stats.entries > limits.entries) throw new IOException("rootfs tem entradas demais");
            if (size < 0 || size > limits.singleFile || size > limits.bytes - stats.bytes) throw new IOException("rootfs excede limite seguro");
            if (metadata && size > 64 * 1024) throw new IOException("metadata TAR grande demais");
            stats.bytes += size;

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
                java.util.Map<String,String> values = parsePax(pax);
                String paxPath = values.getOrDefault("path", "");
                String paxLink = values.getOrDefault("linkpath", "");
                if (values.containsKey("size")) {
                    try { pendingSize = Long.parseLong(values.get("size")); }
                    catch (NumberFormatException error) { throw new IOException("tamanho PAX inválido", error); }
                }
                if (!paxPath.isEmpty()) pendingLongName = paxPath;
                if (!paxLink.isEmpty()) pendingLongLink = paxLink;
                stats.meta += 1;
                continue;
            }
            if (type == 'g') {
                java.util.Map<String,String> global = parsePax(readEntryText(input, size, 64 * 1024));
                if (global.containsKey("path") || global.containsKey("linkpath") || global.containsKey("size")) {
                    throw new IOException("PAX global estrutural não suportado");
                }
                stats.meta += 1;
                continue;
            }

            if (pendingLongName != null) {
                name = pendingLongName;
                pendingLongName = null;
            }
            if (pendingLongLink != null) {
                linkName = pendingLongLink;
                pendingLongLink = null;
            }
            name = cleanTarPath(name);
            if (name.isEmpty()) {
                skipEntry(input, size);
                continue;
            }
            File target = safeTarget(staging, base, name);
            if (type == '5') {
                Files.createDirectories(target.toPath());
                skipEntry(input, size);
                stats.dirs += 1;
            } else if (type == '0' || type == 0) {
                File parent = target.getParentFile();
                if (parent != null) Files.createDirectories(parent.toPath());
                if (Files.isSymbolicLink(target.toPath())) throw new IOException("arquivo não pode sobrescrever symlink");
                try (FileOutputStream out = new FileOutputStream(target, false)) {
                    copyExactly(input, out, size);
                }
                skipPadding(input, size);
                stats.files += 1;
            } else if (type == '2') {
                createSafeSymlink(staging, base, target, linkName, links);
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
        if (size < 0 || size > limit) throw new IOException("metadata TAR grande demais");
        int max = (int) size;
        byte[] data = new byte[max];
        int off = 0;
        while (off < max) {
            int n = input.read(data, off, max - off);
            if (n < 0) throw new IOException("metadata TAR truncada");
            off += n;
        }
        if (size > max) skipFully(input, size - max);
        skipPadding(input, size);
        String text = new String(data, 0, off, StandardCharsets.UTF_8);
        int zero = text.indexOf('\0');
        return zero >= 0 ? text.substring(0, zero) : text;
    }

    private static java.util.Map<String,String> parsePax(String pax) throws IOException {
        byte[] bytes = pax.getBytes(StandardCharsets.UTF_8);
        java.util.Map<String,String> values = new java.util.HashMap<>();
        int offset = 0;
        while (offset < bytes.length) {
            int space = offset;
            while (space < bytes.length && bytes[space] != ' ') space++;
            int length;
            try { length = Integer.parseInt(new String(bytes, offset, space-offset, StandardCharsets.US_ASCII)); }
            catch (NumberFormatException error) { throw new IOException("registro PAX inválido", error); }
            if (length <= space-offset+2 || length > bytes.length-offset || bytes[offset+length-1] != '\n') throw new IOException("comprimento PAX inválido");
            String record = new String(bytes, space+1, offset+length-space-2, StandardCharsets.UTF_8);
            int equal = record.indexOf('=');
            if (equal < 1 || record.indexOf('\0') >= 0) throw new IOException("registro PAX inválido");
            String key = record.substring(0,equal);
            if (key.startsWith("GNU.sparse")) throw new IOException("TAR sparse não suportado");
            values.put(key, record.substring(equal+1));
            offset += length;
        }
        return values;
    }

    private static void createSafeSymlink(File staging, String base, File target, String linkName, LinkCreator links) throws Exception {
        String link = linkName == null ? "" : linkName.replace('\\', '/');
        if (link.length() > 8192 || link.indexOf('\0') >= 0) throw new IOException("symlink inválido");
        if (link.isEmpty()) throw new IOException("symlink vazio em " + target.getName());
        if (link.startsWith("/")) throw new IOException("symlink absoluto bloqueado: " + link);
        File parent = target.getParentFile();
        if (parent != null) parent.mkdirs();
        File resolved = new File(parent == null ? staging : parent, link);
        String resolvedPath = resolved.getCanonicalPath();
        if (!resolvedPath.equals(base) && !resolvedPath.startsWith(base + File.separator)) {
            throw new IOException("symlink escapando do staging: " + link);
        }
        if (CoreLinuxRootfsFilesystem.exists(target)) CoreLinuxRootfsFilesystem.removeTree(target);
        // Link creation failure is fatal; a marker must never make this import usable.
        links.create(target.toPath(), Paths.get(link));
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
        String value = String.valueOf(name == null ? "" : name).replace('\\', '/');
        while (value.startsWith("./")) value = value.substring(2);
        while (value.startsWith("/")) throw new IOException("path absoluto bloqueado: " + clean(value, 120));
        if (value.equals(".") || value.equals("./")) return "";
        String[] parts = value.split("/");
        for (String part : parts) {
            if (part.equals("..")) throw new IOException("path com .. bloqueado: " + clean(value, 120));
        }
        return value;
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
        return new String(block, offset, Math.max(0, end - offset), StandardCharsets.UTF_8);
    }

    private static long tarOctal(byte[] block, int offset, int len) throws IOException {
        String raw = tarString(block, offset, len).trim();
        if (raw.isEmpty()) return 0L;
        if (!raw.matches("[0-7]+")) throw new IOException("campo octal TAR inválido");
        try { return Long.parseLong(raw, 8); }
        catch (NumberFormatException error) { throw new IOException("overflow TAR", error); }
    }

    private static void verifyChecksum(byte[] header) throws IOException {
        long expected = tarOctal(header, 148, 8), actual = 0;
        for (int i = 0; i < header.length; i++) actual += i >= 148 && i < 156 ? 32 : header[i] & 255;
        if (expected != actual) throw new IOException("checksum TAR inválido");
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

    static final class Stats {
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
    interface LinkCreator { void create(Path target, Path link) throws IOException; }
    static final class Limits {
        final long entries, bytes, singleFile;
        Limits(long entries, long bytes, long singleFile) {
            if (entries < 1 || bytes < 1 || singleFile < 1) throw new IllegalArgumentException("limites inválidos");
            this.entries=entries; this.bytes=bytes; this.singleFile=singleFile;
        }
    }
    private static String clean(String value, int limit) {
        String text = value == null ? "" : value;
        return text.substring(0, Math.min(text.length(), limit));
    }

}
