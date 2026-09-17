package dev.core.worker;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.atomic.AtomicReference;
import static dev.core.worker.WorkerHarness.*;

final class ProcessHarness {
    static List<String> command(String... args) {
        List<String> command = new ArrayList<>(Arrays.asList(
                new File(System.getProperty("java.home"), "bin/java").getPath(),
                "-cp", System.getProperty("java.class.path"), ProcessProbe.class.getName()));
        command.addAll(Arrays.asList(args));
        return command;
    }
    static long pid(Path path) throws Exception {
        long deadline = System.nanoTime() + 4_000_000_000L;
        while ((!Files.isRegularFile(path) || Files.size(path)==0) && System.nanoTime()<deadline) Thread.sleep(10);
        return Long.parseLong(Files.readString(path));
    }
    static boolean alive(long pid) { return ProcessHandle.of(pid).map(ProcessHandle::isAlive).orElse(false); }
    static void reaped(long pid) throws Exception {
        long deadline = System.nanoTime()+2_000_000_000L;
        while (alive(pid) && System.nanoTime()<deadline) Thread.sleep(10);
        check(!alive(pid), "child still alive after cleanup");
    }
    static void run(String test) throws Exception {
        Path root = Files.createTempDirectory("worker-process-");
        Path pidFile = root.resolve("pid");
        try {
            if (test.equals("collector_failure")) {
                java.util.concurrent.atomic.AtomicInteger created = new java.util.concurrent.atomic.AtomicInteger();
                AtomicReference<Process> started = new AtomicReference<>();
                fails(() -> CoreWorkerProcessRunner.run(() -> {
                    Process child = new ProcessBuilder(command("continuous", pidFile.toString())).start();
                    started.set(child); return child;
                }, 5000, 1024, runnable -> {
                    if (created.incrementAndGet()==2) throw new IllegalStateException("injected collector failure");
                    return new Thread(runnable);
                }));
                check(started.get()!=null, "test never acquired process");
                reaped(started.get().pid());
            } else if (test.equals("start_failure")) {
                fails(() -> CoreWorkerProcessRunner.run(new ProcessBuilder(root.resolve("missing-binary").toString()), 1000, 1));
            } else if (test.equals("flood")) {
                CoreWorkerProcessRunner.Result result = CoreWorkerProcessRunner.run(new ProcessBuilder(command("flood")), 5000, 4096);
                check(result.exitCode==0 && !result.timedOut && result.truncated, "flood deadlocked or failed");
                check(result.stdout.length()==4096 && result.stderr.length()==4096, "pipe limit ignored");
            } else if (test.equals("limits")) {
                for (String value : new String[]{"", "abc", "abcd", "éé"}) {
                    CoreWorkerProcessRunner.Result result = CoreWorkerProcessRunner.run(new ProcessBuilder(command("bytes", value)), 3000, 3);
                    byte[] bytes = value.getBytes(StandardCharsets.UTF_8);
                    check(result.stdout.equals(new String(Arrays.copyOf(bytes, Math.min(bytes.length,3)), StandardCharsets.UTF_8)), "text contract changed");
                    check(result.truncated == (bytes.length>3), "incorrect truncation flag");
                }
            } else if (test.equals("interrupt")) {
                AtomicReference<Throwable> problem = new AtomicReference<>();
                Thread caller = new Thread(() -> {
                    try {
                        CoreWorkerProcessRunner.run(new ProcessBuilder(command("continuous", pidFile.toString())), 10000, 1024);
                        problem.set(new AssertionError("interruption swallowed"));
                    } catch (InterruptedException expected) {
                        if (!Thread.currentThread().isInterrupted()) problem.set(new AssertionError("interrupt flag lost"));
                    } catch (Throwable error) { problem.set(error); }
                });
                caller.start(); long child = pid(pidFile); caller.interrupt(); caller.join(4000);
                check(!caller.isAlive() && problem.get()==null, "interrupted caller leaked: "+problem.get());
                reaped(child);
            } else if (test.equals("descendant")) {
                long start = System.nanoTime();
                CoreWorkerProcessRunner.Result result = CoreWorkerProcessRunner.run(new ProcessBuilder(command("descendant", pidFile.toString())), 3000, 1024);
                long child = pid(pidFile);
                try { check((System.nanoTime()-start)<5_000_000_000L && result.exitCode==0, "descendant pipe blocked return"); }
                finally { ProcessHandle.of(child).ifPresent(ProcessHandle::destroyForcibly); }
            } else {
                check(test.equals("timeout"), "unknown process test");
                CoreWorkerProcessRunner.Result result = CoreWorkerProcessRunner.run(new ProcessBuilder(command("continuous", pidFile.toString())), 750, 1024);
                check(result.timedOut && result.exitCode==124, "timeout result changed");
                reaped(pid(pidFile));
            }
            long deadline = System.nanoTime()+2_000_000_000L;
            while (collectors()!=0 && System.nanoTime()<deadline) Thread.sleep(10);
            check(collectors()==0, "collector threads survived call");
        } finally {
            if (Files.isRegularFile(pidFile) && Files.size(pidFile)>0) ProcessHandle.of(Long.parseLong(Files.readString(pidFile))).ifPresent(ProcessHandle::destroyForcibly);
            CoreLinuxRootfsFilesystem.removeTree(root.toFile());
        }
    }
    static long collectors() {
        return Thread.getAllStackTraces().keySet().stream().filter(t -> t.isAlive() && t.getName().startsWith("core-worker-process-")).count();
    }
}
