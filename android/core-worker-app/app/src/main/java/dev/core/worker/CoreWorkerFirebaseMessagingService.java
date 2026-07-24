package dev.core.worker;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;

import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.UUID;

public class CoreWorkerFirebaseMessagingService extends FirebaseMessagingService {
    private static final String PREFS = "core_worker_private";
    private static final String CHANNEL_ID = "core_worker_updates";
    private static final int NOTIFICATION_ID = 4102;
    private static final boolean FCM_SERVICE_ENABLED = BuildConfig.CORE_WORKER_FCM_ENABLED;

    @Override
    public void onNewToken(String token) {
        try {
            super.onNewToken(token);
        } catch (Throwable ignored) {
        }
        if (!FCM_SERVICE_ENABLED) {
            try {
                prefs().edit().putString("fcm_state", "desativado no build").apply();
            } catch (Throwable ignored) {
            }
            return;
        }
        try {
            prefs().edit()
                    .putString("fcm_token", token == null ? "" : token)
                    .putString("fcm_state", token == null || token.trim().isEmpty() ? "token vazio" : "ativo")
                    .remove("fcm_disabled_until")
                    .apply();
            registerToken(token, "on_new_token");
        } catch (Throwable ignored) {
        }
    }

    @Override
    public void onMessageReceived(RemoteMessage message) {
        try {
            super.onMessageReceived(message);
        } catch (Throwable ignored) {
        }
        if (!FCM_SERVICE_ENABLED) {
            return;
        }
        try {
            handleMessageReceived(message);
        } catch (Throwable exc) {
            try {
                prefs().edit().putString("fcm_state", "erro no serviço: " + exc.getClass().getSimpleName()).apply();
            } catch (Throwable ignored) {
            }
        }
    }

    private void handleMessageReceived(RemoteMessage message) {
        Map<String, String> data = message == null ? null : message.getData();
        String type = data == null ? "" : str(data.get("type"));
        String notificationId = data == null ? "" : str(data.get("notificationId"));
        String versionName = data == null ? "" : str(data.get("versionName"));
        int versionCode = intValue(data == null ? null : data.get("versionCode"), -1);
        String title = "Atualização do Core Worker";
        String body = versionName.isEmpty()
                ? "Nova versão disponível para instalar."
                : "Versão " + versionName + " disponível para instalar.";
        try {
            if (message != null && message.getNotification() != null) {
                if (message.getNotification().getTitle() != null && !message.getNotification().getTitle().trim().isEmpty()) {
                    title = message.getNotification().getTitle().trim();
                }
                if (message.getNotification().getBody() != null && !message.getNotification().getBody().trim().isEmpty()) {
                    body = message.getNotification().getBody().trim();
                }
            }
        } catch (Throwable ignored) {
        }
        if (notificationId.isEmpty()) {
            notificationId = "fcm-" + (versionCode > 0 ? versionCode : BuildConfig.VERSION_CODE) + "-" + str(versionName);
        }
        report(notificationId, "fcm_received", false, versionName, versionCode, "push FCM recebido: " + (type.isEmpty() ? "data" : type));
        boolean shown = showUpdateNotification(notificationId, title, body);
        report(notificationId, shown ? "fcm_displayed" : "fcm_permission_missing", shown, versionName, versionCode, shown ? "notificação exibida por FCM" : "push recebido, mas Android não permitiu notificação visível");
        try {
            String wakeReason = type != null && type.toLowerCase().contains("job") ? "fcm_jobs_available" : "fcm_message";
            prefs().edit()
                    .putLong("internal_jobs_wake_requested_at", System.currentTimeMillis())
                    .putString("internal_jobs_wake_reason", wakeReason)
                    .apply();
            CoreWorkerUpdateJobService.schedule(this, wakeReason);
            CoreWorkerRuntimeService.requestPoll(this, wakeReason);
        } catch (Throwable ignored) {
        }
        if (shown) {
            try {
                prefs().edit().putString("last_update_notification", notificationId).apply();
            } catch (Throwable ignored) {
            }
        }
    }

