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

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicBoolean;

public class CoreWorkerRuntimeService extends Service {
    public static final String ACTION_START = "dev.core.worker.action.RUNTIME_START";
    public static final String ACTION_STOP = "dev.core.worker.action.RUNTIME_STOP";
    public static final String ACTION_TICK = "dev.core.worker.action.RUNTIME_TICK";

    private static final String PREFS = "core_worker_private";
    private static final String CHANNEL_ID = "core_worker_runtime";
    private static final int NOTIFICATION_ID = 4107;
    private static final long TICK_MS = 180L * 1000L;
    private static final long HEARTBEAT_MIN_MS = 120L * 1000L;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean running = false;
    private NativeTtsManager nativeTtsManager;
    private LocalNativeTtsHttpServer nativeTtsServer;
    private final AtomicBoolean heartbeatRunning = new AtomicBoolean(false);
    private volatile long lastHeartbeatStartedAt = 0L;

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
        long now = System.currentTimeMillis();
        String safeReason = reason == null || reason.trim().isEmpty() ? "foreground" : reason.trim();
        if (heartbeatRunning.get()) {
            return;
        }
        if (lastHeartbeatStartedAt > 0L && now - lastHeartbeatStartedAt < HEARTBEAT_MIN_MS && !safeReason.contains("manual")) {
            return;
        }
        lastHeartbeatStartedAt = now;
        heartbeatRunning.set(true);
        new Thread(() -> {
            try {
                String serverUrl = normalizedServerUrl();
                if (serverUrl.isEmpty()) {
                    return;
                }
                JSONObject payload = buildForegroundHeartbeatPayload(safeReason);
                request("POST", serverUrl + "/core-worker/app/heartbeat", payload);
            } catch (Throwable ignored) {
            } finally {
                heartbeatRunning.set(false);
            }
        }, "core-worker-foreground-heartbeat").start();
    }

    private JSONObject buildForegroundHeartbeatPayload(String reason) throws Exception {
        JSONObject coreLinux = coreLinuxPublicSnapshot();
        JSONObject nativeRuntime = nativeRuntimePublicSnapshot();
        JSONArray supported = supportedLightJobsArray();
        JSONArray capabilities = coreWorkerApkCapabilitiesArray();
        JSONObject runtime = new JSONObject();
        runtime.put("mode", "apk-native-python-linux-assisted-runtime");
        runtime.put("internal_runtime", "apk-foreground-service");
        runtime.put("internal_runtime_state", "foreground-service-visible-runtime");
        runtime.put("jobs_runtime", "foreground-service-visible-runtime");
        runtime.put("capabilities", capabilities);
        runtime.put("supported_tasks", supported);
        runtime.put("supportedTasks", supported);
        runtime.put("foreground_runtime_active", true);
        runtime.put("foreground_runtime_summary", "serviço persistente ativo");
        runtime.put("core_linux_summary", coreLinux.optString("summary", ""));
        runtime.put("core_linux_state", coreLinux.optString("state", ""));
        runtime.put("core_linux_prepared", coreLinux.optBoolean("prepared", false));
        runtime.put("termux_required_now", false);
        runtime.put("termux_fallback_available", false);
        runtime.put("advanced_jobs_require_termux", false);
        runtime.put("coreLinux", coreLinux);
        runtime.put("nativeRuntime", nativeRuntime);

        JSONObject status = new JSONObject();
        status.put("app", "foreground-service");
        status.put("foreground_runtime_active", true);
        status.put("foreground_runtime_summary", "serviço persistente ativo");
        status.put("notification_permission", hasNotificationPermission() ? "granted" : "missing");
        status.put("termux_required_now", false);
        status.put("bedrock_server_mode", "future-foreground-service");
        status.put("bedrock_start_allowed", false);
        status.put("native_tts_bridge_active", nativeTtsServer != null);
        status.put("capabilities", capabilities);
        status.put("supported_tasks", supported);
        status.put("supportedTasks", supported);
        status.put("core_linux_summary", coreLinux.optString("summary", ""));
        status.put("core_linux_state", coreLinux.optString("state", ""));
        status.put("core_linux_prepared", coreLinux.optBoolean("prepared", false));
        status.put("coreLinux", coreLinux);
        status.put("nativeRuntime", nativeRuntime);
        if (nativeTtsManager != null) {
            status.put("android_tts", nativeTtsManager.statusJson());
        }

        JSONObject payload = new JSONObject();
        payload.put("platform", "android");
        payload.put("source", "core-worker-apk-foreground-service");
        payload.put("state", "foreground_runtime");
        payload.put("reason", reason == null || reason.trim().isEmpty() ? "foreground" : reason.trim());
        payload.put("appVersion", BuildConfig.VERSION_NAME);
        payload.put("appVersionCode", BuildConfig.VERSION_CODE);
        payload.put("versionName", BuildConfig.VERSION_NAME);
        payload.put("versionCode", BuildConfig.VERSION_CODE);
        payload.put("workerId", prefs().getString("worker_id", ""));
        payload.put("installId", installId());
        payload.put("deviceName", prefs().getString("device_name", ""));
        payload.put("runtime_mode", "apk-native-python-linux-assisted-runtime");
        payload.put("jobsRuntime", "foreground-service-visible-runtime");
        payload.put("internal_runtime", "apk-foreground-service");
        payload.put("internal_runtime_state", "foreground-service-visible-runtime");
        payload.put("capabilities", capabilities);
        payload.put("supported_tasks", supported);
        payload.put("supportedTasks", supported);
        payload.put("app_jobs", supported);
        payload.put("coreLinux", coreLinux);
        payload.put("nativeRuntime", nativeRuntime);
        payload.put("runtime", runtime);
        payload.put("status", status);
        return payload;
    }

    private JSONArray coreWorkerApkCapabilitiesArray() {
        return new JSONArray()
                .put("apk-native")
                .put("android-status")
                .put("native-boot")
                .put("safe-shell-probe")
                .put("python-embedded")
                .put("internal-jobs")
                .put("core-linux-runtime")
                .put("core-linux-rootfs-manager")
                .put("core-linux-rootfs-import-v1")
                .put("core-linux-runner-preflight-v1")
                .put("core-linux-runner-preflight-v2")
                .put("core-linux-runner-preflight-v3")
                .put("core-linux-runner-preflight-v4")
                .put("core-linux-runner-preflight-v5")
                .put("core-linux-embedded-binaries-intake-v1")
                .put("core-linux-embedded-binaries-intake-v2")
                .put("core-linux-embedded-binaries-intake-v3")
                .put("core-linux-embedded-binaries-intake-v4")
                .put("core-linux-embedded-binaries-intake-v5")
                .put("core-linux-embedded-binaries-build-pipeline-v1")
                .put("core-linux-embedded-binaries-build-pipeline-v2")
                .put("core-linux-embedded-binaries-build-pipeline-v3")
                .put("core-linux-embedded-binaries-build-pipeline-v4")
                .put("core-linux-runtime-v1")
                .put("minecraft-bedrock-manager-safe-plan");
    }

    private JSONArray supportedLightJobsArray() {
        return new JSONArray()
                .put("apk_ping")
                .put("apk_status_refresh")
                .put("apk_upload_app_logs")
                .put("apk_diagnostic")
                .put("apk_check_update")
                .put("apk_test_vps_connection")
                .put("apk_sync_runtime_state")
                .put("apk_job_history")
                .put("apk_device_diagnostic")
                .put("apk_push_diagnostic")
                .put("apk_update_diagnostic")
                .put("apk_runtime_diagnostic")
                .put("apk_worker_bridge_status")
                .put("apk_test_notification")
                .put("apk_repair_local_state")
                .put("apk_reset_job_history")
                .put("apk_trim_cache")
                .put("apk_update_storage_cleanup")
                .put("apk_sync_profile")
                .put("apk_sync_profile_now")
                .put("apk_verify_update_state")
                .put("apk_native_worker_status")
                .put("apk_native_boot_status")
                .put("apk_local_shell_probe")
                .put("apk_core_linux_native_executor_probe")
                .put("apk_core_linux_native_executor_test")
                .put("apk_core_linux_native_runtime_status")
                .put("apk_core_linux_rootfs_status")
                .put("apk_core_linux_rootfs_prepare")
                .put("apk_core_linux_rootfs_validate")
                .put("apk_core_linux_rootfs_preflight")
                .put("apk_core_linux_rootfs_clean_staging")
                .put("apk_core_linux_rootfs_import_status")
                .put("apk_core_linux_rootfs_import_validate")
                .put("apk_core_linux_rootfs_import_abort")
                .put("apk_core_linux_rootfs_real_status")
                .put("apk_core_linux_runner_status")
                .put("apk_core_linux_runner_preflight")
                .put("apk_core_linux_runner_requirements")
                .put("apk_core_linux_runtime_smoke_test");
    }

    private JSONObject coreLinuxPublicSnapshot() {
        File runtimeDir = new File(getFilesDir(), "core-linux/runtime");
        JSONObject runtime = readJson(new File(runtimeDir, "linux-runtime-state.json"));
        JSONObject rootfs = readJson(new File(runtimeDir, "rootfs-state.json"));
        JSONObject smoke = readJson(new File(runtimeDir, "core-linux-smoke-test.json"));
        JSONObject rootfsImport = readJson(new File(runtimeDir, "rootfs-import-state.json"));
        JSONObject runner = readJson(new File(runtimeDir, "runner-preflight-state.json"));
        String rawState = firstNonEmpty(rootfs.optString("state", ""), rootfsImport.optString("state", ""), runtime.optString("state", ""), smoke.optString("state", ""));
        String level = firstNonEmpty(rootfs.optString("validationLevel", ""), rootfs.optString("rootfsValidationLevel", ""), rootfsImport.optString("validationLevel", ""));
        boolean realValidated = rawState.toLowerCase().contains("rootfs_real_validated") || "real".equalsIgnoreCase(level);
        boolean prepared = realValidated || runtime.optBoolean("ok", false) || runtime.optBoolean("rootfsReady", false) || rootfs.optBoolean("rootfsReady", false) || smoke.optBoolean("ok", false);
        String summary = firstNonEmpty(rootfs.optString("summary", ""), rootfsImport.optString("summary", ""), runtime.optString("summary", ""), smoke.optString("summary", ""), prepared ? "Core Linux Runtime v1 pronto" : "Core Linux Runtime v1 aguardando preparo");
        String state = firstNonEmpty(rawState, prepared ? "runtime_v1_ready" : "runtime_v1_pending");
        if (realValidated) {
            state = "rootfs_real_validated";
            summary = firstNonEmpty(rootfs.optString("summary", ""), rootfsImport.optString("summary", ""), "Rootfs real validado · runner real ainda bloqueado");
        }
        JSONObject out = new JSONObject();
        try {
            out.put("summary", summary);
            out.put("state", state);
            out.put("prepared", prepared);
            out.put("rootfsReady", realValidated || runtime.optBoolean("rootfsReady", rootfs.optBoolean("rootfsReady", false)) || smoke.optBoolean("ok", false));
            out.put("executorReady", runtime.optBoolean("executorReady", false));
            out.put("lastCheckAt", Math.max(Math.max(runtime.optLong("updatedAt", 0L), runner.optLong("updatedAt", 0L)), Math.max(rootfs.optLong("updatedAt", 0L), smoke.optLong("updatedAt", 0L))));
            out.put("termuxRequired", false);
            out.put("bedrockStartAllowed", false);
            out.put("rootfsValidationLevel", realValidated ? "real" : rootfs.optString("validationLevel", ""));
            out.put("rootfsDistributionReady", realValidated || rootfs.optBoolean("distributionReady", false));
            out.put("rootfsState", rootfs.optString("state", state));
            out.put("rootfsSummary", rootfs.optString("summary", summary));
            out.put("rootfsImportState", rootfsImport.optString("state", ""));
            out.put("rootfsImportSummary", rootfsImport.optString("summary", ""));
            if (runner.length() > 0) {
                out.put("runnerPreflightState", runner.optString("state", ""));
                out.put("runnerPreflightSummary", runner.optString("summary", ""));
                out.put("runnerPreflightVersion", runner.optInt("preflightVersion", 1));
                out.put("runnerReady", runner.optBoolean("runnerReady", false));
                out.put("runnerBlocked", runner.optBoolean("runnerBlocked", true));
                out.put("runnerExecutionAllowed", runner.optBoolean("runnerExecutionAllowed", false));
                out.put("runnerRequirementsReady", runner.optBoolean("runnerRequirementsReady", false));
                out.put("runnerMissing", runner.optJSONArray("missing") == null ? new JSONArray() : runner.optJSONArray("missing"));
                out.put("runnerPreflight", runner);
            }
            out.put("supportedStage", runner.length() > 0 ? "core-linux-runner-preflight-v5" : (realValidated || rootfs.optBoolean("distributionReady", false) ? "core-linux-rootfs-import-v1" : "core-linux-runtime-v1-smoke"));
            out.put("supportedTasks", supportedLightJobsArray());
            if (runtime.length() > 0) out.put("runtime", runtime);
            if (rootfs.length() > 0) out.put("rootfs", rootfs);
            if (rootfsImport.length() > 0) out.put("rootfsImport", rootfsImport);
            if (smoke.length() > 0) out.put("smoke", smoke);
        } catch (Throwable ignored) {
        }
        return out;
    }

    private JSONObject nativeRuntimePublicSnapshot() {
        JSONObject executor = readJson(new File(new File(getFilesDir(), "core-linux/runtime"), "native-executor-state.json"));
        JSONObject out = new JSONObject();
        try {
            out.put("summary", firstNonEmpty(executor.optString("summary", ""), executor.optBoolean("readyForRootfs", false) ? "executor nativo interno pronto para rootfs" : "executor nativo aguardando teste"));
            out.put("workerOnline", true);
            out.put("workerState", executor.optBoolean("readyForRootfs", false) ? "ready" : "pending");
            out.put("pythonAvailable", false);
            out.put("lastHeartbeatAt", executor.optLong("updatedAt", 0L));
            out.put("supportedTasks", supportedLightJobsArray());
            if (executor.length() > 0) out.put("executor", executor);
        } catch (Throwable ignored) {
        }
        return out;
    }

    private JSONObject readJson(File file) {
        try {
            if (file == null || !file.isFile()) return new JSONObject();
            FileInputStream input = new FileInputStream(file);
            byte[] data = new byte[(int) Math.min(file.length(), 512L * 1024L)];
            int read = input.read(data);
            input.close();
            if (read <= 0) return new JSONObject();
            return new JSONObject(new String(data, 0, read, StandardCharsets.UTF_8));
        } catch (Throwable ignored) {
            return new JSONObject();
        }
    }

    private String firstNonEmpty(String... values) {
        if (values == null) return "";
        for (String value : values) {
            if (value != null && !value.trim().isEmpty()) return value.trim();
        }
        return "";
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
