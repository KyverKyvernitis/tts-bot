package dev.core.worker;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
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

public class CoreWorkerRuntimeService extends Service {
    public static final String ACTION_START = "dev.core.worker.action.RUNTIME_START";
    public static final String ACTION_STOP = "dev.core.worker.action.RUNTIME_STOP";
    public static final String ACTION_TICK = "dev.core.worker.action.RUNTIME_TICK";

    private static final String PREFS = "core_worker_private";
    private static final String CHANNEL_ID = "core_worker_runtime";
    private static final int NOTIFICATION_ID = 4107;
    private static final long TICK_MS = 60L * 1000L;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean running = false;
    private NativeTtsManager nativeTtsManager;
    private LocalNativeTtsHttpServer nativeTtsServer;

    private final Runnable tickRunnable = new Runnable() {
        @Override
        public void run() {
            if (!running) {
                return;
            }
            markTick("serviço persistente ativo");
            reportHeartbeat("foreground_tick");
            handler.postDelayed(this, TICK_MS);
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        createChannel();
        startNativeTtsBridge();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : String.valueOf(intent.getAction());
        if (ACTION_STOP.equals(action)) {
            running = false;
            handler.removeCallbacks(tickRunnable);
            prefs().edit()
                    .putBoolean("foreground_runtime_active", false)
                    .putString("foreground_runtime_state", "serviço persistente parado")
                    .putLong("foreground_runtime_last_tick_at", System.currentTimeMillis())
                    .apply();
            stopForeground(true);
            stopSelf();
            return START_NOT_STICKY;
        }
        running = true;
        startForeground(NOTIFICATION_ID, buildNotification("Runtime persistente ativo"));
        prefs().edit()
                .putBoolean("foreground_runtime_active", true)
                .putString("foreground_runtime_state", "serviço persistente ativo")
                .putLong("foreground_runtime_started_at", System.currentTimeMillis())
                .apply();
        markTick("serviço persistente ativo");
        reportHeartbeat(intent == null ? "foreground_start" : intent.getStringExtra("reason"));
        handler.removeCallbacks(tickRunnable);
        handler.postDelayed(tickRunnable, TICK_MS);
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        handler.removeCallbacks(tickRunnable);
        stopNativeTtsBridge();
        prefs().edit()
                .putBoolean("foreground_runtime_active", false)
                .putString("foreground_runtime_state", "serviço persistente encerrado")
                .putLong("foreground_runtime_last_tick_at", System.currentTimeMillis())
                .apply();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }


    private void startNativeTtsBridge() {
        try {
            if (nativeTtsManager == null) {
                nativeTtsManager = new NativeTtsManager(getApplicationContext(), prefs());
                nativeTtsManager.warmUp();
            }
            if (nativeTtsServer == null) {
                nativeTtsServer = new LocalNativeTtsHttpServer(nativeTtsManager);
                nativeTtsServer.start();
            }
            prefs().edit()
                    .putBoolean("native_tts_bridge_active", true)
                    .putLong("native_tts_bridge_started_at", System.currentTimeMillis())
                    .apply();
        } catch (Throwable exc) {
            prefs().edit()
                    .putBoolean("native_tts_bridge_active", false)
                    .putString("native_tts_bridge_error", String.valueOf(exc.getMessage()))
                    .putLong("native_tts_bridge_started_at", System.currentTimeMillis())
                    .apply();
        }
    }

    private void stopNativeTtsBridge() {
        try {
            if (nativeTtsServer != null) {
                nativeTtsServer.stop();
                nativeTtsServer = null;
            }
        } catch (Throwable ignored) {
        }
        try {
            if (nativeTtsManager != null) {
                nativeTtsManager.shutdown();
                nativeTtsManager = null;
            }
        } catch (Throwable ignored) {
        }
        try {
            prefs().edit()
                    .putBoolean("native_tts_bridge_active", false)
                    .putLong("native_tts_bridge_stopped_at", System.currentTimeMillis())
                    .apply();
        } catch (Throwable ignored) {
        }
    }

    private void markTick(String state) {
        try {
            prefs().edit()
                    .putBoolean("foreground_runtime_active", true)
                    .putString("foreground_runtime_state", state == null ? "serviço persistente ativo" : state)
                    .putLong("foreground_runtime_last_tick_at", System.currentTimeMillis())
                    .apply();
        } catch (Throwable ignored) {
        }
    }

    private void createChannel() {
        try {
            if (Build.VERSION.SDK_INT >= 26) {
                NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                if (manager != null) {
                    NotificationChannel channel = new NotificationChannel(CHANNEL_ID, "Runtime persistente Core Worker", NotificationManager.IMPORTANCE_LOW);
                    channel.setDescription("Mantém o Core Worker acordado para jobs locais e servidor futuro.");
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
        if (Build.VERSION.SDK_INT >= 23) {
            pendingFlags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent pending = PendingIntent.getActivity(this, NOTIFICATION_ID, open, pendingFlags);
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        builder.setSmallIcon(android.R.drawable.stat_sys_upload_done)
                .setContentTitle("Core Worker ativo")
                .setContentText(text == null ? "Runtime persistente ativo" : text)
                .setContentIntent(pending)
                .setOngoing(true)
                .setShowWhen(false);
        return builder.build();
    }

    private void reportHeartbeat(String reason) {
        new Thread(() -> {
            String serverUrl = normalizedServerUrl();
            if (serverUrl.isEmpty()) {
                return;
            }
            try {
                JSONObject payload = new JSONObject();
                payload.put("platform", "android");
                payload.put("source", "core-worker-apk-foreground-service");
                payload.put("state", "foreground_runtime");
                payload.put("reason", reason == null || reason.trim().isEmpty() ? "foreground" : reason);
                payload.put("appVersion", BuildConfig.VERSION_NAME);
                payload.put("appVersionCode", BuildConfig.VERSION_CODE);
                payload.put("versionName", BuildConfig.VERSION_NAME);
                payload.put("versionCode", BuildConfig.VERSION_CODE);
                payload.put("workerId", prefs().getString("worker_id", ""));
                payload.put("installId", installId());
                payload.put("deviceName", prefs().getString("device_name", ""));
                payload.put("runtime_mode", "apk-native-python-linux-assisted-runtime");
                payload.put("jobsRuntime", "foreground-service-visible-runtime");
                JSONObject status = new JSONObject();
                status.put("app", "foreground-service");
                status.put("foreground_runtime_active", true);
                status.put("notification_permission", hasNotificationPermission() ? "granted" : "missing");
                status.put("termux_required_now", false);
                status.put("bedrock_server_mode", "future-foreground-service");
                status.put("native_tts_bridge_active", nativeTtsServer != null);
                if (nativeTtsManager != null) {
                    status.put("android_tts", nativeTtsManager.statusJson());
                }
                payload.put("status", status);
                request("POST", serverUrl + "/core-worker/app/heartbeat", payload);
            } catch (Throwable ignored) {
            }
        }, "core-worker-foreground-heartbeat").start();
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
