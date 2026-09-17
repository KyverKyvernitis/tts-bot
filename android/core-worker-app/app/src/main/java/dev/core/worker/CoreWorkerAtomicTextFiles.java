package dev.core.worker;

import android.util.AtomicFile;
import java.io.*;
import java.nio.charset.StandardCharsets;

/** Durable metadata writes plus bounded reads, including AtomicFile recovery. */
final class CoreWorkerAtomicTextFiles {
    private CoreWorkerAtomicTextFiles() { }

    static synchronized void write(File file, String text) throws IOException {
        File parent = file.getAbsoluteFile().getParentFile();
        if (!parent.isDirectory() && !parent.mkdirs()) throw new IOException("diretório indisponível");
        AtomicFile atomic = new AtomicFile(file);
        FileOutputStream stream = null;
        byte[] expected = (text == null ? "" : text).getBytes(StandardCharsets.UTF_8);
        try {
            stream = atomic.startWrite();
            stream.write(expected);
            stream.flush();
            stream.getFD().sync();
            atomic.finishWrite(stream);
            // AtomicFile may only log a failed rename. Verify the bytes exposed
            // by its recovery path before reporting a completed metadata write.
            if (!java.util.Arrays.equals(read(file, expected.length), expected)) {
                throw new IOException("texto atômico não foi promovido");
            }
        } catch (Throwable failure) {
            if (stream != null) atomic.failWrite(stream);
            throw failure;
        }
    }

    static synchronized byte[] read(File file, int limit) throws IOException {
        return readFully(new AtomicFile(file).openRead(), limit, true);
    }

    // Binary probes intentionally read a prefix, not the entire ELF/library.
    static byte[] readPrefix(File file, int limit) throws IOException {
        return readFully(new FileInputStream(file), limit, false);
    }

    static byte[] readFully(InputStream input, int limit, boolean rejectOverflow) throws IOException {
        if (limit < 0) { input.close(); throw new IllegalArgumentException("limite negativo"); }
        try (InputStream stream = input; ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[Math.max(1, Math.min(8192, limit))];
            while (out.size() < limit) {
                int count = stream.read(buffer, 0, Math.min(buffer.length, limit - out.size()));
                if (count == -1) return out.toByteArray();
                out.write(buffer, 0, count);
            }
            if (rejectOverflow && stream.read() != -1) throw new IOException("arquivo excede limite");
            return out.toByteArray();
        }
    }
}
