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
import android.os.PowerManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

public class CoreWorkerRuntimeService extends Service {
    public static final String ACTION_START = "dev.core.worker.action.RUNTIME_START";
    public static final String ACTION_STOP = "dev.core.worker.action.RUNTIME_STOP";
    public static final String ACTION_TICK = "dev.core.worker.action.RUNTIME_TICK";
    public static final String ACTION_POLL_NOW = "dev.core.worker.action.AGENT_POLL_NOW";

    private static final String PREFS = "core_worker_private";
    private static final String CHANNEL_ID = "core_worker_runtime";
    private static final int NOTIFICATION_ID = 4107;
    private static final long TICK_MS = 30L * 1000L;
    private static final long HEARTBEAT_MIN_MS = 120L * 1000L;
    private static final long POLL_ERROR_BACKOFF_MIN_MS = 15L * 1000L;
    private static final long POLL_ERROR_BACKOFF_MAX_MS = 5L * 60L * 1000L;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean running = false;
    private NativeTtsManager nativeTtsManager;
    private LocalNativeTtsHttpServer nativeTtsServer;
    private final AtomicBoolean heartbeatRunning = new AtomicBoolean(false);
    private final AtomicBoolean pollRunning = new AtomicBoolean(false);
    private final ExecutorService agentExecutor = Executors.newSingleThreadExecutor(r -> {
        Thread thread = new Thread(r, "core-worker-agent");
        thread.setDaemon(true);
        return thread;
    });
    private CoreWorkerJobExecutor jobExecutor;
    private volatile long lastHeartbeatStartedAt = 0L;
    private volatile long nextPollAllowedAt = 0L;
    private volatile long pollErrorBackoffMs = POLL_ERROR_BACKOFF_MIN_MS;

    private final Runnable tickRunnable = new Runnable() {
        @Override
        public void run() {
            if (!running) {
                return;
            }
            markTick("agente autônomo ativo");
            pollJobs("foreground_tick", false);
            reportHeartbeat("foreground_tick");
            handler.postDelayed(this, TICK_MS);
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        createChannel();
        jobExecutor = new CoreWorkerJobExecutor(getApplicationContext());
        startNativeTtsBridge();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : String.valueOf(intent.getAction());
        String reason = intent == null ? "foreground_start" : intent.getStringExtra("reason");
        if (ACTION_STOP.equals(action)) {
            running = false;
            handler.removeCallbacks(tickRunnable);
            prefs().edit()
                    .putBoolean("agent_enabled", false)
                    .putBoolean("job_executor_ready", false)
                    .putBoolean("foreground_runtime_active", false)
                    .putString("foreground_runtime_state", "agente autônomo parado")
                    .putLong("foreground_runtime_last_tick_at", System.currentTimeMillis())
                    .apply();
            stopForeground(true);
            stopSelf();
            return START_NOT_STICKY;
        }

        running = true;
        startForeground(NOTIFICATION_ID, buildNotification("Agente autônomo ativo"));
        prefs().edit()
                .putBoolean("agent_enabled", true)
                .putBoolean("job_executor_ready", true)
                .putBoolean("foreground_runtime_active", true)
                .putString("foreground_runtime_state", "agente autônomo ativo")
                .putLong("foreground_runtime_started_at", System.currentTimeMillis())
                .apply();
        markTick("agente autônomo ativo");
        reportHeartbeat(reason);
        pollJobs(reason == null ? "foreground_start" : reason, ACTION_POLL_NOW.equals(action));
        handler.removeCallbacks(tickRunnable);
        handler.postDelayed(tickRunnable, TICK_MS);
        return START_STICKY;
    }

    static boolean shouldRunAgent(Context context) {
        if (context == null) return false;
        try {
            SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
            if (prefs.contains("agent_enabled")) {
                return prefs.getBoolean("agent_enabled", false);
            }
            boolean paired = prefs.getBoolean("paired_via_native_apk", false)
                    && !prefs.getString("worker_token", "").trim().isEmpty();
            return prefs.getBoolean("foreground_runtime_active", false) || paired;
        } catch (Throwable ignored) {
            return false;
        }
    }

    public static void requestStart(Context context, String reason) {
        if (context == null) return;
        try {
            Intent intent = new Intent(context, CoreWorkerRuntimeService.class);
            intent.setAction(ACTION_START);
            intent.putExtra("reason", reason == null ? "request_start" : reason);
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent);
            else context.startService(intent);
        } catch (Throwable ignored) {
        }
    }

