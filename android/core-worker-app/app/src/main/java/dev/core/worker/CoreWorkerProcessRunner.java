package dev.core.worker;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.TimeUnit;

/** Drains both pipes while a bounded, timed subprocess runs; owns every stream. */
final class CoreWorkerProcessRunner {
    private CoreWorkerProcessRunner() { }

    interface Starter { Process start() throws IOException; }

    static Result run(ProcessBuilder builder, long timeoutMs, int outputLimit) throws Exception {
        return run(builder::start, timeoutMs, outputLimit, Thread::new);
    }

    static Result run(Starter starter, long timeoutMs, int outputLimit,
                      java.util.concurrent.ThreadFactory threads) throws Exception {
        if (timeoutMs <= 0 || outputLimit < 0) throw new IllegalArgumentException("limites de subprocesso inválidos");
        Process process = starter.start();
        Thread out = null, err = null;
        boolean finished = false;
        try {
            // Every operation after start, including thread allocation, belongs
            // to this cleanup scope.
            Collector stdout = new Collector(process, process.getInputStream(), outputLimit);
            Collector stderr = new Collector(process, process.getErrorStream(), outputLimit);
            out = daemon(threads, stdout, "core-worker-process-out");
            err = daemon(threads, stderr, "core-worker-process-err");
            process.getOutputStream().close();
            out.start(); err.start();
            finished = process.waitFor(timeoutMs, TimeUnit.MILLISECONDS);
            if (!finished) terminate(process);
            out.join(1000L); err.join(1000L);
            boolean incomplete = out.isAlive() || err.isAlive();
            if (incomplete) { out.interrupt(); err.interrupt(); joinCleanup(out); joinCleanup(err); }
            return new Result(finished ? process.exitValue() : 124, stdout.text(), stderr.text(), !finished,
                    incomplete || stdout.truncated || stderr.truncated || stdout.error != null || stderr.error != null,
                    stdout.error, stderr.error);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw error;
        } finally {
            if (!finished || process.isAlive()) terminate(process);
            if (out != null) out.interrupt();
            if (err != null) err.interrupt();
            joinCleanup(out); joinCleanup(err);
            close(process.getInputStream()); close(process.getErrorStream()); close(process.getOutputStream());
        }
    }

    private static Thread daemon(java.util.concurrent.ThreadFactory factory, Runnable task, String name) {
        Thread thread = factory.newThread(task);
        if (thread == null) throw new IllegalStateException("collector indisponível");
        thread.setName(name); thread.setDaemon(true); return thread;
    }

    private static void joinCleanup(Thread thread) {
        if (thread == null) return;
        boolean interrupted = Thread.interrupted();
        try { thread.join(500L); }
        catch (InterruptedException error) { interrupted = true; }
        finally { if (interrupted) Thread.currentThread().interrupt(); }
    }

    private static void terminate(Process process) {
        boolean interrupted = Thread.interrupted();
        process.destroy();
        try {
            if (!process.waitFor(200L, TimeUnit.MILLISECONDS)) {
                process.destroyForcibly(); process.waitFor(800L, TimeUnit.MILLISECONDS);
            }
        } catch (InterruptedException error) {
            process.destroyForcibly(); interrupted = true;
        } finally { if (interrupted) Thread.currentThread().interrupt(); }
    }

    private static void close(Closeable stream) { try { stream.close(); } catch (IOException ignored) { } }

    static final class Result {
        final int exitCode;
        final String stdout, stderr;
        final boolean timedOut, truncated;
        final IOException stdoutError, stderrError;
        Result(int exitCode, String stdout, String stderr, boolean timedOut, boolean truncated,
               IOException stdoutError, IOException stderrError) {
            this.exitCode=exitCode; this.stdout=stdout; this.stderr=stderr;
            this.timedOut=timedOut; this.truncated=truncated;
            this.stdoutError=stdoutError; this.stderrError=stderrError;
        }
    }

    private static final class Collector implements Runnable {
        private final Process process;
        private final InputStream input;
        private final int limit;
        private final ByteArrayOutputStream output = new ByteArrayOutputStream();
        volatile boolean truncated;
        volatile IOException error;
        Collector(Process process, InputStream input, int limit) { this.process=process; this.input=input; this.limit=limit; }
        public void run() {
            try {
                byte[] buffer = new byte[8192]; int count;
                // Process pipes support available(). Reading only ready bytes
                // avoids an uninterruptible read held open by an unowned child.
                // Drain bytes already in the pipe when the owned process exits.
                while (!Thread.currentThread().isInterrupted()) {
                    int ready = input.available();
                    if (ready == 0) {
                        if (!process.isAlive()) break;
                        Thread.sleep(5L);
                        continue;
                    }
                    count = input.read(buffer, 0, Math.min(buffer.length, ready));
                    if (count == -1) break;
                    synchronized (this) {
                        int keep = Math.min(count, limit - output.size());
                        output.write(buffer, 0, keep);
                        if (keep < count) truncated=true;
                    }
                    // Continue draining even after the retained output reaches its limit.
                }
            } catch (IOException failure) { error = failure; }
            catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); }
        }
        synchronized String text() { return new String(output.toByteArray(), StandardCharsets.UTF_8); }
    }
}
