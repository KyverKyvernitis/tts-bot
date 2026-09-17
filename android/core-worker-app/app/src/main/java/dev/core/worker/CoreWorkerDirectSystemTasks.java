package dev.core.worker;

import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.BatteryManager;
import android.os.Build;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;

/** SystemTasks operations; no lifecycle or independent runtime state. */
final class CoreWorkerDirectSystemTasks extends CoreWorkerDirectSupport {
    CoreWorkerDirectSystemTasks(Context context, SharedPreferences prefs, NativeTtsManager tts) { super(context, prefs, tts); }

    JSONObject agentStatus() throws Exception {
        return new JSONObject()
                .put("enabled", prefs.getBoolean("agent_enabled", false))
                .put("executor_ready", prefs.getBoolean("job_executor_ready", false))
                .put("foreground_active", prefs.getBoolean("foreground_runtime_active", false))
                .put("direct_http_active", prefs.getBoolean("direct_http_active", false))
                .put("direct_http_port", CoreWorkerRuntimeIdentity.effectiveDirectHttpPort(context))
                .put("last_job", prefs.getString("internal_light_jobs_last_summary", ""))
                .put("last_error", prefs.getString("agent_last_error", ""));
    }

    JSONObject deviceStatus() throws Exception {
        JSONObject out = new JSONObject();
        out.put("manufacturer", Build.MANUFACTURER);
        out.put("model", Build.MODEL);
        out.put("device", Build.DEVICE);
        out.put("android", Build.VERSION.RELEASE);
        out.put("sdk", Build.VERSION.SDK_INT);
        JSONArray abis = new JSONArray();
        if (Build.SUPPORTED_ABIS != null) for (String abi : Build.SUPPORTED_ABIS) abis.put(abi);
        out.put("abis", abis);
        try {
            Intent battery = context.registerReceiver(null, new android.content.IntentFilter(Intent.ACTION_BATTERY_CHANGED));
            if (battery != null) {
                int level = battery.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
                int scale = battery.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
                out.put("battery_percent", level >= 0 ? Math.round(level * 100.0 / Math.max(1, scale)) : -1);
                out.put("charging", battery.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0);
            }
        } catch (Throwable ignored) { }
        return out;
    }

    JSONObject storageStatus() throws Exception {
        File files = context.getFilesDir();
        File cache = context.getCacheDir();
        return new JSONObject()
                .put("files_free_bytes", files.getUsableSpace())
                .put("files_total_bytes", files.getTotalSpace())
                .put("cache_bytes", directorySize(cache))
                .put("files_path", files.getAbsolutePath());
    }

