package dev.core.worker;

import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicReference;
import static dev.core.worker.WorkerHarness.*;

final class NativeCallsHarness {
    static void run(String test) throws Exception {
        CountDownLatch entered = new CountDownLatch(1), release = new CountDownLatch(1), exited = new CountDownLatch(1);
        AtomicReference<Throwable> problem = new AtomicReference<>();
        Callable<String> uncooperative = () -> {
            entered.countDown();
            while (release.getCount()!=0) {
                try { release.await(); } catch (InterruptedException ignored) { }
            }
            exited.countDown(); return "done";
        };
        Thread caller = new Thread(() -> {
            try { CoreWorkerNativeCalls.call(uncooperative, test.equals("timeout") ? 250 : 10000); problem.set(new AssertionError("call unexpectedly returned")); }
            catch (TimeoutException expected) { if (!test.equals("timeout")) problem.set(expected); }
            catch (InterruptedException expected) { if (!test.equals("interrupt") || !Thread.currentThread().isInterrupted()) problem.set(expected); }
            catch (Throwable error) { problem.set(error); }
        });
        try {
            caller.start(); await(entered);
            if (test.equals("interrupt")) caller.interrupt();
            caller.join(2000);
            check(!caller.isAlive() && problem.get()==null, "native caller failed: "+problem.get());
            for (int i=0;i<32;i++) {
                try { CoreWorkerNativeCalls.call(() -> "wrong", 100); throw new AssertionError("busy slot accepted another call"); }
                catch (RejectedExecutionException expected) { }
            }
            long nativeThreads = Thread.getAllStackTraces().keySet().stream().filter(t -> t.isAlive() && t.getName().equals("core-worker-jni") && t.isDaemon()).count();
            check(nativeThreads==1, "native calls accumulated threads or used non-daemon worker");
        } finally { release.countDown(); caller.join(2000); }
        await(exited);
        long deadline = System.nanoTime()+2_000_000_000L;
        while (true) {
            try { check(CoreWorkerNativeCalls.call(() -> "next", 1000).equals("next"), "slot never recovered"); break; }
            catch (RejectedExecutionException busy) { if (System.nanoTime()>deadline) throw busy; Thread.sleep(5); }
        }
    }
}
