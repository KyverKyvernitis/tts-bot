package dev.core.worker;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
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
import java.io.FileOutputStream;
import java.io.FileWriter;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

public class CoreWorkerBedrockService extends Service {
    public static final String ACTION_START = "dev.core.worker.action.BEDROCK_RUNTIME_START";
    public static final String ACTION_STOP = "dev.core.worker.action.BEDROCK_RUNTIME_STOP";

    private static final String PREFS = "core_worker_private";
    private static final String CHANNEL_ID = "core_worker_bedrock_runtime";
    private static final int NOTIFICATION_ID = 4108;
    private static final long TICK_MS = 15L * 1000L;
    private static final long LOG_LIMIT_BYTES = 512L * 1024L;
    // Patch 85.6: o runner Bedrock real fica desligado até o rootfs interno ser estável.
    private static final boolean BEDROCK_RUNTIME_ISOLATED = true;
    private static final String BEDROCK_ISOLATION_SUMMARY = "Runtime Bedrock isolado para proteger o app; serviço não iniciado.";

    private final Handler handler = new Handler(Looper.getMainLooper());
    private volatile boolean active = false;
    private volatile boolean runnerActive = false;
    private volatile Process bedrockProcess = null;
    private volatile long commandQueueOffset = 0L;
    private Thread runnerThread;
    private Thread commandThread;
    private PrintWriter processInput;

