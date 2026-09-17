package dev.core.worker;

import java.io.IOException;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.Set;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/** Owns sockets and a bounded client pool for one start/stop generation. */
final class CoreWorkerSocketListener {
    interface Handler { void handle(Socket socket); }
    interface Failure { void accept(Throwable error); }
    private final String name;
    private final int workers;
    private final int queueSize;
    private Generation current;

    CoreWorkerSocketListener(String name, int workers, int queueSize) {
        this.name = name; this.workers = workers; this.queueSize = queueSize;
    }

    synchronized void start(ServerSocket boundSocket, int timeout, Handler handler, Failure failure) throws IOException {
        if (isRunning()) { boundSocket.close(); return; }
        Generation generation = new Generation(boundSocket);
        current = generation;
        try {
            generation.thread = new Thread(() -> acceptLoop(generation, timeout, handler, failure), name);
            generation.thread.setDaemon(true);
            generation.thread.start();
        } catch (Throwable error) {
            current = null;
            generation.close();
            throw error;
        }
    }

    synchronized boolean isRunning() {
        return current != null && !current.socket.isClosed();
    }

    synchronized void stop() {
        Generation generation = current;
        current = null;
        if (generation != null) generation.close();
    }

    private void acceptLoop(Generation generation, int timeout, Handler handler, Failure failure) {
        Throwable problem = null;
        try {
            while (!generation.socket.isClosed()) {
                Socket socket = generation.socket.accept();
                generation.clients.add(socket);
                try {
                    socket.setSoTimeout(timeout);
                    generation.pool.execute(() -> {
                        try { handler.handle(socket); }
                        finally { generation.clients.remove(socket); closeSocket(socket); }
                    });
                } catch (Throwable error) {
                    generation.clients.remove(socket);
                    closeSocket(socket);
                    if (!(error instanceof java.util.concurrent.RejectedExecutionException)) throw error;
                }
            }
        } catch (Throwable error) {
            problem = error;
        } finally {
            synchronized (this) {
                // A late accept loop must not stop a newer listener or change its status.
                if (current == generation) {
                    current = null;
                    generation.close();
                    failure.accept(problem == null ? new IOException("listener encerrado") : problem);
                } else generation.close();
            }
        }
    }

    private static void closeSocket(Socket socket) {
        try { socket.close(); } catch (IOException ignored) { }
    }

    private final class Generation {
        final ServerSocket socket;
        final Set<Socket> clients = ConcurrentHashMap.newKeySet();
        final ThreadPoolExecutor pool;
        Thread thread;
        Generation(ServerSocket socket) {
            this.socket = socket;
            pool = new ThreadPoolExecutor(workers, workers, 0L, TimeUnit.MILLISECONDS,
                    new ArrayBlockingQueue<>(queueSize), runnable -> {
                        Thread thread = new Thread(runnable, name + "-client");
                        thread.setDaemon(true);
                        return thread;
                    }, new ThreadPoolExecutor.AbortPolicy());
        }
        void close() {
            try { socket.close(); } catch (IOException ignored) { }
            pool.shutdownNow();
            for (Socket client : clients) closeSocket(client);
            clients.clear();
            if (thread != null) thread.interrupt();
        }
    }
}
