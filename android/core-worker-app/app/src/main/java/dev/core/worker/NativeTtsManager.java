package dev.core.worker;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.speech.tts.Voice;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.security.MessageDigest;
import java.util.Locale;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

final class NativeTtsManager {
    private static final int DEFAULT_TIMEOUT_MS = 4500;
    private static final int MAX_TEXT_CHARS = 1200;
    private static final int MAX_AUDIO_BYTES = 8 * 1024 * 1024;

    private final Context context;
    private final SharedPreferences prefs;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Object synthLock = new Object();
    private final AtomicInteger synthCounter = new AtomicInteger(0);

    private volatile TextToSpeech tts;
    private volatile boolean initStarted = false;
    private volatile boolean ready = false;
    private volatile int initStatus = Integer.MIN_VALUE;
    private volatile String lastError = "";
    private volatile long lastInitAt = 0L;
    private volatile long lastOkAt = 0L;
    private volatile long lastSynthMs = 0L;
    private volatile int totalSynth = 0;
    private volatile int failedSynth = 0;

    NativeTtsManager(Context context, SharedPreferences prefs) {
        this.context = context.getApplicationContext();
        this.prefs = prefs;
    }

    void warmUp() {
        ensureTtsStarted();
    }

    void shutdown() {
        TextToSpeech current = tts;
        tts = null;
        ready = false;
        if (current != null) {
            try {
                current.stop();
            } catch (Throwable ignored) {
            }
            try {
                current.shutdown();
            } catch (Throwable ignored) {
            }
        }
    }

    JSONObject statusJson() {
        ensureTtsStarted();
        JSONObject out = new JSONObject();
        try {
            TextToSpeech current = tts;
            out.put("ok", ready);
            out.put("available", ready);
            out.put("ready", ready);
            out.put("engine", "android_native");
            out.put("state", ready ? "ready" : "not_ready");
            out.put("init_started", initStarted);
            out.put("init_status", initStatus);
            out.put("last_error", lastError == null ? "" : lastError);
            out.put("last_init_at", lastInitAt);
            out.put("last_ok_at", lastOkAt);
            out.put("last_synth_ms", lastSynthMs);
            out.put("total_synth", totalSynth);
            out.put("failed_synth", failedSynth);
            out.put("default_locale", Locale.getDefault().toLanguageTag());
            if (current != null) {
                try {
                    out.put("default_engine", current.getDefaultEngine() == null ? "" : current.getDefaultEngine());
                } catch (Throwable ignored) {
                    out.put("default_engine", "");
                }
                JSONArray voices = new JSONArray();
                int voiceCount = 0;
                try {
                    Set<Voice> availableVoices = current.getVoices();
                    if (availableVoices != null) {
                        voiceCount = availableVoices.size();
                        int added = 0;
                        for (Voice voice : availableVoices) {
                            if (voice == null || voice.getName() == null) {
                                continue;
                            }
                            if (added >= 12) {
                                break;
                            }
                            voices.put(voice.getName());
                            added++;
                        }
                    }
                } catch (Throwable ignored) {
                }
                out.put("voice_count", voiceCount);
                out.put("voices_preview", voices);
            }
        } catch (Throwable exc) {
            try {
                out.put("ok", false);
                out.put("available", false);
                out.put("ready", false);
                out.put("engine", "android_native");
                out.put("state", "error");
                out.put("last_error", shortText(exc));
            } catch (Throwable ignored) {
            }
        }
        return out;
    }

