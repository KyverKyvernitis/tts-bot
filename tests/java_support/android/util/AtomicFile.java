package android.util;

import java.io.*;
import java.nio.file.*;

/** JVM boundary fake for Android AtomicFile, including failed writes and old .bak recovery. */
public final class AtomicFile {
    public static boolean failNextWrite;
    public static boolean ignoreNextFinish;
    private final File base, next;
    public AtomicFile(File file) { base=file; next=new File(file+".new"); }
    public FileOutputStream startWrite() throws IOException {
        if (failNextWrite) {
            failNextWrite=false;
            return new FileOutputStream(next) {
                @Override public void write(byte[] bytes) throws IOException {
                    super.write(bytes,0,Math.min(1,bytes.length)); throw new IOException("injected disk failure");
                }
            };
        }
        return new FileOutputStream(next);
    }
    public void finishWrite(FileOutputStream stream) {
        if (ignoreNextFinish) {
            ignoreNextFinish = false;
            try { stream.close(); } catch (IOException ignored) { }
            return; // Android can log a failed rename without throwing.
        }
        try { stream.close(); Files.move(next.toPath(),base.toPath(),StandardCopyOption.REPLACE_EXISTING); }
        catch(IOException error){throw new java.io.UncheckedIOException(error);}
    }
    public void failWrite(FileOutputStream stream) {
        try {stream.close();Files.deleteIfExists(next.toPath());}catch(IOException error){throw new java.io.UncheckedIOException(error);}
    }
    public void delete() { base.delete(); next.delete(); new File(base+".bak").delete(); }
    public FileInputStream openRead() throws IOException {
        Path backup=Path.of(base+".bak");
        if(Files.exists(backup))Files.move(backup,base.toPath(),StandardCopyOption.REPLACE_EXISTING);
        return new FileInputStream(base);
    }
}
