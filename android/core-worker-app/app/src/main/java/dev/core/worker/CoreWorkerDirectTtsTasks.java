package dev.core.worker;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/** TtsTasks operations; no lifecycle or independent runtime state. */
final class CoreWorkerDirectTtsTasks extends CoreWorkerDirectSupport {
    CoreWorkerDirectTtsTasks(Context context, SharedPreferences prefs, NativeTtsManager tts) { super(context, prefs, tts); }

    JSONObject ttsStatus() throws Exception {
        JSONObject status = tts == null ? new JSONObject().put("ok", false).put("available", false).put("error", "Android TTS indisponível") : tts.statusJson();
        status.put("worker_profile", prefs.getString("profile", "midia"));
        status.put("worker_version", BuildConfig.VERSION_NAME);
        status.put("worker_id", workerId());
        status.put("available_engines", new JSONArray().put("android_native"));
        status.put("route", "apk");
        status.put("runtime_mode", "apk-native-direct");
        status.put("synth_ready", status.optBoolean("ready", status.optBoolean("available", false)));
        status.put("preferred_engine", "android_native");
        status.put("active", 0);
        status.put("concurrency_limit", 1);
        return status;
    }

    JSONObject ttsVoices(JSONObject body) throws Exception {
        if (tts == null) throw new IllegalStateException("Android TTS indisponível");
        JSONObject out = tts.voicesJson(body);
        out.put("worker_id", workerId());
        out.put("worker_version", BuildConfig.VERSION_NAME);
        return out;
    }

    JSONObject ttsSynthesize(JSONObject body) throws Exception {
        if (tts == null) throw new IllegalStateException("Android TTS indisponível");
        long started = System.nanoTime();
        JSONObject out = tts.synthesize(body);
        out.put("worker_id", workerId());
        out.put("worker_version", BuildConfig.VERSION_NAME);
        out.put("worker_profile", prefs.getString("profile", "midia"));
        out.put("available_engines", new JSONArray().put("android_native"));
        out.put("total_ms", elapsedMs(started));
        out.put("worker_total_ms", elapsedMs(started));
        return out;
    }

    JSONObject ttsCacheLookup(JSONObject body) throws Exception {
        String key = cacheKey(body);
        File file = new File(ttsCacheDir(), key + ".bin");
        File meta = new File(ttsCacheDir(), key + ".json");
        if (!file.isFile() || !meta.isFile()) return new JSONObject().put("ok", true).put("hit", false).put("cache_hit", false).put("key", key);
        byte[] data = readBytes(file, MAX_OUTPUT_BYTES);
        JSONObject metadata = readJson(meta);
        return new JSONObject()
                .put("ok", true).put("hit", true).put("cache_hit", true).put("key", key)
                .put("audio_format", metadata.optString("audio_format", "wav"))
                .put("size", data.length).put("sha256", sha256(data))
                .put("data_b64", Base64.encodeToString(data, Base64.NO_WRAP));
    }

    JSONObject ttsCacheStore(JSONObject body) throws Exception {
        String key = cacheKey(body);
        byte[] data = decodeBodyData(body);
        if (data.length == 0) throw new IllegalArgumentException("áudio vazio");
        File file = new File(ttsCacheDir(), key + ".bin");
        File meta = new File(ttsCacheDir(), key + ".json");
        writeBytes(file, data);
        writeText(meta, new JSONObject()
                .put("key", key)
                .put("audio_format", safeExtension(body.optString("audio_format", "wav"), "wav"))
                .put("size", data.length)
                .put("sha256", sha256(data))
                .put("updated_at", System.currentTimeMillis()).toString());
        trimCache(ttsCacheDir(), 96, 96L * 1024L * 1024L);
        return new JSONObject().put("ok", true).put("stored", true).put("key", key).put("size", data.length).put("sha256", sha256(data));
    }

    String cacheKey(JSONObject body) throws Exception {
        String explicit = body.optString("key", body.optString("cache_key", "")).trim();
        if (!explicit.isEmpty()) return sha256(explicit.getBytes(StandardCharsets.UTF_8));
        JSONObject material = new JSONObject();
        material.put("text", body.optString("text", ""));
        material.put("voice", body.optString("voice", ""));
        material.put("locale", body.optString("locale", body.optString("language", "")));
        material.put("rate", body.optString("rate", ""));
        material.put("pitch", body.optString("pitch", ""));
        material.put("engine", body.optString("engine", "android_native"));
        return sha256(material.toString().getBytes(StandardCharsets.UTF_8));
    }

    File ttsCacheDir() {
        File dir = new File(context.getCacheDir(), "direct-tts-cache");
        if (!dir.exists()) dir.mkdirs();
        return dir;
    }

    void trimCache(File dir, int maxFiles, long maxBytes) {
        File[] files = dir.listFiles();
        if (files == null) return;
        List<File> list = new ArrayList<>();
        long total = 0L;
        for (File file : files) if (file.isFile()) { list.add(file); total += file.length(); }
        list.sort(Comparator.comparingLong(File::lastModified));
        while (list.size() > maxFiles || total > maxBytes) {
            File file = list.remove(0);
            total -= file.length();
            file.delete();
        }
    }
}
