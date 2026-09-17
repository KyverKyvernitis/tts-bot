package dev.core.worker;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;
import java.util.zip.ZipOutputStream;

/** DataTasks operations; no lifecycle or independent runtime state. */
final class CoreWorkerDirectDataTasks extends CoreWorkerDirectSupport {
    CoreWorkerDirectDataTasks(Context context, SharedPreferences prefs, NativeTtsManager tts) { super(context, prefs, tts); }

    JSONObject sha256Task(JSONObject body) throws Exception {
        byte[] data = decodeBodyData(body);
        return new JSONObject().put("ok", true).put("sha256", sha256(data)).put("size", data.length);
    }

    JSONObject hashBatch(JSONObject body) throws Exception {
        JSONArray items = body.optJSONArray("items");
        if (items == null) items = body.optJSONArray("files");
        if (items == null) throw new IllegalArgumentException("items vazio");
        JSONArray results = new JSONArray();
        long total = 0L;
        for (int i = 0; i < Math.min(80, items.length()); i++) {
            JSONObject item = items.optJSONObject(i);
            if (item == null) continue;
            byte[] data = decodeBodyData(item);
            total += data.length;
            if (total > MAX_BODY_BYTES) throw new IllegalArgumentException("entrada total grande demais");
            results.put(new JSONObject()
                    .put("name", safeName(item.optString("name", "item-" + (i + 1))))
                    .put("size", data.length)
                    .put("sha256", sha256(data)));
        }
        return new JSONObject()
                .put("ok", true)
                .put("summary", results.length() + " hash(es) calculados")
                .put("files", results)
                .put("items", results)
                .put("count", results.length())
                .put("total_bytes", total)
                .put("total_size", total);
    }

    JSONObject textStats(JSONObject body) throws Exception {
        String text = limitedText(body.optString("text", ""));
        int lines = text.isEmpty() ? 0 : text.split("\\R", -1).length;
        String trimmed = text.trim();
        int words = trimmed.isEmpty() ? 0 : trimmed.split("\\s+").length;
        byte[] bytes = text.getBytes(StandardCharsets.UTF_8);
        return new JSONObject()
                .put("ok", true)
                .put("bytes", bytes.length)
                .put("chars", text.length())
                .put("lines", lines)
                .put("words", words)
                .put("sha256", sha256(bytes));
    }

    JSONObject logExtract(JSONObject body) throws Exception {
        String text = limitedText(body.optString("text", ""));
        String regex = body.optString("pattern", "error|exception|traceback|falhou|failed|fatal|timeout");
        Pattern pattern;
        try { pattern = Pattern.compile(regex, Pattern.CASE_INSENSITIVE); }
        catch (Throwable exc) { throw new IllegalArgumentException("regex inválida"); }
        int maxLines = Math.max(1, Math.min(MAX_LOG_LINES, body.optInt("max_lines", 120)));
        List<String> matches = new ArrayList<>();
        for (String line : text.split("\\R")) if (pattern.matcher(line).find()) matches.add(line);
        int start = Math.max(0, matches.size() - maxLines);
        JSONArray returned = new JSONArray();
        for (int i = start; i < matches.size(); i++) returned.put(matches.get(i));
        return new JSONObject().put("ok", true).put("matches", returned).put("count", matches.size()).put("returned", returned.length());
    }

