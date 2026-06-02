package dev.core.worker;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.Locale;

final class LocalNativeTtsHttpServer {
    private static final int DEFAULT_PORT = 8877;
    private static final int MAX_BODY_BYTES = 128 * 1024;
    private static final int HEADER_LIMIT_BYTES = 32 * 1024;

    private final NativeTtsManager ttsManager;
    private final int port;
    private volatile boolean running = false;
    private volatile ServerSocket serverSocket;
    private volatile Thread serverThread;

    LocalNativeTtsHttpServer(NativeTtsManager ttsManager) {
        this(ttsManager, DEFAULT_PORT);
    }

    LocalNativeTtsHttpServer(NativeTtsManager ttsManager, int port) {
        this.ttsManager = ttsManager;
        this.port = port <= 0 ? DEFAULT_PORT : port;
    }

    void start() {
        if (running) {
            return;
        }
        running = true;
        serverThread = new Thread(this::runLoop, "core-worker-native-tts-http");
        serverThread.setDaemon(true);
        serverThread.start();
    }

    void stop() {
        running = false;
        try {
            ServerSocket socket = serverSocket;
            if (socket != null) {
                socket.close();
            }
        } catch (Throwable ignored) {
        }
    }

    private void runLoop() {
        try (ServerSocket socket = new ServerSocket()) {
            serverSocket = socket;
            socket.setReuseAddress(true);
            socket.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), port));
            while (running) {
                Socket client = socket.accept();
                Thread handler = new Thread(() -> handleClient(client), "core-worker-native-tts-request");
                handler.setDaemon(true);
                handler.start();
            }
        } catch (Throwable ignored) {
            running = false;
        }
    }

    private void handleClient(Socket socket) {
        try (Socket client = socket) {
            client.setSoTimeout(16000);
            Request request = readRequest(client.getInputStream());
            if (request == null) {
                return;
            }
            String path = request.path;
            if ("GET".equals(request.method) && "/native-tts/status".equals(path)) {
                JSONObject status = ttsManager.statusJson();
                status.put("http_port", port);
                writeJson(client.getOutputStream(), 200, status);
                return;
            }
            if ("POST".equals(request.method) && "/native-tts/synthesize".equals(path)) {
                JSONObject body = request.body == null || request.body.trim().isEmpty()
                        ? new JSONObject()
                        : new JSONObject(request.body);
                JSONObject result = ttsManager.synthesize(body);
                result.put("http_port", port);
                writeJson(client.getOutputStream(), 200, result);
                return;
            }
            JSONObject error = new JSONObject();
            error.put("ok", false);
            error.put("error", "rota não encontrada");
            writeJson(client.getOutputStream(), 404, error);
        } catch (Throwable exc) {
            try {
                JSONObject error = new JSONObject();
                error.put("ok", false);
                error.put("error", shortText(exc));
                writeJson(socket.getOutputStream(), 500, error);
            } catch (Throwable ignored) {
            }
        }
    }

    private Request readRequest(InputStream input) throws Exception {
        ByteArrayOutputStream headerBuffer = new ByteArrayOutputStream();
        int matched = 0;
        byte[] end = new byte[]{'\r', '\n', '\r', '\n'};
        while (headerBuffer.size() < HEADER_LIMIT_BYTES) {
            int b = input.read();
            if (b < 0) {
                return null;
            }
            headerBuffer.write(b);
            if ((byte) b == end[matched]) {
                matched++;
                if (matched == end.length) {
                    break;
                }
            } else {
                matched = ((byte) b == end[0]) ? 1 : 0;
            }
        }
        String headerText = headerBuffer.toString(StandardCharsets.ISO_8859_1.name());
        String[] lines = headerText.split("\\r?\\n");
        if (lines.length == 0) {
            return null;
        }
        String[] first = lines[0].split(" ");
        if (first.length < 2) {
            return null;
        }
        String method = first[0].trim().toUpperCase(Locale.US);
        String path = first[1].trim();
        int queryPos = path.indexOf('?');
        if (queryPos >= 0) {
            path = path.substring(0, queryPos);
        }
        int contentLength = 0;
        for (String line : lines) {
            int idx = line.indexOf(':');
            if (idx <= 0) {
                continue;
            }
            String name = line.substring(0, idx).trim().toLowerCase(Locale.US);
            String value = line.substring(idx + 1).trim();
            if ("content-length".equals(name)) {
                try {
                    contentLength = Math.max(0, Integer.parseInt(value));
                } catch (Throwable ignored) {
                    contentLength = 0;
                }
            }
        }
        if (contentLength > MAX_BODY_BYTES) {
            throw new IllegalArgumentException("requisição grande demais");
        }
        byte[] bodyBytes = new byte[contentLength];
        int offset = 0;
        while (offset < contentLength) {
            int read = input.read(bodyBytes, offset, contentLength - offset);
            if (read < 0) {
                break;
            }
            offset += read;
        }
        String body = new String(bodyBytes, 0, offset, StandardCharsets.UTF_8);
        return new Request(method, path, body);
    }

    private static void writeJson(OutputStream output, int status, JSONObject payload) throws Exception {
        byte[] body = payload.toString().getBytes(StandardCharsets.UTF_8);
        String statusText = status >= 200 && status < 300 ? "OK" : "ERROR";
        String header = "HTTP/1.1 " + status + " " + statusText + "\r\n"
                + "Content-Type: application/json; charset=utf-8\r\n"
                + "Connection: close\r\n"
                + "Content-Length: " + body.length + "\r\n\r\n";
        output.write(header.getBytes(StandardCharsets.UTF_8));
        output.write(body);
        output.flush();
    }

    private static String shortText(Throwable exc) {
        String text = exc == null ? "erro desconhecido" : exc.getClass().getSimpleName() + ": " + String.valueOf(exc.getMessage());
        text = text.replace('\n', ' ').replace('\r', ' ').trim();
        return text.length() > 180 ? text.substring(0, 180) : text;
    }

    private static final class Request {
        final String method;
        final String path;
        final String body;

        Request(String method, String path, String body) {
            this.method = method;
            this.path = path;
            this.body = body;
        }
    }
}
