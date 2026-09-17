package dev.core.worker;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;


/** Compatible physical dispatcher; task logic lives in domain classes. */
final class CoreWorkerDirectTaskExecutor extends CoreWorkerDirectSupport {
    private final CoreWorkerDirectDataTasks data;
    private final CoreWorkerDirectMediaTasks media;
    private final CoreWorkerDirectTtsTasks speech;
    private final CoreWorkerDirectSystemTasks system;
    CoreWorkerDirectTaskExecutor(Context context, SharedPreferences prefs, NativeTtsManager tts) {
        super(context, prefs, tts);
        data = new CoreWorkerDirectDataTasks(this.context, this.prefs, tts);
        media = new CoreWorkerDirectMediaTasks(this.context, this.prefs, tts);
        speech = new CoreWorkerDirectTtsTasks(this.context, this.prefs, tts);
        system = new CoreWorkerDirectSystemTasks(this.context, this.prefs, tts);
    }

    static JSONArray directSupportedTasks() {
        JSONArray out = new JSONArray();
        String[] tasks = new String[] {
                "ping", "health", "status", "diagnostic_basic", "worker_self_check",
                "network_probe", "endpoint_probe", "tailscale_status", "vps_assist_probe",
                "emoji_recolor", "sha256", "hash_batch", "text_stats", "log_extract", "log_summary",
                "log_digest", "zip", "zip_validate", "zip_audit", "maintenance_plan", "ffmpeg_check",
                "ffprobe_check", "ffmpeg_convert", "ffprobe_media", "media_probe",
                "audio_convert", "tts_agent_status", "tts_agent_synthesize",
                "tts_android_voices", "tts_atts_voices", "android_tts_voices",
                "tts_synthesize_benchmark", "tts_synthesize_piper", "tts_cache_lookup", "tts_cache_store",
                "worker_logs", "boot_status", "service_status"
        };
        for (String task : tasks) out.put(task);
        return out;
    }

    static boolean supports(String rawTask) {
        String task = normalizeTask(rawTask);
        JSONArray supported = directSupportedTasks();
        for (int i = 0; i < supported.length(); i++) {
            if (task.equals(supported.optString(i, ""))) return true;
        }
        return false;
    }

    JSONObject execute(JSONObject request) throws Exception {
        JSONObject body = request == null ? new JSONObject() : request;
        String task = normalizeTask(body.optString("task", body.optString("type", "")));
        if (!supports(task)) {
            return new JSONObject()
                    .put("ok", false)
                    .put("task", task)
                    .put("error", "task não suportada pelo APK")
                    .put("runtime_mode", "apk-native-direct");
        }

        if ("ping".equals(task) || "health".equals(task) || "status".equals(task)) return health();
        if ("diagnostic_basic".equals(task) || "worker_self_check".equals(task)) return diagnostic();
        if ("network_probe".equals(task) || "tailscale_status".equals(task)) return system.networkProbe(body);
        if ("endpoint_probe".equals(task)) return system.endpointProbe(body);
        if ("vps_assist_probe".equals(task)) return system.vpsAssistProbe(body, diagnostic());
        if ("emoji_recolor".equals(task)) return media.emojiRecolor(body);
        if ("sha256".equals(task)) return data.sha256Task(body);
        if ("hash_batch".equals(task)) return data.hashBatch(body);
        if ("text_stats".equals(task)) return data.textStats(body);
        if ("log_extract".equals(task)) return data.logExtract(body);
        if ("log_summary".equals(task) || "log_digest".equals(task)) return data.logSummary(body);
        if ("zip".equals(task)) return data.zip(body);
        if ("zip_validate".equals(task) || "zip_audit".equals(task)) return data.zipValidate(body);
        if ("maintenance_plan".equals(task)) return system.maintenancePlan(body);
        if ("ffmpeg_check".equals(task)) return media.binaryCheck("ffmpeg");
        if ("ffprobe_check".equals(task)) return media.binaryCheck("ffprobe");
        if ("ffmpeg_convert".equals(task) || "audio_convert".equals(task)) return media.ffmpegConvert(body);
        if ("ffprobe_media".equals(task) || "media_probe".equals(task)) return media.ffprobeMedia(body);
        if ("tts_agent_status".equals(task)) return speech.ttsStatus();
        if ("tts_agent_synthesize".equals(task) || "tts_synthesize_benchmark".equals(task) || "tts_synthesize_piper".equals(task)) return speech.ttsSynthesize(body);
        if ("tts_android_voices".equals(task) || "tts_atts_voices".equals(task) || "android_tts_voices".equals(task)) return speech.ttsVoices(body);
        if ("tts_cache_lookup".equals(task)) return speech.ttsCacheLookup(body);
        if ("tts_cache_store".equals(task)) return speech.ttsCacheStore(body);
        if ("worker_logs".equals(task)) return system.workerLogs(body);
        if ("boot_status".equals(task)) return system.bootStatus();
        if ("service_status".equals(task)) return system.serviceStatus(body);

        return new JSONObject().put("ok", false).put("task", task).put("error", "task sem handler");
    }

    NativeTtsManager.SynthesisResult synthesizeRaw(JSONObject body) throws Exception {
        if (tts == null) throw new IllegalStateException("Android TTS indisponível");
        return tts.synthesizeRaw(body == null ? new JSONObject() : body);
    }

    JSONObject health() throws Exception {
        JSONObject out = new JSONObject();
        out.put("ok", true);
        out.put("status", "ok");
        out.put("runtime_mode", "apk-native-direct");
        out.put("runtime", "core-worker-apk");
        out.put("source", "core-worker-apk-direct-http");
        out.put("version", BuildConfig.VERSION_NAME);
        out.put("version_code", BuildConfig.VERSION_CODE);
        out.put("worker_id", workerId());
        out.put("runtime_kind", "apk");
        out.put("parent_worker_id", CoreWorkerRuntimeIdentity.parentWorkerId(context));
        out.put("physical_worker_id", CoreWorkerRuntimeIdentity.canonicalWorkerId(context));
        out.put("bootstrap_shared_worker_identity", CoreWorkerRuntimeIdentity.sharedBootstrapIdentity(context));
        out.put("profile", prefs.getString("profile", "midia"));
        out.put("uptime_seconds", Math.max(0L, (System.currentTimeMillis() - prefs.getLong("foreground_runtime_started_at", System.currentTimeMillis())) / 1000L));
        out.put("supported_tasks", directSupportedTasks());
        out.put("capabilities", CoreWorkerJobCatalog.capabilities(context));
        out.put("roles", CoreWorkerJobCatalog.roles(context));
        out.put("tts_agent", speech.ttsStatus());
        out.put("music_agent", new JSONObject()
                .put("ok", false)
                .put("available", false)
                .put("state", "vps-local-runtime")
                .put("reason", "música e voz permanecem no processo principal da VPS"));
        out.put("agent", system.agentStatus());
        out.put("apk_self_builder", CoreWorkerApkBuildManager.preflight(context, false));
        return out;
    }

    private JSONObject diagnostic() throws Exception {
        JSONObject out = health();
        out.put("device", system.deviceStatus());
        out.put("network", system.networkStatus());
        out.put("storage", system.storageStatus());
        out.put("summary", CoreWorkerRuntimeIdentity.sharedBootstrapIdentity(context)
                ? "diagnóstico coletado pelo runtime APK; Termux reservado ao bootstrap"
                : "diagnóstico coletado diretamente pelo APK");
        return out;
    }
}