    JSONObject synthesize(JSONObject request) throws Exception {
        ensureReady(Math.max(1200, request.optInt("init_timeout_ms", 2500)));
        String text = String.valueOf(request.optString("text", "")).trim();
        if (text.isEmpty()) {
            throw new IllegalArgumentException("texto vazio");
        }
        if (text.length() > MAX_TEXT_CHARS) {
            throw new IllegalArgumentException("texto grande demais para Android TTS (" + text.length() + " > " + MAX_TEXT_CHARS + ")");
        }
        int timeoutMs = clamp(request.optInt("timeout_ms", DEFAULT_TIMEOUT_MS), 1000, 15000);
        int maxAudioBytes = clamp(request.optInt("max_audio_bytes", MAX_AUDIO_BYTES), 1024, MAX_AUDIO_BYTES);
        String localeTag = normalizeLocaleTag(firstNonBlank(
                request.optString("locale", ""),
                request.optString("language", ""),
                prefs == null ? "" : prefs.getString("native_tts_locale", ""),
                "pt-BR"
        ));
        String requestedVoice = firstNonBlank(request.optString("voice", ""), prefs == null ? "" : prefs.getString("native_tts_voice", ""), "");
        float rate = normalizeRate(firstNonBlank(request.optString("rate", ""), prefs == null ? "" : prefs.getString("native_tts_rate", ""), "1.0"));
        float pitch = normalizePitch(firstNonBlank(request.optString("pitch", ""), prefs == null ? "" : prefs.getString("native_tts_pitch", ""), "1.0"));
        String utteranceId = "core-worker-tts-" + System.currentTimeMillis() + "-" + synthCounter.incrementAndGet();
        File dir = new File(context.getCacheDir(), "native-tts");
        if (!dir.exists() && !dir.mkdirs()) {
            throw new IllegalStateException("não consegui criar cache nativo TTS");
        }
        File outFile = new File(dir, utteranceId + ".wav");
        long started = System.nanoTime();
        final String[] callbackError = new String[]{""};
        CountDownLatch latch = new CountDownLatch(1);
        synchronized (synthLock) {
            TextToSpeech current = tts;
            if (current == null || !ready) {
                throw new IllegalStateException("Android TTS não está pronto");
            }
            current.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                @Override
                public void onStart(String id) {
                }

                @Override
                public void onDone(String id) {
                    if (utteranceId.equals(id)) {
                        latch.countDown();
                    }
                }

                @Override
                public void onError(String id) {
                    if (utteranceId.equals(id)) {
                        callbackError[0] = "erro Android TTS";
                        latch.countDown();
                    }
                }

                @Override
                public void onError(String id, int errorCode) {
                    if (utteranceId.equals(id)) {
                        callbackError[0] = "erro Android TTS code=" + errorCode;
                        latch.countDown();
                    }
                }
            });
            mainHandler.post(() -> {
                try {
                    Locale locale = Locale.forLanguageTag(localeTag);
                    int languageStatus = current.setLanguage(locale);
                    if (languageStatus == TextToSpeech.LANG_MISSING_DATA || languageStatus == TextToSpeech.LANG_NOT_SUPPORTED) {
                        callbackError[0] = "idioma Android TTS não suportado: " + localeTag;
                        latch.countDown();
                        return;
                    }
                    Voice voice = findVoice(current, requestedVoice);
                    if (voice != null) {
                        current.setVoice(voice);
                    }
                    current.setSpeechRate(rate);
                    current.setPitch(pitch);
                    Bundle params = new Bundle();
                    params.putString(TextToSpeech.Engine.KEY_PARAM_UTTERANCE_ID, utteranceId);
                    int code = current.synthesizeToFile(text, params, outFile, utteranceId);
                    if (code != TextToSpeech.SUCCESS) {
                        callbackError[0] = "synthesizeToFile falhou code=" + code;
                        latch.countDown();
                    }
                } catch (Throwable exc) {
                    callbackError[0] = shortText(exc);
                    latch.countDown();
                }
            });
            boolean finished = latch.await(timeoutMs, TimeUnit.MILLISECONDS);
            long elapsedMs = Math.max(0L, TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started));
            if (!finished) {
                failedSynth++;
                lastError = "timeout Android TTS após " + timeoutMs + "ms";
                throw new IllegalStateException(lastError);
            }
            if (callbackError[0] != null && !callbackError[0].isEmpty()) {
                failedSynth++;
                lastError = callbackError[0];
                throw new IllegalStateException(callbackError[0]);
            }
            if (!outFile.exists() || outFile.length() <= 0L) {
                failedSynth++;
                lastError = "Android TTS não gerou arquivo";
                throw new IllegalStateException(lastError);
            }
            long size = outFile.length();
            if (size > maxAudioBytes) {
                failedSynth++;
                lastError = "áudio Android TTS grande demais: " + size + " bytes";
                throw new IllegalStateException(lastError);
            }
            byte[] data = readAll(outFile, maxAudioBytes);
            String sha256 = sha256(data);
            totalSynth++;
            lastOkAt = System.currentTimeMillis();
            lastSynthMs = elapsedMs;
            lastError = "";
            JSONObject result = new JSONObject();
            result.put("ok", true);
            result.put("engine", "android_native");
            result.put("selected_engine", "android_native");
            result.put("audio_format", "wav");
            result.put("size", data.length);
            result.put("sha256", sha256);
            result.put("worker_synth_ms", elapsedMs);
            result.put("android_synth_ms", elapsedMs);
            result.put("locale", localeTag);
            result.put("voice", requestedVoice == null ? "" : requestedVoice);
            result.put("rate", rate);
            result.put("pitch", pitch);
            result.put("data_b64", Base64.encodeToString(data, Base64.NO_WRAP));
            JSONArray logs = new JSONArray();
            logs.put("android-native locale=" + localeTag + " rate=" + rate + " pitch=" + pitch + " bytes=" + data.length + " synth=" + elapsedMs + "ms");
            result.put("logs", logs);
            return result;
        }
    }

    private void ensureTtsStarted() {
        if (initStarted && tts != null) {
            return;
        }
        initStarted = true;
        lastInitAt = System.currentTimeMillis();
        mainHandler.post(() -> {
            if (tts != null) {
                return;
            }
            try {
                tts = new TextToSpeech(context, status -> {
                    initStatus = status;
                    if (status == TextToSpeech.SUCCESS) {
                        ready = true;
                        lastError = "";
                    } else {
                        ready = false;
                        lastError = "falha ao inicializar Android TTS status=" + status;
                    }
                });
            } catch (Throwable exc) {
                ready = false;
                lastError = shortText(exc);
            }
        });
    }

    private void ensureReady(int timeoutMs) throws Exception {
        ensureTtsStarted();
        long deadline = System.currentTimeMillis() + Math.max(500, timeoutMs);
        while (System.currentTimeMillis() < deadline) {
            if (ready && tts != null) {
                return;
            }
            if (lastError != null && !lastError.isEmpty() && initStatus != Integer.MIN_VALUE && initStatus != TextToSpeech.SUCCESS) {
                break;
            }
            Thread.sleep(40L);
        }
        throw new IllegalStateException(lastError == null || lastError.isEmpty() ? "Android TTS ainda não inicializou" : lastError);
    }

    private Voice findVoice(TextToSpeech current, String requestedVoice) {
        if (requestedVoice == null || requestedVoice.trim().isEmpty()) {
            return null;
        }
        String target = requestedVoice.trim();
        try {
            Set<Voice> voices = current.getVoices();
            if (voices == null) {
                return null;
            }
            for (Voice voice : voices) {
                if (voice != null && target.equals(voice.getName())) {
                    return voice;
                }
            }
        } catch (Throwable ignored) {
        }
        return null;
    }

    private static byte[] readAll(File file, int maxBytes) throws Exception {
        long size = file.length();
        if (size > maxBytes) {
            throw new IllegalStateException("arquivo grande demais: " + size);
        }
        byte[] data = new byte[(int) size];
        try (FileInputStream input = new FileInputStream(file)) {
            int offset = 0;
            while (offset < data.length) {
                int read = input.read(data, offset, data.length - offset);
                if (read < 0) {
                    break;
                }
                offset += read;
            }
            if (offset != data.length) {
                byte[] copy = new byte[offset];
                System.arraycopy(data, 0, copy, 0, offset);
                return copy;
            }
            return data;
        } finally {
            try {
                file.delete();
            } catch (Throwable ignored) {
            }
        }
    }

    private static String sha256(byte[] data) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        byte[] hash = digest.digest(data == null ? new byte[0] : data);
        StringBuilder builder = new StringBuilder(hash.length * 2);
        for (byte b : hash) {
            builder.append(String.format(Locale.US, "%02x", b));
        }
        return builder.toString();
    }

    private static String firstNonBlank(String... values) {
        if (values == null) {
            return "";
        }
        for (String value : values) {
            if (value != null && !value.trim().isEmpty()) {
                return value.trim();
            }
        }
        return "";
    }

    private static String normalizeLocaleTag(String value) {
        String cleaned = firstNonBlank(value, "pt-BR").replace('_', '-');
        if (cleaned.equalsIgnoreCase("pt") || cleaned.equalsIgnoreCase("pt-br")) {
            return "pt-BR";
        }
        return Locale.forLanguageTag(cleaned).toLanguageTag();
    }

    private static float normalizeRate(String raw) {
        String value = firstNonBlank(raw, "1.0").trim();
        try {
            if (value.endsWith("%")) {
                float pct = Float.parseFloat(value.substring(0, value.length() - 1).replace("+", ""));
                return clampFloat(1.0f + (pct / 100.0f), 0.4f, 2.0f);
            }
            return clampFloat(Float.parseFloat(value), 0.4f, 2.0f);
        } catch (Throwable ignored) {
            return 1.0f;
        }
    }

    private static float normalizePitch(String raw) {
        String value = firstNonBlank(raw, "1.0").trim().replace("Hz", "").replace("hz", "");
        try {
            if (value.startsWith("+") || value.startsWith("-")) {
                float delta = Float.parseFloat(value.replace("+", ""));
                return clampFloat(1.0f + (delta / 100.0f), 0.5f, 2.0f);
            }
            return clampFloat(Float.parseFloat(value), 0.5f, 2.0f);
        } catch (Throwable ignored) {
            return 1.0f;
        }
    }

    private static int clamp(int value, int min, int max) {
        return Math.max(min, Math.min(max, value));
    }

    private static float clampFloat(float value, float min, float max) {
        return Math.max(min, Math.min(max, value));
    }

    private static String shortText(Throwable exc) {
        if (exc == null) {
            return "";
        }
        return shortText(exc.getClass().getSimpleName() + ": " + exc.getMessage());
    }

    private static String shortText(String value) {
        String text = value == null ? "" : value.replace('\n', ' ').replace('\r', ' ').trim();
        if (text.length() > 160) {
            return text.substring(0, 160);
        }
        return text;
    }
}