    private final Runnable tickRunnable = new Runnable() {
        @Override
        public void run() {
            if (!active) return;
            reportHeartbeat("bedrock_service_tick");
            handler.postDelayed(this, TICK_MS);
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        createChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : String.valueOf(intent.getAction());
        if (ACTION_STOP.equals(action)) {
            active = false;
            handler.removeCallbacks(tickRunnable);
            stopRunnerGracefully("service_stop");
            prefs().edit()
                    .putBoolean("bedrock_runtime_service_active", false)
                    .putString("bedrock_runtime_service_state", "serviço Bedrock parado")
                    .putLong("bedrock_runtime_service_last_tick_at", System.currentTimeMillis())
                    .apply();
            stopForeground(true);
            stopSelf();
            return START_NOT_STICKY;
        }

        if (BEDROCK_RUNTIME_ISOLATED) {
            active = false;
            runnerActive = false;
            stopRunnerGracefully("isolated_runtime");
            prefs().edit()
                    .putBoolean("bedrock_runtime_service_active", false)
                    .putString("bedrock_runtime_service_state", BEDROCK_ISOLATION_SUMMARY)
                    .putLong("bedrock_runtime_service_last_tick_at", System.currentTimeMillis())
                    .apply();
            writeRunnerState("isolated", false, false, BEDROCK_ISOLATION_SUMMARY, new JSONArray().put("runtime Bedrock isolado nesta versão"), null);
            appendLog("[service] " + BEDROCK_ISOLATION_SUMMARY);
            try {
                startForeground(NOTIFICATION_ID, buildNotification(BEDROCK_ISOLATION_SUMMARY));
                stopForeground(true);
            } catch (Throwable ignored) {
            }
            stopSelf();
            return START_NOT_STICKY;
        }

        active = true;
        startForeground(NOTIFICATION_ID, buildNotification("Bedrock runtime iniciando"));
        prefs().edit()
                .putBoolean("bedrock_runtime_service_active", true)
                .putString("bedrock_runtime_service_state", "serviço Bedrock ativo · preflight do runner")
                .putLong("bedrock_runtime_service_started_at", System.currentTimeMillis())
                .apply();
        writeRunnerState("starting", true, false, "serviço Bedrock ativo · preflight do runner", null, null);
        startRunnerIfReady();
        reportHeartbeat("bedrock_service_start");
        handler.removeCallbacks(tickRunnable);
        handler.postDelayed(tickRunnable, TICK_MS);
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        active = false;
        handler.removeCallbacks(tickRunnable);
        stopRunnerGracefully("service_destroy");
        prefs().edit()
                .putBoolean("bedrock_runtime_service_active", false)
                .putString("bedrock_runtime_service_state", "serviço Bedrock encerrado")
                .putLong("bedrock_runtime_service_last_tick_at", System.currentTimeMillis())
                .apply();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private synchronized void startRunnerIfReady() {
        if (runnerActive && bedrockProcess != null) {
            try {
                if (bedrockProcess.isAlive()) {
                    writeRunnerState("running", true, true, "Bedrock já está rodando", null, null);
                    return;
                }
            } catch (Throwable ignored) {
            }
        }

        final Preflight preflight = preflight();
        if (!preflight.ready) {
            String summary = "Bedrock bloqueado · " + joinBlockers(preflight.blockers);
            markState(summary);
            appendLog("[preflight] " + summary);
            writeRunnerState("blocked", true, false, summary, preflight.blockers, null);
            updateNotification(summary);
            return;
        }

        runnerThread = new Thread(() -> {
            try {
                List<String> command = new ArrayList<>();
                command.add(preflight.box64.getAbsolutePath());
                command.add(preflight.server.getAbsolutePath());

                ProcessBuilder builder = new ProcessBuilder(command);
                builder.directory(preflight.bedrockDir);
                builder.redirectErrorStream(true);
                builder.environment().put("LD_LIBRARY_PATH", ".");
                builder.environment().put("HOME", preflight.bedrockDir.getAbsolutePath());
                if (preflight.nativeExecutor != null) {
                    builder.environment().put("CORE_WORKER_NATIVE_EXECUTOR", preflight.nativeExecutor.getAbsolutePath());
                }

                commandQueueOffset = commandQueueFile().length();
                appendLog("[runner] iniciando: box64 ./bedrock_server");
                bedrockProcess = builder.start();
                processInput = new PrintWriter(new OutputStreamWriter(bedrockProcess.getOutputStream(), StandardCharsets.UTF_8), true);
                runnerActive = true;
                markState("servidor Bedrock rodando");
                writeRunnerState("running", true, true, "Servidor Bedrock rodando", null, command);
                updateNotification("Servidor Bedrock rodando");
                startCommandLoop();
                readProcessOutput(bedrockProcess.getInputStream());
                int exit = bedrockProcess.waitFor();
                runnerActive = false;
                processInput = null;
                String state = exit == 0 ? "stopped" : "crashed";
                String summary = exit == 0 ? "Servidor Bedrock parado" : "Servidor Bedrock encerrou com código " + exit;
                appendLog("[runner] " + summary);
                markState(summary);
                writeRunnerState(state, active, false, summary, null, command);
                updateNotification(summary);
            } catch (Throwable exc) {
                runnerActive = false;
                String summary = "falha ao iniciar Bedrock: " + shortThrowable(exc);
                appendLog("[runner] " + summary);
                markState(summary);
                writeRunnerState("error", active, false, summary, preflight.blockers, null);
                updateNotification(summary);
            }
        }, "core-worker-bedrock-runner");
        runnerThread.start();
    }

    private void startCommandLoop() {
        commandThread = new Thread(() -> {
            while (active && runnerActive) {
                try {
                    File queue = commandQueueFile();
                    if (queue.exists() && queue.length() > commandQueueOffset) {
                        FileInputStream input = new FileInputStream(queue);
                        long skipped = input.skip(commandQueueOffset);
                        if (skipped < commandQueueOffset) commandQueueOffset = skipped;
                        BufferedReader reader = new BufferedReader(new InputStreamReader(input, StandardCharsets.UTF_8));
                        String line;
                        while ((line = reader.readLine()) != null) {
                            commandQueueOffset += line.getBytes(StandardCharsets.UTF_8).length + 1;
                            String command = extractCommand(line);
                            if (command.isEmpty()) continue;
                            PrintWriter writer = processInput;
                            if (writer != null) {
                                writer.println(command);
                                writer.flush();
                                appendLog("[console>] " + sanitize(command, 160));
                            }
                        }
                        reader.close();
                    }
                    Thread.sleep(500L);
                } catch (Throwable exc) {
                    appendLog("[command-loop] " + shortThrowable(exc));
                    try { Thread.sleep(1200L); } catch (Throwable ignored) {}
                }
            }
        }, "core-worker-bedrock-console");
        commandThread.start();
    }

    private String extractCommand(String jsonLine) {
        try {
            JSONObject obj = new JSONObject(jsonLine == null ? "{}" : jsonLine);
            String command = sanitize(obj.optString("command", ""), 256).trim();
            if (command.indexOf('\n') >= 0 || command.indexOf('\r') >= 0) return "";
            return command;
        } catch (Throwable ignored) {
            return "";
        }
    }

    private void readProcessOutput(InputStream input) {
        try {
            BufferedReader reader = new BufferedReader(new InputStreamReader(input, StandardCharsets.UTF_8));
            String line;
            while ((line = reader.readLine()) != null) {
                appendLog(line);
            }
            reader.close();
        } catch (Throwable exc) {
            appendLog("[stdout] " + shortThrowable(exc));
        }
    }

    private void stopRunnerGracefully(String reason) {
        try {
            PrintWriter writer = processInput;
            if (writer != null) {
                writer.println("stop");
                writer.flush();
                appendLog("[runner] stop enviado · " + reason);
            }
        } catch (Throwable ignored) {
        }
        try {
            Process process = bedrockProcess;
            if (process != null) {
                long until = System.currentTimeMillis() + 4000L;
                while (process.isAlive() && System.currentTimeMillis() < until) {
                    try { Thread.sleep(250L); } catch (Throwable ignored) {}
                }
                if (process.isAlive()) {
                    process.destroy();
                    try { Thread.sleep(1200L); } catch (Throwable ignored) {}
                }
                if (process.isAlive()) {
                    process.destroyForcibly();
                }
            }
        } catch (Throwable ignored) {
        }
        runnerActive = false;
        processInput = null;
        bedrockProcess = null;
        writeRunnerState("stopped", active, false, "Servidor Bedrock parado", null, null);
    }

    private Preflight preflight() {
        Preflight p = new Preflight();
        p.coreLinuxDir = new File(getFilesDir(), "core-linux");
        p.bedrockDir = new File(p.coreLinuxDir, "bedrock");
        p.runtimeDir = new File(p.bedrockDir, "runtime");
        p.server = new File(p.bedrockDir, "bedrock_server");
        p.eula = new File(p.bedrockDir, "eula.txt");
        p.properties = new File(p.bedrockDir, "server.properties");
        File nativeDir = new File(getApplicationInfo() == null || getApplicationInfo().nativeLibraryDir == null ? "" : getApplicationInfo().nativeLibraryDir);
        p.nativeExecutor = firstExisting(
                new File(nativeDir, "libcoreworker_executor.so"),
                new File(nativeDir, "libcoreworker_proot.so"),
                new File(nativeDir, "libcoreworker_busybox.so"),
                new File(nativeDir, "libproot.so"),
                new File(nativeDir, "libbusybox.so")
        );
        p.embeddedBox64 = firstExisting(
                new File(nativeDir, "libcoreworker_box64.so"),
                new File(nativeDir, "libbox64.so")
        );
        File box64A = new File(p.coreLinuxDir, "bin/box64");
        File box64B = new File(p.coreLinuxDir, "box64/box64");
        p.box64 = p.embeddedBox64 != null ? p.embeddedBox64 : (box64A.exists() ? box64A : box64B);
        p.box64InWritableHome = p.box64 != null && isInside(p.box64, new File(getApplicationInfo() == null ? getFilesDir().getParent() : getApplicationInfo().dataDir));
        File rootfsMarker = new File(p.coreLinuxDir, "rootfs/.core-worker-rootfs-ready");
        JSONObject rootfsState = readJsonFile(new File(p.coreLinuxDir, "runtime/rootfs-state.json"));
        p.rootfsReady = rootfsMarker.exists() || rootfsState.optBoolean("rootfsReady", false);
        p.eulaAccepted = eulaAccepted(p.eula);
        p.bedrockDir.mkdirs();
        p.runtimeDir.mkdirs();
        new File(p.bedrockDir, "logs").mkdirs();
        if (!p.properties.exists()) p.blockers.put("server.properties ausente");
        if (!p.eulaAccepted) p.blockers.put("EULA pendente");
        if (!p.server.exists()) p.blockers.put("bedrock_server não instalado");
        if (p.nativeExecutor == null) p.blockers.put("executor interno pendente");
        if (!p.rootfsReady) p.blockers.put("rootfs pendente");
        if (p.box64 == null || !p.box64.exists()) p.blockers.put("Box64 pendente");
        if (p.box64InWritableHome && Build.VERSION.SDK_INT >= 29) p.blockers.put("Box64 em diretório gravável bloqueado pelo Android");
        if (p.box64 != null && p.box64.exists() && !p.box64.canExecute()) p.box64.setExecutable(true, true);
        if (p.server.exists() && !p.server.canExecute()) p.server.setExecutable(true, true);
        p.ready = p.blockers.length() == 0;
        return p;
    }

    private JSONObject readJsonFile(File file) {
        try {
            if (file == null || !file.exists()) return new JSONObject();
            BufferedReader reader = new BufferedReader(new InputStreamReader(new FileInputStream(file), StandardCharsets.UTF_8));
            StringBuilder builder = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null && builder.length() < 65536) {
                builder.append(line).append('\n');
            }
            reader.close();
            return new JSONObject(builder.toString());
        } catch (Throwable ignored) {
            return new JSONObject();
        }
    }

    private boolean eulaAccepted(File eula) {
        try {
            if (eula == null || !eula.exists()) return false;
            BufferedReader reader = new BufferedReader(new InputStreamReader(new FileInputStream(eula), StandardCharsets.UTF_8));
            String line;
            while ((line = reader.readLine()) != null) {
                if (line.toLowerCase().replace(" ", "").contains("eula=true")) {
                    reader.close();
                    return true;
                }
            }
            reader.close();
        } catch (Throwable ignored) {
        }
        return false;
    }

    private File runtimeDir() {
        File dir = new File(new File(getFilesDir(), "core-linux/bedrock"), "runtime");
        dir.mkdirs();
        return dir;
    }

    private File logFile() {
        File logs = new File(new File(getFilesDir(), "core-linux/bedrock"), "logs");
        logs.mkdirs();
        return new File(logs, "bedrock-console.log");
    }

    private File commandQueueFile() {
        return new File(runtimeDir(), "command-queue.jsonl");
    }

    private File stateFile() {
        return new File(runtimeDir(), "runner-state.json");
    }

    private void appendLog(String line) {
        try {
            File log = logFile();
            if (log.exists() && log.length() > LOG_LIMIT_BYTES) {
                File old = new File(log.getParentFile(), "bedrock-console.old.log");
                if (old.exists()) old.delete();
                log.renameTo(old);
            }
            FileWriter writer = new FileWriter(log, true);
            writer.write("[" + System.currentTimeMillis() + "] " + sanitize(line == null ? "" : line, 1000) + "\n");
            writer.close();
        } catch (Throwable ignored) {
        }
    }

    private void writeRunnerState(String state, boolean serviceActive, boolean running, String summary, JSONArray blockers, List<String> command) {
        try {
            JSONObject obj = new JSONObject();
            obj.put("ok", true);
            obj.put("state", state == null ? "unknown" : state);
            obj.put("serviceActive", serviceActive);
            obj.put("running", running);
            obj.put("summary", summary == null ? "Bedrock runner atualizado" : sanitize(summary, 500));
            obj.put("updatedAt", System.currentTimeMillis());
            obj.put("appVersion", BuildConfig.VERSION_NAME);
            obj.put("appVersionCode", BuildConfig.VERSION_CODE);
            obj.put("commandQueue", commandQueueFile().getAbsolutePath());
            obj.put("consoleLog", logFile().getAbsolutePath());
            try {
                Preflight p = preflight();
                obj.put("nativeExecutor", p.nativeExecutor == null ? "" : p.nativeExecutor.getAbsolutePath());
                obj.put("embeddedBox64", p.embeddedBox64 == null ? "" : p.embeddedBox64.getAbsolutePath());
            } catch (Throwable ignored) {
            }
            if (blockers != null) obj.put("blockers", blockers);
            if (command != null) {
                JSONArray arr = new JSONArray();
                for (String item : command) arr.put(item == null ? "" : new File(item).getName());
                obj.put("runnerCommand", arr);
            }
            FileOutputStream out = new FileOutputStream(stateFile());
            out.write(obj.toString(2).getBytes(StandardCharsets.UTF_8));
            out.close();
        } catch (Throwable ignored) {
        }
    }

    private void markState(String state) {
        try {
            prefs().edit()
                    .putBoolean("bedrock_runtime_service_active", active)
                    .putString("bedrock_runtime_service_state", state == null ? "serviço Bedrock ativo" : sanitize(state, 300))
                    .putLong("bedrock_runtime_service_last_tick_at", System.currentTimeMillis())
                    .apply();
        } catch (Throwable ignored) {
        }
    }

    private String joinBlockers(JSONArray blockers) {
        if (blockers == null || blockers.length() == 0) return "sem bloqueios";
        StringBuilder builder = new StringBuilder();
        for (int i = 0; i < blockers.length() && i < 4; i++) {
            if (builder.length() > 0) builder.append("; ");
            builder.append(blockers.optString(i, "pendente"));
        }
        return builder.toString();
    }

    private File firstExisting(File... files) {
        if (files == null) return null;
        for (File file : files) {
            try {
                if (file != null && file.exists()) return file;
            } catch (Throwable ignored) {
            }
        }
        return null;
    }

    private boolean isInside(File file, File parent) {
        try {
            if (file == null || parent == null) return false;
            String f = file.getCanonicalPath();
            String p = parent.getCanonicalPath();
            return f.equals(p) || f.startsWith(p + File.separator);
        } catch (Throwable ignored) {
            return false;
        }
    }

    private void createChannel() {
        try {
            if (Build.VERSION.SDK_INT >= 26) {
                NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                if (manager != null) {
                    NotificationChannel channel = new NotificationChannel(CHANNEL_ID, "Servidor Bedrock Core Worker", NotificationManager.IMPORTANCE_LOW);
                    channel.setDescription("Mantém o servidor Bedrock visível enquanto estiver preparando ou rodando.");
                    manager.createNotificationChannel(channel);
                }
            }
        } catch (Throwable ignored) {
        }
    }

    private void updateNotification(String text) {
        try {
            NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (manager != null) manager.notify(NOTIFICATION_ID, buildNotification(text));
        } catch (Throwable ignored) {
        }
    }

    private Notification buildNotification(String text) {
        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_NEW_TASK);
        int pendingFlags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) pendingFlags |= PendingIntent.FLAG_IMMUTABLE;
        PendingIntent pending = PendingIntent.getActivity(this, NOTIFICATION_ID, open, pendingFlags);
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        builder.setSmallIcon(android.R.drawable.stat_sys_upload_done)
                .setContentTitle("Core Worker Bedrock")
                .setContentText(text == null ? "Bedrock runtime ativo" : sanitize(text, 80))
                .setContentIntent(pending)
                .setOngoing(true)
                .setShowWhen(false);
        return builder.build();
    }

    private void reportHeartbeat(String reason) {
        new Thread(() -> {
            String serverUrl = normalizedServerUrl();
            if (serverUrl.isEmpty()) return;
            try {
                JSONObject payload = new JSONObject();
                payload.put("platform", "android");
                payload.put("source", "core-worker-apk-bedrock-service");
                payload.put("state", "bedrock_runtime_service");
                payload.put("reason", reason == null ? "bedrock_service" : reason);
                payload.put("appVersion", BuildConfig.VERSION_NAME);
                payload.put("appVersionCode", BuildConfig.VERSION_CODE);
                payload.put("versionName", BuildConfig.VERSION_NAME);
                payload.put("versionCode", BuildConfig.VERSION_CODE);
                payload.put("workerId", prefs().getString("worker_id", ""));
                payload.put("installId", installId());
                payload.put("deviceName", prefs().getString("device_name", ""));
                payload.put("runtime_mode", "apk-bedrock-runner-foreground-runtime");
                payload.put("jobsRuntime", "bedrock-foreground-runner");
                JSONObject status = new JSONObject();
                status.put("bedrock_runtime_service_active", active);
                status.put("bedrock_runner_active", runnerActive);
                status.put("bedrock_server_mode", "foreground-runner-preflight");
                status.put("notification_permission", hasNotificationPermission() ? "granted" : "missing");
                status.put("termux_required_now", false);
                payload.put("status", status);
                request("POST", serverUrl + "/core-worker/app/heartbeat", payload);
            } catch (Throwable ignored) {
            }
        }, "core-worker-bedrock-heartbeat").start();
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

    private static String sanitize(String value, int limit) {
        String clean = (value == null ? "" : value)
                .replaceAll("(?i)(token|authorization|bearer|secret|password|passwd|firebase|fcm)[=: ]+[^\\s]+", "$1=[redacted]")
                .replaceAll("([0-9]{1,3}\\.){3}[0-9]{1,3}", "[ip-redacted]")
                .replace('\r', ' ');
        if (clean.length() > limit) clean = clean.substring(0, limit) + "…";
        return clean;
    }

    private static String shortThrowable(Throwable exc) {
        if (exc == null) return "erro desconhecido";
        String msg = exc.getMessage();
        return exc.getClass().getSimpleName() + (msg == null || msg.isEmpty() ? "" : ": " + sanitize(msg, 180));
    }

    private static final class HttpResult {
        final int status;
        final String body;
        HttpResult(int status, String body) {
            this.status = status;
            this.body = body;
        }
    }

    private static final class Preflight {
        File coreLinuxDir;
        File bedrockDir;
        File runtimeDir;
        File server;
        File eula;
        File properties;
        File box64;
        File embeddedBox64;
        File nativeExecutor;
        boolean box64InWritableHome;
        boolean rootfsReady;
        boolean eulaAccepted;
        boolean ready;
        JSONArray blockers = new JSONArray();
    }
}
