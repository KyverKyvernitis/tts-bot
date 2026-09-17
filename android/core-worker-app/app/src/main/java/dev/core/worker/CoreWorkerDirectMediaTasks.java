package dev.core.worker;

import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/** MediaTasks operations; no lifecycle or independent runtime state. */
final class CoreWorkerDirectMediaTasks extends CoreWorkerDirectSupport {
    CoreWorkerDirectMediaTasks(Context context, SharedPreferences prefs, NativeTtsManager tts) { super(context, prefs, tts); }

    JSONObject emojiRecolor(JSONObject body) throws Exception {
        JSONArray emojis = body.optJSONArray("emojis");
        if (emojis == null) throw new IllegalArgumentException("emojis precisa ser lista");
        int baseColor = parseRgb(body.optString("color", "#5865F2"));
        JSONArray items = new JSONArray();
        JSONArray errors = new JSONArray();
        for (int i = 0; i < Math.min(4, emojis.length()); i++) {
            JSONObject source = emojis.optJSONObject(i);
            if (source == null) continue;
            String id = source.optString("id", "").trim();
            if (!id.matches("\\d{15,25}")) continue;
            boolean animated = source.optBoolean("animated", false);
            try {
                byte[] input = downloadEmoji(id, animated);
                byte[] output = recolorEmojiPng(input, baseColor);
                JSONObject item = new JSONObject();
                item.put("raw", source.optString("raw", ""));
                item.put("raw_variants", source.optJSONArray("raw_variants") == null ? new JSONArray() : source.optJSONArray("raw_variants"));
                item.put("key", source.optString("key", ""));
                item.put("id", id);
                item.put("name", limit(source.optString("name", "emoji"), 32));
                item.put("animated", animated);
                item.put("format", "png");
                item.put("size", output.length);
                item.put("data_b64", Base64.encodeToString(output, Base64.NO_WRAP));
                items.put(item);
            } catch (Throwable error) {
                errors.put(new JSONObject().put("id", id).put("error", limit(shortThrowable(error), 160)));
            }
        }
        return new JSONObject()
                .put("ok", true)
                .put("items", items)
                .put("count", items.length())
                .put("errors", errors)
                .put("monochrome", body.optBoolean("monochrome", false))
                .put("summary", items.length() + " emoji(s) recolorido(s)");
    }

    byte[] downloadEmoji(String id, boolean animated) throws Exception {
        String extension = animated ? "gif" : "png";
        URL url = new URL("https://cdn.discordapp.com/emojis/" + id + "." + extension + "?size=128&quality=lossless");
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setConnectTimeout(5000);
        connection.setReadTimeout(7000);
        connection.setRequestProperty("User-Agent", "CoreWorkerAPK/" + BuildConfig.VERSION_NAME);
        try {
            int status = connection.getResponseCode();
            if (status < 200 || status >= 300) throw new IllegalStateException("CDN respondeu HTTP " + status);
            return readLimited(connection.getInputStream(), 900 * 1024);
        } finally {
            connection.disconnect();
        }
    }

    byte[] recolorEmojiPng(byte[] input, int baseColor) throws Exception {
        Bitmap decoded = BitmapFactory.decodeByteArray(input, 0, input.length);
        if (decoded == null) throw new IllegalArgumentException("imagem do emoji inválida");
        Bitmap canvasBitmap = null;
        Bitmap output = null;
        try {
            int width = Math.max(1, decoded.getWidth());
            int height = Math.max(1, decoded.getHeight());
            float scale = Math.min(128f / width, 128f / height);
            int scaledWidth = Math.max(1, Math.round(width * scale));
            int scaledHeight = Math.max(1, Math.round(height * scale));
            Bitmap scaled = Bitmap.createScaledBitmap(decoded, scaledWidth, scaledHeight, true);
            canvasBitmap = Bitmap.createBitmap(128, 128, Bitmap.Config.ARGB_8888);
            Canvas canvas = new Canvas(canvasBitmap);
            canvas.drawBitmap(scaled, (128 - scaledWidth) / 2f, (128 - scaledHeight) / 2f, null);
            if (scaled != decoded) scaled.recycle();

            int[] pixels = new int[128 * 128];
            canvasBitmap.getPixels(pixels, 0, 128, 0, 0, 128, 128);
            int baseR = (baseColor >> 16) & 0xff;
            int baseG = (baseColor >> 8) & 0xff;
            int baseB = baseColor & 0xff;
            for (int i = 0; i < pixels.length; i++) {
                int color = pixels[i];
                int alpha = (color >>> 24) & 0xff;
                if (alpha < 8) {
                    pixels[i] = 0;
                    continue;
                }
                int r = (color >> 16) & 0xff;
                int g = (color >> 8) & 0xff;
                int b = color & 0xff;
                double luminance = Math.max(0.0, Math.min(1.0, (r * 0.299 + g * 0.587 + b * 0.114) / 255.0));
                double shade = 0.42 + (luminance * 0.78);
                int outR = clamp((int) Math.round(baseR * shade), 0, 255);
                int outG = clamp((int) Math.round(baseG * shade), 0, 255);
                int outB = clamp((int) Math.round(baseB * shade), 0, 255);
                pixels[i] = (alpha << 24) | (outR << 16) | (outG << 8) | outB;
            }
            output = Bitmap.createBitmap(pixels, 128, 128, Bitmap.Config.ARGB_8888);
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            if (!output.compress(Bitmap.CompressFormat.PNG, 100, bytes)) throw new IllegalStateException("falha ao gerar PNG");
            byte[] result = bytes.toByteArray();
            if (result.length <= 0 || result.length > 512 * 1024) throw new IllegalStateException("emoji gerado fora do limite");
            return result;
        } finally {
            if (output != null && !output.isRecycled()) output.recycle();
            if (canvasBitmap != null && !canvasBitmap.isRecycled()) canvasBitmap.recycle();
            if (!decoded.isRecycled()) decoded.recycle();
        }
    }

