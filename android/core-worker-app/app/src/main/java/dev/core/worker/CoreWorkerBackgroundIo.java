package dev.core.worker;

import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/** Bounded best-effort telemetry and one coalesced FCM registration lane. */
final class CoreWorkerBackgroundIo {
    private static final ThreadPoolExecutor REPORTS = new ThreadPoolExecutor(1, 1, 20L, TimeUnit.SECONDS,
            new ArrayBlockingQueue<>(16), runnable -> {
                Thread thread = new Thread(runnable, "core-worker-telemetry");
                thread.setDaemon(true); return thread;
            }, new ThreadPoolExecutor.AbortPolicy());
    private static Runnable pendingRegistration;
    private static boolean registering;
    static { REPORTS.allowCoreThreadTimeOut(true); }
    private CoreWorkerBackgroundIo() { }

    static boolean report(Runnable task) {
        try { REPORTS.execute(task); return true; }
        catch (java.util.concurrent.RejectedExecutionException error) { return false; }
    }

    static synchronized void latestRegistration(Runnable task) {
        pendingRegistration = task;
        if (registering) return;
        registering = true;
        try {
            Thread thread = new Thread(CoreWorkerBackgroundIo::drainRegistrations, "core-worker-fcm-registration");
            thread.setDaemon(true);
            thread.start();
        } catch (Throwable error) { registering = false; }
    }

    private static void drainRegistrations() {
        while (true) {
            Runnable task;
            synchronized (CoreWorkerBackgroundIo.class) {
                task = pendingRegistration;
                pendingRegistration = null;
                if (task == null) { registering = false; return; }
            }
            try { task.run(); } catch (Throwable ignored) { }
        }
    }
}
