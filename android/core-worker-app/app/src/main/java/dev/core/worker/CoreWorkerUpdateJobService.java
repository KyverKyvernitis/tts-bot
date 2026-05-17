package dev.core.worker;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.PersistableBundle;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.UUID;

public class CoreWorkerUpdateJobService extends JobService {
    private static final int JOB_ID = 49056;
    private static final int NOTIFICATION_ID = 4102;
    private static final String PREFS = "core_worker_private";
    private static final String CHANNEL_ID = "core_worker_updates";
    private static final long PERIOD_MS = 15L * 60L * 1000L;

    public static void schedule(Context context, String reason) {
        try {
            String serverUrl = normalizedServerUrl();
            if (serverUrl.isEmpty()) {
                return;
            }
            JobScheduler scheduler = (JobScheduler) context.getSystemService(Context.JOB_SCHEDULER_SERVICE);
            if (scheduler == null) {
                return;
            }
            PersistableBundle extras = new PersistableBundle();
            extras.putString("reason", reason == null ? "scheduled" : reason);
            JobInfo job = new JobInfo.Builder(JOB_ID, new ComponentName(context, CoreWorkerUpdateJobService.class))
                    .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                    .setPersisted(true)
                    .setPeriodic(PERIOD_MS)
                    .setExtras(extras)
                    .build();
            scheduler.schedule(job);
        } catch (Throwable ignored) {
        }
    }

    @Override
    public boolean onStartJob(JobParameters params) {
        new Thread(() -> {
            try {
                runUpdateCheck(params);
            } finally {
                jobFinished(params, false);
            }
        }).start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        return true;
    }

    private void runUpdateCheck(JobParameters params) {
        String serverUrl = normalizedServerUrl();
        if (serverUrl.isEmpty()) {
            return;
        }
        try {
            JSONObject manifest = fetchLatestManifest(serverUrl);
            if (manifest == null) {
                return;
            }
            String versionName = manifest.optString("versionName", manifest.optString("version", ""));
            int versionCode = manifest.optInt("versionCode", -1);
            String notificationId = manifest.optString("notificationId", "").trim();
            if (notificationId.isEmpty()) {
                notificationId = "apk-" + versionCode + "-" + manifest.optString("sha256", versionName);
            }
            boolean requested = manifest.optBoolean("notificationRequested", manifest.optBoolean("notifyUsers", false));
            boolean available = versionCode > BuildConfig.VERSION_CODE
                    || (versionCode < 0 && !versionName.isEmpty() && !BuildConfig.VERSION_NAME.equals(versionName));
            if (!available || !requested) {
                return;
            }
            String already = prefs().getString("last_update_notification", "");
            if (notificationId.equals(already)) {
                report(serverUrl, notificationId, "background_duplicate", true, versionName, versionCode, "checagem em segundo plano: notificação já registrada");
                return;
            }
            if (!hasNotificationPermission()) {
                report(serverUrl, notificationId, "background_permission_missing", false, versionName, versionCode, "Android não liberou POST_NOTIFICATIONS para notificar com app fechado");
                return;
            }
            NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (manager == null) {
                report(serverUrl, notificationId, "background_failed", false, versionName, versionCode, "NotificationManager indisponível");
                return;
            }
            if (Build.VERSION.SDK_INT >= 26) {
                NotificationChannel channel = new NotificationChannel(CHANNEL_ID, "Atualizações do Core Worker", NotificationManager.IMPORTANCE_DEFAULT);
                manager.createNotificationChannel(channel);
            }
            Intent open = new Intent(this, MainActivity.class);
            open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_NEW_TASK);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 23) {
                flags |= PendingIntent.FLAG_IMMUTABLE;
            }
            PendingIntent pending = PendingIntent.getActivity(this, 4102, open, flags);
            Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                    ? new Notification.Builder(this, CHANNEL_ID)
                    : new Notification.Builder(this);
            builder.setSmallIcon(android.R.drawable.stat_sys_download_done)
                    .setContentTitle("Atualização do Core Worker")
                    .setContentText("Versão " + (versionName.isEmpty() ? "nova" : versionName) + " disponível")
                    .setContentIntent(pending)
                    .setAutoCancel(true);
            manager.notify(NOTIFICATION_ID, builder.build());
            prefs().edit().putString("last_update_notification", notificationId).apply();
            report(serverUrl, notificationId, "background_displayed", true, versionName, versionCode, "notificação criada por checagem periódica com app fechado");
        } catch (Throwable ignored) {
        }
    }

    private JSONObject fetchLatestManifest(String serverUrl) throws Exception {
        String[] paths = new String[]{"/core-worker/app/latest.json", "/core-worker/latest.json"};
        for (String path : paths) {
            HttpResult result = request("GET", serverUrl + path, null);
            if (result.ok()) {
                return new JSONObject(result.body);
            }
        }
        return null;
    }

    private void report(String serverUrl, String notificationId, String state, boolean delivered, String versionName, int versionCode, String detail) {
        try {
            JSONObject payload = new JSONObject();
            payload.put("notificationId", notificationId == null ? "" : notificationId);
            payload.put("state", state);
            payload.put("delivered", delivered);
            payload.put("versionName", versionName == null ? "" : versionName);
            payload.put("versionCode", versionCode);
            payload.put("appVersion", BuildConfig.VERSION_NAME);
            payload.put("appVersionCode", BuildConfig.VERSION_CODE);
            payload.put("workerId", prefs().getString("worker_id", ""));
            payload.put("installId", installId());
            payload.put("permission", hasNotificationPermission() ? "granted" : "missing");
            payload.put("detail", detail == null ? "" : detail);
            request("POST", serverUrl + "/core-worker/app/notification", payload);
        } catch (Throwable ignored) {
        }
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

    private static final class HttpResult {
        final int status;
        final String body;

        HttpResult(int status, String body) {
            this.status = status;
            this.body = body;
        }

        boolean ok() {
            return status >= 200 && status < 300;
        }
    }
}
