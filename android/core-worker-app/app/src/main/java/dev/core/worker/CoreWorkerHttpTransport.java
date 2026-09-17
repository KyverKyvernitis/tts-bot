package dev.core.worker;

import android.content.SharedPreferences;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/** Bounded control-plane JSON transport. Binary artifacts use their own limits. */
final class CoreWorkerHttpTransport {
    static final int MAX_RESPONSE_BYTES = 2 * 1024 * 1024;

    private CoreWorkerHttpTransport() { }

    static String serverUrl(SharedPreferences prefs, String fallback) {
        String saved = prefs == null ? "" : prefs.getString("server_url", "");
        String url = saved == null || saved.trim().isEmpty() ? fallback : saved;
        return url == null ? "" : url.trim().replaceAll("/+$", "");
    }

    static Result request(String method, String url, String payload, String token,
                          int connectTimeout, int readTimeout) throws IOException {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        try {
            connection.setInstanceFollowRedirects(false);
            connection.setRequestMethod(method);
            connection.setConnectTimeout(connectTimeout);
            connection.setReadTimeout(readTimeout);
            connection.setRequestProperty("Accept", "application/json");
            if (token != null && !token.trim().isEmpty()) {
                connection.setRequestProperty("Authorization", "Bearer " + token.trim());
            }
            if (payload != null) {
                byte[] bytes = payload.getBytes(StandardCharsets.UTF_8);
                connection.setDoOutput(true);
                connection.setFixedLengthStreamingMode(bytes.length);
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                try (OutputStream output = connection.getOutputStream()) { output.write(bytes); }
            }
            int status = connection.getResponseCode();
            if (connection.getContentLengthLong() > MAX_RESPONSE_BYTES) {
                throw new IOException("resposta do control-plane grande demais");
            }
            InputStream input = status >= 200 && status < 400
                    ? connection.getInputStream() : connection.getErrorStream();
            return new Result(status, readBounded(input, MAX_RESPONSE_BYTES));
        } finally {
            connection.disconnect();
        }
    }

    static String readBounded(InputStream input, int limit) throws IOException {
        if (input == null) return "";
        if (limit < 0) throw new IllegalArgumentException("limite negativo");
        try (InputStream stream = input; ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192];
            int count;
            while ((count = stream.read(buffer)) != -1) {
                if (count > limit - out.size()) throw new IOException("resposta grande demais");
                out.write(buffer, 0, count);
            }
            return out.toString(StandardCharsets.UTF_8.name());
        }
    }

    static final class Result {
        final int status;
        final String body;
        Result(int status, String body) { this.status = status; this.body = body; }
        boolean ok() { return status >= 200 && status < 300; }
    }
}
