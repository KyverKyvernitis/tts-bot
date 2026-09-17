package dev.core.worker;

import java.util.concurrent.Callable;
import java.util.concurrent.Future;
import java.util.concurrent.SynchronousQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/** One native call at a time, including calls which ignore cancellation. */
final class CoreWorkerNativeCalls {
    private static final ThreadPoolExecutor EXECUTOR = new ThreadPoolExecutor(
            0, 1, 30L, TimeUnit.SECONDS, new SynchronousQueue<>(), task -> {
                Thread thread = new Thread(task, "core-worker-jni");
                thread.setDaemon(true);
                return thread;
            }, new ThreadPoolExecutor.AbortPolicy());

    private CoreWorkerNativeCalls() { }

    static <T> T call(Callable<T> task, long timeoutMs) throws Exception {
        if (timeoutMs <= 0L) throw new IllegalArgumentException("timeout JNI inválido");
        Future<T> future = EXECUTOR.submit(task);
        try {
            return future.get(timeoutMs, TimeUnit.MILLISECONDS);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            throw interrupted;
        } finally {
            // Cancellation interrupts the running callable, but does not free
            // the executor's only slot until the actual native call returns.
            if (!future.isDone()) future.cancel(true);
        }
    }
}