    JSONObject logSummary(JSONObject body) throws Exception {
        String text = limitedText(body.optString("text", ""));
        Map<String, Pattern> patterns = new HashMap<>();
        patterns.put("critical", Pattern.compile("critical|crítico|fatal", Pattern.CASE_INSENSITIVE));
        patterns.put("error", Pattern.compile("error|erro", Pattern.CASE_INSENSITIVE));
        patterns.put("warning", Pattern.compile("warning|warn|aviso", Pattern.CASE_INSENSITIVE));
        patterns.put("timeout", Pattern.compile("timeout|timed out|tempo esgotado", Pattern.CASE_INSENSITIVE));
        patterns.put("traceback", Pattern.compile("traceback", Pattern.CASE_INSENSITIVE));
        patterns.put("exception", Pattern.compile("exception|exceção", Pattern.CASE_INSENSITIVE));
        patterns.put("failed", Pattern.compile("failed|falhou|failure|falha", Pattern.CASE_INSENSITIVE));
        JSONObject counts = new JSONObject();
        JSONArray recent = new JSONArray();
        List<String> important = new ArrayList<>();
        for (String line : text.split("\\R")) {
            boolean hit = false;
            for (Map.Entry<String, Pattern> entry : patterns.entrySet()) {
                if (entry.getValue().matcher(line).find()) {
                    counts.put(entry.getKey(), counts.optInt(entry.getKey(), 0) + 1);
                    hit = true;
                }
            }
            if (hit) important.add(line.trim());
        }
        int maxRecent = Math.max(1, Math.min(80, body.optInt("max_recent", 12)));
        for (int i = Math.max(0, important.size() - maxRecent); i < important.size(); i++) recent.put(important.get(i));
        return new JSONObject()
                .put("ok", true)
                .put("bytes", text.getBytes(StandardCharsets.UTF_8).length)
                .put("lines", text.isEmpty() ? 0 : text.split("\\R", -1).length)
                .put("counts", counts)
                .put("important_count", important.size())
                .put("recent", recent)
                .put("summary", "logs resumidos pelo APK");
    }

    JSONObject zip(JSONObject body) throws Exception {
        JSONArray files = body.optJSONArray("files");
        if (files == null || files.length() == 0) throw new IllegalArgumentException("files vazio");
        if (files.length() > 80) throw new IllegalArgumentException("arquivos demais");
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        long total = 0L;
        try (ZipOutputStream output = new ZipOutputStream(bytes)) {
            for (int i = 0; i < files.length(); i++) {
                JSONObject item = files.optJSONObject(i);
                if (item == null) throw new IllegalArgumentException("files[" + i + "] inválido");
                byte[] data = decodeBodyData(item);
                total += data.length;
                if (total > MAX_BODY_BYTES) throw new IllegalArgumentException("entrada total grande demais");
                String name = safeZipPath(item.optString("name", "file-" + (i + 1) + ".bin"));
                output.putNextEntry(new ZipEntry(name));
                output.write(data);
                output.closeEntry();
            }
        }
        byte[] result = bytes.toByteArray();
        if (result.length > MAX_OUTPUT_BYTES) throw new IllegalArgumentException("ZIP resultante grande demais");
        return new JSONObject()
                .put("ok", true)
                .put("filename", safeName(body.optString("filename", "core-worker.zip")))
                .put("input_size", total)
                .put("size", result.length)
                .put("sha256", sha256(result))
                .put("data_b64", Base64.encodeToString(result, Base64.NO_WRAP));
    }

    JSONObject zipValidate(JSONObject body) throws Exception {
        byte[] data = decodeBodyData(body);
        if (data.length == 0) throw new IllegalArgumentException("ZIP vazio");
        JSONArray preview = new JSONArray();
        JSONArray warnings = new JSONArray();
        JSONArray errors = new JSONArray();
        int fileCount = 0;
        int dirCount = 0;
        long uncompressed = 0L;
        try (ZipInputStream input = new ZipInputStream(new ByteArrayInputStream(data))) {
            ZipEntry entry;
            int entries = 0;
            while ((entry = input.getNextEntry()) != null) {
                entries++;
                if (entries > Math.min(MAX_ZIP_ENTRIES, Math.max(1, body.optInt("max_entries", 600)))) {
                    errors.put("arquivos demais no ZIP");
                    break;
                }
                String name = entry.getName() == null ? "" : entry.getName();
                if (isSuspiciousZipPath(name)) errors.put("caminho suspeito: " + name);
                if (preview.length() < Math.max(1, Math.min(80, body.optInt("max_preview", 30)))) preview.put(name);
                if (entry.isDirectory()) dirCount++;
                else fileCount++;
                long size = Math.max(0L, entry.getSize());
                uncompressed += size;
                if (size > 64L * 1024L * 1024L) warnings.put("arquivo grande: " + name);
            }
        } catch (Throwable exc) {
            errors.put("ZIP inválido: " + shortThrowable(exc));
        }
        return new JSONObject()
                .put("ok", errors.length() == 0)
                .put("filename", safeName(body.optString("filename", "update.zip")))
                .put("size", data.length)
                .put("sha256", sha256(data))
                .put("file_count", fileCount)
                .put("dir_count", dirCount)
                .put("total_uncompressed", uncompressed)
                .put("preview", preview)
                .put("warnings", warnings)
                .put("errors", errors);
    }
}