    private boolean showUpdateNotification(String notificationId, String title, String body) {
        try {
            if (!hasNotificationPermission()) {
                return false;
            }
            NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (manager == null) {
                return false;
            }
            if (Build.VERSION.SDK_INT >= 26) {
                NotificationChannel channel = new NotificationChannel(CHANNEL_ID, "Atualizações do Core Worker", NotificationManager.IMPORTANCE_DEFAULT);
                manager.createNotificationChannel(channel);
            }
            Intent open = new Intent(this, MainActivity.class);
            open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_NEW_TASK);
            open.putExtra("source", "fcm");
            open.putExtra("notificationId", notificationId == null ? "" : notificationId);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 23) {
                flags |= PendingIntent.FLAG_IMMUTABLE;
            }
            PendingIntent pending = PendingIntent.getActivity(this, 4102, open, flags);
            Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                    ? new Notification.Builder(this, CHANNEL_ID)
                    : new Notification.Builder(this);
            builder.setSmallIcon(android.R.drawable.stat_sys_download_done)
                    .setContentTitle(title == null || title.trim().isEmpty() ? "Atualização do Core Worker" : title.trim())
                    .setContentText(body == null || body.trim().isEmpty() ? "Nova versão disponível." : body.trim())
                    .setContentIntent(pending)
                    .setAutoCancel(true);
            manager.notify(NOTIFICATION_ID, builder.build());
            return true;
        } catch (Throwable ignored) {
            return false;
        }
    }

    private void registerToken(String token, String reason) {
        String serverUrl = normalizedServerUrl();
        if (serverUrl.isEmpty() || token == null || token.trim().isEmpty()) {
            return;
        }
        new Thread(() -> {
            try {
                JSONObject payload = basePayload();
                payload.put("fcmToken", token.trim());
                payload.put("state", "registered");
                payload.put("reason", reason == null ? "service" : reason);
                payload.put("permission", hasNotificationPermission() ? "granted" : "missing");
                request("POST", serverUrl + "/core-worker/app/fcm-token", payload);
            } catch (Throwable ignored) {
            }
        }).start();
    }

    private void report(String notificationId, String state, boolean delivered, String versionName, int versionCode, String detail) {
        String serverUrl = normalizedServerUrl();
        if (serverUrl.isEmpty()) {
            return;
        }
        new Thread(() -> {
            try {
                JSONObject payload = basePayload();
                payload.put("notificationId", notificationId == null ? "" : notificationId);
                payload.put("state", state == null ? "fcm_event" : state);
                payload.put("delivered", delivered);
                payload.put("versionName", versionName == null ? "" : versionName);
                payload.put("versionCode", versionCode);
                payload.put("detail", detail == null ? "" : detail);
                payload.put("permission", hasNotificationPermission() ? "granted" : "missing");
                request("POST", serverUrl + "/core-worker/app/notification", payload);
            } catch (Throwable ignored) {
            }
        }).start();
    }

    private JSONObject basePayload() throws Throwable {
        JSONObject payload = new JSONObject();
        payload.put("platform", "android");
        payload.put("source", "core-worker-apk");
        payload.put("appVersion", BuildConfig.VERSION_NAME);
        payload.put("appVersionCode", BuildConfig.VERSION_CODE);
        payload.put("workerId", CoreWorkerRuntimeIdentity.runtimeWorkerId(getApplicationContext()));
        payload.put("installId", installId());
        payload.put("deviceName", prefs().getString("device_name", ""));
        return payload;
    }

    private boolean hasNotificationPermission() {
        return Build.VERSION.SDK_INT < 33 || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
    }

    private String installId() {
        String id = prefs().getString("install_id", "");
        if (id == null || id.trim().isEmpty()) {
            id = UUID.randomUUID().toString();
            prefs().edit().putString("install_id", id).apply();
        }
        return id;
    }

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, MODE_PRIVATE);
    }

    private static String normalizedServerUrl() {
        String url = BuildConfig.CORE_WORKER_VPS_URL == null ? "" : BuildConfig.CORE_WORKER_VPS_URL.trim();
        return url.replaceAll("/+$", "");
    }

    private HttpResult request(String method, String url, JSONObject payload) throws Throwable {
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

    private String readAll(InputStream input) throws Throwable {
        if (input == null) {
            return "";
        }
        BufferedReader reader = new BufferedReader(new InputStreamReader(input, StandardCharsets.UTF_8));
        StringBuilder builder = new StringBuilder();
        String line;
        while ((line = reader.readLine()) != null) {
            if (builder.length() > 0) {
                builder.append('\n');
            }
            builder.append(line);
        }
        reader.close();
        return builder.toString();
    }

    private static String str(Object value) {
        return value == null ? "" : String.valueOf(value).trim();
    }

    private static int intValue(Object value, int fallback) {
        try {
            return Integer.parseInt(str(value));
        } catch (Throwable ignored) {
            return fallback;
        }
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