    JSONObject networkStatus() throws Exception {
        JSONObject out = new JSONObject();
        try {
            ConnectivityManager manager = (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
            Network active = manager == null ? null : manager.getActiveNetwork();
            NetworkCapabilities caps = manager == null || active == null ? null : manager.getNetworkCapabilities(active);
            out.put("online", caps != null && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET));
            out.put("validated", caps != null && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED));
            out.put("vpn", caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN));
            out.put("wifi", caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI));
            out.put("cellular", caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR));
        } catch (Throwable exc) {
            out.put("online", false);
            out.put("error", shortThrowable(exc));
        }
        return out;
    }

    JSONObject networkProbe(JSONObject body) throws Exception {
        JSONObject out = new JSONObject().put("ok", true).put("network", networkStatus());
        String server = normalizedServerUrl();
        if (!server.isEmpty()) out.put("vps", probeUrl(server + "/health", 5000));
        out.put("summary", "rede verificada pelo APK");
        return out;
    }

    JSONObject vpsAssistProbe(JSONObject body, JSONObject out) throws Exception {
        out.put("task", "vps_assist_probe");
        out.put("vps", normalizedServerUrl());
        out.put("endpoint", prefs.getString("direct_worker_endpoint", ""));
        out.put("direct_http_port", CoreWorkerRuntimeIdentity.effectiveDirectHttpPort(context));
        boolean bootstrap = CoreWorkerRuntimeIdentity.sharedBootstrapIdentity(context);
        out.put("termux_required", bootstrap);
        out.put("summary", bootstrap
                ? "APK pronto para assistência; Termux permanece reservado ao primeiro build"
                : "APK pronto para assistência direta da VPS");
        return out;
    }

    JSONObject endpointProbe(JSONObject body) throws Exception {
        JSONArray targets = body.optJSONArray("targets");
        if (targets == null) targets = body.optJSONArray("urls");
        if (targets == null) {
            targets = new JSONArray();
            String single = body.optString("targets", body.optString("urls", "")).trim();
            if (!single.isEmpty()) targets.put(single);
        }
        if (targets.length() == 0) {
            String server = normalizedServerUrl();
            if (!server.isEmpty()) targets.put(server + "/health");
        }
        int timeoutMs = body.has("timeout_ms")
                ? body.optInt("timeout_ms", 5000)
                : (int) Math.round(body.optDouble("timeout_seconds", 3.0) * 1000.0);
        timeoutMs = Math.max(500, Math.min(8000, timeoutMs));
        int limit = Math.max(1, Math.min(8, body.optInt("max_targets", 4)));
        JSONArray results = new JSONArray();
        for (int i = 0; i < Math.min(limit, targets.length()); i++) {
            String target = targets.optString(i, "").trim();
            if ("auto".equalsIgnoreCase(target)) target = normalizedServerUrl() + "/health";
            JSONObject item;
            if (!target.startsWith("http://") && !target.startsWith("https://")) {
                item = new JSONObject().put("ok", false).put("target", target).put("error", "URL inválida");
            } else {
                item = probeUrl(target, timeoutMs);
            }
            item.put("url", item.optString("target", target));
            if (item.has("elapsed_ms")) item.put("latency_ms", item.optDouble("elapsed_ms", 0.0));
            results.put(item);
        }
        boolean anyOk = false;
        for (int i = 0; i < results.length(); i++) {
            JSONObject item = results.optJSONObject(i);
            anyOk = anyOk || (item != null && item.optBoolean("ok", false));
        }
        return new JSONObject()
                .put("ok", anyOk)
                .put("summary", "endpoints testados pelo APK")
                .put("results", results)
                .put("targets", results)
                .put("count", results.length());
    }

    JSONObject probeUrl(String target, int timeoutMs) throws Exception {
        long started = System.nanoTime();
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(target).openConnection();
            connection.setConnectTimeout(timeoutMs);
            connection.setReadTimeout(timeoutMs);
            connection.setRequestMethod("GET");
            connection.setRequestProperty("Accept", "application/json,*/*;q=0.5");
            int status = connection.getResponseCode();
            return new JSONObject()
                    .put("ok", status >= 200 && status < 400)
                    .put("target", target)
                    .put("status", status)
                    .put("elapsed_ms", elapsedMs(started));
        } catch (Throwable exc) {
            return new JSONObject().put("ok", false).put("target", target).put("error", shortThrowable(exc)).put("elapsed_ms", elapsedMs(started));
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    JSONObject maintenancePlan(JSONObject body) throws Exception {
        JSONArray entries = body.optJSONArray("entries");
        if (entries != null) {
            int maxEntries = Math.max(1, Math.min(5000, body.optInt("max_entries", 1000)));
            double now = body.optDouble("now", System.currentTimeMillis() / 1000.0);
            int scanned = 0;
            long totalSize = 0L;
            long reclaimableTemp = 0L;
            long reclaimableLogs = 0L;
            JSONObject byKind = new JSONObject();
            List<JSONObject> largest = new ArrayList<>();
            List<JSONObject> oldTemp = new ArrayList<>();
            List<JSONObject> oldLogs = new ArrayList<>();
            for (int i = 0; i < Math.min(maxEntries, entries.length()); i++) {
                JSONObject item = entries.optJSONObject(i);
                if (item == null) continue;
                String path = limit(item.optString("path", ""), 260);
                String kind = limit(item.optString("kind", "other"), 40);
                long size = Math.max(0L, item.optLong("size", 0L));
                double mtime = item.has("mtime") ? item.optDouble("mtime", now) : now;
                long ageSeconds = Math.max(0L, (long) (now - mtime));
                JSONObject bucket = byKind.optJSONObject(kind);
                if (bucket == null) bucket = new JSONObject().put("count", 0).put("size", 0L);
                bucket.put("count", bucket.optInt("count", 0) + 1);
                bucket.put("size", bucket.optLong("size", 0L) + size);
                byKind.put(kind, bucket);
                JSONObject record = new JSONObject()
                        .put("path", path)
                        .put("size", size)
                        .put("age_seconds", ageSeconds)
                        .put("kind", kind);
                largest.add(record);
                String pathLc = path.toLowerCase(Locale.ROOT);
                boolean temp = "tmp_audio".equals(kind) || "cache".equals(kind) || "temp".equals(kind)
                        || pathLc.contains("tmp_audio") || pathLc.contains("/cache/");
                boolean log = "log".equals(kind) || pathLc.endsWith(".log") || pathLc.endsWith(".txt");
                if (temp && ageSeconds >= 3600L) {
                    oldTemp.add(record);
                    reclaimableTemp += size;
                }
                if (log && ageSeconds >= 7L * 86400L) {
                    oldLogs.add(record);
                    reclaimableLogs += size;
                }
                scanned++;
                totalSize += size;
            }
            Comparator<JSONObject> bySizeDesc = (a, b) -> Long.compare(b.optLong("size", 0L), a.optLong("size", 0L));
            Comparator<JSONObject> byAgeSizeDesc = (a, b) -> {
                int age = Long.compare(b.optLong("age_seconds", 0L), a.optLong("age_seconds", 0L));
                return age != 0 ? age : bySizeDesc.compare(a, b);
            };
            largest.sort(bySizeDesc);
            oldTemp.sort(byAgeSizeDesc);
            oldLogs.sort(byAgeSizeDesc);
            long reclaimable = reclaimableTemp + reclaimableLogs;
            JSONArray recommendations = new JSONArray();
            if (!oldTemp.isEmpty()) recommendations.put("limpar " + oldTemp.size() + " cache(s)/temporário(s) antigos");
            if (!oldLogs.isEmpty()) recommendations.put("arquivar ou remover " + oldLogs.size() + " log(s) antigos");
            if (recommendations.length() == 0) recommendations.put("nenhuma limpeza automática necessária agora");
            return new JSONObject()
                    .put("ok", true)
                    .put("safe", true)
                    .put("dry_run", true)
                    .put("note", "Plano apenas sugere limpeza; o APK não remove arquivos automaticamente.")
                    .put("scanned", scanned)
                    .put("total_size", totalSize)
                    .put("by_kind", byKind)
                    .put("largest", jsonArray(largest, 30))
                    .put("old_temp_candidates", jsonArray(oldTemp, 80))
                    .put("old_log_candidates", jsonArray(oldLogs, 80))
                    .put("estimated_reclaimable", reclaimable)
                    .put("estimated_reclaimable_temp", reclaimableTemp)
                    .put("estimated_reclaimable_logs", reclaimableLogs)
                    .put("recommendations", recommendations)
                    .put("termux_required", false)
                    .put("summary", scanned + " arquivo(s) analisados; nada foi apagado");
        }

        JSONObject storage = storageStatus();
        long cacheBytes = directorySize(context.getCacheDir(), 4096);
        long filesFree = storage.optLong("files_free_bytes", 0L);
        JSONArray actions = new JSONArray();
        if (cacheBytes > 64L * 1024L * 1024L) actions.put("limpar cache do APK pelo job apk_trim_cache");
        if (filesFree < 512L * 1024L * 1024L) actions.put("liberar pelo menos 512 MiB no armazenamento do aplicativo");
        if (actions.length() == 0) actions.put("nenhuma manutenção obrigatória");
        return new JSONObject()
                .put("ok", true)
                .put("safe", true)
                .put("dry_run", true)
                .put("cache_bytes", cacheBytes)
                .put("storage", storage)
                .put("actions", actions)
                .put("recommendations", actions)
                .put("termux_required", false)
                .put("summary", "plano de manutenção calculado sem alterar arquivos");
    }

    JSONArray jsonArray(List<JSONObject> items, int limit) {
        JSONArray out = new JSONArray();
        for (int i = 0; i < Math.min(Math.max(0, limit), items.size()); i++) out.put(items.get(i));
        return out;
    }

    JSONObject workerLogs(JSONObject body) throws Exception {
        int lines = Math.max(10, Math.min(500, body.optInt("lines", 140)));
        JSONArray history;
        try { history = new JSONArray(prefs.getString("internal_job_history", "[]")); }
        catch (Throwable ignored) { history = new JSONArray(); }
        JSONArray limited = new JSONArray();
        for (int i = 0; i < Math.min(lines, history.length()); i++) limited.put(history.opt(i));
        return new JSONObject()
                .put("ok", true)
                .put("runtime", "apk-native")
                .put("history", limited)
                .put("last_error", prefs.getString("agent_last_error", ""))
                .put("last_summary", prefs.getString("internal_light_jobs_last_summary", ""));
    }

    JSONObject bootStatus() throws Exception {
        return new JSONObject()
                .put("ok", true)
                .put("runtime", "android-boot-receiver")
                .put("agent_enabled", prefs.getBoolean("agent_enabled", false))
                .put("last_boot_event_at", prefs.getLong("native_boot_last_event_at", 0L))
                .put("termux_required", false)
                .put("summary", "boot gerenciado pelo APK");
    }

    JSONObject serviceStatus(JSONObject body) throws Exception {
        String service = normalizeTask(body.optString("service", "core-worker"));
        boolean active;
        if (service.contains("bedrock")) active = prefs.getBoolean("bedrock_runtime_service_active", false);
        else active = prefs.getBoolean("foreground_runtime_active", false);
        return new JSONObject()
                .put("ok", true)
                .put("service", service)
                .put("active", active)
                .put("state", active ? "running" : "stopped")
                .put("runtime", "android-service");
    }
}