    int parseRgb(String raw) {
        String value = raw == null ? "" : raw.trim();
        if (value.startsWith("#")) value = value.substring(1);
        if (!value.matches("[0-9a-fA-F]{6}")) value = "5865F2";
        return Integer.parseInt(value, 16);
    }

    JSONObject binaryCheck(String name) throws Exception {
        File binary = findBinary(name);
        JSONObject out = new JSONObject()
                .put("ok", binary != null)
                .put("available", binary != null)
                .put("command", name)
                .put("runtime", "apk-private");
        if (binary == null) return out.put("error", name + " não encontrado nos binários privados do APK");
        ProcessResult result = runProcess(new String[] {binary.getAbsolutePath(), "-version"}, null, 5000, 256 * 1024);
        String line = firstLine(result.stdout.isEmpty() ? result.stderr : result.stdout);
        return out.put("ok", result.exitCode == 0).put("path", binary.getAbsolutePath()).put("returncode", result.exitCode).put("version_line", line);
    }

    JSONObject ffprobeMedia(JSONObject body) throws Exception {
        File ffprobe = requireBinary("ffprobe");
        byte[] data = decodeBodyData(body);
        String ext = safeExtension(body.optString("input_ext", "bin"), "bin");
        File dir = tempDir("ffprobe");
        File src = new File(dir, "input." + ext);
        writeBytes(src, data);
        try {
            ProcessResult result = runProcess(new String[] {
                    ffprobe.getAbsolutePath(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", src.getAbsolutePath()
            }, dir, clamp(body.optInt("timeout_seconds", 20), 3, 120) * 1000L, 2 * 1024 * 1024);
            if (result.exitCode != 0) throw new IllegalStateException("ffprobe falhou: " + limit(result.stderr, 800));
            JSONObject parsed = new JSONObject(result.stdout.isEmpty() ? "{}" : result.stdout);
            parsed.put("ok", true);
            parsed.put("input_size", data.length);
            return parsed;
        } finally {
            deleteTree(dir);
        }
    }

    JSONObject ffmpegConvert(JSONObject body) throws Exception {
        File ffmpeg = requireBinary("ffmpeg");
        byte[] data = decodeBodyData(body);
        String inputExt = safeExtension(body.optString("input_ext", "bin"), "bin");
        String outputExt = safeExtension(body.optString("output_ext", "ogg"), "ogg");
        JSONArray requested = body.optJSONArray("ffmpeg_args");
        List<String> args = new ArrayList<>();
        if (requested == null || requested.length() == 0) {
            if ("ogg".equals(outputExt) || "opus".equals(outputExt)) {
                Collections.addAll(args, "-vn", "-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1");
                outputExt = "ogg";
            } else if ("mp3".equals(outputExt)) {
                Collections.addAll(args, "-vn", "-c:a", "libmp3lame", "-b:a", "96k");
            } else {
                args.add("-vn");
            }
        } else {
            if (requested.length() > 40) throw new IllegalArgumentException("ffmpeg_args grande demais");
            for (int i = 0; i < requested.length(); i++) {
                String arg = requested.optString(i, "");
                if (!isSafeFfmpegArg(arg)) throw new IllegalArgumentException("argumento ffmpeg bloqueado: " + arg);
                args.add(arg);
            }
        }
        File dir = tempDir("ffmpeg");
        File src = new File(dir, "input." + inputExt);
        File dst = new File(dir, "output." + outputExt);
        writeBytes(src, data);
        try {
            List<String> command = new ArrayList<>();
            Collections.addAll(command, ffmpeg.getAbsolutePath(), "-hide_banner", "-loglevel", "error", "-y", "-i", src.getAbsolutePath());
            command.addAll(args);
            command.add(dst.getAbsolutePath());
            ProcessResult result = runProcess(command.toArray(new String[0]), dir, clamp(body.optInt("timeout_seconds", 45), 3, 180) * 1000L, 1024 * 1024);
            if (result.exitCode != 0 || !dst.isFile()) throw new IllegalStateException("ffmpeg falhou: " + limit(result.stderr, 800));
            byte[] output = readBytes(dst, MAX_OUTPUT_BYTES);
            return new JSONObject()
                    .put("ok", true)
                    .put("output_ext", outputExt)
                    .put("input_size", data.length)
                    .put("size", output.length)
                    .put("sha256", sha256(output))
                    .put("data_b64", Base64.encodeToString(output, Base64.NO_WRAP));
        } finally {
            deleteTree(dir);
        }
    }
}