    public static void requestPoll(Context context, String reason) {
        if (context == null || !shouldRunAgent(context)) return;
        try {
            Intent intent = new Intent(context, CoreWorkerRuntimeService.class);
            intent.setAction(ACTION_POLL_NOW);
            intent.putExtra("reason", reason == null ? "request_poll" : reason);
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent);
            else context.startService(intent);
        } catch (Throwable ignored) {
        }
    }

    @Override
    public void onDestroy() {
        running = false;
        handler.removeCallbacks(tickRunnable);
        stopNativeTtsBridge();
        try { agentExecutor.shutdownNow(); } catch (Throwable ignored) { }
        prefs().edit()
                .putBoolean("job_executor_ready", false)
                .putBoolean("foreground_runtime_active", false)
                .putString("foreground_runtime_state", "agente autônomo encerrado")
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
                    channel.setDescription("Mantém o agente do Core Worker ativo para buscar e executar jobs com a interface fechada.");
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
                .setContentTitle("Core Worker autônomo")
                .setContentText(text == null ? "Runtime persistente ativo" : text)
                .setContentIntent(pending)
                .setOngoing(true)
                .setShowWhen(false);
        return builder.build();
    }

    private void pollJobs(String reason, boolean force) {
        if (!running || jobExecutor == null) return;
        long now = System.currentTimeMillis();
        if (!force && now < nextPollAllowedAt) return;
        if (!pollRunning.compareAndSet(false, true)) return;
        agentExecutor.execute(() -> {
            PowerManager.WakeLock wakeLock = null;
            try {
                PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
                if (power != null) {
                    wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "CoreWorker:AgentCycle");
                    wakeLock.setReferenceCounted(false);
                    wakeLock.acquire(10L * 60L * 1000L);
                }
                runAgentCycle(reason == null ? "background" : reason, force);
                pollErrorBackoffMs = POLL_ERROR_BACKOFF_MIN_MS;
            } catch (Throwable error) {
                long failedAt = System.currentTimeMillis();
                nextPollAllowedAt = failedAt + pollErrorBackoffMs;
                pollErrorBackoffMs = Math.min(POLL_ERROR_BACKOFF_MAX_MS, Math.max(POLL_ERROR_BACKOFF_MIN_MS, pollErrorBackoffMs * 2L));
                prefs().edit()
                        .putString("internal_light_jobs_state", "falha · " + shortThrowable(error))
                        .putString("internal_light_jobs_last_summary", "falha: " + shortThrowable(error))
                        .putString("internal_light_jobs_last_fetch_error", shortThrowable(error))
                        .putString("agent_last_error", shortThrowable(error))
                        .putLong("internal_light_jobs_last_check_at", failedAt)
                        .apply();
            } finally {
                try {
                    if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
                } catch (Throwable ignored) {
                }
                pollRunning.set(false);
            }
        });
    }

    private void runAgentCycle(String reason, boolean force) throws Exception {
        String serverUrl = normalizedServerUrl();
        if (serverUrl.isEmpty()) {
            prefs().edit().putString("internal_light_jobs_state", "VPS não configurada").apply();
            return;
        }

        flushResultOutbox(serverUrl);
        long startedAt = System.currentTimeMillis();
        JSONObject payload = jobExecutor.buildFetchStatus(serverUrl);
        payload.put("installId", installId());
        payload.put("workerId", prefs().getString("worker_id", ""));
        payload.put("appVersion", BuildConfig.VERSION_NAME);
        payload.put("appVersionCode", BuildConfig.VERSION_CODE);
        payload.put("versionName", BuildConfig.VERSION_NAME);
        payload.put("versionCode", BuildConfig.VERSION_CODE);
        payload.put("source", "core-worker-apk-agent-service-v1");
        payload.put("reason", reason);
        payload.put("fetchStage", "autonomous-agent-v1");
        payload.put("force", force || shouldForcePoll(reason));
        payload.put("supportedJobs", CoreWorkerJobCatalog.supportedJobs());
        payload.put("supported_tasks", CoreWorkerJobCatalog.supportedJobs());
        payload.put("supportedTasks", CoreWorkerJobCatalog.supportedJobs());
        payload.put("capabilities", CoreWorkerJobCatalog.capabilities());

        prefs().edit()
                .putLong("internal_light_jobs_last_fetch_started_at", startedAt)
                .putString("internal_light_jobs_last_fetch_reason", reason)
                .putString("internal_light_jobs_last_fetch_app_version", BuildConfig.VERSION_NAME)
                .putInt("internal_light_jobs_last_fetch_app_version_code", BuildConfig.VERSION_CODE)
                .putString("internal_light_jobs_last_fetch_error", "")
                .apply();

        HttpResult response = request("POST", serverUrl + "/core-worker/app/jobs/fetch", payload);
        long checkedAt = System.currentTimeMillis();
        SharedPreferences.Editor state = prefs().edit()
                .putLong("internal_light_jobs_last_check_at", checkedAt)
                .putInt("internal_light_jobs_last_fetch_http_status", response.status);
        if (!response.ok()) {
            String error = compact(response.body);
            state.putString("internal_light_jobs_state", "falha HTTP " + response.status)
                    .putString("internal_light_jobs_last_fetch_error", error)
                    .putString("agent_last_error", error)
                    .apply();
            throw new IllegalStateException("HTTP " + response.status + (error.isEmpty() ? "" : ": " + error));
        }

        JSONObject body = new JSONObject(response.body);
        if (!body.optBoolean("ok", false)) {
            String error = compact(body.optString("error", response.body));
            state.putString("internal_light_jobs_state", "VPS recusou a busca")
                    .putString("internal_light_jobs_last_fetch_error", error)
                    .putString("agent_last_error", error)
                    .apply();
            throw new IllegalStateException(error.isEmpty() ? "VPS recusou a busca de jobs" : error);
        }
        if (body.optBoolean("throttled", false)) {
            int retryAfter = Math.max(1, body.optInt("retryAfterSeconds", 10));
            nextPollAllowedAt = checkedAt + retryAfter * 1000L;
            updateQueueState(body.optJSONObject("queue"));
            state.putString("internal_light_jobs_state", "fila em cooldown")
                    .putString("internal_light_jobs_last_summary", "VPS pediu nova checagem em " + retryAfter + "s")
                    .apply();
            return;
        }

        updateQueueState(body.optJSONObject("queue"));
        updateCatalogState(body.optJSONObject("catalog"));
        JSONArray jobs = body.optJSONArray("jobs");
        int count = jobs == null ? 0 : jobs.length();
        state.putInt("internal_light_jobs_last_count", count)
                .putInt("internal_light_jobs_last_returned_count", count)
                .apply();
        if (count == 0) {
            prefs().edit()
                    .putString("internal_light_jobs_state", "fila vazia")
                    .putString("internal_light_jobs_last_summary", "fila vazia")
                    .putInt("internal_jobs_running_count", 0)
                    .putString("agent_last_error", "")
                    .apply();
            return;
        }

        int okCount = 0;
        int pendingResultCount = 0;
        prefs().edit().putInt("internal_jobs_running_count", 1).apply();
        for (int i = 0; i < count; i++) {
            JSONObject job = jobs.optJSONObject(i);
            if (job == null) continue;
            String jobId = job.optString("id", "").trim();
            String jobType = job.optString("type", "job");
            JSONObject result;
            long jobStartedAt = System.currentTimeMillis();
            File pending = outboxFile(jobId);
            if (pending != null && pending.isFile()) {
                JSONObject envelope = readJsonFile(pending);
                if (postResultEnvelope(serverUrl, envelope)) {
                    pending.delete();
                    rememberCompletedJob(jobId);
                    JSONObject queuedResult = envelope.optJSONObject("result");
                    if (queuedResult != null && queuedResult.optBoolean("ok", false)) okCount++;
                } else {
                    pendingResultCount++;
                }
                continue;
            }
            if (wasJobRecentlyCompleted(jobId)) {
                result = new JSONObject()
                        .put("ok", true)
                        .put("type", jobType)
                        .put("deduplicated", true)
                        .put("message", "job duplicado ignorado pelo agente")
                        .put("jobId", jobId);
            } else {
                try {
                    result = jobExecutor.execute(job, serverUrl);
                } catch (Throwable jobError) {
                    result = new JSONObject()
                            .put("ok", false)
                            .put("type", jobType)
                            .put("error", shortThrowable(jobError))
                            .put("message", "job falhou no agente do APK");
                }
            }
            result.put("durationMs", Math.max(0L, System.currentTimeMillis() - jobStartedAt));
            result.put("attempt", job.optInt("attempt", 1));
            if (result.optBoolean("ok", false)) okCount++;

            JSONObject envelope = buildResultEnvelope(job, result);
            File stored = persistOutbox(jobId, envelope);
            if (postResultEnvelope(serverUrl, envelope)) {
                if (stored != null) stored.delete();
                rememberCompletedJob(jobId);
            } else {
                pendingResultCount++;
            }
            recordJobHistory(jobType, result.optBoolean("ok", false), result.optString("message", result.optString("error", "")));
        }

        String executionState = "executados " + okCount + "/" + count;
        String executionSummary = summarizeJobs(jobs, okCount, count);
        if (pendingResultCount > 0) {
            executionState += " · " + pendingResultCount + " resultado(s) pendente(s)";
            executionSummary += " · confirmação pendente: " + pendingResultCount;
        }
        SharedPreferences.Editor completedState = prefs().edit()
                .putString("internal_light_jobs_state", executionState)
                .putString("internal_light_jobs_last_summary", executionSummary)
                .putInt("internal_jobs_running_count", 0);
        if (pendingResultCount == 0) completedState.putString("agent_last_error", "");
        completedState.apply();
    }

    private void updateQueueState(JSONObject queue) {
        int pending = queue == null ? 0 : queue.optInt("pending", 0);
        int runningJobs = queue == null ? 0 : queue.optInt("running", 0);
        prefs().edit()
                .putInt("internal_jobs_pending_count", pending)
                .putInt("internal_jobs_running_count", runningJobs)
                .putString("internal_jobs_queue_summary", runningJobs + " rodando · " + pending + " pendentes")
                .apply();
    }

    private void updateCatalogState(JSONObject catalog) {
        if (catalog == null) return;
        JSONArray automatic = catalog.optJSONArray("automatic");
        JSONArray manual = catalog.optJSONArray("manual");
        int autoCount = automatic == null ? 0 : automatic.length();
        int manualCount = manual == null ? 0 : manual.length();
        prefs().edit()
                .putInt("internal_jobs_auto_total", autoCount)
                .putInt("internal_jobs_manual_total", manualCount)
                .putString("internal_jobs_catalog_summary", autoCount + " automáticos · " + manualCount + " manuais")
                .apply();
    }

    private boolean shouldForcePoll(String reason) {
        String value = reason == null ? "" : reason.toLowerCase(java.util.Locale.ROOT);
        return value.contains("manual") || value.contains("fcm") || value.contains("resume")
                || value.contains("opened") || value.contains("status") || value.contains("diagnostic");
    }

    private JSONObject buildResultEnvelope(JSONObject job, JSONObject result) throws Exception {
        JSONObject payload = new JSONObject();
        payload.put("jobId", job == null ? "" : job.optString("id", ""));
        payload.put("type", job == null ? "" : job.optString("type", ""));
        payload.put("installId", installId());
        payload.put("workerId", prefs().getString("worker_id", ""));
        payload.put("appVersion", BuildConfig.VERSION_NAME);
        payload.put("appVersionCode", BuildConfig.VERSION_CODE);
        payload.put("result", result == null ? new JSONObject() : result);
        payload.put("queuedAt", System.currentTimeMillis());
        return payload;
    }

    private File persistOutbox(String jobId, JSONObject envelope) {
        try {
            File target = outboxFile(jobId);
            if (target == null) return null;
            File temp = new File(target.getParentFile(), target.getName() + ".tmp");
            FileOutputStream output = new FileOutputStream(temp, false);
            output.write(envelope.toString().getBytes(StandardCharsets.UTF_8));
            output.flush();
            output.close();
            if (!temp.renameTo(target)) {
                FileOutputStream direct = new FileOutputStream(target, false);
                direct.write(envelope.toString().getBytes(StandardCharsets.UTF_8));
                direct.flush();
                direct.close();
                temp.delete();
            }
            return target;
        } catch (Throwable error) {
            prefs().edit().putString("agent_last_error", "outbox: " + shortThrowable(error)).apply();
            return null;
        }
    }

    private void flushResultOutbox(String serverUrl) {
        File dir = jobExecutor == null ? null : jobExecutor.outboxDir();
        File[] files = dir == null ? null : dir.listFiles();
        if (files == null) return;
        for (File file : files) {
            if (file == null || !file.isFile() || !file.getName().endsWith(".json")) continue;
            try {
                JSONObject envelope = readJsonFile(file);
                if (postResultEnvelope(serverUrl, envelope)) {
                    rememberCompletedJob(envelope.optString("jobId", ""));
                    file.delete();
                }
            } catch (Throwable ignored) {
            }
        }
    }

    private boolean postResultEnvelope(String serverUrl, JSONObject envelope) {
        try {
            HttpResult response = request("POST", serverUrl + "/core-worker/app/jobs/result", envelope);
            if (!response.ok()) return false;
            JSONObject body = new JSONObject(response.body);
            return body.optBoolean("ok", false);
        } catch (Throwable error) {
            prefs().edit().putString("agent_last_error", "resultado: " + shortThrowable(error)).apply();
            return false;
        }
    }

    private File outboxFile(String jobId) {
        if (jobExecutor == null) return null;
        String safe = jobId == null ? "" : jobId.trim().replaceAll("[^a-zA-Z0-9._-]", "_");
        if (safe.isEmpty()) safe = "job-" + System.currentTimeMillis();
        return new File(jobExecutor.outboxDir(), safe + ".json");
    }

    private JSONObject readJsonFile(File file) throws Exception {
        if (file == null || !file.isFile()) return new JSONObject();
        FileInputStream input = new FileInputStream(file);
        byte[] data = new byte[(int) Math.min(file.length(), 1024L * 1024L)];
        int read = input.read(data);
        input.close();
        return read <= 0 ? new JSONObject() : new JSONObject(new String(data, 0, read, StandardCharsets.UTF_8));
    }

    private boolean wasJobRecentlyCompleted(String jobId) {
        if (jobId == null || jobId.trim().isEmpty()) return false;
        try {
            JSONArray recent = new JSONArray(prefs().getString("internal_completed_job_ids", "[]"));
            for (int i = 0; i < recent.length(); i++) if (jobId.equals(recent.optString(i, ""))) return true;
        } catch (Throwable ignored) {
        }
        return false;
    }

    private void rememberCompletedJob(String jobId) {
        if (jobId == null || jobId.trim().isEmpty()) return;
        try {
            JSONArray old = new JSONArray(prefs().getString("internal_completed_job_ids", "[]"));
            JSONArray next = new JSONArray().put(jobId);
            for (int i = 0; i < old.length() && next.length() < 32; i++) {
                String value = old.optString(i, "");
                if (!value.isEmpty() && !jobId.equals(value)) next.put(value);
            }
            prefs().edit().putString("internal_completed_job_ids", next.toString()).apply();
        } catch (Throwable ignored) {
        }
    }

    private void recordJobHistory(String type, boolean ok, String message) {
        try {
            JSONArray old = new JSONArray(prefs().getString("internal_job_history", "[]"));
            JSONArray next = new JSONArray();
            next.put(new JSONObject()
                    .put("at", System.currentTimeMillis())
                    .put("type", type == null ? "job" : type)
                    .put("ok", ok)
                    .put("message", message == null ? "" : message));
            for (int i = 0; i < old.length() && next.length() < 12; i++) {
                JSONObject item = old.optJSONObject(i);
                if (item != null) next.put(item);
            }
            prefs().edit().putString("internal_job_history", next.toString()).apply();
        } catch (Throwable ignored) {
        }
    }

    private String summarizeJobs(JSONArray jobs, int okCount, int count) {
        StringBuilder out = new StringBuilder();
        int limit = Math.min(count, 3);
        for (int i = 0; i < limit; i++) {
            JSONObject job = jobs == null ? null : jobs.optJSONObject(i);
            if (job == null) continue;
            if (out.length() > 0) out.append(", ");
            out.append(job.optString("type", "job"));
        }
        if (count > limit) out.append(" +").append(count - limit);
        if (out.length() == 0) out.append("jobs internos");
        return out.append(" · ").append(okCount).append('/').append(count).append(" ok").toString();
    }


    private String compact(String value) {
        String clean = value == null ? "" : value.replaceAll("\\s+", " ").trim();
        return clean.length() <= 600 ? clean : clean.substring(0, 600);
    }

    private String shortThrowable(Throwable error) {
        if (error == null) return "erro desconhecido";
        String message = error.getMessage() == null ? "" : error.getMessage().trim();
        String text = error.getClass().getSimpleName() + (message.isEmpty() ? "" : ": " + message);
        return text.length() <= 180 ? text : text.substring(0, 180);
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
                HttpResult heartbeat = request("POST", serverUrl + "/core-worker/app/heartbeat", payload);
                if (heartbeat.ok()) {
                    prefs().edit()
                            .putLong("native_worker_last_heartbeat_at", System.currentTimeMillis())
                            .putString("native_worker_state", "agente autônomo online")
                            .apply();
                }
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
        runtime.put("jobs_runtime", "foreground-service-autonomous-agent");
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
        runtime.put("job_executor_ready", prefs().getBoolean("job_executor_ready", false));
        runtime.put("coreLinux", coreLinux);
        runtime.put("nativeRuntime", nativeRuntime);

        JSONObject status = new JSONObject();
        status.put("app", "foreground-agent-service");
        status.put("job_executor_ready", prefs().getBoolean("job_executor_ready", false));
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
        payload.put("jobsRuntime", "foreground-service-autonomous-agent");
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
        return CoreWorkerJobCatalog.capabilities();
    }

    private JSONArray supportedLightJobsArray() {
        return CoreWorkerJobCatalog.supportedJobs();
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
            out.put("supportedStage", runner.length() > 0 ? "core-linux-runner-preflight-v11" : (realValidated || rootfs.optBoolean("distributionReady", false) ? "core-linux-rootfs-import-v1" : "core-linux-runtime-v1-smoke"));
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

    private String normalizedServerUrl() {
        String url = prefs().getString("server_url", "").trim();
        if (url.isEmpty()) {
            url = BuildConfig.CORE_WORKER_VPS_URL == null ? "" : BuildConfig.CORE_WORKER_VPS_URL.trim();
        }
        return url.replaceAll("/+$", "");
    }

    private HttpResult request(String method, String url, JSONObject payload) throws Exception {
        return request(method, url, payload, null);
    }

    private HttpResult request(String method, String url, JSONObject payload, String token) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setRequestMethod(method);
        conn.setConnectTimeout(7000);
        conn.setReadTimeout(12000);
        conn.setRequestProperty("Accept", "application/json");
        if (token != null && !token.trim().isEmpty()) {
            conn.setRequestProperty("Authorization", "Bearer " + token.trim());
        }
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

        boolean ok() {
            return status >= 200 && status < 300;
        }
    }
}
