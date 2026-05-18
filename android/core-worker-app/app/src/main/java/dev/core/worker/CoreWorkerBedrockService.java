package dev.core.worker;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.UUID;

public class CoreWorkerBedrockService extends Service {
    public static final String ACTION_START = "dev.core.worker.action.BEDROCK_RUNTIME_START";
    public static final String ACTION_STOP = "dev.core.worker.action.BEDROCK_RUNTIME_STOP";

    private static final String PREFS = "core_worker_private";
    private static final String CHANNEL_ID = "core_worker_bedrock_runtime";
    private static final int NOTIFICATION_ID = 4108;
    private static final long TICK_MS = 45L * 1000L;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean active = false;

    private final Runnable tickRunnable = new Runnable() {
        @Override
        public void run() {
            if (!active) return;
            markState("serviço Bedrock ativo · aguardando start real");
            reportHeartbeat("bedrock_service_tick");
            handler.postDelayed(this, TICK_MS);
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        createChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : String.valueOf(intent.getAction());
        if (ACTION_STOP.equals(action)) {
            active = false;
            handler.removeCallbacks(tickRunnable);
            prefs().edit()
                    .putBoolean("bedrock_runtime_service_active", false)
                    .putString("bedrock_runtime_service_state", "serviço Bedrock parado")
                    .putLong("bedrock_runtime_service_last_tick_at", System.currentTimeMillis())
                    .apply();
            stopForeground(true);
            stopSelf();
            return START_NOT_STICKY;
        }
        active = true;
        startForeground(NOTIFICATION_ID, buildNotification("Bedrock Manager ativo"));
        prefs().edit()
                .putBoolean("bedrock_runtime_service_active", true)
                .putString("bedrock_runtime_service_state", "serviço Bedrock ativo · aguardando start real")
                .putLong("bedrock_runtime_service_started_at", System.currentTimeMillis())
                .apply();
        markState("serviço Bedrock ativo · aguardando start real");
        reportHeartbeat("bedrock_service_start");
        handler.removeCallbacks(tickRunnable);
        handler.postDelayed(tickRunnable, TICK_MS);
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        active = false;
        handler.removeCallbacks(tickRunnable);
        prefs().edit()
                .putBoolean("bedrock_runtime_service_active", false)
                .putString("bedrock_runtime_service_state", "serviço Bedrock encerrado")
                .putLong("bedrock_runtime_service_last_tick_at", System.currentTimeMillis())
                .apply();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void markState(String state) {
        try {
            prefs().edit()
                    .putBoolean("bedrock_runtime_service_active", true)
                    .putString("bedrock_runtime_service_state", state == null ? "serviço Bedrock ativo" : state)
                    .putLong("bedrock_runtime_service_last_tick_at", System.currentTimeMillis())
                    .apply();
        } catch (Throwable ignored) {
        }
    }

    private void createChannel() {
        try {
            if (Build.VERSION.SDK_INT >= 26) {
                NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                if (manager != null) {
                    NotificationChannel channel = new NotificationChannel(CHANNEL_ID, "Servidor Bedrock Core Worker", NotificationManager.IMPORTANCE_LOW);
                    channel.setDescription("Mantém o gerenciador Bedrock visível enquanto o servidor estiver sendo preparado/rodado.");
                    manager.createNotificationChannel(channel);
                }
            }
        } catch (Throwable ignored) {
        }
    }

    private Notification buildNotification(String text) {
        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_NEW_TASK);
        int pendingFlags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) pendingFlags |= PendingIntent.FLAG_IMMUTABLE;
        PendingIntent pending = PendingIntent.getActivity(this, NOTIFICATION_ID, open, pendingFlags);
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        builder.setSmallIcon(android.R.drawable.stat_sys_upload_done)
                .setContentTitle("Core Worker Bedrock")
                .setContentText(text == null ? "Bedrock Manager ativo" : text)
                .setContentIntent(pending)
                .setOngoing(true)
                .setShowWhen(false);
        return builder.build();
    }

    private void reportHeartbeat(String reason) {
        new Thread(() -> {
            String serverUrl = normalizedServerUrl();
            if (serverUrl.isEmpty()) return;
            try {
                JSONObject payload = new JSONObject();
                payload.put("platform", "android");
                payload.put("source", "core-worker-apk-bedrock-service");
                payload.put("state", "bedrock_runtime_service");
                payload.put("reason", reason == null ? "bedrock_service" : reason);
                payload.put("appVersion", BuildConfig.VERSION_NAME);
                payload.put("appVersionCode", BuildConfig.VERSION_CODE);
                payload.put("versionName", BuildConfig.VERSION_NAME);
                payload.put("versionCode", BuildConfig.VERSION_CODE);
                payload.put("workerId", prefs().getString("worker_id", ""));
                payload.put("installId", installId());
                payload.put("deviceName", prefs().getString("device_name", ""));
                payload.put("runtime_mode", "apk-bedrock-assisted-foreground-runtime");
                payload.put("jobsRuntime", "bedrock-foreground-service-visible");
                JSONObject status = new JSONObject();
                status.put("bedrock_runtime_service_active", true);
                status.put("bedrock_server_mode", "assisted-local-preflight");
                status.put("notification_permission", hasNotificationPermission() ? "granted" : "missing");
                status.put("termux_required_now", false);
                payload.put("status", status);
                request("POST", serverUrl + "/core-worker/app/heartbeat", payload);
            } catch (Throwable ignored) {
            }
        }, "core-worker-bedrock-heartbeat").start();
    }

    private boolean hasNotificationPermission() {
        return Build.VERSION.SDK_INT < 33 || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
    }

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, MODE_PRIVATE);
    }

    private String installId() {
        String id = prefs().getString("install_id", "");
        if (id == null || id.trim().isEmpty()) {
            id = UUID.randomUUID().toString();
            prefs().edit().putString("install_id", id).apply();
        }
        return id;
    }

    private static String normalizedServerUrl() {
        String url = BuildConfig.CORE_WORKER_VPS_URL == null ? "" : BuildConfig.CORE_WORKER_VPS_URL.trim();
        return url.replaceAll("/+$", "");
    }

    private HttpResult request(String method, String url, JSONObject payload) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setRequestMethod(method);
        conn.setConnectTimeout(7000);
        conn.setReadTimeout(9000);
        conn.setRequestProperty("Accept", "application/json");
        if (payload != null) {
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            OutputStream output = conn.getOutputStream();
            output.write(payload.toString().getBytes(StandardCharsets.UTF_8));
            output.flush();
            output.close();
        }
        int status = conn.getResponseCode();
        InputStream input = status >= 200 && status < 400 ? conn.getInputStream() : conn.getErrorStream();
        String body = readAll(input);
        conn.disconnect();
        return new HttpResult(status, body == null ? "" : body);
    }

    private String readAll(InputStream input) throws Exception {
        if (input == null) return "";
        BufferedReader reader = new BufferedReader(new InputStreamReader(input, StandardCharsets.UTF_8));
        StringBuilder builder = new StringBuilder();
        String line;
        while ((line = reader.readLine()) != null) {
            if (builder.length() > 0) builder.append('\n');
            builder.append(line);
        }
        reader.close();
        return builder.toString();
    }

    private static final class HttpResult {
        final int status;
        final String body;
        HttpResult(int status, String body) {
            this.status = status;
            this.body = body;
        }
    }
}
