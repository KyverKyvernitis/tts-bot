package dev.core.worker;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.BatteryManager;
import android.os.Build;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.TimeUnit;

/**
 * Executor headless dos jobs seguros do APK.
 *
 * Esta classe não conhece Activity nem View. Todo estado observável é gravado em
 * SharedPreferences/arquivos internos, permitindo execução com a interface fechada.
 */
final class CoreWorkerJobExecutor {
    private static final String PREFS = "core_worker_private";
    private static final String UPDATE_CHANNEL_ID = "core_worker_updates";
    private static final int TEST_NOTIFICATION_ID = 4110;
    private static final long MAX_JSON_FILE_BYTES = 512L * 1024L;

    private final Context context;
    private final SharedPreferences prefs;

    CoreWorkerJobExecutor(Context context) {
        this.context = context.getApplicationContext();
        this.prefs = this.context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    JSONObject execute(JSONObject job, String serverUrl) throws Exception {
        String type = job == null ? "" : job.optString("type", "").trim();
        JSONObject jobPayload = job == null ? null : job.optJSONObject("payload");
        if (jobPayload == null) jobPayload = new JSONObject();

        JSONObject result = baseResult(type);
        if (!CoreWorkerJobCatalog.supports(type)) {
            return fail(result, "job interno não permitido pelo agente do APK: " + type);
        }

        if ("apk_ping".equals(type)) {
            result.put("message", "pong");
            result.put("runtime", "apk-agent-service");
            result.put("agent", agentSnapshot());
            return result;
        }
        if ("apk_status_refresh".equals(type) || "apk_sync_runtime_state".equals(type)) {
            result.put("status", statusSnapshot(serverUrl));
            result.put("runtime", runtimeSnapshot());
            result.put("message", "runtime e estado do agente atualizados pelo serviço");
            return result;
        }
        if ("apk_upload_app_logs".equals(type)) {
            JSONObject logs = new JSONObject();
            logs.put("lastAppError", prefs.getString("app_status_last_error", ""));
            logs.put("agentLastError", prefs.getString("agent_last_error", ""));
            logs.put("agentState", prefs.getString("internal_light_jobs_state", "aguardando"));
            logs.put("lastJob", prefs.getString("internal_light_jobs_last_summary", "nenhum"));
            logs.put("queue", prefs.getString("internal_jobs_queue_summary", "fila aguardando"));
            logs.put("historyText", historyText());
            logs.put("history", historyJson());
            logs.put("agent", agentSnapshot());
            result.put("reportKind", "app-agent-logs-lightweight");
            result.put("logs", logs);
            result.put("message", "logs leves do agente enviados sem Activity/Termux");
            return result;
        }
        if ("apk_diagnostic".equals(type)) {
            result.put("diagnostic", diagnosticSnapshot(serverUrl));
            result.put("message", "diagnóstico headless do APK concluído");
            return result;
        }
        if ("apk_check_update".equals(type)
                || "apk_verify_update_state".equals(type)
                || "apk_update_diagnostic".equals(type)) {
            JSONObject update = updateSnapshot(serverUrl);
            result.put("update", update);
            if (!update.optBoolean("ok", false)) {
                fail(result, update.optString("error", "checagem de atualização falhou"));
            }
            result.put("message", update.optBoolean("ok", false)
                    ? "checagem de atualização concluída pelo serviço"
                    : "checagem de atualização falhou");
            return result;
        }
        if ("apk_test_vps_connection".equals(type)) {
            JSONObject connection = vpsConnectionSnapshot(serverUrl);
            result.put("connection", connection);
            if (!connection.optBoolean("ok", false)) {
                fail(result, connection.optString("error", "teste de conexão falhou"));
            }
            result.put("message", connection.optBoolean("ok", false)
                    ? "teste de conexão VPS concluído pelo serviço"
                    : "teste de conexão VPS falhou");
            return result;
        }
        if ("apk_job_history".equals(type)) {
            result.put("history", historyJson());
            result.put("historyText", historyText());
            result.put("message", "histórico local do agente enviado");
            return result;
        }
        if ("apk_device_diagnostic".equals(type)) {
            result.put("device", deviceSnapshot());
            result.put("message", "diagnóstico do aparelho concluído pelo serviço");
            return result;
        }
        if ("apk_push_diagnostic".equals(type)) {
            result.put("push", pushSnapshot());
            result.put("message", "diagnóstico de push concluído pelo serviço");
            return result;
        }
        if ("apk_runtime_diagnostic".equals(type)) {
            JSONObject runtime = runtimeSnapshot();
            runtime.put("agent", agentSnapshot());
            runtime.put("jobHistoryText", historyText());
            result.put("runtime", runtime);
            result.put("message", "diagnóstico do runtime autônomo concluído");
            return result;
        }
        if ("apk_worker_bridge_status".equals(type)) {
            result.put("bridge", bridgeSnapshot());
            result.put("message", "estado da migração APK/Termux reportado");
            return result;
        }
        if ("apk_test_notification".equals(type)) {
            String state = showTestNotification();
            result.put("notificationState", state);
            result.put("permission", hasNotificationPermission() ? "granted" : "missing");
            if (!"displayed".equals(state)) fail(result, notificationDetail(state));
            result.put("message", "displayed".equals(state)
                    ? "notificação de teste exibida pelo serviço"
                    : "notificação de teste não exibida: " + notificationDetail(state));
            return result;
        }
        if ("apk_repair_local_state".equals(type)) {
            prefs.edit()
                    .remove("fcm_disabled_until")
                    .remove("internal_jobs_wake_requested_at")
                    .remove("agent_last_error")
                    .putBoolean("agent_enabled", true)
                    .putBoolean("job_executor_ready", true)
                    .putString("foreground_runtime_state", "agente autônomo reparado")
                    .putLong("internal_runtime_repair_at", System.currentTimeMillis())
                    .apply();
            result.put("status", statusSnapshot(serverUrl));
            result.put("message", "estado local do agente reparado");
            return result;
        }
        if ("apk_reset_job_history".equals(type)) {
            prefs.edit()
                    .putString("internal_job_history", "[]")
                    .putString("internal_completed_job_ids", "[]")
                    .apply();
            result.put("message", "histórico local de jobs limpo");
            return result;
        }
        if ("apk_trim_cache".equals(type)) {
            long bytes = deleteTreeContents(new File(context.getCacheDir(), "core-worker-jobs"));
            result.put("bytesCleared", bytes);
            result.put("message", "cache interno dos jobs limpo");
            return result;
        }
        if ("apk_update_storage_cleanup".equals(type)) {
            JSONObject cleanup = cleanupUpdateArtifacts("job_update_storage_cleanup");
            result.put("cleanup", cleanup);
            result.put("storage", storageSnapshot());
            if (!cleanup.optBoolean("ok", false)) {
                fail(result, cleanup.optString("error", "limpeza de updates falhou"));
            }
            result.put("message", cleanup.optString("summary", "limpeza de updates concluída"));
            return result;
        }
        if ("apk_sync_profile".equals(type) || "apk_sync_profile_now".equals(type)) {
            String profile = normalizeProfile(jobPayload.optString("profile", prefs.getString("profile", "midia")));
            prefs.edit()
                    .putString("profile", profile)
                    .putLong("agent_profile_synced_at", System.currentTimeMillis())
                    .apply();
            result.put("profile", profile);
            result.put("profileLabel", profileLabel(profile));
            result.put("agentProfileSaved", true);
            result.put("localAgentSynced", false);
            result.put("message", "perfil salvo diretamente no agente do APK");
            return result;
        }
        if ("apk_native_worker_status".equals(type)) {
            result.put("nativeWorker", nativeWorkerSnapshot());
            result.put("message", "estado do worker nativo enviado pelo serviço");
            return result;
        }
        if ("apk_native_boot_status".equals(type)) {
            result.put("boot", bootSnapshot());
            result.put("message", "boot nativo verificado pelo serviço");
            return result;
        }
        if ("apk_local_shell_probe".equals(type)) {
            result.put("shell", localShellProbe());
            result.put("message", "shell controlado do APK verificado");
            return result;
        }

        if (type.startsWith("apk_core_linux_")) {
            return executeCoreLinux(type, result);
        }

        return fail(result, "job sem handler no agente: " + type);
    }

    JSONObject buildFetchStatus(String serverUrl) throws Exception {
        JSONObject payload = statusSnapshot(serverUrl);
        payload.put("runtime", runtimeSnapshot());
        payload.put("agent", agentSnapshot());
        return payload;
    }

    private JSONObject executeCoreLinux(String type, JSONObject result) throws Exception {
        File coreDir = coreLinuxDir();

        if ("apk_core_linux_rootfs_import_status".equals(type)
                || "apk_core_linux_rootfs_real_status".equals(type)
                || "apk_core_linux_rootfs_glibc_preflight".equals(type)
                || "apk_core_linux_rootfs_import_validate".equals(type)
                || "apk_core_linux_rootfs_import_abort".equals(type)) {
            JSONObject rootfsImport;
            if ("apk_core_linux_rootfs_import_abort".equals(type)) {
                rootfsImport = CoreLinuxRootfsImportManager.abort(context, coreDir);
            } else if ("apk_core_linux_rootfs_glibc_preflight".equals(type)) {
                rootfsImport = CoreLinuxRootfsImportManager.glibcPreflight(context, coreDir);
            } else if ("apk_core_linux_rootfs_import_validate".equals(type)
                    || "apk_core_linux_rootfs_real_status".equals(type)) {
                rootfsImport = CoreLinuxRootfsImportManager.validateActive(context, coreDir);
            } else {
                rootfsImport = CoreLinuxRootfsImportManager.status(context, coreDir);
            }
            result.put("rootfsImport", rootfsImport);
            if ("apk_core_linux_rootfs_glibc_preflight".equals(type)) {
                result.put("coreLinuxRootfsGlibcPreflight", rootfsImport);
                result.put("stage", rootfsImport.optString("stage", "core-linux-rootfs-glibc-intake-preflight-v16.2"));
                result.put("state", rootfsImport.optString("state", "rootfs_glibc_preflight"));
                result.put("glibcRuntime", nonNullObject(rootfsImport.optJSONObject("glibcRuntime")));
                result.put("missing", nonNullArray(rootfsImport.optJSONArray("missing")));
                result.put("checks", nonNullObject(rootfsImport.optJSONObject("checks")));
                result.put("validationLevel", rootfsImport.optString("validationLevel", ""));
                result.put("readyForBox64Smoke", rootfsImport.optBoolean("readyForBox64Smoke", false));
            }
            persistCoreLinuxState(rootfsImport);
            if (!rootfsImport.optBoolean("ok", false)) {
                fail(result, rootfsImport.optString("summary", "rootfs import pendente"));
            }
            result.put("message", rootfsImport.optString("summary", "rootfs import verificado"));
            return result;
        }

        if ("apk_core_linux_rootfs_status".equals(type)
                || "apk_core_linux_rootfs_preflight".equals(type)
                || "apk_core_linux_rootfs_prepare".equals(type)
                || "apk_core_linux_rootfs_validate".equals(type)
                || "apk_core_linux_rootfs_clean_staging".equals(type)) {
            String action = "status";
            if ("apk_core_linux_rootfs_preflight".equals(type)) action = "preflight";
            if ("apk_core_linux_rootfs_prepare".equals(type)) action = "prepare";
            if ("apk_core_linux_rootfs_validate".equals(type)) action = "validate";
            if ("apk_core_linux_rootfs_clean_staging".equals(type)) action = "clean_staging";
            JSONObject rootfs = CoreLinuxRuntimeManager.rootfsSnapshot(context, coreDir, action);
            result.put("rootfs", rootfs);
            JSONObject rootfsState = rootfs.optJSONObject("rootfs");
            if (rootfsState != null) result.put("rootfsState", rootfsState);
            persistCoreLinuxState(rootfs);
            if (!rootfs.optBoolean("ok", false)) {
                fail(result, rootfs.optString("summary", "rootfs interno pendente"));
            }
            result.put("message", rootfs.optString("summary", "rootfs interno verificado sem Termux"));
            return result;
        }

        if ("apk_core_linux_runner_status".equals(type)
                || "apk_core_linux_runner_preflight".equals(type)
                || "apk_core_linux_runner_requirements".equals(type)) {
            String action = "status";
            if ("apk_core_linux_runner_preflight".equals(type)) action = "preflight";
            if ("apk_core_linux_runner_requirements".equals(type)) action = "requirements";
            JSONObject runner = CoreLinuxRunnerPreflightManager.preflight(context, coreDir, action);
            result.put("coreLinuxRunner", runner);
            result.put("coreLinux", coreLinuxPublicSnapshot());
            result.put("runnerReady", runner.optBoolean("runnerReady", false));
            result.put("runnerBlocked", runner.optBoolean("runnerBlocked", true));
            result.put("bedrockStarted", false);
            result.put("shellOpened", false);
            persistCoreLinuxState(runner);
            result.put("message", runner.optString("summary", "Runner preflight verificado sem iniciar Bedrock"));
            return result;
        }

        if ("apk_core_linux_runtime_smoke_test".equals(type)) {
            JSONObject nativeExecutor = nativeExecutorSnapshot("test");
            JSONObject smoke = CoreLinuxRuntimeManager.smokeTest(context, coreDir, nativeExecutor);
            result.put("coreLinuxSmokeTest", smoke);
            persistCoreLinuxState(smoke);
            if (!smoke.optBoolean("ok", false)) fail(result, smoke.optString("summary", "smoke test Core Linux pendente"));
            result.put("message", smoke.optString("summary", "Core Linux smoke test executado sem Termux"));
            return result;
        }

        if ("apk_core_linux_rootfs_smoke_test".equals(type)) {
            JSONObject nativeExecutor = nativeExecutorSnapshot("test");
            JSONObject smoke = CoreLinuxRuntimeManager.rootfsProotSmokeTest(context, coreDir, nativeExecutor);
            result.put("coreLinuxRootfsSmokeTest", smoke);
            persistCoreLinuxState(smoke);
            if (!smoke.optBoolean("ok", false)) fail(result, smoke.optString("summary", "smoke rootfs Core Linux pendente"));
            result.put("message", smoke.optString("summary", "Core Linux rootfs smoke executado sem Termux"));
            return result;
        }

        if ("apk_core_linux_box64_preflight".equals(type)) {
            JSONObject nativeExecutor = nativeExecutorSnapshot("test");
            JSONObject box64 = CoreLinuxRuntimeManager.box64IntakePreflight(context, coreDir, nativeExecutor);
            result.put("coreLinuxBox64Preflight", box64);
            persistCoreLinuxState(box64);
            if (!box64.optBoolean("ok", false)) fail(result, box64.optString("summary", "Box64 pendente"));
            result.put("message", box64.optString("summary", "Box64 intake/preflight executado sem iniciar Bedrock"));
            return result;
        }

        if ("apk_core_linux_box64_smoke_test".equals(type)) {
            JSONObject smoke = box64HardGuardSnapshot(coreDir);
            result.put("ok", smoke.optBoolean("ok", false));
            result.put("coreLinuxBox64SmokeTest", smoke);
            persistCoreLinuxState(smoke);
            if (!smoke.optBoolean("ok", false)) result.put("error", smoke.optString("summary", "Box64 bloqueado"));
            result.put("message", smoke.optString("summary", "Box64 hard guard concluído"));
            return result;
        }

        if ("apk_core_linux_native_executor_probe".equals(type)
                || "apk_core_linux_native_executor_test".equals(type)
                || "apk_core_linux_native_runtime_status".equals(type)) {
            String action = "probe";
            if ("apk_core_linux_native_executor_test".equals(type)) action = "test";
            if ("apk_core_linux_native_runtime_status".equals(type)) action = "status";
            JSONObject nativeExecutor = nativeExecutorSnapshot(action);
            result.put("nativeExecutor", nativeExecutor);
            try {
                JSONObject runtime = CoreLinuxRuntimeManager.runtimeSnapshot(context, coreDir, "executor", nativeExecutor);
                result.put("coreLinuxInternal", runtime);
                persistCoreLinuxState(runtime);
            } catch (Throwable ignored) {
                persistCoreLinuxState(nativeExecutor);
            }
            if (!nativeExecutor.optBoolean("ok", false)) {
                fail(result, nativeExecutor.optString("summary", "executor nativo interno pendente"));
            }
            result.put("message", nativeExecutor.optString("summary", "executor nativo interno atualizado"));
            return result;
        }

        return fail(result, "job Core Linux sem handler: " + type);
    }

    private JSONObject baseResult(String type) throws Exception {
        JSONObject result = new JSONObject();
        result.put("ok", true);
        result.put("type", type);
        result.put("executedBy", "core-worker-apk-agent-service");
        result.put("safety", "fila allowlist · contexto headless · sem Activity/Termux obrigatório");
        result.put("appVersion", BuildConfig.VERSION_NAME);
        result.put("appVersionCode", BuildConfig.VERSION_CODE);
        result.put("installId", installId());
        result.put("workerId", effectiveWorkerId());
        return result;
    }

    private JSONObject fail(JSONObject target, String error) throws Exception {
        target.put("ok", false);
        target.put("error", error == null ? "falha" : error);
        return target;
    }

    private JSONObject statusSnapshot(String serverUrl) throws Exception {
        JSONObject status = new JSONObject();
        status.put("app", "agent-service");
        status.put("platform", "android");
        status.put("android_sdk", Build.VERSION.SDK_INT);
        status.put("workerId", effectiveWorkerId());
        status.put("installId", installId());
        status.put("deviceName", prefs.getString("device_name", Build.MANUFACTURER + " " + Build.MODEL));
        status.put("profile", normalizeProfile(prefs.getString("profile", "midia")));
        status.put("pairedDirect", prefs.getBoolean("paired_via_native_apk", false));
        status.put("workerTokenPresent", !prefs.getString("worker_token", "").trim().isEmpty());
        status.put("notificationPermission", hasNotificationPermission() ? "granted" : "missing");
        status.put("network", networkSnapshot(serverUrl));
        status.put("battery", batterySnapshot());
        status.put("agent", agentSnapshot());
        status.put("supported_tasks", CoreWorkerJobCatalog.supportedJobs());
        status.put("capabilities", CoreWorkerJobCatalog.capabilities());
        status.put("termux_required_now", false);
        status.put("termux_role", "fallback-legado");
        return status;
    }

    private JSONObject runtimeSnapshot() throws Exception {
        JSONObject runtime = new JSONObject();
        runtime.put("mode", "apk-agent-service");
        runtime.put("jobs_runtime", "foreground-service-autonomous-agent");
        runtime.put("job_executor_ready", prefs.getBoolean("job_executor_ready", false));
        runtime.put("foreground_runtime_active", prefs.getBoolean("foreground_runtime_active", false));
        runtime.put("foreground_runtime_state", prefs.getString("foreground_runtime_state", "aguardando"));
        runtime.put("foreground_runtime_last_tick_at", prefs.getLong("foreground_runtime_last_tick_at", 0L));
        runtime.put("native_tts_bridge_active", prefs.getBoolean("native_tts_bridge_active", false));
        runtime.put("supported_tasks", CoreWorkerJobCatalog.supportedJobs());
        runtime.put("capabilities", CoreWorkerJobCatalog.capabilities());
        runtime.put("coreLinux", coreLinuxPublicSnapshot());
        runtime.put("termux_required_now", false);
        runtime.put("advanced_jobs_require_termux", false);
        return runtime;
    }

    private JSONObject agentSnapshot() throws Exception {
        JSONObject agent = new JSONObject();
        agent.put("enabled", prefs.getBoolean("agent_enabled", false));
        agent.put("running", prefs.getBoolean("foreground_runtime_active", false));
        agent.put("executorReady", prefs.getBoolean("job_executor_ready", false));
        agent.put("state", prefs.getString("internal_light_jobs_state", "aguardando"));
        agent.put("queue", prefs.getString("internal_jobs_queue_summary", "fila aguardando"));
        agent.put("runningJobs", prefs.getInt("internal_jobs_running_count", 0));
        agent.put("pendingJobs", prefs.getInt("internal_jobs_pending_count", 0));
        agent.put("lastCheckAt", prefs.getLong("internal_light_jobs_last_check_at", 0L));
        agent.put("lastSummary", prefs.getString("internal_light_jobs_last_summary", "nenhum job executado ainda"));
        agent.put("lastError", prefs.getString("agent_last_error", ""));
        agent.put("outboxPending", countFiles(outboxDir()));
        agent.put("supportedJobs", CoreWorkerJobCatalog.size());
        return agent;
    }

    private JSONObject diagnosticSnapshot(String serverUrl) throws Exception {
        JSONObject diagnostic = new JSONObject();
        diagnostic.put("device", deviceSnapshot());
        diagnostic.put("network", networkSnapshot(serverUrl));
        diagnostic.put("storage", storageSnapshot());
        diagnostic.put("runtime", runtimeSnapshot());
        diagnostic.put("bridge", bridgeSnapshot());
        diagnostic.put("boot", bootSnapshot());
        diagnostic.put("nativeWorker", nativeWorkerSnapshot());
        diagnostic.put("history", historyJson());
        diagnostic.put("summary", "diagnóstico leve concluído pelo agente autônomo");
        return diagnostic;
    }

    private JSONObject bridgeSnapshot() throws Exception {
        JSONObject bridge = new JSONObject();
        boolean paired = prefs.getBoolean("paired_via_native_apk", false)
                && !prefs.getString("worker_token", "").trim().isEmpty();
        bridge.put("mode", "apk-agent-service");
        bridge.put("apk_internal_online", prefs.getBoolean("foreground_runtime_active", false));
        bridge.put("apk_native_worker_online", paired);
        bridge.put("termux_worker_online", prefs.getBoolean("legacy_termux_online", false));
        bridge.put("jobs_real_runtime", "apk-agent-service");
        bridge.put("jobs_internal_runtime", "apk-agent-durable-queue");
        bridge.put("termux_role", "fallback-legado");
        bridge.put("ready_for_termux_reduction", paired && prefs.getBoolean("job_executor_ready", false));
        bridge.put("summary", paired
                ? "APK autônomo pareado e executor ativo"
                : "APK aguardando pareamento direto");
        return bridge;
    }

    private JSONObject nativeWorkerSnapshot() throws Exception {
        boolean paired = prefs.getBoolean("paired_via_native_apk", false);
        boolean token = !prefs.getString("worker_token", "").trim().isEmpty();
        boolean running = prefs.getBoolean("foreground_runtime_active", false);
        JSONObject obj = new JSONObject();
        obj.put("online", paired && token && running);
        obj.put("state", paired && token
                ? (running ? "agente autônomo online" : "pareado; serviço parado")
                : "aguardando pareamento direto");
        obj.put("workerId", effectiveWorkerId());
        obj.put("pairedDirect", paired);
        obj.put("lastHeartbeatAt", prefs.getLong("native_worker_last_heartbeat_at", 0L));
        obj.put("executorReady", prefs.getBoolean("job_executor_ready", false));
        obj.put("summary", obj.optString("state", "worker nativo"));
        return obj;
    }

    private JSONObject bootSnapshot() throws Exception {
        JSONObject boot = new JSONObject();
        boot.put("receiver", "CoreWorkerBootReceiver");
        boot.put("receiveBootCompletedPermission", true);
        boot.put("jobScheduler", "CoreWorkerUpdateJobService");
        boot.put("persistedPeriodicJob", true);
        boot.put("agentAutoStart", prefs.getBoolean("agent_enabled", false));
        boot.put("lastWakeReason", prefs.getString("internal_jobs_wake_reason", ""));
        boot.put("lastWakeRequestedAt", prefs.getLong("internal_jobs_wake_requested_at", 0L));
        boot.put("summary", "boot nativo pronto · agente reinicia sem Termux:Boot");
        return boot;
    }

    private JSONObject deviceSnapshot() throws Exception {
        JSONObject device = new JSONObject();
        device.put("manufacturer", Build.MANUFACTURER);
        device.put("model", Build.MODEL);
        device.put("android_sdk", Build.VERSION.SDK_INT);
        device.put("battery", batterySnapshot());
        device.put("storage", storageSnapshot());
        device.put("notificationPermission", hasNotificationPermission() ? "granted" : "missing");
        device.put("summary", Build.MANUFACTURER + " " + Build.MODEL + " · Android " + Build.VERSION.SDK_INT);
        return device;
    }

    private JSONObject batterySnapshot() throws Exception {
        JSONObject battery = new JSONObject();
        Intent state = context.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        if (state == null) {
            battery.put("available", false);
            return battery;
        }
        int level = state.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
        int scale = state.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
        int percent = level >= 0 && scale > 0 ? Math.round(level * 100f / scale) : -1;
        int status = state.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
        battery.put("available", true);
        battery.put("percent", percent);
        battery.put("charging", status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL);
        return battery;
    }

    private JSONObject networkSnapshot(String serverUrl) throws Exception {
        JSONObject network = new JSONObject();
        ConnectivityManager manager = (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
        Network active = manager == null ? null : manager.getActiveNetwork();
        NetworkCapabilities capabilities = manager == null || active == null ? null : manager.getNetworkCapabilities(active);
        boolean available = capabilities != null && capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
        network.put("available", available);
        network.put("validated", capabilities != null && capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED));
        network.put("wifi", capabilities != null && capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI));
        network.put("cellular", capabilities != null && capabilities.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR));
        network.put("vps_ping_ms", serverUrl == null || serverUrl.trim().isEmpty() ? -1 : tcpPing(serverUrl));
        return network;
    }

    private JSONObject pushSnapshot() throws Exception {
        JSONObject push = new JSONObject();
        push.put("enabled_in_build", BuildConfig.CORE_WORKER_FCM_ENABLED);
        push.put("state", prefs.getString("fcm_state", "não verificado"));
        push.put("token_registered_locally", !prefs.getString("fcm_token", "").trim().isEmpty());
        push.put("permission", hasNotificationPermission() ? "granted" : "missing");
        push.put("fallback_periodic_job", true);
        return push;
    }

    private JSONObject storageSnapshot() throws Exception {
        JSONObject storage = new JSONObject();
        File files = context.getFilesDir();
        File cache = context.getCacheDir();
        File jobCache = new File(cache, "core-worker-jobs");
        File outbox = outboxDir();
        storage.put("files_bytes", directorySize(files));
        storage.put("cache_bytes", directorySize(cache));
        storage.put("job_cache_bytes", directorySize(jobCache));
        storage.put("agent_outbox_bytes", directorySize(outbox));
        storage.put("agent_outbox_files", countFiles(outbox));
        storage.put("summary", "cache " + humanBytes(directorySize(cache)) + " · outbox " + countFiles(outbox));
        return storage;
    }

    private JSONObject updateSnapshot(String serverUrl) throws Exception {
        JSONObject update = new JSONObject();
        if (serverUrl == null || serverUrl.trim().isEmpty()) {
            update.put("ok", false);
            update.put("error", "VPS não configurada");
            return update;
        }
        HttpResult response = request("GET", serverUrl + "/core-worker/app/latest.json", null, null);
        if (!response.ok()) {
            response = request("GET", serverUrl + "/core-worker/latest.json", null, null);
        }
        update.put("httpStatus", response.status);
        if (!response.ok()) {
            update.put("ok", false);
            update.put("error", compact(response.body));
            return update;
        }
        JSONObject body = new JSONObject(response.body);
        String latestName = body.optString("versionName", body.optString("version", ""));
        int latestCode = body.optInt("versionCode", -1);
        boolean available = latestCode > BuildConfig.VERSION_CODE
                || (latestCode < 0 && !latestName.isEmpty() && !BuildConfig.VERSION_NAME.equals(latestName));
        prefs.edit()
                .putString("latest_version_name", latestName)
                .putInt("latest_version_code", latestCode)
                .putBoolean("latest_update_available", available)
                .putLong("latest_update_checked_at", System.currentTimeMillis())
                .apply();
        update.put("ok", true);
        update.put("installedVersion", BuildConfig.VERSION_NAME);
        update.put("installedCode", BuildConfig.VERSION_CODE);
        update.put("latestVersion", latestName);
        update.put("latestCode", latestCode);
        update.put("available", available);
        update.put("sha256", body.optString("sha256", ""));
        update.put("state", available ? "atualização disponível" : "em dia");
        return update;
    }

    private JSONObject vpsConnectionSnapshot(String serverUrl) throws Exception {
        JSONObject connection = new JSONObject();
        connection.put("serverConfigured", serverUrl != null && !serverUrl.trim().isEmpty());
        if (serverUrl == null || serverUrl.trim().isEmpty()) {
            connection.put("ok", false);
            connection.put("error", "VPS não configurada");
            return connection;
        }
        connection.put("tcpPingMs", tcpPing(serverUrl));
        HttpResult response = request("GET", serverUrl + "/health", null, null);
        connection.put("httpStatus", response.status);
        connection.put("ok", response.ok());
        if (!response.ok()) connection.put("error", compact(response.body));
        return connection;
    }

    private JSONObject localShellProbe() throws Exception {
        JSONObject shell = new JSONObject();
        shell.put("mode", "allowlist");
        shell.put("scope", "app-sandbox");
        shell.put("arbitraryCommands", false);
        JSONArray commands = new JSONArray();
        commands.put(runCommand("whoami", new String[]{"/system/bin/sh", "-c", "id"}));
        commands.put(runCommand("pwd", new String[]{"/system/bin/sh", "-c", "pwd"}));
        commands.put(runCommand("files", new String[]{"/system/bin/sh", "-c", "ls -la " + shellQuote(context.getFilesDir().getAbsolutePath())}));
        commands.put(runCommand("storage", new String[]{"/system/bin/sh", "-c", "df -k " + shellQuote(context.getFilesDir().getAbsolutePath())}));
        shell.put("commands", commands);
        shell.put("summary", "shell controlado ok · sandbox do APK");
        return shell;
    }

    private JSONObject runCommand(String label, String[] command) throws Exception {
        JSONObject out = new JSONObject();
        out.put("label", label);
        Process process = null;
        try {
            ProcessBuilder builder = new ProcessBuilder(command);
            builder.directory(context.getFilesDir());
            process = builder.start();
            boolean finished = process.waitFor(1800L, TimeUnit.MILLISECONDS);
            if (!finished) {
                process.destroy();
                out.put("ok", false);
                out.put("error", "timeout");
                return out;
            }
            out.put("ok", process.exitValue() == 0);
            out.put("exitCode", process.exitValue());
            out.put("stdout", limit(readAll(process.getInputStream()), 1200));
            out.put("stderr", limit(readAll(process.getErrorStream()), 600));
        } catch (Throwable exc) {
            out.put("ok", false);
            out.put("error", shortThrowable(exc));
        } finally {
            if (process != null) {
                try { process.destroy(); } catch (Throwable ignored) { }
            }
        }
        return out;
    }

    private JSONObject nativeExecutorSnapshot(String action) {
        return CoreWorkerNativeExecutor.snapshot(context, coreLinuxDir(), action == null ? "probe" : action);
    }

    private JSONObject coreLinuxPublicSnapshot() throws Exception {
        File runtimeDir = new File(coreLinuxDir(), "runtime");
        JSONObject runtime = readJson(new File(runtimeDir, "linux-runtime-state.json"));
        JSONObject rootfs = readJson(new File(runtimeDir, "rootfs-state.json"));
        JSONObject smoke = readJson(new File(runtimeDir, "core-linux-smoke-test.json"));
        JSONObject rootfsImport = readJson(new File(runtimeDir, "rootfs-import-state.json"));
        JSONObject runner = readJson(new File(runtimeDir, "runner-preflight-state.json"));
        String rawState = firstNonEmpty(rootfs.optString("state", ""), rootfsImport.optString("state", ""), runtime.optString("state", ""), smoke.optString("state", ""));
        String level = firstNonEmpty(rootfs.optString("validationLevel", ""), rootfs.optString("rootfsValidationLevel", ""), rootfsImport.optString("validationLevel", ""));
        boolean realValidated = rawState.toLowerCase(Locale.ROOT).contains("rootfs_real_validated") || "real".equalsIgnoreCase(level);
        boolean prepared = realValidated || runtime.optBoolean("ok", false) || runtime.optBoolean("rootfsReady", false)
                || rootfs.optBoolean("rootfsReady", false) || smoke.optBoolean("ok", false);
        String summary = firstNonEmpty(rootfs.optString("summary", ""), rootfsImport.optString("summary", ""), runtime.optString("summary", ""), smoke.optString("summary", ""), prepared ? "Core Linux Runtime v1 pronto" : "Core Linux Runtime v1 aguardando preparo");
        String state = firstNonEmpty(rawState, prepared ? "runtime_v1_ready" : "runtime_v1_pending");
        if (realValidated) {
            state = "rootfs_real_validated";
            summary = firstNonEmpty(rootfs.optString("summary", ""), rootfsImport.optString("summary", ""), "Rootfs real validado · runner real ainda bloqueado");
        }
        JSONObject out = new JSONObject();
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
        if (runner.length() > 0) out.put("runnerPreflight", runner);
        out.put("supportedTasks", CoreWorkerJobCatalog.supportedJobs());
        if (runtime.length() > 0) out.put("runtime", runtime);
        if (rootfs.length() > 0) out.put("rootfs", rootfs);
        if (rootfsImport.length() > 0) out.put("rootfsImport", rootfsImport);
        if (smoke.length() > 0) out.put("smoke", smoke);
        return out;
    }

    private JSONObject box64HardGuardSnapshot(File coreDir) throws Exception {
        File rootfs = new File(coreDir, "rootfs");
        File loader = new File(rootfs, "lib/ld-linux-aarch64.so.1");
        File libc1 = new File(rootfs, "lib/aarch64-linux-gnu/libc.so.6");
        File libc2 = new File(rootfs, "usr/lib/aarch64-linux-gnu/libc.so.6");
        File libm1 = new File(rootfs, "lib/aarch64-linux-gnu/libm.so.6");
        File libm2 = new File(rootfs, "usr/lib/aarch64-linux-gnu/libm.so.6");
        File libresolv1 = new File(rootfs, "lib/aarch64-linux-gnu/libresolv.so.2");
        File libresolv2 = new File(rootfs, "usr/lib/aarch64-linux-gnu/libresolv.so.2");
        File marker = new File(rootfs, ".core-worker-rootfs-ready");
        File osRelease = new File(rootfs, "etc/os-release");

        boolean rootfsDir = rootfs.isDirectory();
        boolean rootfsMinimalReady = rootfsDir && marker.isFile() && osRelease.isFile();
        boolean loaderOk = loader.isFile();
        boolean libcOk = libc1.isFile() || libc2.isFile();
        boolean libmOk = libm1.isFile() || libm2.isFile();
        boolean libresolvOk = libresolv1.isFile() || libresolv2.isFile();
        boolean glibcReady = loaderOk && libcOk && libmOk && libresolvOk;
        boolean ok = rootfsMinimalReady && glibcReady;
        String state = !rootfsMinimalReady
                ? "box64_glibc_preflight_blocked_rootfs"
                : (!glibcReady ? "box64_smoke_blocked_missing_glibc_runtime" : "box64_glibc_preflight_ready");
        String summary = ok
                ? "Box64 V15.3.1 pronto · runtime glibc arm64 presente; próxima etapa pode tocar no asset Box64"
                : (!rootfsMinimalReady
                ? "Box64 V15.3.1 bloqueado · rootfs mínimo ainda não validado"
                : "Box64 V15.3.1 bloqueado · rootfs sem runtime glibc arm64 necessário");

        JSONArray missing = new JSONArray();
        if (!rootfsDir) missing.put("rootfs_dir");
        if (!marker.isFile()) missing.put(".core-worker-rootfs-ready");
        if (!osRelease.isFile()) missing.put("etc/os-release");
        if (!loaderOk) missing.put("/lib/ld-linux-aarch64.so.1");
        if (!libcOk) missing.put("libc.so.6");
        if (!libmOk) missing.put("libm.so.6");
        if (!libresolvOk) missing.put("libresolv.so.2");

        JSONObject checks = new JSONObject()
                .put("rootfsDir", rootfsDir)
                .put("readyMarker", marker.isFile())
                .put("osRelease", osRelease.isFile())
                .put("loader", loaderOk)
                .put("libc", libcOk)
                .put("libm", libmOk)
                .put("libresolv", libresolvOk)
                .put("box64AssetOpened", false)
                .put("box64Extracted", false)
                .put("box64Executed", false)
                .put("nativeExecutorSnapshotCalled", false)
                .put("genericRuntimeSnapshotCalled", false);

        return new JSONObject()
                .put("ok", ok)
                .put("type", "core_linux_box64_glibc_hard_guard")
                .put("stage", "core-linux-box64-glibc-preflight-v15.3.1")
                .put("state", state)
                .put("summary", summary)
                .put("termuxTouched", false)
                .put("pythonTouched", false)
                .put("serviceStarted", false)
                .put("bedrockStarted", false)
                .put("box64Started", false)
                .put("shellOpened", false)
                .put("remoteCommandAllowed", false)
                .put("x86_64UserBinaryAllowed", false)
                .put("rootfsDir", rootfs.getAbsolutePath())
                .put("rootfsMinimalReady", rootfsMinimalReady)
                .put("glibcRuntime", new JSONObject()
                        .put("ok", glibcReady)
                        .put("loader", loaderOk)
                        .put("libc", libcOk)
                        .put("libm", libmOk)
                        .put("libresolv", libresolvOk)
                        .put("missing", missing))
                .put("missing", missing)
                .put("checks", checks)
                .put("memoryPolicy", new JSONObject()
                        .put("stage", "v15.3.1")
                        .put("headlessHardGuard", true)
                        .put("doesNotOpenBox64Asset", true)
                        .put("doesNotExtractBox64", true)
                        .put("doesNotHashBox64", true))
                .put("nextStep", ok ? "v15.4-extrair-box64-e-rodar-version-help" : "importar/preparar rootfs Linux arm64 com glibc")
                .put("updatedAt", System.currentTimeMillis());
    }

    private void persistCoreLinuxState(JSONObject snapshot) {
        if (snapshot == null) return;
        prefs.edit()
                .putString("agent_core_linux_summary", snapshot.optString("summary", ""))
                .putString("agent_core_linux_state", snapshot.optString("state", ""))
                .putBoolean("agent_core_linux_prepared", snapshot.optBoolean("ok", snapshot.optBoolean("prepared", false)))
                .putLong("agent_core_linux_last_check_at", System.currentTimeMillis())
                .apply();
    }

    private JSONObject cleanupUpdateArtifacts(String reason) {
        try {
            int pendingCode = prefs.getInt("pending_update_version_code", -1);
            String pendingName = prefs.getString("pending_update_apk_name", "");
            boolean keepPending = pendingCode > BuildConfig.VERSION_CODE && !pendingName.trim().isEmpty();
            long bytes = 0L;
            int files = 0;
            for (File dir : updateArtifactDirs()) {
                DeleteStats stats = cleanupDirectory(dir, keepPending ? pendingName.trim() : "");
                bytes += stats.bytes;
                files += stats.files;
            }
            SharedPreferences.Editor edit = prefs.edit()
                    .putLong("update_cleanup_last_at", System.currentTimeMillis())
                    .putLong("update_cleanup_last_bytes", bytes)
                    .putInt("update_cleanup_last_files", files)
                    .putString("update_cleanup_last_reason", reason == null ? "" : reason);
            if (!keepPending) {
                edit.remove("pending_update_version_code")
                        .remove("pending_update_version_name")
                        .remove("pending_update_apk_name")
                        .remove("pending_update_sha256")
                        .remove("pending_update_saved_at");
            }
            edit.apply();
            return new JSONObject()
                    .put("ok", true)
                    .put("bytesCleared", bytes)
                    .put("filesCleared", files)
                    .put("keptPendingInstaller", keepPending)
                    .put("summary", bytes > 0 ? "updates limpos " + humanBytes(bytes) : "updates sem lixo");
        } catch (Throwable exc) {
            try {
                return new JSONObject().put("ok", false).put("error", shortThrowable(exc)).put("summary", "limpeza de updates falhou");
            } catch (Throwable ignored) {
                return new JSONObject();
            }
        }
    }

    private File[] updateArtifactDirs() {
        File external = context.getExternalFilesDir(null);
        if (external == null) external = context.getFilesDir();
        return new File[] {
                new File(external, "updates"),
                new File(context.getCacheDir(), "updates"),
                new File(context.getFilesDir(), "updates")
        };
    }

    private DeleteStats cleanupDirectory(File dir, String keepName) {
        DeleteStats total = new DeleteStats();
        if (dir == null || !dir.exists()) return total;
        File[] children = dir.listFiles();
        if (children == null) return total;
        for (File child : children) {
            if (child == null || (!keepName.isEmpty() && keepName.equals(child.getName()))) continue;
            DeleteStats stats = deleteArtifact(child);
            total.bytes += stats.bytes;
            total.files += stats.files;
        }
        return total;
    }

    private DeleteStats deleteArtifact(File file) {
        DeleteStats stats = new DeleteStats();
        if (file == null || !file.exists()) return stats;
        if (file.isDirectory()) {
            File[] children = file.listFiles();
            if (children != null) {
                for (File child : children) {
                    DeleteStats nested = deleteArtifact(child);
                    stats.bytes += nested.bytes;
                    stats.files += nested.files;
                }
            }
        }
        stats.bytes += Math.max(0L, file.length());
        try { if (file.delete()) stats.files++; } catch (Throwable ignored) { }
        return stats;
    }

    private String showTestNotification() {
        if (!hasNotificationPermission()) return "permission_missing";
        try {
            NotificationManager manager = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
            if (manager == null) return "manager_unavailable";
            if (Build.VERSION.SDK_INT >= 26) {
                manager.createNotificationChannel(new NotificationChannel(UPDATE_CHANNEL_ID, "Atualizações do Core Worker", NotificationManager.IMPORTANCE_DEFAULT));
            }
            Intent open = new Intent(context, MainActivity.class);
            open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_NEW_TASK);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 23) flags |= PendingIntent.FLAG_IMMUTABLE;
            PendingIntent pending = PendingIntent.getActivity(context, TEST_NOTIFICATION_ID, open, flags);
            Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                    ? new Notification.Builder(context, UPDATE_CHANNEL_ID)
                    : new Notification.Builder(context);
            builder.setSmallIcon(android.R.drawable.stat_sys_upload_done)
                    .setContentTitle("Core Worker")
                    .setContentText("Agente autônomo ativo e apto a receber jobs")
                    .setContentIntent(pending)
                    .setAutoCancel(true);
            manager.notify(TEST_NOTIFICATION_ID, builder.build());
            return "displayed";
        } catch (Throwable exc) {
            prefs.edit().putString("agent_last_error", shortThrowable(exc)).apply();
            return "failed";
        }
    }

    private String notificationDetail(String state) {
        if ("permission_missing".equals(state)) return "permissão de notificação ausente";
        if ("manager_unavailable".equals(state)) return "NotificationManager indisponível";
        return "falha ao exibir notificação";
    }

    private JSONArray historyJson() {
        try { return new JSONArray(prefs.getString("internal_job_history", "[]")); }
        catch (Throwable ignored) { return new JSONArray(); }
    }

    private String historyText() {
        JSONArray history = historyJson();
        if (history.length() == 0) return "sem histórico local";
        StringBuilder out = new StringBuilder();
        for (int i = 0; i < Math.min(4, history.length()); i++) {
            JSONObject item = history.optJSONObject(i);
            if (item == null) continue;
            if (out.length() > 0) out.append("; ");
            out.append(item.optString("type", "job"));
            out.append(item.optBoolean("ok", false) ? " ok" : " falhou");
        }
        return out.length() == 0 ? "sem histórico local" : out.toString();
    }

    private File coreLinuxDir() {
        File dir = new File(context.getFilesDir(), "core-linux");
        if (!dir.exists()) dir.mkdirs();
        return dir;
    }

    File outboxDir() {
        File dir = new File(new File(context.getFilesDir(), "core-worker-agent"), "outbox");
        if (!dir.exists()) dir.mkdirs();
        return dir;
    }

    private JSONObject readJson(File file) {
        try {
            if (file == null || !file.isFile() || file.length() > MAX_JSON_FILE_BYTES) return new JSONObject();
            FileInputStream input = new FileInputStream(file);
            byte[] data = new byte[(int) Math.min(file.length(), MAX_JSON_FILE_BYTES)];
            int read = input.read(data);
            input.close();
            return read <= 0 ? new JSONObject() : new JSONObject(new String(data, 0, read, StandardCharsets.UTF_8));
        } catch (Throwable ignored) {
            return new JSONObject();
        }
    }

    private HttpResult request(String method, String url, JSONObject payload, String token) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setRequestMethod(method);
        connection.setConnectTimeout(7000);
        connection.setReadTimeout(12000);
        connection.setRequestProperty("Accept", "application/json");
        if (token != null && !token.trim().isEmpty()) {
            connection.setRequestProperty("Authorization", "Bearer " + token.trim());
        }
        if (payload != null) {
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            OutputStream output = connection.getOutputStream();
            output.write(payload.toString().getBytes(StandardCharsets.UTF_8));
            output.flush();
            output.close();
        }
        int status = connection.getResponseCode();
        InputStream input = status >= 200 && status < 400 ? connection.getInputStream() : connection.getErrorStream();
        String body = readAll(input);
        connection.disconnect();
        return new HttpResult(status, body == null ? "" : body);
    }

    private long tcpPing(String serverUrl) {
        Socket socket = null;
        try {
            URL url = new URL(serverUrl);
            int port = url.getPort();
            if (port <= 0) port = "https".equalsIgnoreCase(url.getProtocol()) ? 443 : 80;
            long started = System.nanoTime();
            socket = new Socket();
            socket.connect(new InetSocketAddress(url.getHost(), port), 3000);
            return Math.max(0L, Math.round((System.nanoTime() - started) / 1_000_000.0));
        } catch (Throwable ignored) {
            return -1L;
        } finally {
            if (socket != null) try { socket.close(); } catch (Throwable ignored) { }
        }
    }

    private String effectiveWorkerId() {
        String value = prefs.getString("native_worker_id", "").trim();
        if (!value.isEmpty()) return value;
        value = prefs.getString("worker_id", "").trim();
        if (!value.isEmpty()) return value;
        String compact = installId().replace("-", "");
        if (compact.length() > 18) compact = compact.substring(0, 18);
        return "apk-" + compact;
    }

    private String installId() {
        String value = prefs.getString("install_id", "").trim();
        if (!value.isEmpty()) return value;
        value = UUID.randomUUID().toString();
        prefs.edit().putString("install_id", value).apply();
        return value;
    }

    private boolean hasNotificationPermission() {
        return Build.VERSION.SDK_INT < 33 || context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
    }

    private String normalizeProfile(String profile) {
        String value = profile == null ? "" : profile.trim().toLowerCase(Locale.ROOT);
        value = value.replace('í', 'i').replace('á', 'a').replace('é', 'e').replace('ó', 'o').replace('ú', 'u');
        value = value.replaceAll("[^a-z0-9_-]+", "-").replaceAll("^-+|-+$", "");
        if ("normal".equals(value) || "media".equals(value) || "midia".equals(value)) return "midia";
        if ("leve".equals(value) || "completo".equals(value) || "turbo".equals(value) || "bedrock".equals(value)) return value;
        if ("builder".equals(value) || "build".equals(value) || "apk-builder".equals(value)) return "builder";
        return "midia";
    }

    private String profileLabel(String profile) {
        String value = normalizeProfile(profile);
        if ("leve".equals(value)) return "Leve";
        if ("completo".equals(value)) return "Completo";
        if ("builder".equals(value)) return "Builder";
        if ("turbo".equals(value)) return "Turbo";
        if ("bedrock".equals(value)) return "Bedrock";
        return "Normal";
    }

    private long directorySize(File file) {
        if (file == null || !file.exists()) return 0L;
        if (file.isFile()) return Math.max(0L, file.length());
        long total = 0L;
        File[] children = file.listFiles();
        if (children != null) for (File child : children) total += directorySize(child);
        return total;
    }

    private int countFiles(File file) {
        if (file == null || !file.exists()) return 0;
        if (file.isFile()) return 1;
        int total = 0;
        File[] children = file.listFiles();
        if (children != null) for (File child : children) total += countFiles(child);
        return total;
    }

    private long deleteTreeContents(File file) {
        if (file == null || !file.exists()) return 0L;
        long total = 0L;
        File[] children = file.isDirectory() ? file.listFiles() : null;
        if (children != null) {
            for (File child : children) {
                total += deleteTreeContents(child);
                try { child.delete(); } catch (Throwable ignored) { }
            }
        } else if (file.isFile()) {
            total += Math.max(0L, file.length());
            try { file.delete(); } catch (Throwable ignored) { }
        }
        return total;
    }

    private String humanBytes(long bytes) {
        if (bytes < 1024L) return bytes + " B";
        double kb = bytes / 1024.0;
        if (kb < 1024.0) return String.format(Locale.ROOT, "%.1f KiB", kb);
        return String.format(Locale.ROOT, "%.1f MiB", kb / 1024.0);
    }

    private String readAll(InputStream input) throws Exception {
        if (input == null) return "";
        BufferedReader reader = new BufferedReader(new InputStreamReader(input, StandardCharsets.UTF_8));
        StringBuilder out = new StringBuilder();
        String line;
        while ((line = reader.readLine()) != null) {
            if (out.length() > 0) out.append('\n');
            out.append(line);
        }
        reader.close();
        return out.toString();
    }

    private String shellQuote(String value) {
        return "'" + (value == null ? "" : value.replace("'", "'\\''")) + "'";
    }

    private String limit(String value, int max) {
        String clean = value == null ? "" : value.trim();
        return clean.length() <= max ? clean : clean.substring(0, max);
    }

    private String compact(String value) {
        return limit(value == null ? "" : value.replaceAll("\\s+", " "), 600);
    }

    private String shortThrowable(Throwable error) {
        if (error == null) return "erro desconhecido";
        String message = error.getMessage() == null ? "" : error.getMessage().trim();
        return limit(error.getClass().getSimpleName() + (message.isEmpty() ? "" : ": " + message), 180);
    }

    private String firstNonEmpty(String... values) {
        if (values == null) return "";
        for (String value : values) if (value != null && !value.trim().isEmpty()) return value.trim();
        return "";
    }

    private JSONObject nonNullObject(JSONObject value) {
        return value == null ? new JSONObject() : value;
    }

    private JSONArray nonNullArray(JSONArray value) {
        return value == null ? new JSONArray() : value;
    }

    private static final class DeleteStats {
        long bytes;
        int files;
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
