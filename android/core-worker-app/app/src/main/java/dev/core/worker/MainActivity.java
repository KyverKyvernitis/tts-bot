package dev.core.worker;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.Dialog;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.res.ColorStateList;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.StateListDrawable;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.InputType;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import androidx.core.content.FileProvider;
import androidx.recyclerview.widget.RecyclerView;
import androidx.viewpager2.widget.ViewPager2;

import com.google.firebase.FirebaseApp;
import com.google.firebase.messaging.FirebaseMessaging;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.OutputStreamWriter;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

public class MainActivity extends Activity {
    private static final String APP_VERSION = BuildConfig.VERSION_NAME;
    private static final String DEFAULT_VPS_URL = BuildConfig.CORE_WORKER_VPS_URL;
    private static final String DEFAULT_VPS_LABEL = BuildConfig.CORE_WORKER_VPS_LABEL;
    private static final String LOCAL_AGENT_STATUS_URL = "http://127.0.0.1:8766/local/status";
    private static final String LOCAL_AGENT_PROFILE_URL = "http://127.0.0.1:8766/local/profile";
    private static final String LOCAL_AGENT_PAIR_URL = "http://127.0.0.1:8766/local/pair";
    private static final String LOCAL_AGENT_HEARTBEAT_URL = "http://127.0.0.1:8766/local/heartbeat";
    private static final String PREFS = "core_worker_private";
    // Patch 52: FCM volta em modo seguro e em camadas.
    // A tela principal nunca depende do Firebase; token/push ficam em fluxo isolado e tolerante a falhas.
    private static final boolean FCM_ENABLED_IN_APK = BuildConfig.CORE_WORKER_FCM_ENABLED;
    private static final long FCM_DISABLED_MS = 30L * 60L * 1000L;
    private static final long FCM_STARTUP_DELAY_MS = 1500L;

    private static final int BG = Color.rgb(5, 9, 20);
    private static final int CARD = Color.rgb(16, 24, 43);
    private static final int CARD_SOFT = Color.rgb(26, 37, 68);
    private static final int CARD_HIGHLIGHT = Color.rgb(32, 48, 86);
    private static final int TEXT = Color.rgb(248, 250, 255);
    private static final int MUTED = Color.rgb(177, 188, 214);
    private static final int ACCENT = Color.rgb(116, 190, 255);
    private static final int BUTTON_BG = Color.rgb(120, 184, 255);
    private static final int BUTTON_TEXT = Color.rgb(4, 10, 24);
    private static final int BUTTON_DISABLED_BG = Color.rgb(48, 58, 82);
    private static final int BUTTON_DISABLED_TEXT = Color.rgb(164, 173, 199);
    private static final int OK = Color.rgb(110, 225, 145);
    private static final int WARN = Color.rgb(255, 205, 113);
    private static final int DANGER = Color.rgb(255, 123, 123);

    private SharedPreferences prefs;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Object embeddedPythonLock = new Object();
    private final AtomicBoolean bedrockProbeRunning = new AtomicBoolean(false);
    private static final long BEDROCK_STEP_TIMEOUT_MS = 4500L;
    private static final long PERMISSION_GATE_STABILIZE_MS = 1200L;
    private static final long BEDROCK_SAFE_TEST_LOG_LIMIT_BYTES = 256L * 1024L;
    private static final long BEDROCK_TEST_MIN_INTERVAL_MS = 2500L;
    // Patch 85.6: Bedrock/rootfs ficam isolados do fluxo visual até o runtime interno ser validado.
    // Nenhum botão da aba Bedrock deve iniciar Python pesado, rootfs real, Termux, Box64 ou serviço Bedrock.
    private static final boolean BEDROCK_RUNTIME_ISOLATED = true;
    private static final String BEDROCK_ISOLATION_SUMMARY = "Runtime Bedrock isolado para proteger o app; diagnóstico leve apenas.";
    private static final int REQUEST_CORE_LINUX_ROOTFS_IMPORT = 8601;
    private LinearLayout connectCard;
    private TextView connectTitleText;
    private TextView connectHintText;
    private TextView pairingStatusText;
    private LinearLayout pairingForm;
    private Button rePairButton;
    private EditText serverUrlInput;
    private TextView serverInfoText;
    private EditText pairCodeInput;
    private EditText deviceNameInput;
    private RadioGroup profileGroup;
    private TextView statusText;
    private LinearLayout updateBanner;
    private TextView updateBannerText;
    private TextView profileHintText;
    private TextView localAgentText;
    private TextView systemChecklistText;
    private TextView technicalAppText;
    private TextView technicalDeviceText;
    private TextView technicalRuntimeText;
    private TextView technicalDiagnosticsText;
    private TextView technicalBedrockText;
    private TextView technicalTermuxText;
    private TextView technicalDependenciesText;
    private TextView updateText;
    private LinearLayout permissionGateCard;
    private LinearLayout mainContent;
    private LinearLayout pageHost;
    private ViewPager2 pagePager;
    private Button corePageButton;
    private Button bedrockPageButton;
    private LinearLayout bottomNavBar;
    private Switch bedrockServerSwitch;
    private TextView bedrockHeroStatusText;
    private TextView bedrockReadinessText;
    private TextView coreHeroHeadlineText;
    private TextView rootfsHeroText;
    private TextView runnerHeroText;
    private TextView rootfsImportProgressText;
    private TextView rootfsAdvancedStatusText;
    private TextView bedrockTerminalText;
    private EditText bedrockCommandInput;
    private Button bedrockTestAllButton;
    private Button bedrockPrepareServerButton;
    private Button bedrockFilesButton;
    private Button bedrockLogsButton;
    private Button bedrockEulaButton;
    private Button bedrockSendCommandButton;
    private Button bedrockExpandTerminalButton;
    private Button bedrockCopyTerminalButton;
    private LinearLayout bedrockAdvancedContent;
    private Button bedrockAdvancedToggleButton;
    private boolean bedrockAdvancedExpanded = false;
    private LinearLayout bedrockTerminalCard;
    private Dialog bedrockFullTerminalDialog;
    private TextView bedrockFullTerminalText;
    private final Runnable bedrockFullTerminalRefreshRunnable = new Runnable() {
        @Override
        public void run() {
            if (activityDestroyed || bedrockFullTerminalDialog == null || !bedrockFullTerminalDialog.isShowing()) {
                return;
            }
            refreshBedrockTerminalViews();
            mainHandler.postDelayed(this, 1000L);
        }
    };
    private volatile String lastTerminalStatusLine = "";
    private volatile long lastTerminalStatusAt = 0L;
    private boolean suppressBedrockSwitchEvents = false;
    private volatile int permissionGateMissingStreak = 0;
    private volatile long permissionGateFirstMissingAt = 0L;
    private final AtomicBoolean permissionGateDelayedRecheckScheduled = new AtomicBoolean(false);
    private TextView permissionStatusText;
    private Button notificationPermissionButton;
    private Button installPermissionButton;
    private Button batteryPermissionButton;
    private Button refreshPermissionsButton;
    private Button prepareButton;
    private Button termuxButton;
    private Button tailscaleButton;
    private Button testButton;
    private Button pairButton;
    private Button saveProfileButton;
    private Button heartbeatButton;
    private Button updateCheckButton;
    private Button updateInstallButton;
    private Button updateCleanupButton;
    private Button clearButton;
    private LinearLayout technicalDetailsContent;
    private Button technicalToggleButton;
    private boolean technicalExpanded = false;
    private TextView profileSummaryText;
    private LinearLayout profileDetailsContent;
    private Button profileToggleButton;
    private boolean profileExpanded = false;
    private volatile boolean fullStartupDone = false;
    private volatile boolean startupFallbackVisible = false;
    private final AtomicBoolean completingStartup = new AtomicBoolean(false);
    private final AtomicBoolean backgroundStartupStarted = new AtomicBoolean(false);
    private final AtomicBoolean internalRuntimeHeartbeatRunning = new AtomicBoolean(false);
    private final AtomicBoolean nativeWorkerHeartbeatRunning = new AtomicBoolean(false);
    private final AtomicBoolean internalLightJobsFetchRunning = new AtomicBoolean(false);
    private static final long ACTIVITY_RESUME_SYNC_DEBOUNCE_MS = 60_000L;
    private volatile long activityResumeSyncLastAt = 0L;
    private volatile boolean activityDestroyed = false;

    private volatile boolean localAgentOnline = false;
    private volatile String localAgentVersion = "";
    private volatile String localAgentProfile = "";
    private volatile String runtimeMode = "apk-native-python-linux-bedrock-installer";
    private volatile String internalRuntimeState = "não preparado";
    private volatile String internalRuntimePath = "";
    private volatile boolean internalRuntimeOnline = false;
    private volatile String internalRuntimeHeartbeatState = "ainda não enviado";
    private volatile String internalRuntimeLastError = "";
    private volatile long internalRuntimeLastHeartbeatAt = 0L;
    private volatile String localAgentWorkerId = "";
    private volatile String localAgentMessage = "ainda não verificado";
    private volatile String localAgentSshdSummary = "";
    private volatile boolean localAgentVpsConfigured = false;
    private volatile boolean localAgentJobsConfigured = false;
    private volatile String vpsState = "não testada";
    private volatile String latestVersionName = "";
    private volatile int latestVersionCode = -1;
    private volatile String latestApkUrl = "";
    private volatile String latestApkSha256 = "";
    private volatile String latestChangelog = "";
    private volatile String latestNotificationId = "";
    private volatile boolean latestUpdateAvailable = false;
    private volatile boolean updateDownloadBusy = false;
    private volatile String fcmState = "não verificado";
    private volatile String fcmTokenPreview = "";
    private volatile long fcmDisabledUntil = 0L;
    private volatile String appStatusLastError = "";
    private volatile long appStatusLastSentAt = 0L;
    private volatile String internalLightJobsState = "aguardando primeira verificação";
    private volatile long internalLightJobsLastCheckAt = 0L;
    private volatile long internalLightJobsLastFetchStartedAt = 0L;
    private volatile int internalLightJobsLastCount = 0;
    private volatile String internalLightJobsLastSummary = "nenhum job executado ainda";
    private volatile int internalLightJobsRunningCount = 0;
    private volatile int internalLightJobsPendingCount = 0;
    private volatile String internalLightJobsQueueSummary = "fila aguardando";
    private volatile int internalLightJobsAutoTotal = 0;
    private volatile int internalLightJobsManualTotal = 0;
    private volatile String internalLightJobsCatalogSummary = "catálogo aguardando";
    private volatile String internalLightJobsLastFetchReason = "";
    private volatile String internalLightJobsLastFetchAppVersion = "";
    private volatile int internalLightJobsLastFetchAppVersionCode = 0;
    private volatile int internalLightJobsLastFetchHttpStatus = 0;
    private volatile int internalLightJobsLastReturnedCount = 0;
    private volatile String internalLightJobsLastFetchError = "";
    private volatile String internalDiagnosticsSummary = "diagnósticos aguardando";
    private volatile String internalStorageSummary = "cache aguardando";
    private volatile String internalBridgeSummary = "ponte aguardando";
    private volatile long internalDiagnosticsLastAt = 0L;
    private volatile boolean nativeWorkerOnline = false;
    private volatile String nativeWorkerState = "aguardando pareamento direto";
    private volatile long nativeWorkerLastHeartbeatAt = 0L;
    private volatile String nativeBootSummary = "boot nativo aguardando";
    private volatile String nativeShellSummary = "shell controlado aguardando";
    private volatile String nativePythonSummary = "python interno aguardando primeiro teste";
    private volatile boolean nativePythonAvailable = false;
    private volatile String nativePythonVersion = "";
    private volatile long nativePythonLastRunAt = 0L;
    private volatile String nativePythonLastScript = "";
    private volatile String nativePythonLastError = "";
    private volatile String coreLinuxSummary = "runtime Linux aguardando diagnóstico";
    private volatile String coreLinuxState = "preparando base";
    private volatile boolean coreLinuxPrepared = false;
    private volatile long coreLinuxLastCheckAt = 0L;
    private volatile String pendingRootfsImportExpectedSha256 = "";
    private volatile boolean rootfsImportBusy = false;
    private volatile String rootfsState = "rootfs aguardando";
    private volatile String rootfsSummary = "Rootfs aguardando validação";
    private volatile String rootfsValidationLevel = "";
    private volatile boolean rootfsDistributionReady = false;
    private volatile String rootfsLastSha256 = "";
    private volatile long rootfsLastUpdatedAt = 0L;
    private volatile String rootfsLastStatsSummary = "";
    private volatile String bedrockSummary = "Bedrock aguardando diagnóstico";
    private volatile String bedrockState = "não configurado";
    private volatile boolean bedrockReady = false;
    private volatile long bedrockLastCheckAt = 0L;
    private volatile String foregroundRuntimeSummary = "serviço persistente aguardando";
    private volatile boolean foregroundRuntimeActive = false;
    private volatile long foregroundRuntimeLastTickAt = 0L;
    private volatile String linuxInstallStrategySummary = "estratégia Linux aguardando confirmação";
    private volatile String bedrockInstallerSummary = "instalador Bedrock aguardando";
    private volatile String bedrockRuntimeSummary = "runner Bedrock aguardando";
    private volatile String bedrockRuntimeState = "stopped";
    private volatile boolean bedrockRuntimeServiceActive = false;
    private volatile String bedrockInstallerState = "aguardando";
    private volatile String bedrockInstallerNextAction = "validar requisitos";
    private volatile long bedrockLastManualTestAt = 0L;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        activityDestroyed = false;
        startupLog("onCreate:start v" + APP_VERSION + " code=" + BuildConfig.VERSION_CODE);
        try {
            prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
            renderStartupFallbackUi(
                    "Core Worker iniciando",
                    "Abrindo tela segura antes de carregar runtime, rootfs, Python ou Bedrock.",
                    false
            );
            mainHandler.postDelayed(this::completeStartupAfterFirstDraw, 80L);
        } catch (Throwable exc) {
            startupLog("onCreate:fatal-before-safe-ui " + fallbackThrowable(exc));
            renderStartupFallbackUi("Core Worker abriu em modo seguro", fallbackThrowable(exc), true);
        }
    }

    private void completeStartupAfterFirstDraw() {
        if (fullStartupDone) {
            startupLog("completeStartup:already-ready");
            return;
        }
        if (!completingStartup.compareAndSet(false, true)) {
            startupLog("completeStartup:already-running");
            return;
        }
        startupLog("completeStartup:start");
        try {
            migrateFcmSafetyStateForPatch52();
            buildUi();
            loadInputs();
            fullStartupDone = true;
            startupFallbackVisible = false;
            startupLog("completeStartup:ui-ready");
            refreshLocalStatus("Interface pronta. Runtime, rootfs e sincronizações vão carregar em segundo plano.");
            scheduleActivityCreateStartupTasks();
        } catch (Throwable exc) {
            fullStartupDone = false;
            startupFallbackVisible = true;
            backgroundStartupStarted.set(false);
            String detail = fallbackThrowable(exc);
            appStatusLastError = detail;
            startupLog("completeStartup:fallback " + detail);
            renderStartupFallbackUi("Core Worker abriu em modo seguro", detail, true);
        } finally {
            completingStartup.set(false);
        }
    }

    private void scheduleActivityCreateStartupTasks() {
        if (!backgroundStartupStarted.compareAndSet(false, true)) {
            startupLog("startupTasks:already-started");
            return;
        }
        Thread worker = new Thread(this::runActivityCreateStartupTasksInBackground, "core-worker-startup-bg");
        worker.setDaemon(true);
        worker.start();
    }

    private void runActivityCreateStartupTasksInBackground() {
        startupLog("startupTasks:background-start");
        safeStartupTask("cleanupUpdateArtifacts", () -> cleanupUpdateArtifacts(false, "app_start"));
        safeStartupTask("prepareInternalRuntimePreview", this::prepareInternalRuntimePreview);
        safeStartupTask("prepareNativeRuntimeState", this::prepareNativeRuntimeState);
        safeStartupTask("prepareCoreLinuxRuntimeSkeleton", this::prepareCoreLinuxRuntimeStateWithoutRecursiveProbe);
        safeStartupTask("readForegroundRuntimeState", this::readForegroundRuntimeState);
        safeStartupTask("scheduleUpdateJob", () -> CoreWorkerUpdateJobService.schedule(this, "activity_create"));
        safeStartupTask("reportAppOpened", () -> reportAppState("app_opened", "APK aberto; versão instalada " + APP_VERSION + " (" + BuildConfig.VERSION_CODE + ")"));
        safeStartupTask("reportRuntimeReady", () -> reportAppState("runtime_internal_ready", "runtime interno preparado em modo híbrido; heartbeat/status direto ativo"));
        safeStartupTask("sendInternalRuntimeHeartbeat", () -> sendInternalRuntimeHeartbeat(false, "app_opened"));
        safeStartupTask("sendNativeWorkerHeartbeat", () -> sendNativeWorkerHeartbeat(false, "app_opened"));
        safeStartupTask("fetchAndRunLightJobs", () -> fetchAndRunLightJobs(false, "app_opened"));
        safeStartupTask("updatePermissionGate", this::updatePermissionGate);
        show("Pronto. O app verifica pareamento e atualizações sem travar a tela.");
        safeStartupTask("checkLocalAgent", () -> checkLocalAgent(false));
        safeStartupTask("autoVerifySavedPairing", this::autoVerifySavedPairing);
        safeStartupTask("autoCheckForUpdate", this::autoCheckForUpdate);
        safeStartupTask("scheduleFcmTokenRegistration", () -> scheduleFcmTokenRegistration("activity_create"));
        startupLog("startupTasks:background-finished");
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (!fullStartupDone) {
            startupLog("onResume:waiting-full-startup fallback=" + startupFallbackVisible);
            return;
        }
        safeStartupTask("resume:updatePermissionGate", this::updatePermissionGate);
        if (!shouldRunActivityResumeSync()) {
            startupLog("onResume:sync-debounced");
            return;
        }
        safeStartupTask("resume:scheduleUpdateJob", () -> CoreWorkerUpdateJobService.schedule(this, "activity_resume"));
        safeStartupTask("resume:autoVerifySavedPairing", this::autoVerifySavedPairing);
        safeStartupTask("resume:autoCheckForUpdate", this::autoCheckForUpdate);
        safeStartupTask("resume:cleanupUpdateArtifacts", () -> cleanupUpdateArtifacts(false, "activity_resume"));
        safeStartupTask("resume:sendInternalRuntimeHeartbeat", () -> sendInternalRuntimeHeartbeat(false, "activity_resume"));
        safeStartupTask("resume:sendNativeWorkerHeartbeat", () -> sendNativeWorkerHeartbeat(false, "activity_resume"));
        safeStartupTask("resume:readBedrockServiceState", this::readBedrockServiceState);
        safeStartupTask("resume:fetchAndRunLightJobs", () -> fetchAndRunLightJobs(false, "activity_resume"));
        scheduleFcmTokenRegistration("activity_resume");
    }

    private boolean shouldRunActivityResumeSync() {
        long now = System.currentTimeMillis();
        long last = activityResumeSyncLastAt;
        if (last > 0L && now - last < ACTIVITY_RESUME_SYNC_DEBOUNCE_MS) {
            return false;
        }
        activityResumeSyncLastAt = now;
        return true;
    }

    @Override
    protected void onDestroy() {
        activityDestroyed = true;
        bedrockProbeRunning.set(false);
        permissionGateDelayedRecheckScheduled.set(false);
        mainHandler.removeCallbacks(bedrockFullTerminalRefreshRunnable);
        try {
            if (bedrockFullTerminalDialog != null && bedrockFullTerminalDialog.isShowing()) {
                bedrockFullTerminalDialog.dismiss();
            }
        } catch (Throwable ignored) {
        }
        bedrockFullTerminalDialog = null;
        bedrockFullTerminalText = null;
        super.onDestroy();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        safeStartupTask("permission:updatePermissionGate", this::updatePermissionGate);
        refreshLocalStatus(requestCode == 4103 ? "Permissão de notificação atualizada. Verifique as demais permissões necessárias." : null);
        if (requestCode == 4103) {
            safeStartupTask("permission:scheduleUpdateJob", () -> CoreWorkerUpdateJobService.schedule(this, "notification_permission_result"));
            scheduleFcmTokenRegistration("notification_permission_result");
            safeStartupTask("permission:autoCheckForUpdate", this::autoCheckForUpdate);
        }
    }

    private interface SafeStartupRunnable {
        void run() throws Exception;
    }

    private void safeStartupTask(SafeStartupRunnable runnable) {
        safeStartupTask("startup", runnable);
    }

    private void safeStartupTask(String label, SafeStartupRunnable runnable) {
        String cleanLabel = label == null || label.trim().isEmpty() ? "startup" : label.trim();
        long started = System.currentTimeMillis();
        try {
            runnable.run();
            long duration = System.currentTimeMillis() - started;
            if (duration >= 250L || cleanLabel.startsWith("resume:") || cleanLabel.contains("CoreLinux") || cleanLabel.contains("Heartbeat") || cleanLabel.contains("Jobs")) {
                startupLog("safeStartupTask:ok " + cleanLabel + " " + duration + "ms");
            }
        } catch (Throwable exc) {
            appStatusLastError = fallbackThrowable(exc);
            startupLog("safeStartupTask:fail " + cleanLabel + " " + appStatusLastError);
        }
    }

    private void startupLog(String message) {
        try {
            File file = startupLogFile();
            File dir = file.getParentFile();
            if (dir != null && !dir.exists()) dir.mkdirs();
            String line = System.currentTimeMillis() + " " + String.valueOf(message == null ? "" : message) + "\n";
            FileOutputStream out = new FileOutputStream(file, true);
            out.write(line.getBytes(StandardCharsets.UTF_8));
            out.close();
        } catch (Throwable ignored) {
        }
    }

    private File startupLogFile() {
        return new File(new File(getFilesDir(), "core-linux/logs"), "app-startup.log");
    }

    private String readStartupLogTail() {
        File file = startupLogFile();
        if (!file.exists()) {
            return "Log de inicialização ainda não foi criado.";
        }
        StringBuilder builder = new StringBuilder();
        try {
            BufferedReader reader = new BufferedReader(new InputStreamReader(new FileInputStream(file), StandardCharsets.UTF_8));
            String line;
            while ((line = reader.readLine()) != null) {
                builder.append(line).append('\n');
                if (builder.length() > 12000) {
                    builder.delete(0, Math.min(builder.length(), 4000));
                }
            }
            reader.close();
        } catch (Throwable exc) {
            return "Não consegui ler o log interno: " + fallbackThrowable(exc);
        }
        String value = builder.toString().trim();
        if (value.isEmpty()) {
            return "Log de inicialização vazio.";
        }
        return value.length() > 9000 ? value.substring(value.length() - 9000) : value;
    }

    private void showStartupLogDialog() {
        runOnUiThread(() -> {
            String logText = readStartupLogTail();
            TextView text = new TextView(this);
            text.setText(logText);
            text.setTextIsSelectable(true);
            text.setTextColor(TEXT);
            text.setTextSize(12);
            text.setTypeface(Typeface.MONOSPACE);
            text.setPadding(dp(12), dp(12), dp(12), dp(12));
            text.setBackgroundColor(BG);
            ScrollView scroll = new ScrollView(this);
            scroll.addView(text, new ScrollView.LayoutParams(
                    ScrollView.LayoutParams.MATCH_PARENT,
                    ScrollView.LayoutParams.WRAP_CONTENT
            ));
            new AlertDialog.Builder(this)
                    .setTitle("Log do Core Worker")
                    .setView(scroll)
                    .setPositiveButton("Copiar", (dialog, which) -> copyToClipboard("core-worker-startup.log", logText))
                    .setNegativeButton("Fechar", null)
                    .show();
        });
    }

    private void copyToClipboard(String label, String value) {
        try {
            ClipboardManager manager = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
            if (manager != null) {
                manager.setPrimaryClip(ClipData.newPlainText(label == null ? "Core Worker" : label, value == null ? "" : value));
                toast("Log copiado.");
            }
        } catch (Throwable exc) {
            toast("Não consegui copiar o log: " + fallbackThrowable(exc));
        }
    }

    private String fallbackThrowable(Throwable err) {
        if (err == null) return "erro desconhecido";
        String msg = String.valueOf(err.getMessage() == null ? "" : err.getMessage()).trim();
        String text = err.getClass().getSimpleName() + (msg.isEmpty() ? "" : ": " + msg);
        return text.length() > 220 ? text.substring(0, 220) : text;
    }

    private void renderStartupFallbackUi(String titleText, String details, boolean error) {
        try {
            startupFallbackVisible = true;
            LinearLayout root = new LinearLayout(this);
            root.setOrientation(LinearLayout.VERTICAL);
            root.setGravity(Gravity.CENTER_HORIZONTAL);
            root.setPadding(dp(18), dp(24), dp(18), dp(18));
            root.setBackgroundColor(BG);

            TextView title = new TextView(this);
            title.setText(titleText == null || titleText.trim().isEmpty() ? "Core Worker" : titleText);
            title.setTextColor(TEXT);
            title.setTextSize(26);
            title.setTypeface(null, Typeface.BOLD);
            title.setGravity(Gravity.CENTER);
            root.addView(title, new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT
            ));

            TextView body = new TextView(this);
            StringBuilder text = new StringBuilder();
            text.append(error ? "Modo seguro ativo. A tela principal foi protegida para não ficar branca.\n\n" : "Carregando tela principal com segurança.\n\n");
            text.append("Versão: ").append(APP_VERSION).append(" (").append(BuildConfig.VERSION_CODE).append(")\n");
            text.append("Runtime/rootfs: carregamento adiado até a interface estar pronta.\n");
            if (details != null && !details.trim().isEmpty()) {
                text.append("\nDetalhe: ").append(details.trim());
            }
            body.setText(text.toString());
            body.setTextColor(error ? WARN : MUTED);
            body.setTextSize(14);
            body.setGravity(Gravity.CENTER);
            body.setPadding(0, dp(12), 0, dp(18));
            root.addView(body, new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT
            ));

            Button retry = new Button(this);
            retry.setText(error ? "Tentar abrir novamente" : "Abrir agora");
            retry.setAllCaps(false);
            retry.setOnClickListener(v -> completeStartupAfterFirstDraw());
            root.addView(retry, new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT
            ));

            TextView hint = new TextView(this);
            hint.setText("Log local: files/core-linux/logs/app-startup.log");
            hint.setTextColor(MUTED);
            hint.setTextSize(12);
            hint.setGravity(Gravity.CENTER);
            hint.setPadding(0, dp(14), 0, 0);
            root.addView(hint, new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT
            ));

            Button logs = new Button(this);
            logs.setText("Ver log de inicialização");
            logs.setAllCaps(false);
            logs.setOnClickListener(v -> showStartupLogDialog());
            LinearLayout.LayoutParams logsParams = new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT
            );
            logsParams.setMargins(0, dp(10), 0, 0);
            root.addView(logs, logsParams);

            setContentView(root);
        } catch (Throwable ignored) {
        }
    }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(16), dp(18), dp(16), dp(12));
        root.setBackgroundColor(BG);
        root.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.MATCH_PARENT
        ));

        TextView title = new TextView(this);
        title.setText("Core Worker");
        title.setTextColor(TEXT);
        title.setTextSize(31);
        title.setGravity(Gravity.START);
        title.setTypeface(null, 1);
        root.addView(title);

        TextView subtitle = new TextView(this);
        subtitle.setText("Painel limpo do worker local. Detalhes ficam em Avançado.");
        subtitle.setTextColor(MUTED);
        subtitle.setTextSize(14);
        subtitle.setPadding(0, dp(5), 0, dp(12));
        root.addView(subtitle);

        buildPermissionGate(root);

        pageHost = new LinearLayout(this);
        pageHost.setOrientation(LinearLayout.VERTICAL);
        root.addView(pageHost, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                0,
                1f
        ));

        ScrollView coreScroll = pageScroll();
        LinearLayout coreContent = pageContent();
        coreScroll.addView(coreContent, new ScrollView.LayoutParams(
                ScrollView.LayoutParams.MATCH_PARENT,
                ScrollView.LayoutParams.WRAP_CONTENT
        ));
        ScrollView bedrockScroll = pageScroll();
        LinearLayout bedrockContent = pageContent();
        bedrockScroll.addView(bedrockContent, new ScrollView.LayoutParams(
                ScrollView.LayoutParams.MATCH_PARENT,
                ScrollView.LayoutParams.WRAP_CONTENT
        ));

        pagePager = new ViewPager2(this);
        pagePager.setOffscreenPageLimit(2);
        pagePager.setAdapter(new StaticPageAdapter(new View[]{coreScroll, bedrockScroll}));
        pagePager.registerOnPageChangeCallback(new ViewPager2.OnPageChangeCallback() {
            @Override
            public void onPageSelected(int position) {
                updatePageTabs(position);
                animateContentIn(position == 0 ? coreScroll : bedrockScroll);
            }
        });
        pageHost.addView(pagePager, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                0,
                1f
        ));

        statusText = new TextView(this);
        statusText.setTextColor(TEXT);
        statusText.setTextSize(12);
        statusText.setSingleLine(false);
        statusText.setPadding(dp(12), dp(10), dp(12), dp(10));
        statusText.setBackground(cardBackground(CARD));
        LinearLayout.LayoutParams statusParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        statusParams.setMargins(0, dp(8), 0, dp(8));
        root.addView(statusText, statusParams);

        bottomNavBar = new LinearLayout(this);
        bottomNavBar.setOrientation(LinearLayout.HORIZONTAL);
        bottomNavBar.setGravity(Gravity.CENTER);
        bottomNavBar.setPadding(dp(4), dp(6), dp(4), dp(4));
        bottomNavBar.setBackground(cardBackground(Color.rgb(10, 16, 31)));
        root.addView(bottomNavBar, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        ));
        corePageButton = bottomNavButton("⌂\nCore");
        bedrockPageButton = bottomNavButton("▣\nBedrock");
        bottomNavBar.addView(corePageButton, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        LinearLayout.LayoutParams bedrockNavParams = new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        bedrockNavParams.setMargins(dp(8), 0, 0, 0);
        bottomNavBar.addView(bedrockPageButton, bedrockNavParams);
        corePageButton.setOnClickListener(v -> pagePager.setCurrentItem(0, true));
        bedrockPageButton.setOnClickListener(v -> pagePager.setCurrentItem(1, true));
        updatePageTabs(0);

        mainContent = coreContent;

        updateBanner = card();
        updateBanner.setVisibility(View.GONE);
        updateBanner.setBackground(cardBackground(Color.rgb(38, 55, 93)));
        updateBannerText = smallText("Atualização disponível.");
        updateBannerText.setTextColor(TEXT);
        updateBanner.addView(updateBannerText);
        updateInstallButton = button("Atualizar");
        updateInstallButton.setOnClickListener(v -> downloadAndInstallUpdate());
        updateBanner.addView(updateInstallButton);
        LinearLayout.LayoutParams updateBannerParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        updateBannerParams.setMargins(0, 0, 0, dp(14));
        mainContent.addView(updateBanner, updateBannerParams);

        LinearLayout prepareCard = card();
        mainContent.addView(prepareCard);
        prepareCard.addView(sectionTitle("Visão geral"));
        prepareCard.addView(smallText("Estado essencial do worker. O resto fica em Avançado."));

        localAgentText = smallText("Este celular ainda não foi verificado.");
        localAgentText.setTextColor(TEXT);
        localAgentText.setBackground(cardBackground(CARD_HIGHLIGHT));
        localAgentText.setPadding(dp(12), dp(10), dp(12), dp(10));
        prepareCard.addView(localAgentText);

        coreHeroHeadlineText = largeStatusText("Pronto para trabalhar");
        prepareCard.addView(coreHeroHeadlineText);

        rootfsHeroText = smallText("Rootfs: aguardando status");
        rootfsHeroText.setTextColor(TEXT);
        rootfsHeroText.setBackground(cardBackground(CARD_SOFT));
        rootfsHeroText.setPadding(dp(12), dp(10), dp(12), dp(10));
        prepareCard.addView(rootfsHeroText);

        runnerHeroText = smallText("Runner: bloqueado com segurança");
        runnerHeroText.setTextColor(MUTED);
        runnerHeroText.setPadding(0, dp(8), 0, dp(4));
        prepareCard.addView(runnerHeroText);

        prepareButton = primaryButton("Verificar status");
        prepareButton.setOnClickListener(v -> checkLocalAgent(true));
        prepareCard.addView(prepareButton);

        connectCard = cardWithTopMargin(mainContent);
        connectTitleText = sectionTitle("Conexão");
        connectCard.addView(connectTitleText);
        connectHintText = smallText("Conexão salva com a VPS principal.");
        connectCard.addView(connectHintText);

        pairingStatusText = smallText("");
        pairingStatusText.setTextColor(TEXT);
        pairingStatusText.setBackground(cardBackground(CARD_SOFT));
        pairingStatusText.setPadding(dp(10), dp(10), dp(10), dp(10));
        connectCard.addView(pairingStatusText);

        rePairButton = secondaryButton("Trocar pareamento");
        rePairButton.setOnClickListener(v -> showPairingForm(true, "Modo de pareamento aberto. Gere um código novo no Discord se quiser trocar o vínculo deste celular."));
        connectCard.addView(rePairButton);

        pairingForm = new LinearLayout(this);
        pairingForm.setOrientation(LinearLayout.VERTICAL);
        connectCard.addView(pairingForm);

        serverUrlInput = input("", DEFAULT_VPS_URL);
        serverUrlInput.setVisibility(View.GONE);
        pairingForm.addView(label("VPS do projeto"));
        serverInfoText = smallText("VPS atual: " + serverDisplayLabel());
        serverInfoText.setTextColor(TEXT);
        serverInfoText.setBackground(cardBackground(CARD_SOFT));
        serverInfoText.setPadding(dp(10), dp(10), dp(10), dp(10));
        pairingForm.addView(serverInfoText);

        pairCodeInput = input("Código CORE-XXXX", "CORE-XXXXXXXX");
        pairCodeInput.setAllCaps(true);
        pairingForm.addView(label("Código de pareamento"));
        pairingForm.addView(pairCodeInput);

        deviceNameInput = input("Nome do celular", defaultDeviceName());
        pairingForm.addView(label("Nome deste celular"));
        pairingForm.addView(deviceNameInput);

        testButton = secondaryButton("Testar conexão");
        testButton.setOnClickListener(v -> testServer());
        pairingForm.addView(testButton);

        pairButton = primaryButton("Conectar este celular");
        pairButton.setOnClickListener(v -> pairWorker());
        pairingForm.addView(pairButton);

        LinearLayout profileCard = cardWithTopMargin(mainContent);
        profileCard.addView(sectionTitle("Perfil"));
        profileCard.addView(smallText("Quanto este celular pode ajudar quando estiver livre."));

        profileSummaryText = smallText("");
        profileSummaryText.setTextColor(TEXT);
        profileSummaryText.setBackground(cardBackground(CARD_SOFT));
        profileSummaryText.setPadding(dp(10), dp(10), dp(10), dp(10));
        profileCard.addView(profileSummaryText);

        profileToggleButton = secondaryButton("Alterar perfil");
        profileToggleButton.setOnClickListener(v -> toggleProfileDetails());
        profileCard.addView(profileToggleButton);

        profileDetailsContent = new LinearLayout(this);
        profileDetailsContent.setOrientation(LinearLayout.VERTICAL);
        profileDetailsContent.setVisibility(View.GONE);
        profileCard.addView(profileDetailsContent);

        profileGroup = new RadioGroup(this);
        profileGroup.setOrientation(RadioGroup.VERTICAL);
        profileGroup.setPadding(0, dp(6), 0, dp(6));
        addProfileRadio("leve", "Leve · economia de bateria");
        addProfileRadio("midia", "Normal · recomendado");
        addProfileRadio("completo", "Completo · tarefas extras");
        addProfileRadio("builder", "Builder · compilar APK");
        addProfileRadio("turbo", "Turbo · máximo desempenho");
        addProfileRadio("bedrock", "Bedrock · reservado");
        profileGroup.setOnCheckedChangeListener((group, checkedId) -> {
            String profile = selectedProfile();
            updateProfileSelectionHint(profile);
            refreshLocalStatus("Perfil selecionado: " + profileLabel(profile) + ". Toque em Aplicar para sincronizar.");
        });
        profileDetailsContent.addView(profileGroup);

        profileHintText = smallText("");
        profileDetailsContent.addView(profileHintText);

        saveProfileButton = primaryButton("Aplicar perfil");
        saveProfileButton.setOnClickListener(v -> updateOwnProfile());
        profileDetailsContent.addView(saveProfileButton);

        LinearLayout updateCard = cardWithTopMargin(mainContent);
        updateCard.addView(sectionTitle("Atualizações"));
        updateCard.addView(smallText("Versão instalada e publicação na VPS."));
        updateText = smallText("APK " + APP_VERSION + " · ainda não verificado.");
        updateText.setTextColor(TEXT);
        updateText.setBackground(cardBackground(CARD_SOFT));
        updateText.setPadding(dp(10), dp(10), dp(10), dp(10));
        updateCard.addView(updateText);

        updateCheckButton = secondaryButton("Verificar atualização");
        updateCheckButton.setOnClickListener(v -> checkForUpdate());
        updateCard.addView(updateCheckButton);

        updateCleanupButton = secondaryButton("Limpeza segura de updates");
        updateCleanupButton.setOnClickListener(v -> runManualUpdateCleanup());
        updateCleanupButton.setVisibility(View.GONE);
        updateCard.addView(updateCleanupButton);

        LinearLayout technicalCard = cardWithTopMargin(mainContent);
        technicalCard.addView(sectionTitle("Avançado"));
        technicalCard.addView(smallText("Diagnósticos, logs e fallback legado ficam escondidos aqui."));
        technicalToggleButton = secondaryButton("Abrir avançado");
        technicalToggleButton.setOnClickListener(v -> toggleTechnicalDetails());
        technicalCard.addView(technicalToggleButton);

        technicalDetailsContent = new LinearLayout(this);
        technicalDetailsContent.setOrientation(LinearLayout.VERTICAL);
        technicalDetailsContent.setVisibility(View.GONE);
        technicalCard.addView(technicalDetailsContent);

        technicalAppText = technicalInfoBlock(technicalDetailsContent, "App");
        technicalDeviceText = technicalInfoBlock(technicalDetailsContent, "Aparelho");
        technicalRuntimeText = technicalInfoBlock(technicalDetailsContent, "Runtime");
        technicalDiagnosticsText = technicalInfoBlock(technicalDetailsContent, "Diagnósticos APK");
        technicalTermuxText = technicalInfoBlock(technicalDetailsContent, "Fallback Termux");
        technicalDependenciesText = technicalInfoBlock(technicalDetailsContent, "Migração sem Termux");

        systemChecklistText = smallText(prepareChecklistText());
        systemChecklistText.setVisibility(View.GONE);
        technicalDetailsContent.addView(systemChecklistText);

        termuxButton = secondaryButton("Abrir Termux (fallback)");
        termuxButton.setOnClickListener(v -> openTermux());
        technicalDetailsContent.addView(termuxButton);

        tailscaleButton = secondaryButton("Abrir Tailscale");
        tailscaleButton.setOnClickListener(v -> openTailscale());
        technicalDetailsContent.addView(tailscaleButton);

        heartbeatButton = secondaryButton("Sincronizar painel workers");
        heartbeatButton.setOnClickListener(v -> sendHeartbeat());
        technicalDetailsContent.addView(heartbeatButton);

        Button appLogsButton = secondaryButton("Ver logs do app");
        appLogsButton.setOnClickListener(v -> showStartupLogDialog());
        technicalDetailsContent.addView(appLogsButton);

        Button foregroundStartButton = secondaryButton("Ativar runtime persistente");
        foregroundStartButton.setOnClickListener(v -> startForegroundRuntimeFromUi());
        technicalDetailsContent.addView(foregroundStartButton);

        Button foregroundStopButton = secondaryButton("Parar runtime persistente");
        foregroundStopButton.setOnClickListener(v -> stopForegroundRuntimeFromUi());
        technicalDetailsContent.addView(foregroundStopButton);

        clearButton = dangerButton("Esquecer conexão local");
        clearButton.setBackground(makeButtonBackground(Color.rgb(91, 50, 57), BUTTON_DISABLED_BG));
        clearButton.setTextColor(new ColorStateList(new int[][]{new int[]{-android.R.attr.state_enabled}, new int[]{}}, new int[]{BUTTON_DISABLED_TEXT, TEXT}));
        clearButton.setOnClickListener(v -> confirmClearPairing());
        technicalDetailsContent.addView(clearButton);

        LinearLayout bedrockHeroCard = card();
        bedrockContent.addView(bedrockHeroCard);
        bedrockHeroCard.addView(sectionTitle("Minecraft Bedrock"));
        bedrockHeroCard.addView(smallText("Prepare, acompanhe e ligue o servidor sem expor detalhes técnicos."));
        bedrockHeroStatusText = largeStatusText("Servidor não instalado");
        bedrockHeroCard.addView(bedrockHeroStatusText);

        LinearLayout switchRow = new LinearLayout(this);
        switchRow.setOrientation(LinearLayout.HORIZONTAL);
        switchRow.setGravity(Gravity.CENTER_VERTICAL);
        switchRow.setPadding(dp(12), dp(10), dp(12), dp(10));
        switchRow.setBackground(cardBackground(CARD_SOFT));
        LinearLayout.LayoutParams switchRowParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        switchRowParams.setMargins(0, dp(10), 0, 0);
        bedrockHeroCard.addView(switchRow, switchRowParams);

        TextView switchLabel = new TextView(this);
        switchLabel.setText("Servidor Bedrock");
        switchLabel.setTextColor(TEXT);
        switchLabel.setTextSize(16);
        switchLabel.setTypeface(null, Typeface.BOLD);
        switchRow.addView(switchLabel, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));

        bedrockServerSwitch = new Switch(this);
        bedrockServerSwitch.setText("");
        bedrockServerSwitch.setTextColor(TEXT);
        bedrockServerSwitch.setOnCheckedChangeListener((buttonView, isChecked) -> {
            if (suppressBedrockSwitchEvents) return;
            handleBedrockSwitchChange(isChecked);
        });
        switchRow.addView(bedrockServerSwitch, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        ));

        LinearLayout readinessCard = cardWithTopMargin(bedrockContent);
        readinessCard.addView(sectionTitle("Configuração"));
        bedrockReadinessText = smallText("Próxima ação aparece aqui. Recursos técnicos ficam em Avançado.");
        bedrockReadinessText.setTextColor(TEXT);
        bedrockReadinessText.setBackground(cardBackground(CARD_SOFT));
        bedrockReadinessText.setPadding(dp(12), dp(10), dp(12), dp(10));
        readinessCard.addView(bedrockReadinessText);

        rootfsImportProgressText = smallText("Rootfs real: aguardando status");
        rootfsImportProgressText.setTextColor(TEXT);
        rootfsImportProgressText.setBackground(cardBackground(Color.rgb(18, 34, 50)));
        rootfsImportProgressText.setPadding(dp(12), dp(10), dp(12), dp(10));
        readinessCard.addView(rootfsImportProgressText);

        bedrockTestAllButton = primaryButton("Verificar servidor");
        bedrockTestAllButton.setOnClickListener(v -> testBedrockServerFromUi());
        readinessCard.addView(bedrockTestAllButton);

        bedrockPrepareServerButton = secondaryButton("Preparar servidor");
        bedrockPrepareServerButton.setOnClickListener(v -> prepareBedrockServerFromUi());
        readinessCard.addView(bedrockPrepareServerButton);

        bedrockAdvancedToggleButton = secondaryButton("Abrir avançado do servidor");
        bedrockAdvancedToggleButton.setOnClickListener(v -> toggleBedrockAdvanced());
        readinessCard.addView(bedrockAdvancedToggleButton);

        bedrockAdvancedContent = new LinearLayout(this);
        bedrockAdvancedContent.setOrientation(LinearLayout.VERTICAL);
        bedrockAdvancedContent.setVisibility(View.GONE);
        readinessCard.addView(bedrockAdvancedContent);

        Button rootfsImportButton = secondaryButton("Importar rootfs real");
        rootfsImportButton.setOnClickListener(v -> showCoreLinuxRootfsImportDialog());
        bedrockAdvancedContent.addView(rootfsImportButton);

        Button rootfsImportStatusButton = secondaryButton("Status rootfs real");
        rootfsImportStatusButton.setOnClickListener(v -> showCoreLinuxRootfsImportStatusFromUi());
        bedrockAdvancedContent.addView(rootfsImportStatusButton);

        Button rootfsValidateButton = secondaryButton("Validar rootfs ativo");
        rootfsValidateButton.setOnClickListener(v -> validateCoreLinuxRootfsFromUi());
        bedrockAdvancedContent.addView(rootfsValidateButton);

        Button rootfsAbortButton = secondaryButton("Cancelar importação pendente");
        rootfsAbortButton.setOnClickListener(v -> abortCoreLinuxRootfsImportFromUi());
        bedrockAdvancedContent.addView(rootfsAbortButton);

        rootfsAdvancedStatusText = technicalInfoBlock(bedrockAdvancedContent, "Rootfs real");

        LinearLayout bedrockActionRow = new LinearLayout(this);
        bedrockActionRow.setOrientation(LinearLayout.HORIZONTAL);
        bedrockAdvancedContent.addView(bedrockActionRow);
        bedrockFilesButton = compactButton("Arquivos");
        bedrockFilesButton.setOnClickListener(v -> showBedrockFilesFromUi());
        bedrockLogsButton = compactButton("Logs");
        bedrockLogsButton.setOnClickListener(v -> refreshBedrockRuntimeLogsFromUi());
        bedrockActionRow.addView(bedrockFilesButton, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        LinearLayout.LayoutParams logsParams = new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        logsParams.setMargins(dp(8), 0, 0, 0);
        bedrockActionRow.addView(bedrockLogsButton, logsParams);

        Button openConsoleButton = secondaryButton("Abrir console");
        openConsoleButton.setOnClickListener(v -> toggleBedrockTerminalCard());
        bedrockAdvancedContent.addView(openConsoleButton);

        bedrockEulaButton = dangerButton("Confirmar termos");
        bedrockEulaButton.setVisibility(View.GONE);
        bedrockEulaButton.setOnClickListener(v -> confirmBedrockEulaFromUi());

        bedrockTerminalCard = cardWithTopMargin(bedrockContent);
        bedrockTerminalCard.setVisibility(View.GONE);
        LinearLayout terminalCard = bedrockTerminalCard;
        LinearLayout terminalHeader = new LinearLayout(this);
        terminalHeader.setOrientation(LinearLayout.HORIZONTAL);
        terminalHeader.setGravity(Gravity.CENTER_VERTICAL);
        TextView terminalTitle = sectionTitle("Terminal do servidor");
        terminalHeader.addView(terminalTitle, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        bedrockCopyTerminalButton = compactButton("Copiar");
        bedrockCopyTerminalButton.setOnClickListener(v -> copyBedrockTerminalLogsFromUi());
        bedrockExpandTerminalButton = compactButton("▣");
        bedrockExpandTerminalButton.setOnClickListener(v -> showBedrockTerminalFullScreen());
        terminalHeader.addView(bedrockCopyTerminalButton, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        LinearLayout.LayoutParams expandTerminalParams = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        expandTerminalParams.setMargins(dp(8), 0, 0, 0);
        terminalHeader.addView(bedrockExpandTerminalButton, expandTerminalParams);
        terminalCard.addView(terminalHeader);
        terminalCard.addView(smallText("Console e logs em tempo real do Core Worker. Não é shell livre do Android."));
        bedrockTerminalText = terminalText();
        terminalCard.addView(bedrockTerminalText);

        LinearLayout commandRow = new LinearLayout(this);
        commandRow.setOrientation(LinearLayout.HORIZONTAL);
        commandRow.setGravity(Gravity.CENTER_VERTICAL);
        terminalCard.addView(commandRow, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        ));
        bedrockCommandInput = input("help, list, stop...", "");
        bedrockCommandInput.setSingleLine(true);
        bedrockCommandInput.setTypeface(Typeface.MONOSPACE);
        commandRow.addView(bedrockCommandInput, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        bedrockSendCommandButton = compactButton("Enviar");
        bedrockSendCommandButton.setOnClickListener(v -> sendBedrockConsoleCommandFromUi());
        LinearLayout.LayoutParams sendParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        sendParams.setMargins(dp(8), dp(7), 0, 0);
        commandRow.addView(bedrockSendCommandButton, sendParams);

        setContentView(root);
        updatePermissionGate();
    }

    private void showCoreLinuxRootfsImportDialog() {
        final EditText shaInput = input("SHA-256 esperado (opcional, 64 hex)", prefs.getString("core_linux_rootfs_expected_sha256", ""));
        shaInput.setSingleLine(true);
        shaInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        new AlertDialog.Builder(this)
                .setTitle("Importar rootfs real")
                .setMessage("Escolha um .tar, .tar.gz ou .tgz. O APK calcula o SHA-256, extrai em staging, valida e só promove se passar. Runner, Bedrock, Box64 e shell livre continuam bloqueados.")
                .setView(shaInput)
                .setPositiveButton("Escolher arquivo", (dialog, which) -> {
                    pendingRootfsImportExpectedSha256 = shaInput.getText() == null ? "" : shaInput.getText().toString().trim();
                    prefs.edit().putString("core_linux_rootfs_expected_sha256", pendingRootfsImportExpectedSha256).apply();
                    openCoreLinuxRootfsDocumentPicker();
                })
                .setNegativeButton("Cancelar", null)
                .show();
    }

    private void openCoreLinuxRootfsDocumentPicker() {
        try {
            Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            intent.addCategory(Intent.CATEGORY_OPENABLE);
            intent.setType("*/*");
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
            startActivityForResult(intent, REQUEST_CORE_LINUX_ROOTFS_IMPORT);
        } catch (Throwable exc) {
            refreshLocalStatus("Não consegui abrir seletor rootfs: " + shortThrowable(exc));
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQUEST_CORE_LINUX_ROOTFS_IMPORT) {
            if (resultCode != RESULT_OK || data == null || data.getData() == null) {
                refreshLocalStatus("Importação rootfs cancelada.");
                return;
            }
            Uri uri = data.getData();
            try {
                getContentResolver().takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
            } catch (Throwable ignored) {
            }
            importCoreLinuxRootfsFromUri(uri, pendingRootfsImportExpectedSha256);
        }
    }

    private void importCoreLinuxRootfsFromUri(Uri uri, String expectedSha256) {
        rootfsImportBusy = true;
        rootfsState = "rootfs_import_starting";
        rootfsSummary = "Importação rootfs iniciada";
        refreshLocalStatus("Importando rootfs real em staging. Não feche o app.");
        appendBedrockTerminal("rootfs", "importação rootfs iniciada em staging; runner/Bedrock/Box64 seguem bloqueados");
        updateRootfsUi();
        new Thread(() -> {
            JSONObject result = CoreLinuxRootfsImportManager.importFromUri(this, coreLinuxDir(), uri, expectedSha256);
            applyCoreLinuxRootfsState(result);
            rootfsImportBusy = false;
            mainHandler.post(() -> {
                refreshLocalStatus(coreLinuxSummary);
                appendBedrockTerminal("rootfs", coreLinuxSummary);
                refreshBedrockVisualState();
                sendInternalRuntimeHeartbeat(false, "rootfs_import");
            });
        }, "core-linux-rootfs-import").start();
    }

    private void showCoreLinuxRootfsImportStatusFromUi() {
        JSONObject status = CoreLinuxRootfsImportManager.status(this, coreLinuxDir());
        applyCoreLinuxRootfsState(status);
        appendBedrockTerminal("rootfs", status.optString("summary", "rootfs import aguardando"));
        refreshLocalStatus(status.optString("summary", "rootfs import aguardando"));
        refreshBedrockVisualState();
        showRootfsStatusDialog(status);
    }

    private void validateCoreLinuxRootfsFromUi() {
        runBusy("Validando rootfs ativo...", () -> {
            JSONObject status = CoreLinuxRootfsImportManager.validateActive(this, coreLinuxDir());
            applyCoreLinuxRootfsState(status);
            mainHandler.post(() -> {
                appendBedrockTerminal("rootfs", status.optString("summary", "rootfs validado"));
                refreshLocalStatus(status.optString("summary", "rootfs validado"));
                refreshBedrockVisualState();
                sendInternalRuntimeHeartbeat(false, "rootfs_validate");
            });
        });
    }

    private void abortCoreLinuxRootfsImportFromUi() {
        JSONObject status = CoreLinuxRootfsImportManager.abort(this, coreLinuxDir());
        applyCoreLinuxRootfsState(status);
        appendBedrockTerminal("rootfs", status.optString("summary", "importação cancelada"));
        refreshLocalStatus(status.optString("summary", "importação cancelada"));
        refreshBedrockVisualState();
    }

    private void showRootfsStatusDialog(JSONObject status) {
        try {
            JSONObject rootfs = status.optJSONObject("rootfs");
            JSONObject stats = rootfs == null ? null : rootfs.optJSONObject("stats");
            StringBuilder builder = new StringBuilder();
            builder.append(status.optString("summary", rootfsSummary)).append("\n\n");
            builder.append("Estado: ").append(firstNonEmpty(status.optString("state", ""), rootfsState)).append("\n");
            if (rootfs != null) {
                builder.append("Validação: ").append(rootfs.optString("validationLevel", rootfsValidationLevel)).append("\n");
                builder.append("Distribuição: ").append(rootfs.optString("distribution", "rootfs importado")).append("\n");
                String sha = rootfs.optString("sha256", rootfsLastSha256);
                if (!sha.isEmpty()) builder.append("SHA-256: ").append(sha.substring(0, Math.min(12, sha.length()))).append("…\n");
                if (stats != null) builder.append("Conteúdo: ").append(statsSummary(stats)).append("\n");
            }
            builder.append("\nRunner, Bedrock, Box64 e shell livre continuam bloqueados.");
            new AlertDialog.Builder(this)
                    .setTitle("Rootfs real")
                    .setMessage(builder.toString())
                    .setPositiveButton("OK", null)
                    .show();
        } catch (Throwable ignored) {
        }
    }


    private void applyCoreLinuxRootfsState(JSONObject status) {
        if (status == null) return;
        JSONObject rootfs = status.optJSONObject("rootfs");
        if (rootfs == null) rootfs = status.optJSONObject("rootfsState");
        String statusState = status.optString("state", "");
        String statusSummary = status.optString("summary", "");
        String rootState = rootfs == null ? "" : rootfs.optString("state", "");
        String rootSummary = rootfs == null ? "" : rootfs.optString("summary", "");
        rootfsState = firstNonEmpty(rootState, statusState, rootfsState);
        rootfsSummary = firstNonEmpty(rootSummary, statusSummary, rootfsSummary);
        rootfsValidationLevel = rootfs == null ? rootfsValidationLevel : firstNonEmpty(rootfs.optString("validationLevel", ""), rootfs.optString("rootfsValidationLevel", ""), rootfsValidationLevel);
        rootfsDistributionReady = (rootfs != null && rootfs.optBoolean("distributionReady", rootfsDistributionReady)) || status.optBoolean("distributionReady", false);
        rootfsLastSha256 = rootfs == null ? firstNonEmpty(status.optString("sha256", ""), rootfsLastSha256) : firstNonEmpty(rootfs.optString("sha256", ""), status.optString("sha256", ""), rootfsLastSha256);
        rootfsLastUpdatedAt = Math.max(rootfsLastUpdatedAt, Math.max(status.optLong("updatedAt", 0L), rootfs == null ? 0L : rootfs.optLong("updatedAt", 0L)));
        JSONObject stats = status.optJSONObject("stats");
        if (stats == null && rootfs != null) stats = rootfs.optJSONObject("stats");
        rootfsLastStatsSummary = stats == null ? rootfsLastStatsSummary : statsSummary(stats);
        boolean realValidated = isRootfsRealValidated(status) || (rootfs != null && isRootfsRealValidated(rootfs));
        if (realValidated) {
            coreLinuxPrepared = true;
            coreLinuxState = "rootfs_real_validated";
            coreLinuxSummary = firstNonEmpty(rootfsSummary, "Rootfs real validado · runner real ainda bloqueado");
        } else {
            coreLinuxSummary = firstNonEmpty(statusSummary, rootSummary, coreLinuxSummary);
            coreLinuxState = firstNonEmpty(statusState, rootState, coreLinuxState);
            coreLinuxPrepared = status.optBoolean("rootfsReady", coreLinuxPrepared) || (rootfs != null && rootfs.optBoolean("rootfsReady", false));
        }
        coreLinuxLastCheckAt = System.currentTimeMillis();
        internalDiagnosticsSummary = coreLinuxSummary;
        internalDiagnosticsLastAt = System.currentTimeMillis();
        updateRootfsUi();
    }

    private boolean isRootfsRealValidated(JSONObject value) {
        if (value == null) return false;
        String state = value.optString("state", "").toLowerCase(Locale.ROOT);
        String importState = value.optString("rootfsImportState", "").toLowerCase(Locale.ROOT);
        String level = value.optString("validationLevel", value.optString("rootfsValidationLevel", "")).toLowerCase(Locale.ROOT);
        return state.contains("rootfs_real_validated") || importState.contains("rootfs_real_validated") || "real".equals(level);
    }

    private String statsSummary(JSONObject stats) {
        if (stats == null) return "";
        long files = stats.optLong("files", -1L);
        long dirs = stats.optLong("dirs", -1L);
        long symlinks = stats.optLong("symlinks", -1L);
        long bytes = stats.optLong("bytes", -1L);
        StringBuilder builder = new StringBuilder();
        if (files >= 0) builder.append(files).append(" arquivos");
        if (dirs >= 0) builder.append(builder.length() == 0 ? "" : " · ").append(dirs).append(" diretórios");
        if (symlinks > 0) builder.append(" · ").append(symlinks).append(" links");
        if (bytes >= 0) builder.append(" · ").append(formatBytes(bytes));
        return builder.toString();
    }

    private void refreshRootfsStateFromDisk() {
        try {
            JSONObject rootfsStateJson = readJsonFile(new File(new File(coreLinuxDir(), "runtime"), "rootfs-state.json"));
            JSONObject importStateJson = readJsonFile(new File(new File(coreLinuxDir(), "runtime"), "rootfs-import-state.json"));
            JSONObject merged = new JSONObject();
            if (rootfsStateJson.length() > 0) safePutPayload(merged, "rootfs", rootfsStateJson);
            if (importStateJson.length() > 0) safePutPayload(merged, "import", importStateJson);
            String state = firstNonEmpty(rootfsStateJson.optString("state", ""), importStateJson.optString("state", ""));
            String summary = firstNonEmpty(rootfsStateJson.optString("summary", ""), importStateJson.optString("summary", ""));
            if (!state.isEmpty()) merged.put("state", state);
            if (!summary.isEmpty()) merged.put("summary", summary);
            if (rootfsStateJson.optBoolean("rootfsReady", false)) merged.put("rootfsReady", true);
            if (rootfsStateJson.length() > 0 || importStateJson.length() > 0) applyCoreLinuxRootfsState(merged);
        } catch (Throwable ignored) {
        }
    }

    private void updateRootfsUi() {
        runOnUiThread(() -> {
            String state = firstNonEmpty(rootfsState, coreLinuxState).toLowerCase(Locale.ROOT);
            String summary = firstNonEmpty(rootfsSummary, coreLinuxSummary, "Rootfs aguardando status");
            boolean real = state.contains("rootfs_real_validated") || "real".equalsIgnoreCase(rootfsValidationLevel);
            boolean importing = rootfsImportBusy || state.contains("import");
            String shortState = real ? "Rootfs real validado" : (importing ? "Importando rootfs" : "Rootfs em preparação");
            String runner = real ? "Runner bloqueado · próximo estágio seguro" : "Runner bloqueado · aguarde rootfs real";
            String detail = shortState + "\n" + summary;
            if (!rootfsLastStatsSummary.isEmpty()) detail += "\n" + rootfsLastStatsSummary;
            if (!rootfsLastSha256.isEmpty()) detail += "\nSHA-256: " + rootfsLastSha256.substring(0, Math.min(12, rootfsLastSha256.length())) + "…";
            if (coreHeroHeadlineText != null) {
                coreHeroHeadlineText.setText(real ? "Pronto · rootfs real validado" : (coreLinuxPrepared ? "Runtime APK pronto" : "Preparando runtime"));
            }
            if (rootfsHeroText != null) rootfsHeroText.setText(detail);
            if (runnerHeroText != null) runnerHeroText.setText(runner);
            if (rootfsImportProgressText != null) rootfsImportProgressText.setText((importing ? "⏳ " : (real ? "✅ " : "• ")) + detail);
            if (rootfsAdvancedStatusText != null) {
                rootfsAdvancedStatusText.setText("Rootfs real\n" + detail + "\nValidação: " + firstNonEmpty(rootfsValidationLevel, real ? "real" : "pendente") + "\nExecução: bloqueada nesta etapa");
            }
        });
    }

    private ScrollView pageScroll() {
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(false);
        scroll.setBackgroundColor(BG);
        return scroll;
    }

    private LinearLayout pageContent() {
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(0, 0, 0, dp(12));
        return content;
    }

    private Button bottomNavButton(String text) {
        Button button = styledButton(text, Color.rgb(18, 27, 48), TEXT, dp(54));
        button.setTextSize(12);
        button.setGravity(Gravity.CENTER);
        button.setPadding(dp(8), dp(6), dp(8), dp(6));
        return button;
    }

    private Button compactButton(String text) {
        Button button = styledButton(text, Color.rgb(35, 49, 82), TEXT, dp(36));
        button.setTextSize(13);
        return button;
    }

    private TextView largeStatusText(String value) {
        TextView text = new TextView(this);
        text.setText(value);
        text.setTextColor(TEXT);
        text.setTextSize(20);
        text.setTypeface(null, Typeface.BOLD);
        text.setPadding(dp(12), dp(12), dp(12), dp(12));
        text.setBackground(cardBackground(Color.rgb(30, 48, 86)));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        params.setMargins(0, dp(8), 0, 0);
        text.setLayoutParams(params);
        return text;
    }

    private TextView terminalText() {
        TextView text = new TextView(this);
        text.setTextColor(Color.rgb(195, 255, 205));
        text.setTextSize(12);
        text.setTypeface(Typeface.MONOSPACE);
        text.setLineSpacing(dp(1), 1.0f);
        text.setMinLines(5);
        text.setMaxLines(12);
        text.setText(readBedrockTerminalTail());
        text.setPadding(dp(12), dp(10), dp(12), dp(10));
        text.setBackground(cardBackground(Color.rgb(4, 9, 16)));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        params.setMargins(0, dp(8), 0, dp(8));
        text.setLayoutParams(params);
        return text;
    }

    private Button pageTabButton(String text) {
        Button button = styledButton(text, Color.rgb(35, 49, 82), TEXT, dp(36));
        button.setTextSize(13);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        params.setMargins(0, 0, 0, 0);
        button.setLayoutParams(params);
        attachButtonMicroAnimation(button);
        return button;
    }

    private void attachButtonMicroAnimation(Button button) {
        if (button == null) return;
        button.setOnTouchListener((view, event) -> {
            if (!view.isEnabled()) return false;
            if (event.getAction() == MotionEvent.ACTION_DOWN) {
                view.animate().scaleX(0.985f).scaleY(0.985f).setDuration(70L).start();
            } else if (event.getAction() == MotionEvent.ACTION_UP || event.getAction() == MotionEvent.ACTION_CANCEL) {
                view.animate().scaleX(1f).scaleY(1f).setDuration(90L).start();
            }
            return false;
        });
    }

    private void updatePageTabs(int position) {
        if (corePageButton != null) {
            corePageButton.setBackground(makeButtonBackground(position == 0 ? Color.rgb(45, 75, 132) : Color.rgb(18, 27, 48), BUTTON_DISABLED_BG));
            corePageButton.setTextColor(position == 0 ? TEXT : MUTED);
        }
        if (bedrockPageButton != null) {
            bedrockPageButton.setBackground(makeButtonBackground(position == 1 ? Color.rgb(45, 92, 76) : Color.rgb(18, 27, 48), BUTTON_DISABLED_BG));
            bedrockPageButton.setTextColor(position == 1 ? TEXT : MUTED);
        }
        refreshBedrockVisualState();
    }

    private static final class StaticPageAdapter extends RecyclerView.Adapter<StaticPageAdapter.PageHolder> {
        private final View[] pages;

        StaticPageAdapter(View[] pages) {
            this.pages = pages == null ? new View[0] : pages;
        }

        @Override
        public PageHolder onCreateViewHolder(ViewGroup parent, int viewType) {
            FrameLayout frame = new FrameLayout(parent.getContext());
            frame.setLayoutParams(new ViewGroup.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.MATCH_PARENT
            ));
            return new PageHolder(frame);
        }

        @Override
        public void onBindViewHolder(PageHolder holder, int position) {
            holder.container.removeAllViews();
            if (position >= 0 && position < pages.length) {
                View page = pages[position];
                if (page.getParent() instanceof ViewGroup) {
                    ((ViewGroup) page.getParent()).removeView(page);
                }
                holder.container.addView(page, new FrameLayout.LayoutParams(
                        FrameLayout.LayoutParams.MATCH_PARENT,
                        FrameLayout.LayoutParams.MATCH_PARENT
                ));
            }
        }

        @Override
        public int getItemCount() {
            return pages.length;
        }

        static final class PageHolder extends RecyclerView.ViewHolder {
            final FrameLayout container;
            PageHolder(FrameLayout view) {
                super(view);
                this.container = view;
            }
        }
    }


    private void buildPermissionGate(LinearLayout root) {
        permissionGateCard = card();
        permissionGateCard.setBackground(cardBackground(CARD_SOFT));
        // Não mostrar alerta falso na primeira renderização: as permissões são verificadas
        // imediatamente depois da UI nascer e no onResume. O card só aparece se faltar algo real.
        permissionGateCard.setVisibility(View.GONE);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        params.setMargins(0, 0, 0, dp(14));
        root.addView(permissionGateCard, params);

        permissionGateCard.addView(sectionTitle("Permissões necessárias"));
        TextView intro = smallText("Só aparece quando faltar alguma permissão real para avisos, atualização ou segundo plano.");
        intro.setTextColor(TEXT);
        permissionGateCard.addView(intro);

        permissionStatusText = smallText("");
        permissionStatusText.setTextColor(TEXT);
        permissionStatusText.setBackground(cardBackground(CARD));
        permissionStatusText.setPadding(dp(10), dp(10), dp(10), dp(10));
        permissionGateCard.addView(permissionStatusText);

        notificationPermissionButton = button("Permitir notificações");
        notificationPermissionButton.setOnClickListener(v -> requestNotificationPermission());
        permissionGateCard.addView(notificationPermissionButton);

        installPermissionButton = button("Permitir instalar atualizações");
        installPermissionButton.setOnClickListener(v -> openInstallPermissionSettings());
        permissionGateCard.addView(installPermissionButton);

        batteryPermissionButton = button("Permitir rodar em segundo plano");
        batteryPermissionButton.setOnClickListener(v -> openBatteryOptimizationSettings());
        permissionGateCard.addView(batteryPermissionButton);

        refreshPermissionsButton = button("Verificar permissões");
        refreshPermissionsButton.setOnClickListener(v -> updatePermissionGate());
        permissionGateCard.addView(refreshPermissionsButton);
    }

    private boolean hasNotificationPermission() {
        return Build.VERSION.SDK_INT < 33 || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
    }

    private boolean hasInstallPermission() {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.O || getPackageManager().canRequestPackageInstalls();
    }

    private boolean hasBatteryPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
            return true;
        }
        try {
            PowerManager manager = (PowerManager) getSystemService(POWER_SERVICE);
            return manager == null || manager.isIgnoringBatteryOptimizations(getPackageName());
        } catch (Throwable ignored) {
            return true;
        }
    }

    private boolean hasRequiredAppPermissions() {
        // Bateria/segundo plano varia muito por fabricante e pode voltar falso até depois do usuário permitir.
        // Para não poluir a tela inicial, o gate obrigatório só considera permissões objetivas do Android.
        return hasNotificationPermission() && hasInstallPermission();
    }

    private void updatePermissionGate() {
        runOnUiThread(() -> {
            boolean notificationOk = hasNotificationPermission();
            boolean installOk = hasInstallPermission();
            boolean batteryOk = hasBatteryPermission();
            boolean allOk = notificationOk && installOk;
            long now = System.currentTimeMillis();

            if (allOk) {
                permissionGateMissingStreak = 0;
                permissionGateFirstMissingAt = 0L;
                permissionGateDelayedRecheckScheduled.set(false);
            } else {
                if (permissionGateFirstMissingAt <= 0L) {
                    permissionGateFirstMissingAt = now;
                    permissionGateMissingStreak = 1;
                } else {
                    permissionGateMissingStreak = Math.min(1000, permissionGateMissingStreak + 1);
                }
            }
            boolean stableMissing = !allOk
                    && permissionGateMissingStreak >= 2
                    && (now - permissionGateFirstMissingAt) >= PERMISSION_GATE_STABILIZE_MS;

            if (!allOk && !stableMissing && permissionGateDelayedRecheckScheduled.compareAndSet(false, true)) {
                mainHandler.postDelayed(() -> {
                    permissionGateDelayedRecheckScheduled.set(false);
                    updatePermissionGate();
                }, PERMISSION_GATE_STABILIZE_MS);
            }

            if (notificationPermissionButton != null) {
                notificationPermissionButton.setVisibility((stableMissing && !notificationOk) ? View.VISIBLE : View.GONE);
            }
            if (installPermissionButton != null) {
                installPermissionButton.setVisibility((stableMissing && !installOk) ? View.VISIBLE : View.GONE);
            }
            if (batteryPermissionButton != null) {
                // Não mostrar sozinho no início: em MIUI/Android alguns aparelhos reportam bateria como pendente
                // mesmo depois do usuário liberar, e isso fazia o cartão aparecer sem necessidade.
                batteryPermissionButton.setVisibility((stableMissing && !batteryOk && (!notificationOk || !installOk)) ? View.VISIBLE : View.GONE);
            }
            if (refreshPermissionsButton != null) {
                refreshPermissionsButton.setVisibility(stableMissing ? View.VISIBLE : View.GONE);
            }

            if (permissionStatusText != null) {
                if (allOk || !stableMissing) {
                    permissionStatusText.setText("");
                    permissionStatusText.setVisibility(View.GONE);
                } else {
                    StringBuilder builder = new StringBuilder();
                    if (!notificationOk) {
                        builder.append(permissionLine("Notificações", false, "avisar APK novo publicado pela VPS")).append('\n');
                    }
                    if (!installOk) {
                        builder.append(permissionLine("Instalar atualizações", false, "abrir o APK baixado da VPS")).append('\n');
                    }
                    if (!batteryOk && (!notificationOk || !installOk)) {
                        builder.append(permissionLine("Segundo plano/bateria", false, "opcional para manter checagens locais mais confiáveis"));
                    }
                    String status = builder.toString().trim();
                    permissionStatusText.setText(status);
                    permissionStatusText.setVisibility(status.isEmpty() ? View.GONE : View.VISIBLE);
                }
            }

            if (permissionGateCard != null) {
                permissionGateCard.setVisibility(stableMissing ? View.VISIBLE : View.GONE);
            }
            // Permissão pendente não deve esconder a interface principal nem gerar aparência de app quebrado.
            LinearLayout visibleHost = pageHost != null ? pageHost : mainContent;
            if (visibleHost != null) {
                visibleHost.setVisibility(View.VISIBLE);
            }
        });
    }

    private String permissionLine(String label, boolean ok, String reason) {
        return (ok ? "✅ " : "⚠️ ") + label + ": " + (ok ? "ok" : "pendente") + " · " + reason;
    }

    private void requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33 && !hasNotificationPermission()) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 4103);
        } else {
            updatePermissionGate();
        }
    }

    private void openInstallPermissionSettings() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                Intent intent = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + getPackageName()));
                startActivity(intent);
            } else {
                updatePermissionGate();
            }
        } catch (Throwable exc) {
            try {
                startActivity(new Intent(Settings.ACTION_SECURITY_SETTINGS));
            } catch (Throwable ignored) {
                refreshLocalStatus("Não consegui abrir a tela de instalação de APK. Abra as configurações do Android e permita instalações pelo Core Worker.");
            }
        }
    }

    private void openBatteryOptimizationSettings() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
            updatePermissionGate();
            return;
        }
        try {
            Intent intent = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:" + getPackageName()));
            startActivity(intent);
        } catch (Throwable exc) {
            try {
                startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
            } catch (Throwable ignored) {
                refreshLocalStatus("Não consegui abrir a tela de bateria. Desative otimização de bateria manualmente para o Core Worker.");
            }
        }
    }

    private LinearLayout cardWithTopMargin(LinearLayout root) {
        LinearLayout card = card();
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        params.setMargins(0, dp(8), 0, 0);
        root.addView(card, params);
        return card;
    }

    private LinearLayout card() {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(13), dp(12), dp(13), dp(12));
        card.setBackground(cardBackground(CARD));
        return card;
    }

    private GradientDrawable cardBackground(int color) {
        GradientDrawable drawable = rounded(color);
        drawable.setStroke(dp(1), Color.rgb(34, 45, 72));
        return drawable;
    }

    private TextView sectionTitle(String value) {
        TextView title = new TextView(this);
        title.setText(value);
        title.setTextColor(TEXT);
        title.setTextSize(18);
        title.setTypeface(null, 1);
        title.setPadding(0, 0, 0, dp(3));
        return title;
    }

    private TextView smallText(String value) {
        TextView text = new TextView(this);
        text.setText(value);
        text.setTextColor(MUTED);
        text.setTextSize(12);
        text.setPadding(0, dp(1), 0, dp(4));
        return text;
    }

    private TextView technicalInfoBlock(LinearLayout parent, String title) {
        TextView text = new TextView(this);
        text.setText(title);
        text.setTextColor(TEXT);
        text.setTextSize(13);
        text.setLineSpacing(dp(1), 1.02f);
        text.setPadding(dp(12), dp(10), dp(12), dp(10));
        text.setBackground(cardBackground(CARD_SOFT));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        params.setMargins(0, dp(8), 0, 0);
        parent.addView(text, params);
        return text;
    }

    private TextView label(String value) {
        TextView label = new TextView(this);
        label.setText(value);
        label.setTextColor(MUTED);
        label.setTextSize(13);
        label.setPadding(0, dp(8), 0, dp(3));
        return label;
    }

    private EditText input(String hint, String value) {
        EditText edit = new EditText(this);
        edit.setSingleLine(true);
        edit.setHint(hint);
        edit.setText(value);
        edit.setTextColor(TEXT);
        edit.setHintTextColor(MUTED);
        edit.setTextSize(15);
        edit.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        edit.setSelectAllOnFocus(false);
        edit.setPadding(dp(10), dp(7), dp(10), dp(7));
        return edit;
    }

    private Button button(String text) {
        return styledButton(text, BUTTON_BG, BUTTON_TEXT, dp(40));
    }

    private Button primaryButton(String text) {
        return styledButton(text, BUTTON_BG, BUTTON_TEXT, dp(40));
    }

    private Button secondaryButton(String text) {
        return styledButton(text, Color.rgb(35, 49, 82), TEXT, dp(36));
    }

    private Button dangerButton(String text) {
        return styledButton(text, Color.rgb(91, 50, 57), TEXT, dp(36));
    }

    private Button styledButton(String text, int enabledColor, int textColor, int minHeight) {
        Button button = new Button(this);
        button.setText(text);
        button.setAllCaps(false);
        button.setTextColor(new ColorStateList(
                new int[][]{new int[]{-android.R.attr.state_enabled}, new int[]{}},
                new int[]{BUTTON_DISABLED_TEXT, textColor}
        ));
        button.setTextSize(14);
        button.setTypeface(null, Typeface.BOLD);
        button.setMinHeight(minHeight);
        button.setPadding(dp(12), dp(6), dp(12), dp(6));
        button.setBackground(makeButtonBackground(enabledColor, BUTTON_DISABLED_BG));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        params.setMargins(0, dp(7), 0, 0);
        button.setLayoutParams(params);
        attachButtonMicroAnimation(button);
        return button;
    }

    private ColorStateList buttonTextColors() {
        return new ColorStateList(
                new int[][]{
                        new int[]{-android.R.attr.state_enabled},
                        new int[]{}
                },
                new int[]{BUTTON_DISABLED_TEXT, BUTTON_TEXT}
        );
    }

    private StateListDrawable makeButtonBackground(int enabledColor, int disabledColor) {
        StateListDrawable states = new StateListDrawable();
        states.addState(new int[]{-android.R.attr.state_enabled}, rounded(disabledColor));
        states.addState(new int[]{android.R.attr.state_pressed}, rounded(Color.rgb(166, 211, 255)));
        states.addState(new int[]{}, rounded(enabledColor));
        return states;
    }

    private GradientDrawable rounded(int color) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(dp(16));
        return drawable;
    }

    private void addProfileRadio(String tag, String text) {
        RadioButton radio = new RadioButton(this);
        radio.setText(text);
        radio.setTag(tag);
        radio.setTextColor(TEXT);
        radio.setTextSize(14);
        radio.setId(View.generateViewId());
        profileGroup.addView(radio);
        if ("midia".equals(tag)) {
            radio.setChecked(true);
        }
    }

    private void loadInputs() {
        serverUrlInput.setText(DEFAULT_VPS_URL);
        pairCodeInput.setText("");
        deviceNameInput.setText(prefs.getString("device_name", defaultDeviceName()));
        String profile = normalizeProfile(prefs.getString("profile", "midia"));
        updateProfileRadioSelection(profile);
        updateProfileHint(profile);
        updateSystemChecklistText();
        updatePairingUi();
    }

    private void toggleTechnicalDetails() {
        technicalExpanded = !technicalExpanded;
        if (technicalDetailsContent != null) {
            setAnimatedVisibility(technicalDetailsContent, technicalExpanded);
        }
        if (technicalToggleButton != null) {
            technicalToggleButton.setText(technicalExpanded ? "Fechar avançado" : "Abrir avançado");
        }
        refreshLocalStatus(null);
    }

    private void toggleBedrockAdvanced() {
        bedrockAdvancedExpanded = !bedrockAdvancedExpanded;
        if (bedrockAdvancedContent != null) {
            setAnimatedVisibility(bedrockAdvancedContent, bedrockAdvancedExpanded);
        }
        if (bedrockAdvancedToggleButton != null) {
            bedrockAdvancedToggleButton.setText(bedrockAdvancedExpanded ? "Fechar avançado" : "Abrir avançado do servidor");
        }
    }

    private void toggleBedrockTerminalCard() {
        if (bedrockTerminalCard == null) return;
        boolean show = bedrockTerminalCard.getVisibility() != View.VISIBLE;
        setAnimatedVisibility(bedrockTerminalCard, show);
        if (show) {
            refreshBedrockTerminalViews();
        }
    }

    private void setAnimatedVisibility(View view, boolean visible) {
        if (view == null) return;
        if (visible) {
            view.setAlpha(0f);
            view.setTranslationY(dp(6));
            view.setVisibility(View.VISIBLE);
            view.animate().alpha(1f).translationY(0f).setDuration(180L).start();
        } else {
            view.animate().alpha(0f).translationY(dp(4)).setDuration(140L).withEndAction(() -> {
                view.setVisibility(View.GONE);
                view.setAlpha(1f);
                view.setTranslationY(0f);
            }).start();
        }
    }

    private void animateContentIn(View view) {
        if (view == null) return;
        view.setAlpha(0.96f);
        view.setTranslationY(dp(4));
        view.animate().alpha(1f).translationY(0f).setDuration(160L).start();
    }

    private void toggleProfileDetails() {
        profileExpanded = !profileExpanded;
        if (profileDetailsContent != null) {
            profileDetailsContent.setVisibility(profileExpanded ? View.VISIBLE : View.GONE);
        }
        if (profileToggleButton != null) {
            profileToggleButton.setText(profileExpanded ? "Fechar opções" : "Alterar perfil");
        }
        updateProfileHint(appliedProfile());
    }

    private void showPairingForm(boolean show, String message) {
        if (pairingForm != null) {
            pairingForm.setVisibility(show ? View.VISIBLE : View.GONE);
        }
        if (rePairButton != null) {
            rePairButton.setVisibility(show ? View.GONE : View.VISIBLE);
        }
        if (message != null && !message.trim().isEmpty()) {
            refreshLocalStatus(message);
        }
    }

    private void updatePairingUi() {
        runOnUiThread(() -> {
            boolean paired = hasPairing();
            if (connectTitleText != null) {
                connectTitleText.setText("Conexão");
            }
            if (connectHintText != null) {
                connectHintText.setText(paired
                        ? "Vínculo ativo. Nenhum código necessário."
                        : "Use o código do painel workers.");
            }
            if (pairingStatusText != null) {
                String profile = appliedProfile();
                pairingStatusText.setText(paired
                        ? "VPS principal conectada · perfil " + profileLabel(profile)
                        : "Ainda não conectado. Gere um código no Discord.");
            }
            if (rePairButton != null) {
                rePairButton.setVisibility(paired ? View.VISIBLE : View.GONE);
            }
            if (pairingForm != null) {
                pairingForm.setVisibility(paired ? View.GONE : View.VISIBLE);
            }
        });
    }

    private void testServer() {
        String serverUrl = normalizedServerUrl();
        if (serverUrl.isEmpty()) {
            refreshLocalStatus("Servidor da VPS não configurado no APK.");
            return;
        }
        runBusy("Testando conexão com a VPS...", () -> {
            HttpResult result = request("GET", serverUrl + "/health", null, null);
            vpsState = result.ok() ? "ok" : "falha HTTP " + result.status;
            double ping = measureTcpPingMs(serverUrl);
            String message = result.ok() ? "VPS online" : "A VPS respondeu HTTP " + result.status;
            if (ping >= 0) {
                message += " · ping " + Math.round(ping) + "ms";
            }
            if (result.ok()) {
                message += " · bot saudável";
            } else {
                message += "\n" + compactResultBody(result.body);
            }
            show(message);
        });
    }

    private void pairWorker() {
        String serverUrl = normalizedServerUrl();
        if (serverUrl.isEmpty()) {
            refreshLocalStatus("VPS não configurada neste APK. Compile o APK privado com CORE_WORKER_VPS_URL.");
            return;
        }
        String code = pairCodeInput.getText().toString().trim();
        String name = deviceNameInput.getText().toString().trim();
        String profile = selectedProfile();
        if (code.isEmpty() || name.isEmpty()) {
            refreshLocalStatus("Preencha o código CORE e o nome do celular.");
            return;
        }
        saveLocalFields(profile);

        runBusy("Conectando este celular à VPS...", () -> {
            JSONObject nativePair = pairNativeWorkerDirect(serverUrl, code, name, profile);
            if (nativePair.optBoolean("ok", false)) {
                String workerId = nativePair.optString("worker_id", nativeWorkerId());
                prefs.edit()
                        .putString("server_url", serverUrl)
                        .putString("device_name", name)
                        .putString("profile", profile)
                        .putString("worker_id", workerId)
                        .putString("native_worker_id", workerId)
                        .putString("worker_token", nativePair.optString("token", ""))
                        .putBoolean("paired_via_native_apk", true)
                        .remove("paired_via_local_agent")
                        .apply();
                nativeWorkerOnline = true;
                nativeWorkerState = "pareado direto na VPS";
                nativeWorkerLastHeartbeatAt = System.currentTimeMillis();
                localAgentWorkerId = workerId;
                updatePairingUi();
                registerFcmTokenAsync("native_pair_success");
                sendInternalRuntimeHeartbeat(false, "native_pair_success");
                sendNativeWorkerHeartbeat(false, "native_pair_success");
                vpsState = "ok";
                show("Celular conectado direto pelo APK.\nPerfil: " + profileLabel(profile) + "\nWorker: " + emptyFallback(workerId, "apk") + "\nTermux ficou apenas como fallback temporário.");
                return;
            }

            if (!updateLocalAgentStatus(true)) {
                show("Pareamento direto pelo APK falhou: " + nativePair.optString("error", "sem resposta") + "\n\nTermux também está offline. Gere outro código e tente novamente quando a rede estiver estável.");
                return;
            }

            JSONObject payload = new JSONObject();
            payload.put("code", code);
            payload.put("vps_url", serverUrl);
            payload.put("name", name);
            payload.put("device_name", name);
            putProfilePayload(payload, profile);
            payload.put("source", "core-worker-apk-companion-fallback");
            payload.put("apk_version", APP_VERSION);

            HttpResult result = request("POST", LOCAL_AGENT_PAIR_URL, payload, null);
            if (!result.ok()) {
                show("Falha no fallback pelo worker local: HTTP " + result.status + "\n\n" + compactResultBody(result.body));
                return;
            }
            JSONObject body = new JSONObject(result.body);
            if (!body.optBoolean("ok", false)) {
                show("O fallback local não conseguiu parear.\n\n" + compactResultBody(result.body));
                return;
            }
            String workerId = body.optString("worker_id", localAgentWorkerId);
            prefs.edit()
                    .putString("server_url", serverUrl)
                    .putString("device_name", name)
                    .putString("profile", profile)
                    .putString("worker_id", workerId)
                    .putBoolean("paired_via_local_agent", true)
                    .remove("paired_via_native_apk")
                    .remove("worker_token")
                    .apply();
            applyLocalAgentStatus(body);
            showLocalAgentText();
            updatePairingUi();
            registerFcmTokenAsync("pair_success_fallback");
            vpsState = "ok";
            show("Celular conectado pelo fallback Termux.\nPerfil: " + profileLabel(profile) + "\nWorker: " + emptyFallback(workerId, "local"));
        });
    }

    private void updateOwnProfile() {
        String profile = selectedProfile();
        saveLocalFields(profile);
        updateProfileHint(profile);
        collapseProfileDetails();
        runBusy("Aplicando perfil...", () -> {
            boolean localSynced = syncProfileToLocalAgent(profile);
            String prefix = localSynced
                    ? "Perfil aplicado: " + profileLabel(profile)
                    : "Perfil salvo no APK. Worker local offline; abra o Termux por enquanto para sincronizar.";
            sendInternalRuntimeHeartbeat(false, "profile_apply");
            sendNativeWorkerHeartbeat(false, "profile_apply");
            if (hasPairing()) {
                sendHeartbeatInternal(true, prefix);
            } else {
                show(prefix + "\n\nEste celular ainda não está pareado com a VPS.");
            }
        });
    }

    private void sendHeartbeat() {
        saveLocalFields(appliedProfile());
        runBusy("Atualizando status no painel...", () -> {
            updateLocalAgentStatus(false);
            sendInternalRuntimeHeartbeat(false, "manual_sync");
            sendNativeWorkerHeartbeat(false, "manual_sync");
            sendHeartbeatInternal(true);
        });
    }

    private void sendHeartbeatInternal(boolean showResult) throws Exception {
        sendHeartbeatInternal(showResult, null);
    }

    private void sendHeartbeatInternal(boolean showResult, String successPrefix) throws Exception {
        if (!updateLocalAgentStatus(true)) {
            sendInternalRuntimeHeartbeat(showResult, "termux_offline_sync");
            if (showResult) {
                show("Termux offline. Runtime nativo do APK sincronizado; Termux é apenas fallback temporário.");
            }
            return;
        }
        HttpResult result = request("POST", LOCAL_AGENT_HEARTBEAT_URL, new JSONObject(), null);
        if (!result.ok()) {
            vpsState = "falha HTTP " + result.status;
            show("Falha ao pedir status ao worker local: HTTP " + result.status + "\n\n" + compactResultBody(result.body));
            return;
        }
        JSONObject body = new JSONObject(result.body);
        applyLocalAgentStatus(body);
        showLocalAgentText();
        boolean synced = body.optBoolean("synced_to_vps", false);
        vpsState = synced ? "ok" : "pendente";
        if (showResult) {
            String message = successPrefix == null ? "Status solicitado ao worker local." : successPrefix;
            message += synced ? "\nVPS recebeu heartbeat do fallback Termux." : "\nWorker local respondeu, mas ainda não confirmou heartbeat na VPS.";
            show(message);
        } else {
            show("Worker local sincronizado. Confira o painel workers no Discord.");
        }
    }

    private void autoCheckForUpdate() {
        String serverUrl = normalizedServerUrl();
        if (serverUrl.isEmpty()) {
            updateUpdateUi("APK " + APP_VERSION + " · VPS não configurada.", false, false);
            return;
        }
        new Thread(() -> {
            try {
                checkForUpdateInternal(serverUrl, false);
            } catch (Throwable ignored) {
                updateUpdateUi("APK " + APP_VERSION + " · ainda não verificado.", false, false);
            }
        }).start();
    }

    private void checkForUpdate() {
        String serverUrl = normalizedServerUrl();
        if (serverUrl.isEmpty()) {
            refreshLocalStatus("Servidor da VPS não configurado no APK.");
            return;
        }
        runBusy("Procurando atualização na VPS...", () -> checkForUpdateInternal(serverUrl, true));
    }

    private void checkForUpdateInternal(String serverUrl, boolean userVisible) throws Exception {
        HttpResult result = fetchLatestManifest(serverUrl);
        if (!result.ok()) {
            latestUpdateAvailable = false;
            latestVersionName = "";
            latestVersionCode = -1;
            updateUpdateUi("APK " + APP_VERSION + " · manifesto indisponível.", false, false);
            if (userVisible) {
                show("Não encontrei manifesto de atualização na VPS.\nHTTP " + result.status + " · " + compactResultBody(result.body));
            }
            return;
        }
        JSONObject body = new JSONObject(result.body);
        latestVersionName = body.optString("versionName", body.optString("version", ""));
        latestVersionCode = body.optInt("versionCode", -1);
        latestApkSha256 = body.optString("sha256", "");
        latestApkUrl = resolveUpdateUrl(serverUrl, body.optString("downloadUrl", body.optString("directApkUrl", body.optString("apkUrl", body.optString("url", "")))));
        latestChangelog = changelogText(body.optJSONArray("changelog"));
        latestNotificationId = body.optString("notificationId", "");
        if (latestNotificationId.trim().isEmpty()) {
            latestNotificationId = "apk-" + latestVersionCode + "-" + emptyFallback(latestApkSha256, latestVersionName);
        }
        String requiredAgent = body.optString("requiredAgentVersion", body.optString("minAgentVersion", ""));
        boolean notificationRequested = body.optBoolean("notificationRequested", body.optBoolean("notifyUsers", false));
        boolean available = isLatestUpdateAvailable();
        latestUpdateAvailable = available;

        StringBuilder text = new StringBuilder();
        text.append("APK ").append(APP_VERSION).append(" → VPS ").append(emptyFallback(latestVersionName, "sem nome"));
        if (latestVersionCode >= 0) text.append(" (").append(latestVersionCode).append(")");
        text.append("\n");
        text.append(available ? "Atualização pronta para instalar." : "Tudo em dia.");
        if (!requiredAgent.isEmpty() && technicalExpanded) {
            text.append("\nWorker recomendado: ").append(requiredAgent).append(".");
        }
        if (!latestChangelog.isEmpty() && technicalExpanded) {
            text.append("\n\nMudanças:\n").append(latestChangelog);
        }
        updateUpdateUi(text.toString(), available, true);
        if (notificationRequested) {
            reportUpdateNotification(serverUrl, "manifest_seen", false, available ? "manifesto lido; atualização disponível" : "manifesto lido; app em dia");
            if (!available) {
                reportUpdateNotification(serverUrl, "app_opened", true, "APK instalado já está na versão publicada");
            }
        }
        if (available && notificationRequested) {
            String notifyState = notifyUpdateAvailable();
            reportUpdateNotification(serverUrl, notifyState, "displayed".equals(notifyState) || "duplicate".equals(notifyState), notificationDetail(notifyState));
        }
        if (userVisible) {
            show(available ? "Atualização encontrada. Toque em Atualizar no topo do app." : "Nenhuma atualização nova encontrada.");
        }
    }

    private HttpResult fetchLatestManifest(String serverUrl) throws Exception {
        String[] paths = new String[] {
                "/core-worker/app/latest.json",
                "/core-worker/latest.json"
        };
        HttpResult first = null;
        for (String path : paths) {
            HttpResult result = request("GET", serverUrl + path, null, null);
            if (first == null) first = result;
            if (result.ok()) return result;
        }
        return first == null ? new HttpResult(0, "") : first;
    }

    private boolean isLatestUpdateAvailable() {
        if (latestVersionCode > BuildConfig.VERSION_CODE) {
            return true;
        }
        return latestVersionCode < 0 && latestVersionName != null && !latestVersionName.isEmpty() && !APP_VERSION.equals(latestVersionName);
    }

    private void downloadAndInstallUpdate() {
        String serverUrl = normalizedServerUrl();
        if (serverUrl.isEmpty()) {
            refreshLocalStatus("Servidor da VPS não configurado no APK.");
            toast("VPS não configurada no APK.");
            return;
        }
        if (updateDownloadBusy) {
            refreshLocalStatus("Download da atualização já está em andamento. Aguarde o status no aviso superior.");
            toast("Download já está em andamento.");
            return;
        }

        updateDownloadBusy = true;
        setButtonsEnabled(false);
        setUpdateActionState("Preparando atualização...\nVou buscar o latest.json, baixar o APK direto da VPS e abrir o instalador.", "Baixando...", true, true);
        refreshLocalStatus("Preparando download da atualização do Core Worker...");
        toast("Preparando download da atualização...");

        new Thread(() -> {
            try {
                reportUpdateNotification(serverUrl, "download_tap", true, "usuário tocou em Atualizar no APK");
                setUpdateActionState("Lendo manifesto latest.json da VPS...", "Baixando...", true, true);
                if (latestApkUrl == null || latestApkUrl.trim().isEmpty() || !latestUpdateAvailable) {
                    checkForUpdateInternal(serverUrl, false);
                }
                if (!latestUpdateAvailable) {
                    show("Este APK já está em dia.");
                    setUpdateActionState("Este APK já está em dia.", "Atualizar", false, false);
                    return;
                }
                if (latestApkUrl == null || latestApkUrl.trim().isEmpty()) {
                    String detail = "A VPS avisou atualização, mas o manifesto não trouxe downloadUrl/directApkUrl/apkUrl.";
                    show(detail);
                    setUpdateActionState("Falha: manifesto sem URL direta do APK.\nToque em Procurar atualização na VPS e tente novamente.", "Tentar novamente", true, false);
                    reportUpdateNotification(serverUrl, "download_failed", false, "manifesto sem downloadUrl/directApkUrl/apkUrl");
                    return;
                }

                String version = emptyFallback(latestVersionName, "nova versão");
                setUpdateActionState("Baixando Core Worker " + version + " direto da VPS...\nSe falhar, o erro aparecerá aqui.", "Baixando...", true, true);
                reportUpdateNotification(serverUrl, "download_started", true, "download direto iniciado pelo APK");
                File updateDir = ensureWritableUpdateDir();
                String apkName = safeLocalApkName();
                cleanupUpdateArtifactsKeeping(apkName, "pre_download");
                updateDir = ensureWritableUpdateDir();
                File apkFile = new File(updateDir, apkName);
                boolean reusedExisting = false;
                if (apkFile.exists() && latestApkSha256 != null && latestApkSha256.trim().matches("(?i)[a-f0-9]{64}")) {
                    setUpdateActionState("APK já baixado. Validando arquivo local antes de reutilizar...", "Validando...", true, true);
                    String actual = sha256Of(apkFile);
                    if (actual.equalsIgnoreCase(latestApkSha256.trim())) {
                        reusedExisting = true;
                    } else {
                        apkFile.delete();
                    }
                }
                if (!reusedExisting) {
                    updateDir = ensureWritableUpdateDir();
                    File partFile = new File(updateDir, apkName + ".download");
                    ensureParentDirectory(partFile);
                    if (partFile.exists()) partFile.delete();
                    downloadFile(latestApkUrl, partFile, (done, total) -> {
                        String progress = total > 0
                                ? "Baixando " + version + "... " + Math.max(0, Math.min(100, (int) ((done * 100L) / total))) + "% · " + formatBytes(done) + " / " + formatBytes(total)
                                : "Baixando " + version + "... " + formatBytes(done);
                        setUpdateActionState(progress, "Baixando...", true, true);
                    });
                    setUpdateActionState("Download concluído. Validando APK...", "Validando...", true, true);
                    if (latestApkSha256 != null && !latestApkSha256.trim().isEmpty()) {
                        String actual = sha256Of(partFile);
                        if (!actual.equalsIgnoreCase(latestApkSha256.trim())) {
                            partFile.delete();
                            String detail = "Atualização baixada, mas o hash não confere. Instalação bloqueada por segurança.";
                            show(detail);
                            setUpdateActionState("Falha: hash SHA-256 diferente do latest.json.\nInstalação bloqueada por segurança.", "Tentar novamente", true, false);
                            reportUpdateNotification(serverUrl, "download_failed", false, "sha256 divergente no APK baixado");
                            return;
                        }
                    }
                    if (apkFile.exists() && !apkFile.delete()) {
                        throw new Exception("não consegui substituir APK local antigo");
                    }
                    if (!partFile.renameTo(apkFile)) {
                        partFile.delete();
                        throw new Exception("não consegui finalizar arquivo local de atualização");
                    }
                }
                rememberPendingUpdateArtifact(apkFile);
                reportUpdateNotification(serverUrl, "download_verified", true, reusedExisting ? "APK local validado e reutilizado" : "APK baixado direto e sha256 validado");
                updateUpdateUi("Atualização pronta e verificada. Vou abrir o instalador do Android.\nArquivo: " + apkFile.getName() + "\nDepois de instalar, o APK novo limpará este instalador automaticamente.", true, true);
                setUpdateActionState("APK pronto e validado. Abrindo instalador do Android...", "Abrindo...", true, true);
                openApkInstaller(apkFile);
            } catch (Throwable exc) {
                String detail = exc.getClass().getSimpleName() + ": " + String.valueOf(exc.getMessage());
                reportUpdateNotification(serverUrl, "download_failed", false, detail);
                show("Falha ao atualizar: " + detail);
                setUpdateActionState("Falha ao baixar/abrir atualização.\n" + detail + "\nToque em Atualizar para tentar novamente.", "Tentar novamente", true, false);
            } finally {
                updateDownloadBusy = false;
                runOnUiThread(() -> setButtonsEnabled(true));
            }
        }).start();
    }


    private String safeLocalApkName() {
        String clean = (latestVersionName == null || latestVersionName.trim().isEmpty()) ? "update" : latestVersionName.trim();
        clean = clean.replaceAll("[^A-Za-z0-9._-]+", "-");
        return "CoreWorker-" + clean + ".apk";
    }

    private File primaryUpdateDir() {
        File filesBase = getExternalFilesDir(null);
        if (filesBase == null) {
            filesBase = getFilesDir();
        }
        return new File(filesBase, "updates");
    }

    private boolean ensureDirectory(File dir) {
        if (dir == null) return false;
        if (dir.exists()) return dir.isDirectory();
        try {
            return dir.mkdirs() || dir.exists();
        } catch (Throwable ignored) {
            return false;
        }
    }

    private File ensureWritableUpdateDir() throws Exception {
        File primary = primaryUpdateDir();
        if (ensureDirectory(primary)) return primary;

        File internal = new File(getFilesDir(), "updates");
        if (ensureDirectory(internal)) return internal;

        File cache = new File(getCacheDir(), "updates");
        if (ensureDirectory(cache)) return cache;

        throw new Exception("não consegui criar a pasta temporária de atualização");
    }

    private void ensureParentDirectory(File file) throws Exception {
        if (file == null) throw new Exception("arquivo temporário de update inválido");
        File parent = file.getParentFile();
        if (!ensureDirectory(parent)) {
            throw new Exception("não consegui recriar a pasta temporária de atualização");
        }
    }

    private File[] updateArtifactDirs() {
        return new File[] {
                primaryUpdateDir(),
                new File(getCacheDir(), "updates"),
                new File(getFilesDir(), "updates")
        };
    }

    private void rememberPendingUpdateArtifact(File apkFile) {
        try {
            prefs.edit()
                    .putInt("pending_update_version_code", latestVersionCode)
                    .putString("pending_update_version_name", latestVersionName == null ? "" : latestVersionName)
                    .putString("pending_update_apk_name", apkFile == null ? "" : apkFile.getName())
                    .putString("pending_update_sha256", latestApkSha256 == null ? "" : latestApkSha256)
                    .putLong("pending_update_saved_at", System.currentTimeMillis())
                    .apply();
        } catch (Throwable ignored) {
        }
    }

    private JSONObject cleanupUpdateArtifacts(boolean manual, String reason) {
        try {
            int pendingCode = prefs.getInt("pending_update_version_code", -1);
            String pendingName = prefs.getString("pending_update_apk_name", "");
            boolean keepPendingInstaller = pendingCode > BuildConfig.VERSION_CODE && pendingName != null && !pendingName.trim().isEmpty();
            long bytes = 0L;
            int files = 0;
            for (File dir : updateArtifactDirs()) {
                CleanupCount count = cleanupUpdateDir(dir, keepPendingInstaller ? pendingName.trim() : "");
                bytes += count.bytes;
                files += count.files;
            }
            if (!keepPendingInstaller) {
                prefs.edit()
                        .remove("pending_update_version_code")
                        .remove("pending_update_version_name")
                        .remove("pending_update_apk_name")
                        .remove("pending_update_sha256")
                        .remove("pending_update_saved_at")
                        .apply();
            }
            prefs.edit()
                    .putLong("update_cleanup_last_at", System.currentTimeMillis())
                    .putLong("update_cleanup_last_bytes", bytes)
                    .putInt("update_cleanup_last_files", files)
                    .putString("update_cleanup_last_reason", reason == null ? "" : reason)
                    .apply();
            internalStorageSummary = bytes > 0 ? "updates limpos " + humanBytes(bytes) : "updates sem lixo";
            if (manual) {
                show(bytes > 0
                        ? "Limpeza segura concluída. Liberei " + humanBytes(bytes) + " de instaladores/staging antigos."
                        : "Limpeza segura concluída. Não havia instaladores antigos para apagar.");
            }
            return new JSONObject()
                    .put("ok", true)
                    .put("bytesCleared", bytes)
                    .put("filesCleared", files)
                    .put("keptPendingInstaller", keepPendingInstaller)
                    .put("summary", bytes > 0 ? "updates limpos " + humanBytes(bytes) : "updates sem lixo");
        } catch (Throwable exc) {
            try {
                return new JSONObject()
                        .put("ok", false)
                        .put("error", shortThrowable(exc))
                        .put("summary", "limpeza de updates falhou");
            } catch (Throwable ignored) {
                return new JSONObject();
            }
        }
    }

    private long cleanupUpdateArtifactsKeeping(String keepName, String reason) {
        long bytes = 0L;
        int files = 0;
        try {
            for (File dir : updateArtifactDirs()) {
                CleanupCount count = cleanupUpdateDir(dir, keepName == null ? "" : keepName.trim());
                bytes += count.bytes;
                files += count.files;
            }
            prefs.edit()
                    .putLong("update_cleanup_last_at", System.currentTimeMillis())
                    .putLong("update_cleanup_last_bytes", bytes)
                    .putInt("update_cleanup_last_files", files)
                    .putString("update_cleanup_last_reason", reason == null ? "" : reason)
                    .apply();
        } catch (Throwable ignored) {
        }
        return bytes;
    }

    private CleanupCount cleanupUpdateDir(File dir, String keepName) {
        CleanupCount count = new CleanupCount();
        if (dir == null || !dir.exists()) return count;
        File[] children = dir.listFiles();
        if (children == null) return count;
        String keep = keepName == null ? "" : keepName.trim();
        for (File child : children) {
            if (child == null) continue;
            String name = child.getName();
            if (!keep.isEmpty() && keep.equals(name)) {
                continue;
            }
            CleanupCount childCount = deleteUpdateArtifact(child);
            count.bytes += childCount.bytes;
            count.files += childCount.files;
        }
        try {
            ensureDirectory(dir);
        } catch (Throwable ignored) {
        }
        return count;
    }

    private CleanupCount deleteUpdateArtifact(File file) {
        CleanupCount count = new CleanupCount();
        if (file == null || !file.exists()) return count;
        if (file.isDirectory()) {
            File[] children = file.listFiles();
            if (children != null) {
                for (File child : children) {
                    CleanupCount childCount = deleteUpdateArtifact(child);
                    count.bytes += childCount.bytes;
                    count.files += childCount.files;
                }
            }
        }
        try {
            count.bytes += Math.max(0L, file.length());
        } catch (Throwable ignored) {
        }
        try {
            if (file.delete()) count.files += 1;
        } catch (Throwable ignored) {
        }
        return count;
    }

    private JSONObject updateArtifactsSnapshot() throws Exception {
        JSONObject updates = new JSONObject();
        long bytes = 0L;
        int files = 0;
        JSONArray dirs = new JSONArray();
        for (File dir : updateArtifactDirs()) {
            if (dir == null) continue;
            long dirBytes = directorySize(dir);
            int dirFiles = directoryFileCount(dir);
            bytes += dirBytes;
            files += dirFiles;
            dirs.put(new JSONObject()
                    .put("name", dir.getName())
                    .put("exists", dir.exists())
                    .put("bytes", dirBytes)
                    .put("files", dirFiles));
        }
        updates.put("bytes", bytes);
        updates.put("files", files);
        updates.put("pendingVersionCode", prefs.getInt("pending_update_version_code", -1));
        updates.put("pendingVersionName", prefs.getString("pending_update_version_name", ""));
        updates.put("pendingApkName", prefs.getString("pending_update_apk_name", ""));
        updates.put("lastCleanupAt", prefs.getLong("update_cleanup_last_at", 0L));
        updates.put("lastCleanupBytes", prefs.getLong("update_cleanup_last_bytes", 0L));
        updates.put("lastCleanupFiles", prefs.getInt("update_cleanup_last_files", 0));
        updates.put("lastCleanupReason", prefs.getString("update_cleanup_last_reason", ""));
        updates.put("dirs", dirs);
        updates.put("summary", bytes > 0 ? "updates " + humanBytes(bytes) : "updates limpos");
        return updates;
    }

    private void runManualUpdateCleanup() {
        runBusy("Limpando instaladores antigos com segurança...", () -> {
            JSONObject cleanup = cleanupUpdateArtifacts(true, "manual_button");
            safePutPayload(cleanup, "storage", storageSnapshot());
            reportAppState("update_storage_cleanup", cleanup.optString("summary", "limpeza segura de updates"));
            updateUpdateUi("APK " + APP_VERSION + " · " + cleanup.optString("summary", "limpeza segura concluída"), latestUpdateAvailable, true);
        });
    }

    private static final class CleanupCount {
        long bytes = 0L;
        int files = 0;
    }

    private void updateUpdateUi(String value, boolean available, boolean refreshSummary) {
        latestUpdateAvailable = available;
        runOnUiThread(() -> {
            if (updateText != null) {
                updateText.setText(value);
            }
            if (updateBanner != null) {
                updateBanner.setVisibility(available ? View.VISIBLE : View.GONE);
            }
            if (updateBannerText != null) {
                String version = emptyFallback(latestVersionName, "nova versão");
                updateBannerText.setText("Atualização disponível: " + version + "\nBaixe direto da VPS e conclua pelo instalador do Android.");
            }
            applyUpdateButtonState(available, updateDownloadBusy ? "Baixando..." : "Atualizar agora", updateDownloadBusy);
            if (refreshSummary) {
                refreshLocalStatus(null);
            }
        });
    }

    private void setUpdateActionState(String message, String buttonText, boolean showBanner, boolean busy) {
        runOnUiThread(() -> {
            if (updateBanner != null) {
                updateBanner.setVisibility(showBanner ? View.VISIBLE : View.GONE);
            }
            if (updateBannerText != null && message != null && !message.trim().isEmpty()) {
                updateBannerText.setText(message);
            }
            applyUpdateButtonState(showBanner && latestUpdateAvailable, buttonText, busy);
            if (updateText != null && message != null && !message.trim().isEmpty()) {
                updateText.setText("APK " + APP_VERSION + " · " + message);
            }
        });
    }

    private void applyUpdateButtonState(boolean available, String text, boolean busy) {
        if (updateInstallButton == null) {
            return;
        }
        updateInstallButton.setText(emptyFallback(text, available ? "Atualizar agora" : "Atualizar"));
        updateInstallButton.setEnabled(available && !busy);
        updateInstallButton.setTextColor(buttonTextColors());
        updateInstallButton.setBackground(makeButtonBackground(available ? BUTTON_BG : Color.rgb(96, 110, 138), BUTTON_DISABLED_BG));
    }


    private String notifyUpdateAvailable() {
        try {
            String version = emptyFallback(latestVersionName, "nova versão");
            String notificationKey = emptyFallback(latestNotificationId, version);
            String already = prefs.getString("last_update_notification", "");
            if (notificationKey.equals(already)) {
                return "duplicate";
            }
            if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 4103);
                return "permission_missing";
            }
            NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (manager == null) {
                return "manager_unavailable";
            }
            String channelId = "core_worker_updates";
            if (Build.VERSION.SDK_INT >= 26) {
                NotificationChannel channel = new NotificationChannel(channelId, "Atualizações do Core Worker", NotificationManager.IMPORTANCE_DEFAULT);
                manager.createNotificationChannel(channel);
            }
            Intent open = new Intent(this, MainActivity.class);
            open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 23) {
                flags |= PendingIntent.FLAG_IMMUTABLE;
            }
            PendingIntent pending = PendingIntent.getActivity(this, 4102, open, flags);
            Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                    ? new Notification.Builder(this, channelId)
                    : new Notification.Builder(this);
            builder.setSmallIcon(android.R.drawable.stat_sys_download_done)
                    .setContentTitle("Atualização do Core Worker")
                    .setContentText("Versão " + version + " disponível")
                    .setContentIntent(pending)
                    .setAutoCancel(true);
            manager.notify(4102, builder.build());
            prefs.edit().putString("last_update_notification", notificationKey).apply();
            return "displayed";
        } catch (Throwable ignored) {
            return "failed";
        }
    }

    private String showInternalTestNotification() {
        try {
            if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 4103);
                return "permission_missing";
            }
            NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (manager == null) {
                return "manager_unavailable";
            }
            String channelId = "core_worker_updates";
            if (Build.VERSION.SDK_INT >= 26) {
                NotificationChannel channel = new NotificationChannel(channelId, "Atualizações do Core Worker", NotificationManager.IMPORTANCE_DEFAULT);
                manager.createNotificationChannel(channel);
            }
            Intent open = new Intent(this, MainActivity.class);
            open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 23) {
                flags |= PendingIntent.FLAG_IMMUTABLE;
            }
            PendingIntent pending = PendingIntent.getActivity(this, 4104, open, flags);
            Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                    ? new Notification.Builder(this, channelId)
                    : new Notification.Builder(this);
            builder.setSmallIcon(android.R.drawable.stat_sys_download_done)
                    .setContentTitle("Core Worker")
                    .setContentText("Notificação de teste do runtime interno")
                    .setContentIntent(pending)
                    .setAutoCancel(true);
            manager.notify(4104, builder.build());
            return "displayed";
        } catch (Throwable ignored) {
            return "failed";
        }
    }


    private String notificationDetail(String state) {
        if ("displayed".equals(state)) return "notificação local exibida pelo APK";
        if ("background_displayed".equals(state)) return "notificação local exibida por checagem com app fechado";
        if ("fcm_received".equals(state)) return "push FCM recebido pelo APK";
        if ("fcm_displayed".equals(state)) return "notificação exibida por push FCM";
        if ("fcm_permission_missing".equals(state)) return "push FCM recebido sem permissão para notificação visível";
        if ("background_duplicate".equals(state)) return "checagem com app fechado viu notificação já registrada";
        if ("background_permission_missing".equals(state)) return "checagem em segundo plano sem permissão de notificação";
        if ("background_failed".equals(state)) return "falha criando notificação em segundo plano";
        if ("duplicate".equals(state)) return "notificação já exibida/confirmada para essa versão";
        if ("permission_missing".equals(state)) return "permissão POST_NOTIFICATIONS ausente";
        if ("manager_unavailable".equals(state)) return "NotificationManager indisponível";
        if ("download_tap".equals(state)) return "usuário tocou no botão Atualizar";
        if ("download_started".equals(state)) return "download direto iniciado pelo APK";
        if ("download_verified".equals(state)) return "APK baixado e validado localmente";
        if ("install_permission_missing".equals(state)) return "permissão de instalação ausente";
        if ("install_intent_opened".equals(state)) return "instalador Android aberto";
        if ("install_direct_url_opened".equals(state)) return "URL direta do APK aberta como fallback";
        if ("install_intent_failed".equals(state)) return "falha abrindo instalador Android";
        if ("download_failed".equals(state)) return "falha no download direto";
        if ("failed".equals(state)) return "falha criando notificação local";
        return state;
    }

    private void migrateFcmSafetyStateForPatch52() {
        try {
            int migrated = prefs.getInt("fcm_patch52_migration_code", 0);
            if (migrated < BuildConfig.VERSION_CODE) {
                prefs.edit()
                        .putInt("fcm_patch52_migration_code", BuildConfig.VERSION_CODE)
                        .putBoolean("fcm_kill_switch", false)
                        .remove("fcm_disabled_until")
                        .putString("fcm_state", "não verificado")
                        .apply();
                fcmState = "não verificado";
                fcmDisabledUntil = 0L;
                fcmTokenPreview = tokenPreview(prefs.getString("fcm_token", ""));
            } else {
                fcmState = prefs.getString("fcm_state", "não verificado");
                fcmDisabledUntil = prefs.getLong("fcm_disabled_until", 0L);
                fcmTokenPreview = tokenPreview(prefs.getString("fcm_token", ""));
            }
        } catch (Throwable ignored) {
            fcmState = "não verificado";
            fcmDisabledUntil = 0L;
        }
    }

    private void scheduleFcmTokenRegistration(String reason) {
        try {
            mainHandler.postDelayed(() -> safeStartupTask(() -> registerFcmTokenAsync(reason)), FCM_STARTUP_DELAY_MS);
        } catch (Throwable ignored) {
        }
    }

    private void registerFcmTokenAsync(String reason) {
        if (!FCM_ENABLED_IN_APK) {
            markFcmState("desativado no build", "CORE_WORKER_FCM_ENABLED=false", false);
            return;
        }
        long disabledUntil = prefs.getLong("fcm_disabled_until", 0L);
        if (disabledUntil > System.currentTimeMillis()) {
            fcmDisabledUntil = disabledUntil;
            markFcmState("pausado temporariamente", "aguardando próxima tentativa segura", false);
            return;
        }
        String serverUrl = normalizedServerUrl();
        if (serverUrl == null || serverUrl.trim().isEmpty()) {
            markFcmState("aguardando VPS do build", "CORE_WORKER_VPS_URL vazio", false);
            return;
        }
        markFcmState("verificando", "preparando Firebase Messaging", false);
        try {
            if (!ensureFirebaseReady()) {
                return;
            }
            try {
                FirebaseMessaging.getInstance().setAutoInitEnabled(true);
            } catch (Throwable ignored) {
            }
            FirebaseMessaging.getInstance().getToken().addOnCompleteListener(task -> {
                try {
                    if (task == null || !task.isSuccessful()) {
                        Throwable err = task == null ? null : task.getException();
                        markFcmError("token indisponível", err, true);
                        reportAppState("fcm_token_failed", err == null ? "Firebase não retornou token" : shortThrowable(err));
                        return;
                    }
                    String token = task.getResult();
                    if (token == null || token.trim().isEmpty()) {
                        markFcmState("token vazio", "Firebase retornou token vazio", true);
                        reportAppState("fcm_token_failed", "Firebase retornou token vazio");
                        return;
                    }
                    prefs.edit()
                            .putString("fcm_token", token.trim())
                            .putBoolean("fcm_kill_switch", false)
                            .remove("fcm_disabled_until")
                            .putString("fcm_state", "ativo")
                            .apply();
                    fcmTokenPreview = tokenPreview(token);
                    fcmDisabledUntil = 0L;
                    fcmState = "ativo";
                    runOnUiThread(() -> refreshLocalStatus(null));
                    reportFcmToken(serverUrl, token, reason);
                } catch (Throwable err) {
                    markFcmError("falha registrando token", err, true);
                }
            });
        } catch (Throwable err) {
            markFcmError("falha inicializando FCM", err, true);
        }
    }

    private boolean ensureFirebaseReady() {
        try {
            FirebaseApp app;
            try {
                app = FirebaseApp.getInstance();
            } catch (Throwable missing) {
                app = FirebaseApp.initializeApp(getApplicationContext());
            }
            if (app == null) {
                markFcmState("Firebase não inicializado", "google-services não gerou opções válidas", true);
                return false;
            }
            return true;
        } catch (Throwable err) {
            markFcmError("Firebase indisponível", err, true);
            return false;
        }
    }

    private void markFcmState(String state, String detail, boolean temporaryPause) {
        String cleanState = emptyFallback(state, "não verificado");
        fcmState = cleanState;
        if (temporaryPause) {
            fcmDisabledUntil = System.currentTimeMillis() + FCM_DISABLED_MS;
        } else if (!"pausado temporariamente".equals(cleanState)) {
            fcmDisabledUntil = 0L;
        }
        try {
            SharedPreferences.Editor editor = prefs.edit()
                    .putString("fcm_state", cleanState)
                    .putString("fcm_last_detail", detail == null ? "" : detail);
            if (temporaryPause) {
                editor.putLong("fcm_disabled_until", fcmDisabledUntil);
            } else if (!"pausado temporariamente".equals(cleanState)) {
                editor.remove("fcm_disabled_until");
            }
            editor.apply();
        } catch (Throwable ignored) {
        }
        runOnUiThread(() -> refreshLocalStatus(null));
    }

    private void markFcmError(String state, Throwable err, boolean temporaryPause) {
        String detail = err == null ? "sem detalhe" : shortThrowable(err);
        markFcmState(state + " · " + detail, detail, temporaryPause);
    }

    private String shortThrowable(Throwable err) {
        if (err == null) return "erro desconhecido";
        String msg = String.valueOf(err.getMessage() == null ? "" : err.getMessage()).trim();
        String text = err.getClass().getSimpleName() + (msg.isEmpty() ? "" : ": " + msg);
        return text.length() > 160 ? text.substring(0, 160) : text;
    }

    private String tokenPreview(String token) {
        String clean = token == null ? "" : token.trim();
        if (clean.length() <= 12) return clean;
        return clean.substring(0, 6) + "…" + clean.substring(clean.length() - 4);
    }



    private void readForegroundRuntimeState() {
        try {
            foregroundRuntimeActive = prefs.getBoolean("foreground_runtime_active", false);
            foregroundRuntimeLastTickAt = prefs.getLong("foreground_runtime_last_tick_at", 0L);
            String detail = prefs.getString("foreground_runtime_state", "");
            if (foregroundRuntimeActive) {
                foregroundRuntimeSummary = detail == null || detail.trim().isEmpty()
                        ? "serviço persistente ativo"
                        : detail.trim();
            } else {
                foregroundRuntimeSummary = detail == null || detail.trim().isEmpty()
                        ? "serviço persistente parado"
                        : detail.trim();
            }
        } catch (Throwable exc) {
            foregroundRuntimeActive = false;
            foregroundRuntimeSummary = "serviço persistente indisponível · " + shortThrowable(exc);
        }
        updateSystemChecklistText();
    }

    private void startForegroundRuntimeFromUi() {
        try {
            JSONObject out = startForegroundRuntime("ui");
            show(out.optString("summary", "Runtime persistente solicitado."));
        } catch (Throwable exc) {
            show("Não consegui ativar runtime persistente: " + shortThrowable(exc));
        }
    }

    private void stopForegroundRuntimeFromUi() {
        try {
            JSONObject out = stopForegroundRuntime("ui");
            show(out.optString("summary", "Runtime persistente parado."));
        } catch (Throwable exc) {
            show("Não consegui parar runtime persistente: " + shortThrowable(exc));
        }
    }

    private JSONObject foregroundRuntimeSnapshot(String focus) throws Exception {
        readForegroundRuntimeState();
        JSONObject out = new JSONObject();
        out.put("ok", true);
        out.put("focus", focus == null ? "probe" : focus);
        out.put("active", foregroundRuntimeActive);
        out.put("lastTickAt", foregroundRuntimeLastTickAt);
        out.put("summary", foregroundRuntimeSummary == null ? "serviço persistente aguardando" : foregroundRuntimeSummary);
        out.put("mode", "foreground-service-visible-runtime");
        out.put("androidModel", Build.MANUFACTURER + " " + Build.MODEL);
        out.put("safety", "Foreground Service visível; sem instalar rootfs, sem baixar Bedrock, sem confirmação automática de termos e sem shell livre");
        return out;
    }

    private JSONObject startForegroundRuntime(String reason) throws Exception {
        Intent intent = new Intent(this, CoreWorkerRuntimeService.class);
        intent.setAction(CoreWorkerRuntimeService.ACTION_START);
        intent.putExtra("reason", reason == null ? "manual" : reason);
        if (Build.VERSION.SDK_INT >= 26) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }
        prefs.edit()
                .putBoolean("foreground_runtime_active", true)
                .putString("foreground_runtime_state", "serviço persistente solicitado")
                .putLong("foreground_runtime_last_requested_at", System.currentTimeMillis())
                .apply();
        readForegroundRuntimeState();
        JSONObject out = foregroundRuntimeSnapshot("start");
        out.put("started", true);
        out.put("summary", "Runtime persistente solicitado com notificação fixa");
        return out;
    }

    private JSONObject stopForegroundRuntime(String reason) throws Exception {
        Intent intent = new Intent(this, CoreWorkerRuntimeService.class);
        intent.setAction(CoreWorkerRuntimeService.ACTION_STOP);
        intent.putExtra("reason", reason == null ? "manual" : reason);
        startService(intent);
        prefs.edit()
                .putBoolean("foreground_runtime_active", false)
                .putString("foreground_runtime_state", "serviço persistente parado")
                .putLong("foreground_runtime_last_requested_at", System.currentTimeMillis())
                .apply();
        readForegroundRuntimeState();
        JSONObject out = foregroundRuntimeSnapshot("stop");
        out.put("stopped", true);
        out.put("summary", "Runtime persistente parado; jobs curtos continuam por fetch manual/agendado");
        return out;
    }


    private JSONObject coreLinuxPublicSnapshotSafe() {
        try {
            return coreLinuxPublicSnapshot();
        } catch (Throwable ignored) {
            return new JSONObject();
        }
    }

    private void refreshBedrockVisualState() {
        refreshRootfsStateFromDisk();
        runOnUiThread(() -> {
            updateRootfsUi();
            boolean running = bedrockRuntimeServiceActive || (bedrockRuntimeState != null && bedrockRuntimeState.toLowerCase(Locale.ROOT).contains("ativo"));
            boolean ready = bedrockReady || (bedrockSummary != null && bedrockSummary.toLowerCase(Locale.ROOT).contains("pronto"));
            String status;
            if (running) {
                status = "Rodando";
            } else if (ready) {
                status = "Pronto para ligar";
            } else if (bedrockSummary != null && bedrockSummary.toLowerCase(Locale.ROOT).contains("instalado")) {
                status = "Instalado · configuração pendente";
            } else {
                status = "Não instalado";
            }
            if (bedrockHeroStatusText != null) {
                bedrockHeroStatusText.setText(status);
            }
            if (bedrockReadinessText != null) {
                StringBuilder builder = new StringBuilder();
                boolean rootfsReal = rootfsState != null && rootfsState.toLowerCase(Locale.ROOT).contains("rootfs_real_validated");
                builder.append(running ? "Servidor ativo. Console disponível em Avançado." : (ready ? "Servidor pronto. Use o switch para ligar." : "Servidor ainda bloqueado para segurança."));
                builder.append("\n");
                builder.append("Rootfs: ").append(rootfsReal ? "real validado" : emptyFallback(rootfsSummary, "em preparação"));
                builder.append("\n");
                builder.append("Runner: bloqueado nesta etapa");
                builder.append("\n");
                builder.append("Próxima ação: ").append(rootfsReal ? "runner preflight futuro" : emptyFallback(bedrockInstallerNextAction, "validar rootfs"));
                bedrockReadinessText.setText(builder.toString());
            }
            if (bedrockServerSwitch != null) {
                suppressBedrockSwitchEvents = true;
                bedrockServerSwitch.setChecked(running);
                bedrockServerSwitch.setEnabled(running || ready);
                bedrockServerSwitch.setText(running ? "ON" : "OFF");
                suppressBedrockSwitchEvents = false;
            }
            if (bedrockTerminalText != null && (bedrockTerminalText.getText() == null || bedrockTerminalText.getText().toString().contains("servidor desligado"))) {
                bedrockTerminalText.setText(running
                        ? "Core Bedrock Console\n$ servidor conectado · digite comandos do Bedrock"
                        : "Core Bedrock Console\n$ servidor desligado · prepare/inicie para enviar comandos");
            }
        });
    }

    private void handleBedrockSwitchChange(boolean isChecked) {
        if (isChecked) {
            if (!bedrockReady) {
                suppressBedrockSwitchEvents = true;
                if (bedrockServerSwitch != null) {
                    bedrockServerSwitch.setChecked(false);
                    bedrockServerSwitch.setText("OFF");
                }
                suppressBedrockSwitchEvents = false;
                showBedrockPrepareDialog();
                return;
            }
            confirmStartBedrockRuntimeFromSwitch();
        } else if (bedrockRuntimeServiceActive) {
            stopBedrockRuntimeFromUi();
        }
    }

    private void showBedrockPrepareDialog() {
        new AlertDialog.Builder(this)
                .setTitle("Servidor ainda não está pronto")
                .setMessage("Antes de ligar, o Core Worker precisa preparar ambiente, arquivos e preflight. Use Preparar servidor ou Testar servidor para ver pendências.")
                .setPositiveButton("Preparar servidor", (dialog, which) -> prepareBedrockServerFromUi())
                .setNegativeButton("Agora não", null)
                .show();
    }

    private void confirmStartBedrockRuntimeFromSwitch() {
        new AlertDialog.Builder(this)
                .setTitle("Ligar servidor Bedrock?")
                .setMessage("O app vai ativar o runtime Bedrock assistido com serviço visível. Se ainda faltar algo no ambiente, o start real continuará bloqueado com segurança.")
                .setPositiveButton("Ligar", (dialog, which) -> startBedrockRuntimeFromUi())
                .setNegativeButton("Cancelar", (dialog, which) -> refreshBedrockVisualState())
                .show();
    }

    private void testBedrockServerFromUi() {
        long now = System.currentTimeMillis();
        if (now - bedrockLastManualTestAt < BEDROCK_TEST_MIN_INTERVAL_MS) {
            refreshLocalStatus("Aguarde um instante antes de testar de novo.");
            return;
        }
        bedrockLastManualTestAt = now;
        if (!bedrockProbeRunning.compareAndSet(false, true)) {
            refreshLocalStatus("Teste Bedrock já está em andamento. Aguarde o resultado atual.");
            return;
        }
        startupLog("bedrock:test:start isolated-static");
        refreshLocalStatus("Testando Bedrock em modo seguro: sem rootfs, Python, Termux, Box64 ou serviço.");
        if (bedrockTestAllButton != null) bedrockTestAllButton.setEnabled(false);
        Thread worker = new Thread(() -> {
            String summary = "Diagnóstico Bedrock leve concluído.";
            try {
                JSONObject result = bedrockServerLightweightTestSnapshot();
                summary = result.optString("summary", summary);
                startupLog("bedrock:test:ok " + summary);
                appendBedrockTerminal("test", summary);
                exportBedrockDebugSnapshot("manual-test", result);
            } catch (Throwable exc) {
                summary = "Diagnóstico Bedrock protegido falhou: " + shortThrowable(exc);
                appStatusLastError = summary;
                startupLog("bedrock:test:fail " + summary);
                appendBedrockTerminal("test", summary);
                exportBedrockDebugSnapshot("manual-test-failed", null);
            } finally {
                final String finalSummary = summary;
                bedrockProbeRunning.set(false);
                runOnUiThread(() -> {
                    if (activityDestroyed) return;
                    if (bedrockTestAllButton != null) bedrockTestAllButton.setEnabled(true);
                    refreshLocalStatus(finalSummary);
                    refreshBedrockVisualState();
                });
            }
        }, "core-worker-bedrock-static-test");
        worker.setDaemon(true);
        worker.start();
    }

    private interface BedrockProbeStep {
        JSONObject run() throws Exception;
    }

    private JSONObject bedrockServerSafeTestSnapshot() throws Exception {
        JSONObject out = bedrockServerLightweightTestSnapshot();
        out.put("safeSnapshot", true);
        out.put("heavyStepsSkipped", true);
        return out;
    }


    private JSONObject bedrockServerLightweightTestSnapshot() throws Exception {
        long startedAt = System.currentTimeMillis();
        prepareCoreLinuxRuntimeStateWithoutRecursiveProbe();

        File core = coreLinuxDir();
        File rootfs = new File(core, "rootfs");
        File runtime = new File(core, "runtime");
        File bedrock = new File(core, "bedrock");
        File provision = new File(core, "provision");
        File logs = new File(core, "logs");
        File serverProperties = new File(bedrock, "server.properties");
        File serverPropertiesTemplate = new File(bedrock, "server.properties.template");
        File server = new File(bedrock, "bedrock_server");
        File box64A = new File(core, "bin/box64");
        File box64B = new File(core, "box64/box64");
        File rootfsReadyMarker = new File(rootfs, ".core-worker-rootfs-ready");
        File rootfsState = new File(runtime, "rootfs-state.json");
        File nativeExecutorState = new File(runtime, "native-executor-state.json");
        File appNativeExecutor = new File(getApplicationInfo() == null || getApplicationInfo().nativeLibraryDir == null ? "" : getApplicationInfo().nativeLibraryDir, "libcoreworker_executor.so");

        boolean rootfsDir = safeDirectoryExists(rootfs);
        boolean rootfsReady = safeFileExists(rootfsReadyMarker) || safeTextContains(rootfsState, "\"rootfsReady\"", "true");
        boolean executorBundled = safeFileExists(appNativeExecutor);
        boolean executorStateOk = safeTextContains(nativeExecutorState, "\"ok\"", "true") || safeTextContains(nativeExecutorState, "readyForRootfs", "true");
        boolean serverPropertiesReady = safeFileExists(serverProperties) || safeFileExists(serverPropertiesTemplate);
        boolean serverInstalled = safeFileExists(server);
        boolean box64Ready = safeFileExists(box64A) || safeFileExists(box64B);

        JSONArray blockers = new JSONArray();
        if (!rootfsDir) blockers.put("rootfs ausente");
        if (!rootfsReady) blockers.put("rootfs ainda não validado");
        if (!executorBundled && !executorStateOk) blockers.put("executor interno ainda não confirmado");
        if (!serverPropertiesReady) blockers.put("server.properties ainda não preparado");
        if (!serverInstalled) blockers.put("bedrock_server não instalado");
        if (!box64Ready) blockers.put("Box64 pendente");
        if (BEDROCK_RUNTIME_ISOLATED) blockers.put("runtime Bedrock isolado nesta versão para evitar crash/ANR");

        boolean filesReady = blockers.length() == (BEDROCK_RUNTIME_ISOLATED ? 1 : 0);
        boolean ready = !BEDROCK_RUNTIME_ISOLATED && filesReady;
        String rootfsStateLabel = !rootfsDir ? "rootfs_missing" : (rootfsReady ? "rootfs_ready" : "rootfs_detected");
        String bedrockStateLabel = ready ? "bedrock_ready" : "bedrock_runtime_isolated";
        String nextAction = BEDROCK_RUNTIME_ISOLATED
                ? "corrigir estabilidade antes de reativar runtime Bedrock"
                : (ready ? "ativar runtime Bedrock" : bedrockNextAction(rootfsReady, executorBundled || executorStateOk, serverPropertiesReady, serverInstalled, box64Ready));
        String summary = BEDROCK_RUNTIME_ISOLATED
                ? "Diagnóstico parcial concluído. Runtime Bedrock ainda não está pronto."
                : (ready ? "Bedrock pronto para preflight do runner." : "Diagnóstico parcial concluído. Runtime Bedrock ainda não está pronto.");

        JSONObject rootfsJson = new JSONObject();
        rootfsJson.put("state", rootfsStateLabel);
        rootfsJson.put("dir", rootfs.getAbsolutePath());
        rootfsJson.put("exists", rootfsDir);
        rootfsJson.put("ready", rootfsReady);
        rootfsJson.put("stateFile", safeFileStatus(rootfsState));
        rootfsJson.put("readyMarker", safeFileStatus(rootfsReadyMarker));
        rootfsJson.put("pythonTouched", false);
        rootfsJson.put("prootStarted", false);

        JSONObject executorJson = new JSONObject();
        executorJson.put("state", executorBundled || executorStateOk ? "executor_detected" : "executor_missing");
        executorJson.put("bundledLibrary", safeFileStatus(appNativeExecutor));
        executorJson.put("stateFile", safeFileStatus(nativeExecutorState));
        executorJson.put("validated", executorStateOk);
        executorJson.put("note", "Teste Bedrock não chama JNI/Python/Termux para proteger a interface.");

        JSONObject bedrockJson = new JSONObject();
        bedrockJson.put("state", bedrockStateLabel);
        bedrockJson.put("dir", bedrock.getAbsolutePath());
        bedrockJson.put("properties", safeFileStatus(serverProperties));
        bedrockJson.put("propertiesTemplate", safeFileStatus(serverPropertiesTemplate));
        bedrockJson.put("server", safeFileStatus(server));
        bedrockJson.put("box64", safeFileExists(box64A) ? safeFileStatus(box64A) : safeFileStatus(box64B));
        bedrockJson.put("ready", ready);
        bedrockJson.put("isolationMode", BEDROCK_RUNTIME_ISOLATED);

        JSONObject out = new JSONObject();
        out.put("ok", true);
        out.put("ready", ready);
        out.put("filesReady", filesReady);
        out.put("state", bedrockStateLabel);
        out.put("summary", summary);
        out.put("nextAction", nextAction);
        out.put("rootfs", rootfsJson);
        out.put("executor", executorJson);
        out.put("bedrock", bedrockJson);
        out.put("provision", safeFileStatus(provision));
        out.put("blockers", blockers);
        out.put("durationMs", Math.max(0L, System.currentTimeMillis() - startedAt));
        out.put("uiSafe", true);
        out.put("isolationMode", BEDROCK_RUNTIME_ISOLATED);
        out.put("pythonTouched", false);
        out.put("nativeTouched", false);
        out.put("termuxTouched", false);
        out.put("serviceStarted", false);

        bedrockReady = ready;
        bedrockState = bedrockStateLabel;
        bedrockSummary = summary;
        bedrockInstallerSummary = ready ? "preflight local pronto" : "pendências locais encontradas pelo diagnóstico seguro";
        bedrockInstallerState = ready ? "ready" : "blocked";
        bedrockInstallerNextAction = nextAction;
        bedrockRuntimeSummary = BEDROCK_RUNTIME_ISOLATED ? BEDROCK_ISOLATION_SUMMARY : (ready ? emptyFallback(bedrockRuntimeSummary, "runtime Bedrock aguardando start") : "runtime Bedrock bloqueado até concluir pendências");
        bedrockRuntimeState = BEDROCK_RUNTIME_ISOLATED ? "isolated" : bedrockRuntimeState;
        bedrockRuntimeServiceActive = false;
        bedrockLastCheckAt = System.currentTimeMillis();
        internalDiagnosticsSummary = summary;
        internalDiagnosticsLastAt = System.currentTimeMillis();

        appendBoundedTextFile(new File(logs, "bedrock-test.log"), out.toString() + "\n", BEDROCK_SAFE_TEST_LOG_LIMIT_BYTES);
        appendBoundedTextFile(new File(logs, "rootfs-check.log"), rootfsJson.toString() + "\n", BEDROCK_SAFE_TEST_LOG_LIMIT_BYTES);
        updateSystemChecklistText();
        return out;
    }

    private void exportBedrockDebugSnapshot(String reason, JSONObject snapshot) {
        try {
            File dir = exportDiagnosticsDir();
            if (dir == null) return;
            JSONObject out = new JSONObject();
            out.put("reason", reason == null ? "manual" : reason);
            out.put("createdAt", System.currentTimeMillis());
            out.put("appVersion", APP_VERSION);
            out.put("appVersionCode", BuildConfig.VERSION_CODE);
            out.put("bedrockState", bedrockState == null ? "" : bedrockState);
            out.put("bedrockSummary", bedrockSummary == null ? "" : bedrockSummary);
            out.put("bedrockRuntimeState", bedrockRuntimeState == null ? "" : bedrockRuntimeState);
            out.put("bedrockRuntimeSummary", bedrockRuntimeSummary == null ? "" : bedrockRuntimeSummary);
            out.put("isolationMode", BEDROCK_RUNTIME_ISOLATED);
            out.put("lastError", appStatusLastError == null ? "" : appStatusLastError);
            if (snapshot != null) out.put("snapshot", snapshot);
            File file = new File(dir, "bedrock-safe-state.json");
            writeTextFile(file, out.toString(2));
            appendBoundedTextFile(new File(dir, "bedrock-safe-events.log"), out.toString() + "\n", BEDROCK_SAFE_TEST_LOG_LIMIT_BYTES);
        } catch (Throwable exc) {
            startupLog("bedrock:export-failed " + shortThrowable(exc));
        }
    }

    private File exportDiagnosticsDir() {
        File[] candidates = new File[] {
                new File("/sdcard/Download/CoreWorker"),
                getExternalFilesDir("CoreWorkerDiagnostics"),
                new File(getFilesDir(), "core-linux/logs/export")
        };
        for (File dir : candidates) {
            try {
                if (dir == null) continue;
                if (!dir.exists()) dir.mkdirs();
                if (dir.exists() && dir.isDirectory() && dir.canWrite()) return dir;
            } catch (Throwable ignored) {
            }
        }
        return null;
    }

    private String readTextFileTail(File file, int limit) {
        try {
            if (file == null || !file.exists() || !file.isFile()) return "";
            BufferedReader reader = new BufferedReader(new InputStreamReader(new FileInputStream(file), StandardCharsets.UTF_8));
            StringBuilder builder = new StringBuilder();
            String line;
            int max = Math.max(256, limit);
            while ((line = reader.readLine()) != null) {
                builder.append(line).append('\n');
                if (builder.length() > max * 2) {
                    builder.delete(0, builder.length() - max);
                }
            }
            reader.close();
            String text = builder.toString().trim();
            return text.length() > max ? text.substring(text.length() - max) : text;
        } catch (Throwable ignored) {
            return "";
        }
    }

    private JSONObject readJsonFile(File file) {
        try {
            String text = readTextFileTail(file, 64 * 1024);
            if (text == null || text.trim().isEmpty()) return new JSONObject();
            return new JSONObject(text);
        } catch (Throwable ignored) {
            return new JSONObject();
        }
    }

    private String bedrockNextAction(boolean rootfsReady, boolean executorReady, boolean propertiesReady, boolean serverInstalled, boolean box64Ready) {
        if (!executorReady) return "validar executor interno";
        if (!rootfsReady) return "preparar rootfs interno assistido";
        if (!propertiesReady) return "preparar server.properties";
        if (!serverInstalled) return "instalar Bedrock oficial com confirmação";
        if (!box64Ready) return "preparar Box64";
        return "ativar runtime Bedrock";
    }

    private JSONObject safeFileStatus(File file) {
        JSONObject out = new JSONObject();
        try {
            out.put("path", file == null ? "" : file.getAbsolutePath());
            boolean exists = file != null && file.exists();
            out.put("exists", exists);
            out.put("directory", exists && file.isDirectory());
            out.put("file", exists && file.isFile());
            out.put("bytes", exists && file.isFile() ? Math.max(0L, file.length()) : 0L);
        } catch (Throwable ignored) {
        }
        return out;
    }

    private boolean safeFileExists(File file) {
        try {
            return file != null && file.exists() && file.isFile();
        } catch (Throwable ignored) {
            return false;
        }
    }

    private boolean safeDirectoryExists(File file) {
        try {
            return file != null && file.exists() && file.isDirectory();
        } catch (Throwable ignored) {
            return false;
        }
    }

    private boolean safeTextContains(File file, String... needles) {
        try {
            if (file == null || !file.exists() || !file.isFile()) return false;
            BufferedReader reader = new BufferedReader(new InputStreamReader(new FileInputStream(file), StandardCharsets.UTF_8));
            StringBuilder builder = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null && builder.length() < 12000) {
                builder.append(line).append('\n');
            }
            reader.close();
            String text = builder.toString().toLowerCase(Locale.ROOT).replace(" ", "");
            if (needles == null || needles.length == 0) return !text.isEmpty();
            for (String needle : needles) {
                String clean = String.valueOf(needle == null ? "" : needle).toLowerCase(Locale.ROOT).replace(" ", "");
                if (!clean.isEmpty() && !text.contains(clean)) return false;
            }
            return true;
        } catch (Throwable ignored) {
            return false;
        }
    }

    private void appendBoundedTextFile(File file, String value, long maxBytes) {
        try {
            File parent = file == null ? null : file.getParentFile();
            if (parent != null && !parent.exists()) parent.mkdirs();
            if (file != null && file.exists() && file.length() > Math.max(1024L, maxBytes)) {
                File old = new File(file.getParentFile(), file.getName() + ".old");
                try { if (old.exists()) old.delete(); } catch (Throwable ignored) {}
                try {
                    if (!file.renameTo(old)) {
                        writeTextFile(file, "");
                    }
                } catch (Throwable ignored) {
                    writeTextFile(file, "");
                }
            }
            FileOutputStream out = new FileOutputStream(file, true);
            out.write(String.valueOf(value == null ? "" : value).getBytes(StandardCharsets.UTF_8));
            out.flush();
            out.close();
        } catch (Throwable ignored) {
        }
    }

    private void collectBedrockStepWarning(JSONArray warnings, String label, JSONObject step) {
        if (warnings == null || step == null) return;
        if (!step.optBoolean("ok", false) || step.optBoolean("timeout", false)) {
            JSONObject warning = new JSONObject();
            try {
                warning.put("step", label);
                warning.put("summary", step.optString("summary", step.optString("error", "pendente")));
                warnings.put(warning);
            } catch (Throwable ignored) {
            }
        }
    }

    private JSONObject runBedrockStepWithTimeout(String label, BedrockProbeStep step, long timeoutMs) {
        final JSONObject[] result = new JSONObject[1];
        final Throwable[] error = new Throwable[1];
        Thread thread = new Thread(() -> {
            try {
                result[0] = step.run();
            } catch (Throwable exc) {
                error[0] = exc;
            }
        }, "core-worker-bedrock-probe-" + sanitizeThreadName(label));
        thread.setDaemon(true);
        thread.start();
        try {
            thread.join(Math.max(1000L, timeoutMs));
        } catch (InterruptedException exc) {
            Thread.currentThread().interrupt();
            return bedrockStepError(label, "interrompido", exc);
        }
        if (thread.isAlive()) {
            return bedrockStepTimeout(label, timeoutMs);
        }
        if (error[0] != null) {
            return bedrockStepError(label, "falha", error[0]);
        }
        return result[0] == null ? bedrockStepError(label, "resultado vazio", null) : result[0];
    }

    private JSONObject bedrockStepTimeout(String label, long timeoutMs) {
        JSONObject out = new JSONObject();
        try {
            out.put("ok", false);
            out.put("step", label == null ? "unknown" : label);
            out.put("timeout", true);
            out.put("timeoutMs", timeoutMs);
            out.put("summary", "diagnóstico Bedrock demorou demais em " + (label == null ? "etapa" : label));
            out.put("error", "timeout");
        } catch (Throwable ignored) {
        }
        return out;
    }

    private JSONObject bedrockStepError(String label, String summary, Throwable exc) {
        JSONObject out = new JSONObject();
        try {
            out.put("ok", false);
            out.put("step", label == null ? "unknown" : label);
            out.put("summary", summary == null ? "falha Bedrock" : summary);
            out.put("error", exc == null ? "erro desconhecido" : shortThrowable(exc));
        } catch (Throwable ignored) {
        }
        return out;
    }

    private String firstNonEmpty(String... values) {
        if (values == null) return "";
        for (String value : values) {
            if (value != null && !value.trim().isEmpty()) {
                return value.trim();
            }
        }
        return "";
    }

    private String sanitizeThreadName(String value) {
        String text = value == null ? "step" : value.toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9_-]", "-");
        return text.isEmpty() ? "step" : text;
    }

    private void prepareBedrockServerFromUi() {
        runBusy("Preparando servidor Bedrock em modo seguro...", () -> {
            prepareCoreLinuxRuntimeStateWithoutRecursiveProbe();
            writeCoreLinuxProvisionerFiles(coreLinuxDir());
            JSONObject snapshot = bedrockServerLightweightTestSnapshot();
            String summary = "Arquivos base preparados. " + snapshot.optString("summary", BEDROCK_ISOLATION_SUMMARY);
            appendBedrockTerminal("prepare", summary);
            refreshLocalStatus(summary);
            exportBedrockDebugSnapshot("prepare-safe", snapshot);
        });
    }


    private void showBedrockFilesFromUi() {
        File bedrockDir = new File(coreLinuxDir(), "bedrock");
        String message = "Arquivos do servidor\n"
                + "server.properties · logs · worlds · backups\n"
                + bedrockDir.getAbsolutePath();
        appendBedrockTerminal("files", "área de arquivos controlada: server.properties, logs, worlds, backups");
        new AlertDialog.Builder(this)
                .setTitle("Arquivos Bedrock")
                .setMessage(message)
                .setPositiveButton("OK", null)
                .show();
    }

    private void sendBedrockConsoleCommandFromUi() {
        String command = bedrockCommandInput == null ? "" : bedrockCommandInput.getText().toString().trim();
        if (command.isEmpty()) {
            refreshLocalStatus("Digite um comando do console Bedrock.");
            return;
        }
        if (bedrockCommandInput != null) {
            bedrockCommandInput.setText("");
        }
        handleBedrockConsoleCommand(command);
    }


    private void handleBedrockConsoleCommand(String rawCommand) {
        String command = rawCommand == null ? "" : rawCommand.trim();
        if (command.isEmpty()) {
            refreshLocalStatus("Digite um comando do console Bedrock.");
            return;
        }
        String normalized = command.toLowerCase(Locale.ROOT);
        if ("clear".equals(normalized) || "limpar".equals(normalized)) {
            clearBedrockTerminalLogs();
            refreshLocalStatus("Terminal limpo.");
            return;
        }
        if ("copy".equals(normalized) || "copiar".equals(normalized)) {
            copyBedrockTerminalLogsFromUi();
            appendBedrockTerminal(command, "logs copiados para a área de transferência");
            return;
        }
        if ("help".equals(normalized) || "ajuda".equals(normalized)) {
            appendBedrockTerminal(command, "comandos seguros: help, status, logs, test, prepare, clear, copy. Comandos reais do Bedrock ficam bloqueados até o runtime estar validado.");
            refreshLocalStatus("Ajuda do terminal exibida.");
            return;
        }
        if ("status".equals(normalized) || "info".equals(normalized)) {
            String summary = "Bedrock: " + emptyFallback(bedrockSummary, "aguardando diagnóstico")
                    + " · runtime: " + emptyFallback(bedrockRuntimeSummary, "parado")
                    + " · próxima ação: " + emptyFallback(bedrockInstallerNextAction, "validar requisitos");
            appendBedrockTerminal(command, summary);
            refreshLocalStatus(summary);
            return;
        }
        if ("logs".equals(normalized) || "log".equals(normalized)) {
            refreshBedrockRuntimeLogsFromUi();
            return;
        }
        if ("test".equals(normalized) || "teste".equals(normalized)) {
            appendBedrockTerminal(command, "iniciando diagnóstico seguro pelo botão interno");
            testBedrockServerFromUi();
            return;
        }
        if ("prepare".equals(normalized) || "preparar".equals(normalized)) {
            appendBedrockTerminal(command, "preparando arquivos base em modo seguro");
            prepareBedrockServerFromUi();
            return;
        }
        String summary = "ignorado para proteger a interface · runtime Bedrock ainda isolado";
        appendBedrockTerminal(command, summary);
        refreshLocalStatus(summary);
        exportBedrockDebugSnapshot("console-command-blocked", null);
    }


    private void appendBedrockTerminal(String command, String response) {
        String safeCommand = sanitizeCommandOutput(command == null ? "" : command, 120);
        String safeResponse = sanitizeCommandOutput(response == null ? "" : response, 420);
        String line = System.currentTimeMillis() + " > " + safeCommand + "\n"
                + System.currentTimeMillis() + " $ " + safeResponse + "\n";
        appendBoundedTextFile(terminalLogFile(), line, BEDROCK_SAFE_TEST_LOG_LIMIT_BYTES);
        refreshBedrockTerminalViews();
    }


    private void appendCoreTerminalEvent(String label, String message) {
        String cleanMessage = message == null ? "" : message.trim();
        if (cleanMessage.isEmpty()) return;
        long now = System.currentTimeMillis();
        String key = (label == null ? "status" : label.trim()) + ":" + cleanMessage;
        if (key.equals(lastTerminalStatusLine) && now - lastTerminalStatusAt < 1800L) {
            return;
        }
        lastTerminalStatusLine = key;
        lastTerminalStatusAt = now;
        appendBedrockTerminal(label == null ? "status" : label, cleanMessage);
    }


    private File terminalLogFile() {
        return new File(new File(getFilesDir(), "core-linux/logs"), "bedrock-terminal.log");
    }


    private String terminalDefaultText() {
        return "Core Bedrock Console\n$ servidor desligado · prepare/inicie para enviar comandos";
    }


    private String readBedrockTerminalTail() {
        File file = terminalLogFile();
        if (!file.exists()) {
            return terminalDefaultText();
        }
        String value = readTextFileTail(file, 7000).trim();
        if (value.isEmpty()) {
            return terminalDefaultText();
        }
        return "Core Bedrock Console\n" + value;
    }


    private void refreshBedrockTerminalViews() {
        runOnUiThread(() -> {
            if (activityDestroyed) return;
            String text = readBedrockTerminalTail();
            if (bedrockTerminalText != null) {
                bedrockTerminalText.setText(text);
            }
            if (bedrockFullTerminalText != null) {
                bedrockFullTerminalText.setText(text);
            }
        });
    }


    private void clearBedrockTerminalLogs() {
        try {
            writeTextFile(terminalLogFile(), "");
        } catch (Throwable exc) {
            startupLog("terminal:clear-failed " + shortThrowable(exc));
        }
        refreshBedrockTerminalViews();
    }


    private void copyBedrockTerminalLogsFromUi() {
        copyToClipboard("core-worker-bedrock-terminal.log", readBedrockTerminalTail());
    }


    private void showBedrockTerminalFullScreen() {
        runOnUiThread(() -> {
            if (activityDestroyed) return;
            if (bedrockFullTerminalDialog != null && bedrockFullTerminalDialog.isShowing()) {
                refreshBedrockTerminalViews();
                return;
            }
            Dialog dialog = new Dialog(this);
            dialog.requestWindowFeature(Window.FEATURE_NO_TITLE);
            bedrockFullTerminalDialog = dialog;

            LinearLayout root = new LinearLayout(this);
            root.setOrientation(LinearLayout.VERTICAL);
            root.setPadding(dp(14), dp(14), dp(14), dp(14));
            root.setBackgroundColor(BG);

            LinearLayout header = new LinearLayout(this);
            header.setOrientation(LinearLayout.HORIZONTAL);
            header.setGravity(Gravity.CENTER_VERTICAL);
            TextView title = sectionTitle("▣ Terminal do Core Worker");
            header.addView(title, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));

            Button copy = compactButton("Copiar");
            copy.setOnClickListener(v -> copyBedrockTerminalLogsFromUi());
            header.addView(copy, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT));

            Button minimize = compactButton("Mínimo");
            minimize.setOnClickListener(v -> dialog.dismiss());
            LinearLayout.LayoutParams minimizeParams = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
            minimizeParams.setMargins(dp(8), 0, 0, 0);
            header.addView(minimize, minimizeParams);
            root.addView(header);

            TextView hint = smallText("Logs ao vivo e comandos seguros. Shell Android livre continua bloqueado.");
            root.addView(hint);

            ScrollView scroll = new ScrollView(this);
            scroll.setFillViewport(true);
            bedrockFullTerminalText = terminalText();
            bedrockFullTerminalText.setMaxLines(Integer.MAX_VALUE);
            bedrockFullTerminalText.setText(readBedrockTerminalTail());
            scroll.addView(bedrockFullTerminalText, new ScrollView.LayoutParams(
                    ScrollView.LayoutParams.MATCH_PARENT,
                    ScrollView.LayoutParams.WRAP_CONTENT
            ));
            LinearLayout.LayoutParams scrollParams = new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    0,
                    1f
            );
            scrollParams.setMargins(0, dp(8), 0, dp(8));
            root.addView(scroll, scrollParams);

            LinearLayout commandRow = new LinearLayout(this);
            commandRow.setOrientation(LinearLayout.HORIZONTAL);
            commandRow.setGravity(Gravity.CENTER_VERTICAL);
            EditText commandInput = input("help, status, logs, test...", "");
            commandInput.setSingleLine(true);
            commandInput.setTypeface(Typeface.MONOSPACE);
            commandRow.addView(commandInput, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
            Button send = compactButton("Enviar");
            send.setOnClickListener(v -> {
                String cmd = commandInput.getText() == null ? "" : commandInput.getText().toString().trim();
                commandInput.setText("");
                handleBedrockConsoleCommand(cmd);
            });
            LinearLayout.LayoutParams sendParams = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
            sendParams.setMargins(dp(8), 0, 0, 0);
            commandRow.addView(send, sendParams);
            root.addView(commandRow);

            dialog.setContentView(root);
            dialog.setOnDismissListener(d -> {
                mainHandler.removeCallbacks(bedrockFullTerminalRefreshRunnable);
                bedrockFullTerminalText = null;
                bedrockFullTerminalDialog = null;
            });
            dialog.show();
            Window window = dialog.getWindow();
            if (window != null) {
                window.setLayout(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
                window.setBackgroundDrawable(new android.graphics.drawable.ColorDrawable(BG));
            }
            mainHandler.removeCallbacks(bedrockFullTerminalRefreshRunnable);
            mainHandler.post(bedrockFullTerminalRefreshRunnable);
        });
    }

    private void prepareBedrockManagerFromUi() {
        prepareBedrockServerFromUi();
    }


    private void refreshBedrockManagerFromUi() {
        testBedrockServerFromUi();
    }


    private void confirmBedrockEulaFromUi() {
        new AlertDialog.Builder(this)
                .setTitle("Confirmar termos do servidor?")
                .setMessage("Isso registra a confirmação local antes de um start real futuro. O runtime continua isolado e não será iniciado automaticamente.")
                .setPositiveButton("Confirmar", (dialog, which) -> runBusy("Gravando confirmação local...", () -> {
                    File bedrockDir = new File(coreLinuxDir(), "bedrock");
                    if (!bedrockDir.exists()) bedrockDir.mkdirs();
                    writeTextFile(new File(bedrockDir, "eula.txt"), "# Aceito manualmente pelo usuário no Core Worker\neula=true\n");
                    JSONObject snapshot = bedrockServerLightweightTestSnapshot();
                    String summary = "Confirmação local registrada. Runtime continua isolado por segurança.";
                    appendBedrockTerminal("eula", summary);
                    refreshLocalStatus(summary);
                    exportBedrockDebugSnapshot("eula-safe", snapshot);
                }))
                .setNegativeButton("Cancelar", null)
                .show();
    }


    private void bedrockWizardFromUi(String focus, String fallbackMessage) {
        runBusy("Atualizando Bedrock em modo seguro...", () -> {
            JSONObject out = bedrockServerLightweightTestSnapshot();
            out.put("focus", focus == null ? "status" : focus);
            String summary = out.optString("summary", fallbackMessage == null ? BEDROCK_ISOLATION_SUMMARY : fallbackMessage);
            appendBedrockTerminal("wizard", summary);
            refreshLocalStatus(summary);
            exportBedrockDebugSnapshot("wizard-safe", out);
        });
    }


    private JSONObject bedrockInstallerWizardSnapshot(String focus) throws Exception {
        JSONObject out = bedrockServerLightweightTestSnapshot();
        out.put("focus", focus == null ? "status" : focus);
        out.put("mode", "installer-static-no-python");
        bedrockInstallerSummary = out.optString("summary", bedrockInstallerSummary);
        bedrockInstallerState = "blocked";
        bedrockInstallerNextAction = out.optString("nextAction", bedrockInstallerNextAction);
        return out;
    }


    private JSONObject bedrockManagerSnapshot(String focus, boolean acceptEula) throws Exception {
        if (acceptEula) {
            File bedrockDir = new File(coreLinuxDir(), "bedrock");
            if (!bedrockDir.exists()) bedrockDir.mkdirs();
            writeTextFile(new File(bedrockDir, "eula.txt"), "# Aceito manualmente pelo usuário no Core Worker\neula=true\n");
        }
        JSONObject out = bedrockServerLightweightTestSnapshot();
        out.put("focus", focus == null ? "status" : focus);
        out.put("mode", "manager-static-no-python");
        out.put("acceptEula", acceptEula);
        bedrockSummary = out.optString("summary", bedrockSummary);
        bedrockState = out.optString("state", bedrockState);
        bedrockReady = out.optBoolean("ready", false);
        bedrockLastCheckAt = System.currentTimeMillis();
        updateSystemChecklistText();
        return out;
    }


    private void confirmStartBedrockRuntimeFromUi() {
        new AlertDialog.Builder(this)
                .setTitle("Iniciar runtime Bedrock?")
                .setMessage("Isso ativa o serviço visível do Bedrock Manager. O servidor real só fica liberado depois de ambiente, Bedrock server e preflight estarem prontos. Nada é baixado automaticamente.")
                .setPositiveButton("Ativar runtime", (dialog, which) -> startBedrockRuntimeFromUi())
                .setNegativeButton("Cancelar", null)
                .show();
    }

    private void startBedrockRuntimeFromUi() {
        bedrockRuntimeServiceActive = false;
        bedrockRuntimeState = "isolated";
        bedrockRuntimeSummary = BEDROCK_ISOLATION_SUMMARY;
        if (prefs != null) {
            prefs.edit()
                    .putBoolean("bedrock_runtime_service_active", false)
                    .putString("bedrock_runtime_service_state", BEDROCK_ISOLATION_SUMMARY)
                    .putLong("bedrock_runtime_service_last_tick_at", System.currentTimeMillis())
                    .apply();
        }
        appendBedrockTerminal("start", "bloqueado: runtime Bedrock isolado até a estabilidade do rootfs ser validada");
        refreshLocalStatus("Start Bedrock bloqueado com segurança. Primeiro vamos estabilizar rootfs/runtime interno.");
        refreshBedrockVisualState();
        exportBedrockDebugSnapshot("start-blocked", null);
    }


    private void stopBedrockRuntimeFromUi() {
        try {
            stopBedrockService("manual-stop-safe");
        } catch (Throwable exc) {
            startupLog("bedrock:stop-service-ignored " + shortThrowable(exc));
        }
        bedrockRuntimeServiceActive = false;
        bedrockRuntimeState = "stopped";
        bedrockRuntimeSummary = "runtime Bedrock parado";
        appendBedrockTerminal("stop", "runtime Bedrock parado/bloqueado com segurança");
        refreshLocalStatus("Runtime Bedrock parado.");
        refreshBedrockVisualState();
    }


    private void refreshBedrockRuntimeLogsFromUi() {
        runBusy("Coletando logs locais do Bedrock...", () -> {
            JSONObject out = bedrockRuntimeStaticSnapshot("console_tail");
            String tail = readTextFileTail(new File(new File(coreLinuxDir(), "bedrock/logs"), "bedrock-console.log"), 1800);
            String summary = tail.trim().isEmpty() ? "Nenhum log Bedrock interno encontrado." : tail;
            appendBedrockTerminal("logs", summary);
            refreshLocalStatus("Logs Bedrock locais verificados sem iniciar runtime.");
            exportBedrockDebugSnapshot("logs-safe", out);
        });
    }


    private void startBedrockService(String reason) {
        if (BEDROCK_RUNTIME_ISOLATED) {
            bedrockRuntimeServiceActive = false;
            bedrockRuntimeState = "isolated";
            bedrockRuntimeSummary = BEDROCK_ISOLATION_SUMMARY;
            startupLog("bedrock:service-start-blocked " + (reason == null ? "" : reason));
            return;
        }
        Intent intent = new Intent(this, CoreWorkerBedrockService.class);
        intent.setAction(CoreWorkerBedrockService.ACTION_START);
        intent.putExtra("reason", reason == null ? "manual" : reason);
        if (Build.VERSION.SDK_INT >= 26) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }
        prefs.edit()
                .putBoolean("bedrock_runtime_service_active", true)
                .putString("bedrock_runtime_service_state", "serviço Bedrock solicitado")
                .putLong("bedrock_runtime_service_requested_at", System.currentTimeMillis())
                .apply();
        readBedrockServiceState();
    }

    private void stopBedrockService(String reason) {
        Intent intent = new Intent(this, CoreWorkerBedrockService.class);
        intent.setAction(CoreWorkerBedrockService.ACTION_STOP);
        intent.putExtra("reason", reason == null ? "manual" : reason);
        startService(intent);
        prefs.edit()
                .putBoolean("bedrock_runtime_service_active", false)
                .putString("bedrock_runtime_service_state", "serviço Bedrock parado")
                .putLong("bedrock_runtime_service_requested_at", System.currentTimeMillis())
                .apply();
        readBedrockServiceState();
    }

    private void readBedrockServiceState() {
        try {
            if (BEDROCK_RUNTIME_ISOLATED) {
                bedrockRuntimeServiceActive = false;
                bedrockRuntimeState = "isolated";
                bedrockRuntimeSummary = BEDROCK_ISOLATION_SUMMARY;
                if (prefs != null && prefs.getBoolean("bedrock_runtime_service_active", false)) {
                    prefs.edit()
                            .putBoolean("bedrock_runtime_service_active", false)
                            .putString("bedrock_runtime_service_state", BEDROCK_ISOLATION_SUMMARY)
                            .putLong("bedrock_runtime_service_last_tick_at", System.currentTimeMillis())
                            .apply();
                }
                updateSystemChecklistText();
                return;
            }
            bedrockRuntimeServiceActive = prefs.getBoolean("bedrock_runtime_service_active", false);
            bedrockRuntimeState = prefs.getString("bedrock_runtime_service_state", bedrockRuntimeServiceActive ? "serviço Bedrock ativo" : "stopped");
            bedrockRuntimeSummary = bedrockRuntimeServiceActive ? bedrockRuntimeState : "runtime Bedrock parado";
        } catch (Throwable ignored) {
            bedrockRuntimeServiceActive = false;
            bedrockRuntimeState = "unknown";
        }
        updateSystemChecklistText();
    }

    private JSONObject bedrockRuntimeSnapshot(String action) throws Exception {
        return bedrockRuntimeSnapshot(action, null);
    }

    private JSONObject bedrockRuntimeSnapshot(String action, String consoleCommand) throws Exception {
        JSONObject out = bedrockRuntimeStaticSnapshot(action == null ? "status" : action);
        out.put("consoleCommandBlocked", consoleCommand != null && !consoleCommand.trim().isEmpty());
        out.put("mode", "runtime-static-no-python");
        out.put("isolationMode", BEDROCK_RUNTIME_ISOLATED);
        bedrockRuntimeSummary = out.optString("summary", bedrockRuntimeSummary);
        bedrockRuntimeState = out.optString("state", bedrockRuntimeState);
        bedrockRuntimeServiceActive = false;
        bedrockLastCheckAt = System.currentTimeMillis();
        updateSystemChecklistText();
        return out;
    }



    private void prepareNativeRuntimeState() {
        try {
            File runtimeDir = new File(getFilesDir(), "core-runtime");
            File shellDir = new File(runtimeDir, "shell");
            File pythonDir = new File(runtimeDir, "python");
            shellDir.mkdirs();
            pythonDir.mkdirs();
            JSONObject health = new JSONObject();
            health.put("ok", true);
            health.put("created_by", "core-worker-apk");
            health.put("summary", "Runtime nativo preparado no sandbox do app");
            writeTextFile(new File(runtimeDir, "native-health.json"), health.toString());
            writeTextFile(new File(pythonDir, "runtime-marker.json"), new JSONObject()
                    .put("ok", true)
                    .put("runtime", "chaquopy-embedded-python")
                    .put("arbitraryCode", false)
                    .toString());
            nativeBootSummary = "boot receiver nativo pronto";
            nativeShellSummary = "shell controlado pronto";
            nativePythonSummary = "Python Chaquopy preparado · aguardando health check";
            if (prefs.getBoolean("paired_via_native_apk", false) && !prefs.getString("worker_token", "").trim().isEmpty()) {
                nativeWorkerState = "pareado direto · aguardando heartbeat";
            }
        } catch (Throwable exc) {
            nativeBootSummary = "falha preparando runtime nativo";
            nativeShellSummary = shortThrowable(exc);
            nativePythonSummary = "python interno indisponível";
            appStatusLastError = shortThrowable(exc);
        }
        updateSystemChecklistText();
    }

    private void prepareCoreLinuxRuntimeState() {
        try {
            File base = new File(getFilesDir(), "core-linux");
            File rootfs = new File(base, "rootfs");
            File bin = new File(base, "bin");
            File scripts = new File(base, "scripts");
            File logs = new File(base, "logs");
            File downloads = new File(base, "downloads");
            File staging = new File(base, "staging");
            File runtime = new File(base, "runtime");
            File manifests = new File(base, "manifests");
            File bedrock = new File(base, "bedrock");
            File provision = new File(base, "provision");
            rootfs.mkdirs();
            bin.mkdirs();
            scripts.mkdirs();
            logs.mkdirs();
            downloads.mkdirs();
            staging.mkdirs();
            runtime.mkdirs();
            manifests.mkdirs();
            bedrock.mkdirs();
            provision.mkdirs();
            JSONObject marker = new JSONObject();
            marker.put("ok", true);
            marker.put("createdBy", "core-worker-apk");
            marker.put("mode", "core-linux-internal-manager");
            marker.put("primaryStrategy", "core-linux-internal");
            marker.put("termuxRole", "fallback-legado");
            marker.put("rootfsPrepared", false);
            marker.put("box64Prepared", false);
            marker.put("bedrockBundled", false);
            marker.put("provisionerReady", true);
            marker.put("autoDownload", false);
                        marker.put("arbitraryShell", false);
            marker.put("androidWritableExecBlocked", Build.VERSION.SDK_INT >= 29);
            marker.put("requiresEmbeddedExecutor", true);
            marker.put("summary", "base do Core Linux interno preparada; Termux fica só como fallback legado");
            writeTextFile(new File(base, "runtime-marker.json"), marker.toString());
            writeTextFile(new File(scripts, "README.txt"), "Scripts internos do Core Worker. Não cole tokens, IP privado ou segredos aqui.\n");
            writeCoreLinuxProvisionerFiles(base);
            // Não chamar Python/rootfs profundo aqui. Este método pode rodar na abertura do app,
            // então só cria o esqueleto leve; diagnósticos profundos ficam sob ação explícita.
            coreLinuxPrepared = true;
            coreLinuxState = "core-linux-internal preparado";
            coreLinuxSummary = "Core Linux interno preparado · executor/rootfs/Box64 pendentes";
            if (bedrockSummary == null || bedrockSummary.trim().isEmpty() || bedrockSummary.contains("aguardando")) {
                bedrockSummary = "Bedrock Manager pronto para plano/diagnóstico";
            }
        } catch (Throwable exc) {
            coreLinuxPrepared = false;
            coreLinuxState = "falha preparando base";
            coreLinuxSummary = "Core Linux Runtime falhou · " + shortThrowable(exc);
            appStatusLastError = shortThrowable(exc);
        }
        updateSystemChecklistText();
    }

    private File coreLinuxDir() {
        return new File(getFilesDir(), "core-linux");
    }

    private void writeCoreLinuxProvisionerFiles(File base) throws Exception {
        if (base == null) base = coreLinuxDir();
        File provision = new File(base, "provision");
        File scripts = new File(base, "scripts");
        File bedrock = new File(base, "bedrock");
        File logs = new File(base, "logs");
        provision.mkdirs();
        scripts.mkdirs();
        bedrock.mkdirs();
        logs.mkdirs();

        JSONObject rootfsPlan = new JSONObject();
        rootfsPlan.put("ok", true);
        rootfsPlan.put("kind", "rootfs-plan");
        rootfsPlan.put("target", "Core Worker rootfs scaffold validável; Ubuntu real fica para etapa assistida futura");
        rootfsPlan.put("status", "rootfs-scaffold-planned");
        rootfsPlan.put("autoDownload", false);
        rootfsPlan.put("destructive", false);
        rootfsPlan.put("requiresExplicitUserAction", true);
        rootfsPlan.put("steps", new JSONArray()
                .put("validar arm64-v8a e armazenamento livre")
                .put("criar scaffold controlado em core-linux/rootfs")
                .put("gravar manifesto e rootfs-state.json")
                .put("validar layout antes da etapa Box64")
                .put("nunca aceitar comandos arbitrários da VPS"));
        writeTextFile(new File(provision, "rootfs-plan.json"), rootfsPlan.toString());

        JSONObject box64Plan = new JSONObject();
        box64Plan.put("ok", true);
        box64Plan.put("kind", "box64-plan");
        box64Plan.put("target", "Box64 ARM64 -> x86_64 Linux userland");
        box64Plan.put("status", "planned");
        box64Plan.put("autoDownload", false);
        box64Plan.put("destructive", false);
        box64Plan.put("requiresExplicitUserAction", true);
        box64Plan.put("notes", "Box64 fica pendente até rootfs/proot ou fallback Termux/proot estar pronto.");
        writeTextFile(new File(provision, "box64-plan.json"), box64Plan.toString());

        JSONObject bedrockPlan = new JSONObject();
        bedrockPlan.put("ok", true);
        bedrockPlan.put("kind", "bedrock-install-plan");
        bedrockPlan.put("target", "Minecraft Bedrock Dedicated Server oficial para Ubuntu");
        bedrockPlan.put("status", "planned");
        bedrockPlan.put("autoDownload", false);
        bedrockPlan.put("requiresExplicitUserAction", true);
        bedrockPlan.put("port", 19132);
        bedrockPlan.put("steps", new JSONArray()
                .put("validar requisitos do aparelho")
                .put("preparar rootfs Linux")
                .put("preparar Box64 quando necessário")
                .put("baixar Bedrock oficial somente após ação explícita")
                .put("iniciar servidor apenas em Foreground Service futuro"));
        writeTextFile(new File(provision, "bedrock-install-plan.json"), bedrockPlan.toString());

        String propertiesTemplate = "server-name=Core Worker Bedrock\n"
                + "gamemode=survival\n"
                + "difficulty=easy\n"
                + "allow-cheats=false\n"
                + "max-players=5\n"
                + "online-mode=true\n"
                + "server-port=19132\n"
                + "server-portv6=19133\n"
                + "view-distance=12\n"
                + "tick-distance=4\n"
                + "player-idle-timeout=30\n"
                + "level-name=Bedrock level\n";
        writeTextFile(new File(bedrock, "server.properties.template"), propertiesTemplate);
        writeTextFile(new File(scripts, "bedrock-start.template.sh"), "#!/bin/sh\n# Template futuro. Não executado automaticamente.\n# cd $CORE_BEDROCK_DIR && LD_LIBRARY_PATH=. ./bedrock_server\n");
        writeTextFile(new File(logs, "README.txt"), "Logs do Core Linux Runtime/Bedrock ficarão aqui em patches futuros.\n");
    }

    private JSONObject coreLinuxRuntimeProbeSnapshot(String focus) throws Exception {
        JSONObject out = coreLinuxStaticSnapshot(focus == null ? "runtime" : focus);
        out.put("mode", "runtime-static-no-python");
        coreLinuxState = out.optString("state", coreLinuxState);
        coreLinuxSummary = out.optString("summary", coreLinuxSummary);
        coreLinuxLastCheckAt = System.currentTimeMillis();
        return out;
    }


    private JSONObject coreLinuxNativeExecutorSnapshot(String action) throws Exception {
        JSONObject nativeExecutor = CoreWorkerNativeExecutor.snapshot(this, coreLinuxDir(), action == null ? "probe" : action);
        try {
            String summary = nativeExecutor.optString("summary", "executor nativo interno atualizado");
            if (summary != null && !summary.trim().isEmpty()) {
                coreLinuxSummary = summary;
                coreLinuxState = nativeExecutor.optString("state", coreLinuxState);
                coreLinuxLastCheckAt = System.currentTimeMillis();
            }
        } catch (Throwable ignored) {
        }
        return nativeExecutor;
    }

    private JSONObject coreLinuxInternalSnapshot(String action) throws Exception {
        JSONObject out = coreLinuxStaticSnapshot(action == null ? "probe" : action);
        out.put("mode", "internal-static-no-python");
        coreLinuxPrepared = true;
        coreLinuxState = out.optString("state", "static_safe_check");
        coreLinuxSummary = out.optString("summary", coreLinuxSummary);
        coreLinuxLastCheckAt = System.currentTimeMillis();
        updateSystemChecklistText();
        return out;
    }


    private JSONObject coreLinuxRootfsSnapshot(String action) throws Exception {
        JSONObject out = coreLinuxRootfsStaticSnapshot(action == null ? "status" : action);
        coreLinuxPrepared = out.optBoolean("rootfsReady", coreLinuxPrepared);
        coreLinuxState = out.optString("state", coreLinuxState);
        coreLinuxSummary = out.optString("summary", coreLinuxSummary);
        coreLinuxLastCheckAt = System.currentTimeMillis();
        updateSystemChecklistText();
        return out;
    }


    private void prepareCoreLinuxRuntimeStateWithoutRecursiveProbe() {
        try {
            File base = new File(getFilesDir(), "core-linux");
            new File(base, "rootfs").mkdirs();
            new File(base, "bin").mkdirs();
            new File(base, "scripts").mkdirs();
            new File(base, "logs").mkdirs();
            new File(base, "downloads").mkdirs();
            new File(base, "staging").mkdirs();
            new File(base, "runtime").mkdirs();
            new File(base, "manifests").mkdirs();
            new File(base, "bedrock").mkdirs();
            new File(base, "provision").mkdirs();
        } catch (Throwable ignored) {
        }
    }


    private JSONObject bedrockRequirementsSnapshot() throws Exception {
        JSONObject out = bedrockServerLightweightTestSnapshot();
        out.put("mode", "requirements-static-no-python");
        bedrockSummary = out.optString("summary", bedrockSummary);
        bedrockState = out.optString("state", bedrockState);
        bedrockReady = out.optBoolean("ready", false);
        bedrockLastCheckAt = System.currentTimeMillis();
        return out;
    }


    private JSONObject bedrockProbeSnapshot(String focus) throws Exception {
        JSONObject out = bedrockServerLightweightTestSnapshot();
        out.put("focus", focus == null ? "probe" : focus);
        out.put("mode", "probe-static-no-python");
        bedrockSummary = out.optString("summary", bedrockSummary);
        bedrockState = out.optString("state", bedrockState);
        bedrockReady = out.optBoolean("ready", false);
        bedrockLastCheckAt = System.currentTimeMillis();
        return out;
    }


    private JSONObject coreLinuxProvisionPlanSnapshot(String focus) throws Exception {
        prepareCoreLinuxRuntimeStateWithoutRecursiveProbe();
        JSONObject out = coreLinuxStaticSnapshot(focus == null ? "plan" : focus);
        out.put("mode", "provision-plan-static-no-python");
        out.put("summary", "plano Core Linux disponível em modo seguro; execução pesada pausada");
        coreLinuxPrepared = true;
        coreLinuxState = "provisioner_static_safe";
        coreLinuxSummary = out.optString("summary", coreLinuxSummary);
        coreLinuxLastCheckAt = System.currentTimeMillis();
        return out;
    }


    private JSONObject linuxAssistedInstallSnapshot(String focus) throws Exception {
        prepareCoreLinuxRuntimeStateWithoutRecursiveProbe();
        JSONObject out = coreLinuxStaticSnapshot(focus == null ? "strategy" : focus);
        out.put("mode", "assisted-install-static-no-python");
        out.put("summary", "instalação assistida Linux pausada em modo seguro; nenhum download/rootfs iniciado");
        linuxInstallStrategySummary = out.optString("summary", linuxInstallStrategySummary);
        internalDiagnosticsSummary = linuxInstallStrategySummary;
        internalDiagnosticsLastAt = System.currentTimeMillis();
        return out;
    }


    private JSONObject bedrockInstallPlanSnapshot(String focus) throws Exception {
        JSONObject out = bedrockServerLightweightTestSnapshot();
        out.put("focus", focus == null ? "install_plan" : focus);
        out.put("mode", "install-plan-static-no-python");
        out.put("summary", "plano Bedrock disponível em modo seguro; nenhum download/runner iniciado");
        bedrockSummary = out.optString("summary", bedrockSummary);
        bedrockState = out.optString("state", "install-plan-static-safe");
        bedrockReady = false;
        bedrockLastCheckAt = System.currentTimeMillis();
        return out;
    }


    private String nativeWorkerId() {
        String saved = prefs == null ? "" : prefs.getString("native_worker_id", "");
        if (saved != null && !saved.trim().isEmpty()) return saved.trim();
        String compact = installId().replace("-", "");
        if (compact.length() > 18) compact = compact.substring(0, 18);
        return "apk-" + compact;
    }

    private String effectiveWorkerId() {
        String nativeId = prefs == null ? "" : prefs.getString("native_worker_id", "");
        if (nativeId != null && !nativeId.trim().isEmpty()) return nativeId.trim();
        String saved = prefs == null ? "" : prefs.getString("worker_id", "");
        if (saved != null && !saved.trim().isEmpty()) return saved.trim();
        if (localAgentWorkerId != null && !localAgentWorkerId.trim().isEmpty()) return localAgentWorkerId.trim();
        return nativeWorkerId();
    }

    private JSONObject pairNativeWorkerDirect(String serverUrl, String code, String name, String profile) {
        JSONObject out = new JSONObject();
        try {
            JSONObject payload = new JSONObject();
            payload.put("code", code);
            payload.put("name", name);
            payload.put("device_name", name);
            payload.put("worker_id", nativeWorkerId());
            payload.put("version", APP_VERSION);
            payload.put("source", "core-worker-apk-native");
            payload.put("endpoint", "apk://" + installId());
            putProfilePayload(payload, profile);
            payload.put("roles", new JSONArray().put("apk-native").put("diagnostics").put("internal-jobs").put("linux-runtime").put("rootfs-manager").put("bedrock-manager"));
            payload.put("capabilities", coreWorkerApkCapabilitiesArray());
            payload.put("supported_tasks", supportedLightJobsArray());
            payload.put("supportedTasks", supportedLightJobsArray());
            payload.put("app_jobs", supportedLightJobsArray());
            safePutPayload(payload, "coreLinux", coreLinuxPublicSnapshot());
            safePutPayload(payload, "nativeRuntime", nativeRuntimePublicSnapshot());
            JSONObject status = payload.optJSONObject("status");
            if (status == null) status = new JSONObject();
            status.put("apk_native_worker", true);
            status.put("termux_required_now", false);
            status.put("termux_role", "fallback-legado");
            status.put("migration_stage", "apk-native-runtime-python-linux-rootfs-bedrock-installer");
            status.put("native_shell", "allowlist-probe");
            status.put("python_runtime", nativePythonAvailable ? "embedded-ok" : "embedded-pending");
            safePutPayload(payload, "status", status);
            safePutPayload(payload, "battery", batterySnapshot());
            safePutPayload(payload, "network", networkSnapshot(serverUrl));
            HttpResult result = request("POST", serverUrl + "/core-worker/pair", payload, null);
            if (!result.ok()) {
                out.put("ok", false);
                out.put("error", "HTTP " + result.status + " · " + compactResultBody(result.body));
                return out;
            }
            JSONObject body = new JSONObject(result.body);
            if (!body.optBoolean("ok", false)) {
                out.put("ok", false);
                out.put("error", compactResultBody(result.body));
                return out;
            }
            out.put("ok", true);
            out.put("worker_id", body.optString("worker_id", nativeWorkerId()));
            out.put("token", body.optString("token", ""));
            return out;
        } catch (Throwable exc) {
            try {
                out.put("ok", false);
                out.put("error", shortThrowable(exc));
            } catch (Throwable ignored) {
            }
            return out;
        }
    }

    private void sendNativeWorkerHeartbeat(boolean showResult, String reason) {
        if (!nativeWorkerHeartbeatRunning.compareAndSet(false, true)) {
            nativeWorkerState = "heartbeat nativo já em andamento";
            return;
        }
        new Thread(() -> {
            try {
                sendNativeWorkerHeartbeatInternal(showResult, reason);
            } finally {
                nativeWorkerHeartbeatRunning.set(false);
            }
        }, "core-worker-native-heartbeat").start();
    }

    private void sendNativeWorkerHeartbeatInternal(boolean showResult, String reason) {
        try {
            String serverUrl = normalizedServerUrl();
            String token = prefs.getString("worker_token", "").trim();
            String workerId = effectiveWorkerId();
            if (serverUrl.isEmpty() || token.isEmpty() || workerId.isEmpty()) {
                nativeWorkerOnline = false;
                nativeWorkerState = hasPairing() ? "token nativo ausente" : "aguardando pareamento direto";
                return;
            }
            JSONObject payload = new JSONObject();
            payload.put("worker_id", workerId);
            payload.put("id", workerId);
            payload.put("name", prefs.getString("device_name", defaultDeviceName()));
            payload.put("version", APP_VERSION);
            payload.put("source", "core-worker-apk-native");
            payload.put("roles", new JSONArray().put("apk-native").put("diagnostics").put("internal-jobs").put("linux-runtime").put("rootfs-manager").put("bedrock-manager"));
            payload.put("capabilities", coreWorkerApkCapabilitiesArray());
            payload.put("supported_tasks", supportedLightJobsArray());
            payload.put("supportedTasks", supportedLightJobsArray());
            payload.put("app_jobs", supportedLightJobsArray());
            safePutPayload(payload, "coreLinux", coreLinuxPublicSnapshot());
            safePutPayload(payload, "nativeRuntime", nativeRuntimePublicSnapshot());
            safePutPayload(payload, "battery", batterySnapshot());
            safePutPayload(payload, "network", networkSnapshot(serverUrl));
            JSONObject status = statusSnapshot();
            status.put("native_heartbeat_reason", reason == null ? "manual" : reason);
            status.put("core_worker_jobs", new JSONObject()
                    .put("runtime", "apk-native-internal")
                    .put("state", internalLightJobsState == null ? "" : internalLightJobsState)
                    .put("queue", internalLightJobsQueueSummary == null ? "" : internalLightJobsQueueSummary)
                    .put("last_result_summary", internalLightJobsLastSummary == null ? "" : internalLightJobsLastSummary));
            safePutPayload(payload, "status", status);
            HttpResult result = request("POST", serverUrl + "/core-worker/heartbeat", payload, token);
            if (result.ok()) {
                JSONObject body = new JSONObject(result.body);
                if (body.optBoolean("ok", false)) {
                    nativeWorkerOnline = true;
                    nativeWorkerState = "online direto na VPS";
                    nativeWorkerLastHeartbeatAt = System.currentTimeMillis();
                    vpsState = "ok";
                    if (showResult) show("Worker nativo do APK sincronizado com a VPS.");
                } else {
                    nativeWorkerOnline = false;
                    nativeWorkerState = "VPS recusou heartbeat nativo";
                    if (showResult) show("Heartbeat nativo recusado: " + compactResultBody(result.body));
                }
            } else {
                nativeWorkerOnline = false;
                nativeWorkerState = "falha HTTP " + result.status;
                if (showResult) show("Heartbeat nativo falhou: HTTP " + result.status + "\n" + compactResultBody(result.body));
            }
        } catch (Throwable exc) {
            nativeWorkerOnline = false;
            nativeWorkerState = "falha · " + shortThrowable(exc);
            appStatusLastError = shortThrowable(exc);
            if (showResult) show("Heartbeat nativo falhou: " + shortThrowable(exc));
        }
        updateSystemChecklistText();
        showLocalAgentText();
    }

    private JSONObject nativeWorkerStatusSnapshot() throws Exception {
        JSONObject obj = new JSONObject();
        obj.put("online", nativeWorkerOnline);
        obj.put("state", nativeWorkerState == null ? "" : nativeWorkerState);
        obj.put("workerId", effectiveWorkerId());
        obj.put("pairedDirect", prefs.getBoolean("paired_via_native_apk", false));
        obj.put("lastHeartbeatAt", nativeWorkerLastHeartbeatAt);
        obj.put("summary", nativeWorkerOnline ? "worker nativo online" : nativeWorkerState);
        return obj;
    }

    private JSONObject nativeBootSnapshot() throws Exception {
        JSONObject boot = new JSONObject();
        boot.put("receiver", "CoreWorkerBootReceiver");
        boot.put("receiveBootCompletedPermission", true);
        boot.put("jobScheduler", "CoreWorkerUpdateJobService");
        boot.put("persistedPeriodicJob", true);
        boot.put("lastWakeReason", prefs.getString("internal_jobs_wake_reason", ""));
        boot.put("lastWakeRequestedAt", prefs.getLong("internal_jobs_wake_requested_at", 0L));
        boot.put("summary", "boot nativo pronto · sem Termux:Boot obrigatório");
        return boot;
    }

    private JSONObject localShellProbeSnapshot() throws Exception {
        JSONObject shell = new JSONObject();
        shell.put("mode", "allowlist");
        shell.put("scope", "app-sandbox");
        shell.put("arbitraryCommands", false);
        JSONArray commands = new JSONArray();
        commands.put(runAllowedShellCommand("whoami", new String[]{"/system/bin/sh", "-c", "id"}));
        commands.put(runAllowedShellCommand("pwd", new String[]{"/system/bin/sh", "-c", "pwd"}));
        commands.put(runAllowedShellCommand("files", new String[]{"/system/bin/sh", "-c", "ls -la " + shellQuote(getFilesDir().getAbsolutePath())}));
        commands.put(runAllowedShellCommand("storage", new String[]{"/system/bin/sh", "-c", "df -k " + shellQuote(getFilesDir().getAbsolutePath()) + " " + shellQuote(getCacheDir().getAbsolutePath())}));
        shell.put("commands", commands);
        shell.put("summary", "shell controlado ok · sandbox do APK");
        return shell;
    }

    private JSONObject runAllowedShellCommand(String label, String[] cmd) throws Exception {
        JSONObject out = new JSONObject();
        out.put("label", label);
        Process process = null;
        try {
            ProcessBuilder builder = new ProcessBuilder(cmd);
            builder.directory(getFilesDir());
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
            out.put("stdout", sanitizeCommandOutput(readAll(process.getInputStream()), 1200));
            out.put("stderr", sanitizeCommandOutput(readAll(process.getErrorStream()), 600));
        } catch (Throwable exc) {
            out.put("ok", false);
            out.put("error", shortThrowable(exc));
        } finally {
            if (process != null) {
                try { process.destroy(); } catch (Throwable ignored) {}
            }
        }
        return out;
    }

    private String shellQuote(String value) {
        return "'" + String.valueOf(value == null ? "" : value).replace("'", "'\\''") + "'";
    }

    private String sanitizeCommandOutput(String value, int limit) {
        String clean = String.valueOf(value == null ? "" : value)
                .replaceAll("(?i)(token|authorization|bearer|secret|password|passwd|firebase|fcm)[=: ]+[^\\s]+", "$1=[redacted]")
                .replaceAll("([0-9]{1,3}\\.){3}[0-9]{1,3}", "[ip-redacted]");
        if (clean.length() > limit) clean = clean.substring(0, limit) + "…[truncated]";
        return clean;
    }

    private JSONObject pythonRuntimeProbeSnapshot() throws Exception {
        return runEmbeddedPythonJob("health_check", new JSONObject().put("probe", true));
    }

    private JSONObject pythonRuntimeInfoSnapshot() throws Exception {
        return runEmbeddedPythonJob("runtime_info", new JSONObject().put("probe", true));
    }

    private JSONObject pythonStorageCheckSnapshot() throws Exception {
        return runEmbeddedPythonJob("storage_check", new JSONObject().put("probe", true));
    }

    private JSONObject pythonStatusBundleSnapshot(String serverUrl) throws Exception {
        JSONObject extra = new JSONObject();
        safePutPayload(extra, "diagnostic", diagnosticSnapshot(serverUrl));
        safePutPayload(extra, "bundle", collectStatusBundle(serverUrl));
        return runEmbeddedPythonJob("status_bundle", extra);
    }

    private JSONObject pythonLogSummarySnapshot() throws Exception {
        JSONObject extra = new JSONObject();
        safePutPayload(extra, "history", internalJobHistoryJson());
        extra.put("historyText", internalJobHistoryText());
        return runEmbeddedPythonJob("log_summarizer", extra);
    }

    private JSONObject pythonNetworkDiagnosticSnapshot(String serverUrl) throws Exception {
        JSONObject extra = new JSONObject();
        safePutPayload(extra, "network", networkSnapshot(serverUrl));
        extra.put("serverUrlConfigured", serverUrl != null && !serverUrl.trim().isEmpty());
        return runEmbeddedPythonJob("network_diagnostic", extra);
    }

    private JSONObject pythonRuntimeFilesCheckSnapshot() throws Exception {
        JSONObject extra = new JSONObject();
        safePutPayload(extra, "storage", storageSnapshot());
        return runEmbeddedPythonJob("runtime_files_check", extra);
    }

    private JSONObject buildPythonJobContext(JSONObject extra) throws Exception {
        JSONObject ctx = new JSONObject();
        ctx.put("appVersion", APP_VERSION);
        ctx.put("appVersionCode", BuildConfig.VERSION_CODE);
        ctx.put("installId", installId());
        ctx.put("workerId", effectiveWorkerId());
        ctx.put("deviceName", prefs == null ? "" : prefs.getString("device_name", defaultDeviceName()));
        ctx.put("filesDir", getFilesDir().getAbsolutePath());
        ctx.put("cacheDir", getCacheDir().getAbsolutePath());
        ctx.put("runtimeDir", new File(getFilesDir(), "core-runtime").getAbsolutePath());
        ctx.put("coreLinuxDir", coreLinuxDir().getAbsolutePath());
        ctx.put("bedrockDir", new File(coreLinuxDir(), "bedrock").getAbsolutePath());
        ctx.put("foregroundRuntimeActive", foregroundRuntimeActive);
        ctx.put("foregroundRuntimeSummary", foregroundRuntimeSummary == null ? "" : foregroundRuntimeSummary);
        ctx.put("linuxInstallStrategySummary", linuxInstallStrategySummary == null ? "" : linuxInstallStrategySummary);
        ctx.put("termuxInstalled", isPackageInstalled("com.termux"));
        ctx.put("termuxApiInstalled", isPackageInstalled("com.termux.api"));
        ctx.put("termuxBootInstalled", isPackageInstalled("com.termux.boot"));
        safePutPayload(ctx, "battery", batterySnapshot());
        safePutPayload(ctx, "network", networkSnapshot(normalizedServerUrl()));
        safePutPayload(ctx, "runtime", runtimeSnapshot());
        safePutPayload(ctx, "status", statusSnapshot());
        safePutPayload(ctx, "history", internalJobHistoryJson());
        if (extra != null) {
            JSONArray names = extra.names();
            if (names != null) {
                for (int i = 0; i < names.length(); i++) {
                    String key = names.optString(i, "");
                    if (!key.isEmpty()) ctx.put(key, extra.opt(key));
                }
            }
        }
        return ctx;
    }

    private JSONObject runEmbeddedPythonJob(String script, JSONObject extra) throws Exception {
        long startedAt = System.currentTimeMillis();
        JSONObject out = new JSONObject();
        String cleanScript = script == null ? "" : script.trim();
        if (!isAllowedPythonScript(cleanScript)) {
            out.put("ok", false);
            out.put("embedded", true);
            out.put("script", cleanScript);
            out.put("arbitraryCode", false);
            out.put("error", "script Python não permitido pelo APK");
            out.put("summary", "Python bloqueado por allowlist");
            return out;
        }
        try {
            synchronized (embeddedPythonLock) {
                if (!Python.isStarted()) {
                    Python.start(new AndroidPlatform(getApplicationContext()));
                }
                Python py = Python.getInstance();
                PyObject sys = py.getModule("sys");
                nativePythonVersion = sys.get("version").toString();
                PyObject module = py.getModule("coreworker." + cleanScript);
                JSONObject context = buildPythonJobContext(extra);
                PyObject response = module.callAttr("run", context.toString());
                String raw = sanitizeCommandOutput(response == null ? "" : response.toString(), 8000);
                try {
                    out = new JSONObject(raw);
                } catch (Throwable parseError) {
                    out = new JSONObject();
                    out.put("ok", false);
                    out.put("raw", raw);
                    out.put("error", "Python retornou JSON inválido: " + shortThrowable(parseError));
                }
                out.put("embedded", true);
                out.put("script", cleanScript);
                out.put("module", "coreworker." + cleanScript);
                out.put("pythonVersion", nativePythonVersion);
                out.put("arbitraryCode", false);
                out.put("durationMs", Math.max(0L, System.currentTimeMillis() - startedAt));
                nativePythonAvailable = out.optBoolean("ok", true);
                nativePythonLastRunAt = System.currentTimeMillis();
                nativePythonLastScript = cleanScript;
                nativePythonLastError = out.optBoolean("ok", false) ? "" : out.optString("error", "erro Python");
                nativePythonSummary = out.optBoolean("ok", false)
                        ? "Python Chaquopy ok · " + cleanScript
                        : "Python Chaquopy falhou · " + out.optString("error", "erro");
                return out;
            }
        } catch (Throwable exc) {
            nativePythonAvailable = false;
            nativePythonLastRunAt = System.currentTimeMillis();
            nativePythonLastScript = cleanScript;
            nativePythonLastError = shortThrowable(exc);
            nativePythonSummary = "Python Chaquopy indisponível · " + nativePythonLastError;
            out.put("ok", false);
            out.put("embedded", true);
            out.put("script", cleanScript);
            out.put("arbitraryCode", false);
            out.put("durationMs", Math.max(0L, System.currentTimeMillis() - startedAt));
            out.put("error", nativePythonLastError);
            out.put("summary", nativePythonSummary);
            return out;
        }
    }

    private boolean isAllowedPythonScript(String script) {
        return "health_check".equals(script)
                || "runtime_info".equals(script)
                || "status_bundle".equals(script)
                || "storage_check".equals(script)
                || "log_summarizer".equals(script)
                || "network_diagnostic".equals(script)
                || "runtime_files_check".equals(script)
                || "linux_runtime_probe".equals(script)
                || "core_linux_internal".equals(script)
                || "core_linux_rootfs".equals(script)
                || "linux_provision_plan".equals(script)
                || "bedrock_requirements".equals(script)
                || "bedrock_probe".equals(script)
                || "bedrock_install_plan".equals(script)
                || "linux_assisted_install".equals(script)
                || "bedrock_installer_wizard".equals(script)
                || "bedrock_manager".equals(script)
                || "bedrock_runtime".equals(script);
    }


    private void prepareInternalRuntimePreview() {
        try {
            File runtimeDir = new File(getFilesDir(), "core-runtime");
            if (!runtimeDir.exists() && !runtimeDir.mkdirs()) {
                internalRuntimeState = "não preparado";
                internalRuntimePath = runtimeDir.getAbsolutePath();
                internalRuntimeOnline = false;
                return;
            }
            File state = new File(runtimeDir, "runtime-state.json");
            JSONObject meta = new JSONObject();
            meta.put("ok", true);
            meta.put("mode", "apk-native-python-linux-bedrock-installer");
            meta.put("active", true);
            meta.put("internal_runtime", "apk-native-runtime");
            meta.put("apk_version", APP_VERSION);
            meta.put("version_code", BuildConfig.VERSION_CODE);
            meta.put("created_by", "core-worker-apk");
            meta.put("summary", "Runtime interno ativo para status, boot, jobs seguros, Python, shell controlado, Foreground Service e Core Linux Runtime Manager. Termux fica só como fallback legado.");
            meta.put("migration_stage", "apk-native-bedrock-installer-phase");
            writeTextFile(state, meta.toString());
            internalRuntimeState = "preparado · heartbeat ativo";
            internalRuntimePath = runtimeDir.getAbsolutePath();
            runtimeMode = "apk-native-python-linux-bedrock-installer";
        } catch (Throwable exc) {
            internalRuntimeState = "falha ao preparar · " + exc.getClass().getSimpleName();
            internalRuntimeOnline = false;
            internalRuntimeLastError = shortThrowable(exc);
            appStatusLastError = internalRuntimeLastError;
        }
        updateSystemChecklistText();
    }

    private void writeTextFile(File file, String value) throws Exception {
        File parent = file.getParentFile();
        if (parent != null && !parent.exists()) {
            parent.mkdirs();
        }
        FileOutputStream out = new FileOutputStream(file, false);
        out.write(String.valueOf(value == null ? "" : value).getBytes(StandardCharsets.UTF_8));
        out.flush();
        out.close();
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
                .put("core-linux-runner-preflight-v6")
                .put("core-linux-runner-preflight-v7")
                .put("core-linux-runner-preflight-v8")
                .put("core-linux-runner-preflight-v10")
                .put("core-linux-runner-preflight-v11")
                .put("core-linux-base-tools-smoke-v12")
                .put("core-linux-rootfs-proot-smoke-v13")
                .put("core-linux-rootfs-proot-smoke-v13.1")
                .put("core-linux-rootfs-proot-smoke-v13.2")
                .put("core-linux-rootfs-proot-smoke-v13.3")
                .put("core-linux-box64-intake-preflight-v14.2.1")
                .put("core-linux-box64-version-smoke-v15")
                .put("core-linux-box64-version-smoke-v15.2")
                .put("core-linux-box64-glibc-preflight-v15.3")
                .put("core-linux-embedded-binaries-intake-v1")
                .put("core-linux-embedded-binaries-intake-v2")
                .put("core-linux-embedded-binaries-intake-v3")
                .put("core-linux-embedded-binaries-intake-v4")
                .put("core-linux-embedded-binaries-intake-v5")
                .put("core-linux-embedded-binaries-intake-v6")
                .put("core-linux-embedded-binaries-intake-v7")
                .put("core-linux-embedded-binaries-intake-v8")
                .put("core-linux-embedded-binaries-intake-v10")
                .put("core-linux-embedded-binaries-intake-v11")
                .put("core-linux-embedded-binaries-build-pipeline-v1")
                .put("core-linux-embedded-binaries-build-pipeline-v2")
                .put("core-linux-embedded-binaries-build-pipeline-v3")
                .put("core-linux-embedded-binaries-build-pipeline-v4")
                .put("core-linux-embedded-binaries-build-pipeline-v5")
                .put("core-linux-embedded-binaries-build-pipeline-v6")
                .put("core-linux-runtime-v1")
                .put("minecraft-bedrock-manager-safe-plan");
    }

    private JSONObject coreLinuxPublicSnapshot() throws Exception {
        JSONObject rootfsState = readJsonFile(new File(new File(coreLinuxDir(), "runtime"), "rootfs-state.json"));
        JSONObject importState = readJsonFile(new File(new File(coreLinuxDir(), "runtime"), "rootfs-import-state.json"));
        String summary = firstNonEmpty(
                rootfsState.optString("summary", ""),
                importState.optString("summary", ""),
                coreLinuxSummary
        );
        String state = firstNonEmpty(
                rootfsState.optString("state", ""),
                importState.optString("state", ""),
                coreLinuxState
        );
        boolean realValidated = state.toLowerCase(Locale.ROOT).contains("rootfs_real_validated")
                || "real".equalsIgnoreCase(rootfsState.optString("validationLevel", ""));
        if (realValidated) {
            state = "rootfs_real_validated";
            summary = firstNonEmpty(summary, "Rootfs real validado · runner real ainda bloqueado");
            coreLinuxState = state;
            coreLinuxSummary = summary;
            rootfsState = rootfsState.length() > 0 ? rootfsState : importState.optJSONObject("rootfs");
        }
        boolean prepared = coreLinuxPrepared || realValidated || (rootfsState != null && rootfsState.optBoolean("rootfsReady", false));
        if (rootfsState == null) rootfsState = new JSONObject();
        JSONObject out = new JSONObject();
        out.put("summary", summary == null ? "" : summary);
        out.put("state", state == null ? "" : state);
        out.put("prepared", prepared);
        out.put("lastCheckAt", Math.max(coreLinuxLastCheckAt, Math.max(rootfsState.optLong("updatedAt", 0L), importState.optLong("updatedAt", 0L))));
        out.put("termuxRequired", false);
        out.put("bedrockStartAllowed", false);
        out.put("rootfsReady", rootfsState.optBoolean("rootfsReady", prepared));
        out.put("rootfsValidationLevel", rootfsState.optString("validationLevel", ""));
        out.put("rootfsDistributionReady", rootfsState.optBoolean("distributionReady", false));
        out.put("rootfsSummary", rootfsState.optString("summary", ""));
        out.put("rootfsState", rootfsState.optString("state", ""));
        out.put("rootfsImportState", importState.optString("state", ""));
        out.put("rootfsImportSummary", importState.optString("summary", ""));
        JSONObject runnerPreflight = readJsonFile(new File(new File(coreLinuxDir(), "runtime"), "runner-preflight-state.json"));
        if (runnerPreflight.length() > 0) {
            out.put("runnerPreflightState", runnerPreflight.optString("state", ""));
            out.put("runnerPreflightSummary", runnerPreflight.optString("summary", ""));
            out.put("runnerPreflightVersion", runnerPreflight.optInt("preflightVersion", 1));
            out.put("runnerReady", runnerPreflight.optBoolean("runnerReady", false));
            out.put("runnerBlocked", runnerPreflight.optBoolean("runnerBlocked", true));
            out.put("runnerExecutionAllowed", runnerPreflight.optBoolean("runnerExecutionAllowed", false));
            out.put("runnerRequirementsReady", runnerPreflight.optBoolean("runnerRequirementsReady", false));
            safePutPayload(out, "runnerPreflight", runnerPreflight);
        }
        out.put("supportedStage", runnerPreflight.length() > 0 ? "core-linux-runner-preflight-v11" : (rootfsState.optBoolean("distributionReady", false) ? "core-linux-rootfs-import-v1" : "core-linux-runtime-v1-smoke"));
        out.put("supportedTasks", supportedLightJobsArray());
        if (rootfsState.length() > 0) safePutPayload(out, "rootfs", rootfsState);
        if (importState.length() > 0) safePutPayload(out, "rootfsImport", importState);
        return out;
    }

    private JSONObject nativeRuntimePublicSnapshot() throws Exception {
        JSONObject out = new JSONObject();
        out.put("summary", nativeShellSummary == null ? "" : nativeShellSummary);
        out.put("workerOnline", nativeWorkerOnline);
        out.put("workerState", nativeWorkerState == null ? "" : nativeWorkerState);
        out.put("bootSummary", nativeBootSummary == null ? "" : nativeBootSummary);
        out.put("pythonAvailable", nativePythonAvailable);
        out.put("pythonSummary", nativePythonSummary == null ? "" : nativePythonSummary);
        out.put("lastHeartbeatAt", nativeWorkerLastHeartbeatAt);
        out.put("supportedTasks", supportedLightJobsArray());
        return out;
    }

    private JSONObject runtimeSnapshot() throws Exception {
        JSONObject runtime = new JSONObject();
        runtime.put("mode", runtimeMode == null || runtimeMode.trim().isEmpty() ? "apk-native-python-linux-bedrock-installer" : runtimeMode.trim());
        runtime.put("current_worker", nativeWorkerOnline ? "apk-native-worker" : (localAgentOnline ? "termux-fallback" : "apk-internal-heartbeat"));
        runtime.put("internal_runtime", "apk-native-runtime");
        runtime.put("capabilities", coreWorkerApkCapabilitiesArray());
        runtime.put("supported_tasks", supportedLightJobsArray());
        runtime.put("supportedTasks", supportedLightJobsArray());
        runtime.put("internal_runtime_state", internalRuntimeState == null ? "" : internalRuntimeState);
        runtime.put("internal_runtime_online", internalRuntimeOnline);
        runtime.put("internal_runtime_heartbeat_state", internalRuntimeHeartbeatState == null ? "" : internalRuntimeHeartbeatState);
        runtime.put("internal_runtime_last_heartbeat_at", internalRuntimeLastHeartbeatAt);
        runtime.put("internal_runtime_last_error", internalRuntimeLastError == null ? "" : internalRuntimeLastError);
        runtime.put("internal_runtime_path", internalRuntimePath == null ? "" : internalRuntimePath);
        runtime.put("foreground_runtime_active", foregroundRuntimeActive);
        runtime.put("foreground_runtime_summary", foregroundRuntimeSummary == null ? "" : foregroundRuntimeSummary);
        runtime.put("foreground_runtime_last_tick_at", foregroundRuntimeLastTickAt);
        runtime.put("linux_install_strategy_summary", linuxInstallStrategySummary == null ? "" : linuxInstallStrategySummary);
        runtime.put("bedrock_installer_summary", bedrockInstallerSummary == null ? "" : bedrockInstallerSummary);
        runtime.put("bedrock_installer_state", bedrockInstallerState == null ? "" : bedrockInstallerState);
        runtime.put("bedrock_installer_next_action", bedrockInstallerNextAction == null ? "" : bedrockInstallerNextAction);
        runtime.put("termux_required_now", false);
        runtime.put("termux_fallback_available", localAgentOnline);
        runtime.put("advanced_jobs_require_termux", false);
        runtime.put("jobs_runtime", "apk-native-python-linux-bedrock-installer");
        runtime.put("migration_stage", "apk-native-bedrock-installer-phase");
        runtime.put("light_jobs_state", internalLightJobsState == null ? "" : internalLightJobsState);
        runtime.put("light_jobs_last_check_at", internalLightJobsLastCheckAt);
        runtime.put("light_jobs_last_count", internalLightJobsLastCount);
        runtime.put("light_jobs_last_summary", internalLightJobsLastSummary == null ? "" : internalLightJobsLastSummary);
        runtime.put("light_jobs_last_fetch_reason", internalLightJobsLastFetchReason == null ? "" : internalLightJobsLastFetchReason);
        runtime.put("light_jobs_last_fetch_app_version", internalLightJobsLastFetchAppVersion == null ? "" : internalLightJobsLastFetchAppVersion);
        runtime.put("light_jobs_last_fetch_app_version_code", internalLightJobsLastFetchAppVersionCode);
        runtime.put("light_jobs_last_fetch_http_status", internalLightJobsLastFetchHttpStatus);
        runtime.put("light_jobs_last_returned_count", internalLightJobsLastReturnedCount);
        runtime.put("light_jobs_last_fetch_error", internalLightJobsLastFetchError == null ? "" : internalLightJobsLastFetchError);
        runtime.put("internal_jobs_queue", internalLightJobsQueueSummary == null ? "" : internalLightJobsQueueSummary);
        runtime.put("internal_jobs_running", internalLightJobsRunningCount);
        runtime.put("internal_jobs_pending", internalLightJobsPendingCount);
        runtime.put("internal_jobs_auto_total", internalLightJobsAutoTotal);
        runtime.put("internal_jobs_manual_total", internalLightJobsManualTotal);
        runtime.put("internal_jobs_catalog", internalLightJobsCatalogSummary == null ? "" : internalLightJobsCatalogSummary);
        runtime.put("diagnostics_summary", internalDiagnosticsSummary == null ? "" : internalDiagnosticsSummary);
        runtime.put("storage_summary", internalStorageSummary == null ? "" : internalStorageSummary);
        runtime.put("bridge_summary", internalBridgeSummary == null ? "" : internalBridgeSummary);
        runtime.put("native_worker_online", nativeWorkerOnline);
        runtime.put("native_worker_state", nativeWorkerState == null ? "" : nativeWorkerState);
        runtime.put("native_boot_summary", nativeBootSummary == null ? "" : nativeBootSummary);
        runtime.put("native_shell_summary", nativeShellSummary == null ? "" : nativeShellSummary);
        runtime.put("native_python_summary", nativePythonSummary == null ? "" : nativePythonSummary);
        runtime.put("native_python_available", nativePythonAvailable);
        runtime.put("native_python_version", nativePythonVersion == null ? "" : nativePythonVersion);
        runtime.put("native_python_last_script", nativePythonLastScript == null ? "" : nativePythonLastScript);
        runtime.put("native_python_last_error", nativePythonLastError == null ? "" : nativePythonLastError);
        runtime.put("native_python_last_run_at", nativePythonLastRunAt);
        JSONObject corePublicForRuntime = coreLinuxPublicSnapshot();
        runtime.put("core_linux_summary", corePublicForRuntime.optString("summary", coreLinuxSummary == null ? "" : coreLinuxSummary));
        runtime.put("core_linux_state", corePublicForRuntime.optString("state", coreLinuxState == null ? "" : coreLinuxState));
        runtime.put("core_linux_prepared", coreLinuxPrepared || corePublicForRuntime.optBoolean("prepared", false));
        runtime.put("core_linux_last_check_at", coreLinuxLastCheckAt);
        runtime.put("bedrock_summary", bedrockSummary == null ? "" : bedrockSummary);
        runtime.put("bedrock_state", bedrockState == null ? "" : bedrockState);
        runtime.put("bedrock_ready", bedrockReady);
        runtime.put("bedrock_last_check_at", bedrockLastCheckAt);
        safePutPayload(runtime, "coreLinux", corePublicForRuntime);
        safePutPayload(runtime, "nativeRuntime", nativeRuntimePublicSnapshot());
        runtime.put("summary", "APK assume status, boot, jobs internos e Core Linux Runtime v1; Termux fica como fallback legado.");
        return runtime;
    }

    private String runtimeStatusLabel() {
        String state = internalRuntimeState == null || internalRuntimeState.trim().isEmpty() ? "não preparado" : internalRuntimeState.trim();
        String hb = internalRuntimeOnline ? "APK online" : "APK aguardando sync";
        return "APK nativo · " + hb + " · " + state;
    }

    private void sendInternalRuntimeHeartbeat(boolean showResult, String reason) {
        if (!internalRuntimeHeartbeatRunning.compareAndSet(false, true)) {
            internalRuntimeHeartbeatState = "heartbeat já em andamento";
            return;
        }
        new Thread(() -> {
            try {
                sendInternalRuntimeHeartbeatInternal(showResult, reason);
            } finally {
                internalRuntimeHeartbeatRunning.set(false);
            }
        }, "core-worker-apk-heartbeat").start();
    }

    private void sendInternalRuntimeHeartbeatInternal(boolean showResult, String reason) {
        String serverUrl = normalizedServerUrl();
        if (serverUrl.isEmpty()) {
            internalRuntimeOnline = false;
            internalRuntimeHeartbeatState = "VPS não configurada";
            updateSystemChecklistText();
            return;
        }
        try {
            JSONObject payload = statusSnapshot();
            payload.put("state", "internal_heartbeat");
            payload.put("reason", reason == null ? "manual" : reason);
            payload.put("source", "core-worker-apk-internal-runtime");
            payload.put("runtime_mode", "apk-native-python-linux-bedrock-installer");
            payload.put("internal_runtime", "apk-native-runtime");
            payload.put("internal_runtime_state", internalRuntimeState == null ? "" : internalRuntimeState);
            payload.put("internal_runtime_path", internalRuntimePath == null ? "" : internalRuntimePath);
            payload.put("workerId", effectiveWorkerId());
            payload.put("installId", installId());
            payload.put("deviceName", deviceNameInput == null ? defaultDeviceName() : deviceNameInput.getText().toString().trim());
            payload.put("appVersion", APP_VERSION);
            payload.put("appVersionCode", BuildConfig.VERSION_CODE);
            payload.put("capabilities", coreWorkerApkCapabilitiesArray());
            payload.put("supported_tasks", supportedLightJobsArray());
            payload.put("supportedTasks", supportedLightJobsArray());
            payload.put("app_jobs", supportedLightJobsArray());
            safePutPayload(payload, "runtime", runtimeSnapshot());
            safePutPayload(payload, "coreLinux", coreLinuxPublicSnapshot());
            safePutPayload(payload, "nativeRuntime", nativeRuntimePublicSnapshot());
            payload.put("profile", appliedProfile());
            payload.put("profileLabel", profileLabel(appliedProfile()));
            payload.put("localAgentOnline", localAgentOnline);
            payload.put("termuxWorkerOnline", localAgentOnline);
            payload.put("nativeWorkerOnline", nativeWorkerOnline);
            payload.put("jobsRuntime", "apk-native-python-linux-bedrock-installer");
            safePutPayload(payload, "battery", batterySnapshot());
            safePutPayload(payload, "network", networkSnapshot(serverUrl));
            safePutPayload(payload, "update", updateSnapshot());
            safePutPayload(payload, "app_status", appStatusSnapshot());
            HttpResult result = request("POST", serverUrl + "/core-worker/app/heartbeat", payload, null);
            boolean accepted = false;
            if (result.ok()) {
                try {
                    accepted = new JSONObject(result.body).optBoolean("ok", false);
                } catch (Throwable ignored) {
                    accepted = false;
                }
            }
            if (result.ok() && accepted) {
                internalRuntimeOnline = true;
                internalRuntimeHeartbeatState = "online";
                internalRuntimeLastError = "";
                appStatusLastError = "";
                internalRuntimeLastHeartbeatAt = System.currentTimeMillis();
                appStatusLastSentAt = internalRuntimeLastHeartbeatAt;
                vpsState = "ok";
                reportAppState("internal_runtime_heartbeat", "APK enviou heartbeat direto para a VPS");
                if (showResult) {
                    show("Runtime nativo do APK sincronizado com a VPS.\nTermux fica só como fallback para jobs avançados enquanto a migração continua.");
                }
            } else {
                internalRuntimeOnline = false;
                internalRuntimeHeartbeatState = "falha HTTP " + result.status;
                internalRuntimeLastError = compactResultBody(result.body);
                if (showResult) {
                    show("Runtime interno não confirmou heartbeat: HTTP " + result.status + "\n" + compactResultBody(result.body));
                }
            }
        } catch (Throwable exc) {
            internalRuntimeOnline = false;
            internalRuntimeHeartbeatState = "falha";
            internalRuntimeLastError = shortThrowable(exc);
            appStatusLastError = internalRuntimeLastError;
            if (showResult) {
                show("Runtime interno não confirmou heartbeat: " + shortThrowable(exc));
            }
        }
        updateSystemChecklistText();
        showLocalAgentText();
    }

    private void fetchAndRunLightJobs(boolean showResult, String reason) {
        String serverUrl = normalizedServerUrl();
        if (serverUrl.isEmpty()) {
            internalLightJobsState = "VPS não configurada";
            updateSystemChecklistText();
            return;
        }
        long now = System.currentTimeMillis();
        long minInterval = showResult ? 0L : 25_000L;
        if (minInterval > 0L && internalLightJobsLastFetchStartedAt > 0L && now - internalLightJobsLastFetchStartedAt < minInterval) {
            internalLightJobsState = "sincronização recente";
            internalLightJobsLastSummary = "aguardando próximo lote";
            updateSystemChecklistText();
            return;
        }
        if (!internalLightJobsFetchRunning.compareAndSet(false, true)) {
            internalLightJobsState = "sincronização já em andamento";
            updateSystemChecklistText();
            return;
        }
        internalLightJobsLastFetchStartedAt = now;
        new Thread(() -> {
            try {
                JSONObject payload = statusSnapshot();
                payload.put("installId", installId());
                payload.put("workerId", effectiveWorkerId());
                payload.put("appVersion", APP_VERSION);
                payload.put("appVersionCode", BuildConfig.VERSION_CODE);
                payload.put("versionName", APP_VERSION);
                payload.put("versionCode", BuildConfig.VERSION_CODE);
                payload.put("source", "core-worker-apk-foreground-fetch-v12-2");
                payload.put("reason", reason == null ? "background" : reason);
                payload.put("fetchStage", "core-linux-job-fetch-v12.2");
                payload.put("force", shouldForceLightJobFetch(reason, showResult));
                payload.put("supportedJobs", supportedLightJobsArray());
                payload.put("supported_tasks", supportedLightJobsArray());
                payload.put("supportedTasks", supportedLightJobsArray());
                payload.put("capabilities", coreWorkerApkCapabilitiesArray());
                safePutPayload(payload, "runtime", runtimeSnapshot());
                internalLightJobsLastFetchReason = reason == null ? "background" : reason;
                internalLightJobsLastFetchAppVersion = APP_VERSION;
                internalLightJobsLastFetchAppVersionCode = BuildConfig.VERSION_CODE;
                internalLightJobsLastFetchError = "";
                HttpResult response = request("POST", serverUrl + "/core-worker/app/jobs/fetch", payload, null);
                internalLightJobsLastCheckAt = System.currentTimeMillis();
                internalLightJobsLastFetchHttpStatus = response.status;
                if (!response.ok()) {
                    internalLightJobsState = "falha HTTP " + response.status;
                    internalRuntimeLastError = compactResultBody(response.body);
                    internalLightJobsLastFetchError = internalRuntimeLastError;
                    updateSystemChecklistText();
                    return;
                }
                JSONObject body = new JSONObject(response.body);
                if (body.optBoolean("throttled", false)) {
                    int retryAfter = Math.max(1, body.optInt("retryAfterSeconds", 10));
                    internalLightJobsState = "fila em cooldown";
                    internalLightJobsLastSummary = "VPS pediu nova checagem em " + retryAfter + "s";
                    JSONObject queue = body.optJSONObject("queue");
                    if (queue != null) {
                        internalLightJobsPendingCount = queue.optInt("pending", 0);
                        internalLightJobsRunningCount = queue.optInt("running", 0);
                        internalLightJobsQueueSummary = internalLightJobsRunningCount + " rodando · " + internalLightJobsPendingCount + " pendentes";
                    }
                    updateSystemChecklistText();
                    return;
                }
                JSONArray jobs = body.optJSONArray("jobs");
                int count = jobs == null ? 0 : jobs.length();
                internalLightJobsLastCount = count;
                internalLightJobsLastReturnedCount = count;
                JSONObject queue = body.optJSONObject("queue");
                if (queue != null) {
                    internalLightJobsPendingCount = queue.optInt("pending", 0);
                    internalLightJobsRunningCount = queue.optInt("running", 0);
                    internalLightJobsQueueSummary = internalLightJobsRunningCount + " rodando · " + internalLightJobsPendingCount + " pendentes";
                } else {
                    internalLightJobsPendingCount = 0;
                    internalLightJobsRunningCount = 0;
                    internalLightJobsQueueSummary = "fila sincronizada";
                }
                JSONObject catalog = body.optJSONObject("catalog");
                if (catalog != null) {
                    JSONArray automatic = catalog.optJSONArray("automatic");
                    JSONArray manual = catalog.optJSONArray("manual");
                    internalLightJobsAutoTotal = automatic == null ? 0 : automatic.length();
                    internalLightJobsManualTotal = manual == null ? 0 : manual.length();
                    internalLightJobsCatalogSummary = internalLightJobsAutoTotal + " automáticos · " + internalLightJobsManualTotal + " manuais";
                }
                if (count <= 0) {
                    internalLightJobsState = "fila vazia";
                    internalLightJobsLastSummary = "fila vazia";
                    internalLightJobsRunningCount = 0;
                    internalLightJobsQueueSummary = internalLightJobsPendingCount > 0 ? (internalLightJobsPendingCount + " pendentes") : "fila vazia";
                    clearTransientApkNetworkError();
                    updateSystemChecklistText();
                    if (showResult) show("Runtime interno verificado. Nenhum job interno pendente.");
                    return;
                }
                int okCount = 0;
                for (int i = 0; i < count; i++) {
                    JSONObject job = jobs.optJSONObject(i);
                    if (job == null) continue;
                    JSONObject result;
                    long startedAt = System.currentTimeMillis();
                    String jobId = job.optString("id", "");
                    String jobType = job.optString("type", "job");
                    try {
                        if (wasJobRecentlyCompleted(jobId)) {
                            result = new JSONObject();
                            result.put("ok", true);
                            result.put("type", jobType);
                            result.put("deduplicated", true);
                            result.put("message", "job interno duplicado ignorado pelo APK");
                        } else {
                            result = executeLightJob(job);
                        }
                    } catch (Throwable jobError) {
                        result = new JSONObject();
                        result.put("ok", false);
                        result.put("type", jobType);
                        result.put("error", shortThrowable(jobError));
                        result.put("message", "job interno falhou no APK");
                    }
                    result.put("durationMs", Math.max(0L, System.currentTimeMillis() - startedAt));
                    result.put("attempt", job.optInt("attempt", 1));
                    if (result.optBoolean("ok", false)) okCount++;
                    postLightJobResult(serverUrl, job, result);
                    rememberCompletedJob(jobId);
                    recordInternalJobHistory(jobType, result.optBoolean("ok", false), result.optString("message", result.optString("error", "")));
                }
                internalLightJobsState = "executados " + okCount + "/" + count;
                internalLightJobsRunningCount = 0;
                internalLightJobsPendingCount = Math.max(0, internalLightJobsPendingCount);
                internalLightJobsQueueSummary = internalLightJobsPendingCount > 0 ? (internalLightJobsPendingCount + " pendentes") : "fila vazia";
                internalLightJobsLastSummary = summarizeLightJobs(jobs, okCount, count);
                if (okCount > 0) {
                    clearTransientApkNetworkError();
                }
                updateSystemChecklistText();
                if (showResult) show("Jobs internos do APK executados: " + okCount + "/" + count);
            } catch (Throwable exc) {
                internalLightJobsState = "falha · " + shortThrowable(exc);
                internalLightJobsLastSummary = "falha: " + shortThrowable(exc);
                internalRuntimeLastError = shortThrowable(exc);
                internalLightJobsLastFetchError = internalRuntimeLastError;
                appStatusLastError = internalRuntimeLastError;
                updateSystemChecklistText();
            } finally {
                internalLightJobsFetchRunning.set(false);
            }
        }, "core-worker-apk-light-jobs").start();
    }

    private boolean shouldForceLightJobFetch(String reason, boolean showResult) {
        if (showResult) return true;
        String r = reason == null ? "" : reason.toLowerCase(Locale.ROOT);
        return r.contains("manual")
                || r.contains("smoke")
                || r.contains("status")
                || r.contains("diagnostic")
                || r.contains("sync")
                || r.contains("app_opened")
                || r.contains("activity_resume");
    }

    private JSONArray supportedLightJobsArray() {
        // Core Linux Runtime v1: anuncie apenas jobs que o APK realmente executa
        // sem Termux, sem shell livre e sem iniciar Bedrock. O painel da VPS usa
        // esta lista para esconder ações antigas/pesadas que ainda são futuro.
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
                .put("apk_core_linux_runtime_smoke_test")
                .put("apk_core_linux_rootfs_smoke_test")
                .put("apk_core_linux_box64_preflight")
                .put("apk_core_linux_box64_smoke_test");
    }

    private boolean isCoreLinuxRuntimeV1JobType(String type) {
        String t = type == null ? "" : type.trim();
        return "apk_core_linux_rootfs_status".equals(t)
                || "apk_core_linux_rootfs_preflight".equals(t)
                || "apk_core_linux_rootfs_prepare".equals(t)
                || "apk_core_linux_rootfs_validate".equals(t)
                || "apk_core_linux_rootfs_clean_staging".equals(t)
                || "apk_core_linux_rootfs_import_status".equals(t)
                || "apk_core_linux_rootfs_import_validate".equals(t)
                || "apk_core_linux_rootfs_import_abort".equals(t)
                || "apk_core_linux_rootfs_real_status".equals(t)
                || "apk_core_linux_runner_status".equals(t)
                || "apk_core_linux_runner_preflight".equals(t)
                || "apk_core_linux_runner_requirements".equals(t)
                || "apk_core_linux_native_executor_probe".equals(t)
                || "apk_core_linux_native_executor_test".equals(t)
                || "apk_core_linux_native_runtime_status".equals(t)
                || "apk_core_linux_runtime_smoke_test".equals(t)
                || "apk_core_linux_rootfs_smoke_test".equals(t)
                || "apk_core_linux_box64_preflight".equals(t)
                || "apk_core_linux_box64_smoke_test".equals(t);
    }

    private boolean isPausedRootfsBedrockJobType(String type) {
        String t = type == null ? "" : type.trim();
        if (t.isEmpty()) return false;
        if (t.startsWith("apk_python_")) return true;
        if (t.startsWith("apk_linux_")) return true;
        if (isCoreLinuxRuntimeV1JobType(t)) return false;
        if (t.startsWith("apk_core_linux_rootfs")) return true;
        if (t.startsWith("apk_minecraft_bedrock_")) return true;
        if ("apk_storage_diagnostic".equals(t) || "apk_network_diagnostic".equals(t)) return true;
        if ("apk_collect_status_bundle".equals(t) || "apk_force_status_bundle".equals(t)) return true;
        if ("apk_linux_strategy_plan".equals(t) || "apk_linux_manifest_plan".equals(t) || "apk_minecraft_bedrock_assisted_install_plan".equals(t)) return true;
        if ("apk_core_linux_internal_probe".equals(t)
                || "apk_core_linux_internal_bootstrap".equals(t)
                || "apk_core_linux_executor_probe".equals(t)
                || "apk_core_linux_rootfs_manifest".equals(t)
                || "apk_core_linux_box64_manifest".equals(t)
                || "apk_core_linux_bedrock_preflight".equals(t)
                || "apk_core_linux_internal_repair".equals(t)) return true;
        return false;
    }

    private JSONObject pausedRootfsBedrockJobResult(String type, JSONObject base) throws Exception {
        JSONObject result = base == null ? new JSONObject() : base;
        result.put("ok", false);
        result.put("blocked", true);
        result.put("deferred", true);
        result.put("type", type == null ? "job" : type);
        result.put("message", "job pausado pelo APK para proteger a interface; rootfs/Bedrock pesado precisa de etapa assistida futura");
        result.put("error", "rootfs_bedrock_job_paused_for_ui_safety");
        result.put("summary", "rootfs/Bedrock pesado pausado para evitar crash/ANR");
        result.put("safeMode", "patch-85.5");
        internalDiagnosticsSummary = "job pausado: " + (type == null ? "rootfs/Bedrock" : type);
        internalDiagnosticsLastAt = System.currentTimeMillis();
        startupLog("light-job:paused " + String.valueOf(type));
        return result;
    }

    private String summarizeLightJobs(JSONArray jobs, int okCount, int count) {
        try {
            StringBuilder builder = new StringBuilder();
            int limit = Math.min(count, 3);
            for (int i = 0; i < limit; i++) {
                JSONObject job = jobs == null ? null : jobs.optJSONObject(i);
                if (job == null) continue;
                if (builder.length() > 0) builder.append(", ");
                builder.append(job.optString("type", "job"));
            }
            if (count > limit) builder.append(" +").append(count - limit);
            if (builder.length() == 0) builder.append("jobs internos");
            builder.append(" · ").append(okCount).append("/").append(count).append(" ok");
            return builder.toString();
        } catch (Throwable ignored) {
            return "jobs internos · " + okCount + "/" + count + " ok";
        }
    }

    private boolean wasJobRecentlyCompleted(String jobId) {
        if (jobId == null || jobId.trim().isEmpty()) return false;
        try {
            JSONArray recent = new JSONArray(prefs.getString("internal_completed_job_ids", "[]"));
            for (int i = 0; i < recent.length(); i++) {
                if (jobId.equals(recent.optString(i, ""))) return true;
            }
        } catch (Throwable ignored) {
        }
        return false;
    }

    private void rememberCompletedJob(String jobId) {
        if (jobId == null || jobId.trim().isEmpty()) return;
        try {
            JSONArray old = new JSONArray(prefs.getString("internal_completed_job_ids", "[]"));
            JSONArray next = new JSONArray();
            next.put(jobId);
            for (int i = 0; i < old.length() && next.length() < 32; i++) {
                String value = old.optString(i, "");
                if (!value.isEmpty() && !jobId.equals(value)) next.put(value);
            }
            prefs.edit().putString("internal_completed_job_ids", next.toString()).apply();
        } catch (Throwable ignored) {
        }
    }

    private void recordInternalJobHistory(String type, boolean ok, String message) {
        try {
            JSONArray old = new JSONArray(prefs.getString("internal_job_history", "[]"));
            JSONArray next = new JSONArray();
            JSONObject item = new JSONObject();
            item.put("at", System.currentTimeMillis());
            item.put("type", type == null ? "job" : type);
            item.put("ok", ok);
            item.put("message", message == null ? "" : message);
            next.put(item);
            for (int i = 0; i < old.length() && next.length() < 12; i++) {
                JSONObject existing = old.optJSONObject(i);
                if (existing != null) next.put(existing);
            }
            prefs.edit().putString("internal_job_history", next.toString()).apply();
        } catch (Throwable ignored) {
        }
    }

    private JSONArray internalJobHistoryJson() {
        try {
            return new JSONArray(prefs.getString("internal_job_history", "[]"));
        } catch (Throwable ignored) {
            return new JSONArray();
        }
    }

    private String internalJobHistoryText() {
        try {
            JSONArray history = internalJobHistoryJson();
            if (history.length() == 0) return "sem histórico local";
            StringBuilder builder = new StringBuilder();
            int limit = Math.min(4, history.length());
            for (int i = 0; i < limit; i++) {
                JSONObject item = history.optJSONObject(i);
                if (item == null) continue;
                if (builder.length() > 0) builder.append("; ");
                builder.append(item.optString("type", "job"));
                builder.append(item.optBoolean("ok", false) ? " ok" : " falhou");
            }
            return builder.length() == 0 ? "sem histórico local" : builder.toString();
        } catch (Throwable ignored) {
            return "histórico indisponível";
        }
    }

    private void clearTransientApkNetworkError() {
        String internal = internalRuntimeLastError == null ? "" : internalRuntimeLastError;
        String app = appStatusLastError == null ? "" : appStatusLastError;
        if (internal.contains("NetworkOnMainThreadException")) {
            internalRuntimeLastError = "";
        }
        if (app.contains("NetworkOnMainThreadException")) {
            appStatusLastError = "";
        }
    }

    private JSONObject executeLightJob(JSONObject job) throws Exception {
        String type = job == null ? "" : job.optString("type", "");
        JSONObject jobPayload = job == null ? null : job.optJSONObject("payload");
        if (jobPayload == null) jobPayload = new JSONObject();
        String serverUrl = normalizedServerUrl();
        JSONObject result = new JSONObject();
        result.put("ok", true);
        result.put("type", type);
        result.put("executedBy", "core-worker-apk-internal-runtime");
        result.put("safety", "fila interna allowlist · shell controlado no sandbox · sem Termux obrigatório");
        result.put("appVersion", APP_VERSION);
        result.put("appVersionCode", BuildConfig.VERSION_CODE);
        result.put("installId", installId());
        result.put("workerId", emptyFallback(localAgentWorkerId, prefs.getString("worker_id", "")));
        if (isPausedRootfsBedrockJobType(type)) {
            return pausedRootfsBedrockJobResult(type, result);
        }
        if ("apk_ping".equals(type)) {
            result.put("message", "pong");
            result.put("runtime", runtimeStatusLabel());
            return result;
        }
        if ("apk_status_refresh".equals(type) || "apk_refresh_runtime".equals(type)) {
            prepareInternalRuntimePreview();
            sendInternalRuntimeHeartbeat(false, "apk_refresh_runtime".equals(type) ? "job_refresh_runtime" : "job_status_refresh");
            safePutPayload(result, "status", statusSnapshot());
            safePutPayload(result, "runtime", runtimeSnapshot());
            result.put("message", "runtime e status atualizados pelo APK");
            return result;
        }
        if ("apk_report_logs".equals(type)) {
            JSONObject logs = new JSONObject();
            logs.put("lastAppError", appStatusLastError == null ? "" : appStatusLastError);
            logs.put("internalRuntimeLastError", internalRuntimeLastError == null ? "" : internalRuntimeLastError);
            logs.put("fcmState", fcmStatusLabel());
            logs.put("lightJobs", internalLightJobsState == null ? "" : internalLightJobsState);
            logs.put("lastLightJob", internalLightJobsLastSummary == null ? "" : internalLightJobsLastSummary);
            logs.put("queue", internalLightJobsQueueSummary == null ? "" : internalLightJobsQueueSummary);
            logs.put("historyText", internalJobHistoryText());
            safePutPayload(logs, "history", internalJobHistoryJson());
            safePutPayload(result, "logs", logs);
            result.put("message", "logs internos reportados pelo APK");
            return result;
        }
        if ("apk_upload_app_logs".equals(type)) {
            JSONObject logs = new JSONObject();
            logs.put("lastAppError", appStatusLastError == null ? "" : appStatusLastError);
            logs.put("internalRuntimeLastError", internalRuntimeLastError == null ? "" : internalRuntimeLastError);
            logs.put("fcmState", fcmStatusLabel());
            logs.put("queue", internalLightJobsQueueSummary == null ? "" : internalLightJobsQueueSummary);
            logs.put("historyText", internalJobHistoryText());
            safePutPayload(logs, "history", internalJobHistoryJson());
            safePutPayload(logs, "status", statusSnapshot());
            result.put("reportKind", "app-internal-logs-lightweight");
            safePutPayload(result, "logs", logs);
            result.put("message", "logs leves do APK enviados sem Python/Termux");
            return result;
        }
        if ("apk_diagnostic".equals(type)) {
            safePutPayload(result, "diagnostic", diagnosticSnapshot(serverUrl));
            result.put("message", "diagnóstico interno do APK concluído");
            return result;
        }
        if ("apk_check_update".equals(type) || "apk_verify_update_state".equals(type)) {
            JSONObject update = checkUpdateForJob(serverUrl);
            safePutPayload(result, "update", update);
            if (!update.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", update.optString("error", "checagem de atualização falhou"));
            }
            result.put("message", update.optBoolean("ok", false) ? "checagem de atualização concluída pelo APK" : "checagem de atualização falhou");
            return result;
        }
        if ("apk_test_vps_connection".equals(type)) {
            JSONObject connection = vpsConnectionTest(serverUrl);
            safePutPayload(result, "connection", connection);
            if (!connection.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", connection.optString("error", "teste de conexão falhou"));
            }
            result.put("message", connection.optBoolean("ok", false) ? "teste de conexão VPS concluído pelo APK" : "teste de conexão VPS falhou");
            return result;
        }
        if ("apk_upload_report".equals(type)) {
            JSONObject report = new JSONObject();
            safePutPayload(report, "status", statusSnapshot());
            safePutPayload(report, "diagnostic", diagnosticSnapshot(serverUrl));
            result.put("reportKind", "internal-status-report");
            safePutPayload(result, "report", report);
            result.put("message", "relatório interno enviado pelo APK");
            return result;
        }
        if ("apk_sync_runtime_state".equals(type)) {
            safePutPayload(result, "runtime", runtimeSnapshot());
            safePutPayload(result, "status", statusSnapshot());
            result.put("queue", internalLightJobsQueueSummary == null ? "" : internalLightJobsQueueSummary);
            result.put("message", "estado do runtime interno sincronizado");
            return result;
        }
        if ("apk_job_history".equals(type)) {
            safePutPayload(result, "history", internalJobHistoryJson());
            result.put("historyText", internalJobHistoryText());
            result.put("message", "histórico local de jobs internos enviado");
            return result;
        }
        if ("apk_device_diagnostic".equals(type)) {
            JSONObject device = deviceDiagnosticSnapshot();
            safePutPayload(result, "device", device);
            internalDiagnosticsSummary = "aparelho ok · " + quickBatteryLabel();
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", "diagnóstico do aparelho concluído pelo APK");
            return result;
        }
        if ("apk_network_diagnostic".equals(type)) {
            JSONObject network = networkDiagnosticSnapshot(serverUrl);
            JSONObject python = pythonNetworkDiagnosticSnapshot(serverUrl);
            safePutPayload(result, "network", network);
            safePutPayload(result, "python", python);
            boolean ok = network.optBoolean("ok", false) && python.optBoolean("ok", false);
            internalDiagnosticsSummary = ok ? "rede ok · Python interno" : "rede com atenção";
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!ok) {
                result.put("ok", false);
                result.put("error", network.optString("error", python.optString("error", "rede indisponível")));
            }
            result.put("message", ok ? "diagnóstico de rede concluído pelo Python interno" : "diagnóstico de rede encontrou problema");
            return result;
        }
        if ("apk_push_diagnostic".equals(type)) {
            JSONObject push = pushDiagnosticSnapshot();
            safePutPayload(result, "push", push);
            internalDiagnosticsSummary = "push " + fcmCompactLabel();
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", "diagnóstico de push concluído pelo APK");
            return result;
        }
        if ("apk_update_diagnostic".equals(type)) {
            JSONObject update = checkUpdateForJob(serverUrl);
            safePutPayload(result, "update", update);
            internalDiagnosticsSummary = "update " + updateChecklistLabel();
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!update.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", update.optString("error", "checagem de atualização falhou"));
            }
            result.put("message", update.optBoolean("ok", false) ? "diagnóstico de atualização concluído" : "diagnóstico de atualização falhou");
            return result;
        }
        if ("apk_runtime_diagnostic".equals(type)) {
            JSONObject runtime = runtimeDiagnosticSnapshot();
            safePutPayload(result, "runtime", runtime);
            internalDiagnosticsSummary = "runtime interno ok";
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", "diagnóstico do runtime interno concluído");
            return result;
        }
        if ("apk_storage_diagnostic".equals(type)) {
            JSONObject storage = storageSnapshot();
            JSONObject python = pythonStorageCheckSnapshot();
            safePutPayload(result, "storage", storage);
            safePutPayload(result, "python", python);
            internalStorageSummary = python.optBoolean("ok", false) ? python.optString("summary", storageSummary(storage)) : storageSummary(storage);
            internalDiagnosticsSummary = "armazenamento " + internalStorageSummary;
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", python.optBoolean("ok", false) ? "armazenamento verificado pelo Python interno" : "diagnóstico de armazenamento interno concluído");
            return result;
        }
        if ("apk_worker_bridge_status".equals(type)) {
            JSONObject bridge = workerBridgeStatusSnapshot();
            safePutPayload(result, "bridge", bridge);
            internalBridgeSummary = bridge.optString("summary", "ponte atualizada");
            internalDiagnosticsSummary = internalBridgeSummary;
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", "estado da ponte APK/Termux reportado");
            return result;
        }
        if ("apk_collect_status_bundle".equals(type) || "apk_force_status_bundle".equals(type)) {
            JSONObject bundle = collectStatusBundle(serverUrl);
            JSONObject python = pythonStatusBundleSnapshot(serverUrl);
            safePutPayload(result, "bundle", bundle);
            safePutPayload(result, "python", python);
            internalDiagnosticsSummary = python.optBoolean("ok", false) ? "pacote de status Python enviado" : "pacote de status enviado";
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", python.optBoolean("ok", false) ? "pacote completo gerado com Python interno" : "pacote completo de status do APK enviado para a VPS");
            return result;
        }
        if ("apk_test_notification".equals(type)) {
            String state = showInternalTestNotification();
            result.put("notificationState", state);
            result.put("permission", hasNotificationPermission() ? "granted" : "missing");
            if (!"displayed".equals(state)) {
                result.put("ok", false);
                result.put("error", notificationDetail(state));
            }
            result.put("message", "displayed".equals(state) ? "notificação de teste exibida pelo APK" : "notificação de teste não exibida: " + notificationDetail(state));
            return result;
        }
        if ("apk_repair_local_state".equals(type)) {
            clearTransientApkNetworkError();
            prepareInternalRuntimePreview();
            internalRuntimeOnline = true;
            internalRuntimeHeartbeatState = "reparado por job interno";
            internalRuntimeLastHeartbeatAt = System.currentTimeMillis();
            internalDiagnosticsSummary = "estado local reparado";
            internalDiagnosticsLastAt = System.currentTimeMillis();
            prefs.edit()
                    .remove("fcm_disabled_until")
                    .remove("internal_jobs_wake_requested_at")
                    .putString("internal_runtime_repair_at", String.valueOf(System.currentTimeMillis()))
                    .apply();
            sendInternalRuntimeHeartbeat(false, "job_repair_local_state");
            safePutPayload(result, "status", statusSnapshot());
            result.put("message", "estado local seguro reparado pelo APK");
            return result;
        }
        if ("apk_reset_job_history".equals(type)) {
            prefs.edit()
                    .putString("internal_job_history", "[]")
                    .putString("internal_completed_job_ids", "[]")
                    .apply();
            internalLightJobsLastSummary = "histórico local limpo";
            result.put("message", "histórico local de jobs internos limpo");
            return result;
        }
        if ("apk_clear_app_cache".equals(type) || "apk_cache_cleanup".equals(type) || "apk_cleanup_runtime_cache".equals(type) || "apk_trim_cache".equals(type)) {
            long bytes = clearInternalJobCache();
            result.put("bytesCleared", bytes);
            result.put("message", "cache interno do APK limpo");
            return result;
        }
        if ("apk_update_storage_cleanup".equals(type)) {
            JSONObject cleanup = cleanupUpdateArtifacts(false, "job_update_storage_cleanup");
            safePutPayload(result, "cleanup", cleanup);
            safePutPayload(result, "storage", storageSnapshot());
            result.put("ok", cleanup.optBoolean("ok", true));
            if (!cleanup.optBoolean("ok", true)) {
                result.put("error", cleanup.optString("error", "limpeza de updates falhou"));
            }
            result.put("message", cleanup.optString("summary", "limpeza segura de updates concluída"));
            return result;
        }
        if ("apk_sync_profile".equals(type) || "apk_sync_profile_now".equals(type)) {
            String requested = normalizeProfile(jobPayload.optString("profile", appliedProfile()));
            boolean localSynced = syncProfileToLocalAgent(requested);
            saveLocalFields(requested);
            result.put("profile", requested);
            result.put("profileLabel", profileLabel(requested));
            result.put("localAgentSynced", localSynced);
            result.put("message", localSynced ? "perfil sincronizado pelo APK" : "perfil salvo no APK; Termux ainda não confirmou");
            return result;
        }
        if ("apk_native_worker_status".equals(type)) {
            JSONObject nativeStatus = nativeWorkerStatusSnapshot();
            safePutPayload(result, "nativeWorker", nativeStatus);
            sendNativeWorkerHeartbeat(false, "job_native_worker_status");
            result.put("message", "estado do worker nativo enviado pelo APK");
            return result;
        }
        if ("apk_native_boot_status".equals(type)) {
            JSONObject boot = nativeBootSnapshot();
            safePutPayload(result, "boot", boot);
            nativeBootSummary = boot.optString("summary", "boot nativo verificado");
            internalDiagnosticsSummary = nativeBootSummary;
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", "boot nativo verificado pelo APK");
            return result;
        }
        if ("apk_local_shell_probe".equals(type)) {
            JSONObject shell = localShellProbeSnapshot();
            safePutPayload(result, "shell", shell);
            nativeShellSummary = shell.optString("summary", "shell controlado verificado");
            internalDiagnosticsSummary = nativeShellSummary;
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", "shell controlado do APK verificado");
            return result;
        }
        if ("apk_python_runtime_probe".equals(type) || "apk_python_health_check".equals(type)) {
            JSONObject python = pythonRuntimeProbeSnapshot();
            safePutPayload(result, "python", python);
            nativePythonSummary = python.optString("summary", "Python interno verificado");
            internalDiagnosticsSummary = nativePythonSummary;
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!python.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", python.optString("error", "Python interno falhou"));
            }
            result.put("message", python.optBoolean("ok", false) ? "Python interno real verificado pelo APK" : "Python interno falhou no APK");
            return result;
        }
        if ("apk_python_runtime_info".equals(type)) {
            JSONObject python = pythonRuntimeInfoSnapshot();
            safePutPayload(result, "python", python);
            nativePythonSummary = python.optString("summary", "runtime Python reportado");
            internalDiagnosticsSummary = nativePythonSummary;
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!python.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", python.optString("error", "runtime Python falhou"));
            }
            result.put("message", python.optBoolean("ok", false) ? "informações do runtime Python enviadas" : "runtime Python indisponível");
            return result;
        }
        if ("apk_python_status_bundle".equals(type)) {
            JSONObject python = pythonStatusBundleSnapshot(serverUrl);
            safePutPayload(result, "python", python);
            internalDiagnosticsSummary = python.optString("summary", "status Python enviado");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!python.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", python.optString("error", "status Python falhou"));
            }
            result.put("message", python.optBoolean("ok", false) ? "bundle de status gerado pelo Python interno" : "bundle Python falhou");
            return result;
        }
        if ("apk_python_storage_check".equals(type)) {
            JSONObject python = pythonStorageCheckSnapshot();
            safePutPayload(result, "python", python);
            internalStorageSummary = python.optString("summary", "storage Python verificado");
            internalDiagnosticsSummary = internalStorageSummary;
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!python.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", python.optString("error", "storage Python falhou"));
            }
            result.put("message", python.optBoolean("ok", false) ? "armazenamento verificado pelo Python interno" : "checagem Python de armazenamento falhou");
            return result;
        }
        if ("apk_python_log_summary".equals(type)) {
            JSONObject python = pythonLogSummarySnapshot();
            safePutPayload(result, "python", python);
            internalDiagnosticsSummary = python.optString("summary", "resumo Python de logs enviado");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!python.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", python.optString("error", "resumo Python falhou"));
            }
            result.put("message", python.optBoolean("ok", false) ? "histórico resumido pelo Python interno" : "resumo Python de logs falhou");
            return result;
        }
        if ("apk_python_network_diagnostic".equals(type)) {
            JSONObject python = pythonNetworkDiagnosticSnapshot(serverUrl);
            safePutPayload(result, "python", python);
            internalDiagnosticsSummary = python.optString("summary", "diagnóstico Python de rede enviado");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!python.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", python.optString("error", "diagnóstico Python de rede falhou"));
            }
            result.put("message", python.optBoolean("ok", false) ? "rede diagnosticada pelo Python interno" : "diagnóstico Python de rede falhou");
            return result;
        }
        if ("apk_python_runtime_files_check".equals(type)) {
            JSONObject python = pythonRuntimeFilesCheckSnapshot();
            safePutPayload(result, "python", python);
            internalDiagnosticsSummary = python.optString("summary", "arquivos runtime verificados pelo Python");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!python.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", python.optString("error", "verificação Python de arquivos falhou"));
            }
            result.put("message", python.optBoolean("ok", false) ? "arquivos do runtime verificados pelo Python interno" : "verificação Python de arquivos falhou");
            return result;
        }

        if ("apk_runtime_foreground_probe".equals(type)) {
            JSONObject foreground = foregroundRuntimeSnapshot("probe");
            safePutPayload(result, "foregroundRuntime", foreground);
            internalDiagnosticsSummary = foreground.optString("summary", "serviço persistente verificado");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", "runtime persistente do APK verificado");
            return result;
        }
        if ("apk_runtime_foreground_start".equals(type)) {
            JSONObject foreground = startForegroundRuntime("job");
            safePutPayload(result, "foregroundRuntime", foreground);
            internalDiagnosticsSummary = foreground.optString("summary", "serviço persistente iniciado");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", "runtime persistente iniciado pelo APK");
            return result;
        }
        if ("apk_runtime_foreground_stop".equals(type)) {
            JSONObject foreground = stopForegroundRuntime("job");
            safePutPayload(result, "foregroundRuntime", foreground);
            internalDiagnosticsSummary = foreground.optString("summary", "serviço persistente parado");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", "runtime persistente parado pelo APK");
            return result;
        }
        if ("apk_linux_strategy_plan".equals(type) || "apk_linux_manifest_plan".equals(type) || "apk_minecraft_bedrock_assisted_install_plan".equals(type)) {
            String focus = "strategy";
            if ("apk_linux_manifest_plan".equals(type)) focus = "manifest";
            if ("apk_minecraft_bedrock_assisted_install_plan".equals(type)) focus = "bedrock_assisted";
            JSONObject plan = linuxAssistedInstallSnapshot(focus);
            safePutPayload(result, "linuxAssistedInstall", plan);
            internalDiagnosticsSummary = plan.optString("summary", "plano assistido Linux gerado");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!plan.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", plan.optString("error", "plano assistido pendente"));
            }
            result.put("message", plan.optBoolean("ok", false) ? "plano assistido Linux/Bedrock gerado sem baixar nada" : "plano assistido Linux/Bedrock pendente");
            return result;
        }

        if ("apk_core_linux_rootfs_import_status".equals(type)
                || "apk_core_linux_rootfs_real_status".equals(type)
                || "apk_core_linux_rootfs_import_validate".equals(type)
                || "apk_core_linux_rootfs_import_abort".equals(type)) {
            JSONObject rootfsImport;
            if ("apk_core_linux_rootfs_import_abort".equals(type)) {
                rootfsImport = CoreLinuxRootfsImportManager.abort(this, coreLinuxDir());
            } else if ("apk_core_linux_rootfs_import_validate".equals(type) || "apk_core_linux_rootfs_real_status".equals(type)) {
                rootfsImport = CoreLinuxRootfsImportManager.validateActive(this, coreLinuxDir());
            } else {
                rootfsImport = CoreLinuxRootfsImportManager.status(this, coreLinuxDir());
            }
            safePutPayload(result, "rootfsImport", rootfsImport);
            JSONObject rootfsState = rootfsImport.optJSONObject("rootfs");
            coreLinuxSummary = rootfsImport.optString("summary", "rootfs import em modo seguro");
            coreLinuxState = rootfsImport.optString("state", "rootfs_import");
            coreLinuxPrepared = rootfsImport.optBoolean("rootfsReady", coreLinuxPrepared)
                    || (rootfsState != null && rootfsState.optBoolean("rootfsReady", false));
            coreLinuxLastCheckAt = System.currentTimeMillis();
            internalDiagnosticsSummary = coreLinuxSummary;
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!rootfsImport.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", rootfsImport.optString("summary", "rootfs import pendente"));
            }
            result.put("message", rootfsImport.optString("summary", "rootfs import verificado"));
            return result;
        }

        if ("apk_core_linux_rootfs_status".equals(type)
                || "apk_core_linux_rootfs_preflight".equals(type)
                || "apk_core_linux_rootfs_prepare".equals(type)
                || "apk_core_linux_rootfs_validate".equals(type)
                || "apk_core_linux_rootfs_repair".equals(type)
                || "apk_core_linux_rootfs_clean_staging".equals(type)) {
            String action = "status";
            if ("apk_core_linux_rootfs_preflight".equals(type)) action = "preflight";
            if ("apk_core_linux_rootfs_prepare".equals(type)) action = "prepare";
            if ("apk_core_linux_rootfs_validate".equals(type)) action = "validate";
            if ("apk_core_linux_rootfs_repair".equals(type)) action = "repair";
            if ("apk_core_linux_rootfs_clean_staging".equals(type)) action = "clean_staging";
            JSONObject rootfs = CoreLinuxRuntimeManager.rootfsSnapshot(this, coreLinuxDir(), action);
            safePutPayload(result, "rootfs", rootfs);
            JSONObject rootfsState = rootfs.optJSONObject("rootfs");
            coreLinuxSummary = rootfs.optString("summary", "rootfs interno em modo seguro");
            coreLinuxState = rootfs.optString("state", "rootfs");
            coreLinuxPrepared = rootfs.optBoolean("rootfsReady", false);
            coreLinuxLastCheckAt = System.currentTimeMillis();
            internalDiagnosticsSummary = coreLinuxSummary;
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!rootfs.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", rootfs.optString("summary", "rootfs interno pendente"));
            }
            result.put("message", rootfs.optString("summary", "rootfs interno verificado sem Python/Termux"));
            if (rootfsState != null) safePutPayload(result, "rootfsState", rootfsState);
            return result;
        }

        if ("apk_core_linux_runner_status".equals(type)
                || "apk_core_linux_runner_preflight".equals(type)
                || "apk_core_linux_runner_requirements".equals(type)) {
            String action = "status";
            if ("apk_core_linux_runner_preflight".equals(type)) action = "preflight";
            if ("apk_core_linux_runner_requirements".equals(type)) action = "requirements";
            JSONObject runner = CoreLinuxRunnerPreflightManager.preflight(this, coreLinuxDir(), action);
            safePutPayload(result, "coreLinuxRunner", runner);
            try {
                JSONObject core = coreLinuxPublicSnapshot();
                safePutPayload(result, "coreLinux", core);
                coreLinuxSummary = firstNonEmpty(core.optString("summary", ""), coreLinuxSummary);
                coreLinuxState = firstNonEmpty(core.optString("state", ""), coreLinuxState);
                coreLinuxPrepared = core.optBoolean("prepared", coreLinuxPrepared);
            } catch (Throwable ignored) {
            }
            internalDiagnosticsSummary = runner.optString("summary", "runner preflight verificado");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", runner.optString("summary", "Runner preflight verificado sem iniciar Bedrock"));
            result.put("runnerReady", runner.optBoolean("runnerReady", false));
            result.put("runnerBlocked", runner.optBoolean("runnerBlocked", true));
            result.put("bedrockStarted", false);
            result.put("shellOpened", false);
            return result;
        }

        if ("apk_core_linux_runtime_smoke_test".equals(type)) {
            JSONObject nativeExecutor = coreLinuxNativeExecutorSnapshot("test");
            JSONObject smoke = CoreLinuxRuntimeManager.smokeTest(this, coreLinuxDir(), nativeExecutor);
            safePutPayload(result, "coreLinuxSmokeTest", smoke);
            coreLinuxSummary = smoke.optString("summary", "smoke test Core Linux executado");
            coreLinuxState = smoke.optString("state", "smoke_test");
            coreLinuxPrepared = smoke.optBoolean("ok", false);
            coreLinuxLastCheckAt = System.currentTimeMillis();
            internalDiagnosticsSummary = coreLinuxSummary;
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!smoke.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", smoke.optString("summary", "smoke test Core Linux pendente"));
            }
            result.put("message", smoke.optString("summary", "Core Linux smoke test executado sem Termux"));
            return result;
        }



        if ("apk_core_linux_rootfs_smoke_test".equals(type)) {
            JSONObject nativeExecutor = coreLinuxNativeExecutorSnapshot("test");
            JSONObject smoke = CoreLinuxRuntimeManager.rootfsProotSmokeTest(this, coreLinuxDir(), nativeExecutor);
            safePutPayload(result, "coreLinuxRootfsSmokeTest", smoke);
            coreLinuxSummary = smoke.optString("summary", "smoke rootfs Core Linux executado");
            coreLinuxState = smoke.optString("state", "rootfs_smoke_test");
            coreLinuxPrepared = smoke.optBoolean("ok", false);
            coreLinuxLastCheckAt = System.currentTimeMillis();
            internalDiagnosticsSummary = coreLinuxSummary;
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!smoke.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", smoke.optString("summary", "smoke rootfs Core Linux pendente"));
            }
            result.put("message", smoke.optString("summary", "Core Linux rootfs smoke executado sem Termux"));
            return result;
        }

        if ("apk_core_linux_box64_preflight".equals(type)) {
            JSONObject nativeExecutor = coreLinuxNativeExecutorSnapshot("test");
            JSONObject box64 = CoreLinuxRuntimeManager.box64IntakePreflight(this, coreLinuxDir(), nativeExecutor);
            safePutPayload(result, "coreLinuxBox64Preflight", box64);
            coreLinuxSummary = box64.optString("summary", "preflight Box64 Core Linux executado");
            coreLinuxState = box64.optString("state", "box64_intake_preflight");
            coreLinuxPrepared = box64.optBoolean("ok", coreLinuxPrepared);
            coreLinuxLastCheckAt = System.currentTimeMillis();
            internalDiagnosticsSummary = coreLinuxSummary;
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!box64.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", box64.optString("summary", "Box64 pendente"));
            }
            result.put("message", box64.optString("summary", "Box64 intake/preflight executado sem iniciar Bedrock"));
            return result;
        }

        if ("apk_core_linux_box64_smoke_test".equals(type)) {
            JSONObject nativeExecutor = coreLinuxNativeExecutorSnapshot("test");
            JSONObject smoke = CoreLinuxRuntimeManager.box64VersionSmokeTest(this, coreLinuxDir(), nativeExecutor);
            safePutPayload(result, "coreLinuxBox64SmokeTest", smoke);
            coreLinuxSummary = smoke.optString("summary", "smoke Box64 Core Linux executado");
            coreLinuxState = smoke.optString("state", "box64_version_smoke");
            coreLinuxPrepared = smoke.optBoolean("ok", coreLinuxPrepared);
            coreLinuxLastCheckAt = System.currentTimeMillis();
            internalDiagnosticsSummary = coreLinuxSummary;
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!smoke.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", smoke.optString("summary", "Box64 smoke pendente"));
            }
            result.put("message", smoke.optString("summary", "Box64 smoke controlado executado sem iniciar Bedrock"));
            return result;
        }

        if ("apk_core_linux_native_executor_probe".equals(type)
                || "apk_core_linux_native_executor_test".equals(type)
                || "apk_core_linux_native_runtime_status".equals(type)
                || "apk_core_linux_internal_repair".equals(type)) {
            String action = "probe";
            if ("apk_core_linux_native_executor_test".equals(type)) action = "test";
            if ("apk_core_linux_native_runtime_status".equals(type)) action = "status";
            if ("apk_core_linux_internal_repair".equals(type)) action = "repair";
            JSONObject nativeExecutor = coreLinuxNativeExecutorSnapshot(action);
            safePutPayload(result, "nativeExecutor", nativeExecutor);
            try {
                JSONObject runtime = CoreLinuxRuntimeManager.runtimeSnapshot(this, coreLinuxDir(), "executor", nativeExecutor);
                safePutPayload(result, "coreLinuxInternal", runtime);
                coreLinuxSummary = runtime.optString("summary", coreLinuxSummary);
                coreLinuxState = runtime.optString("state", coreLinuxState);
                coreLinuxPrepared = runtime.optBoolean("ok", coreLinuxPrepared);
                coreLinuxLastCheckAt = System.currentTimeMillis();
            } catch (Throwable ignored) {
            }
            internalDiagnosticsSummary = nativeExecutor.optString("summary", "executor nativo interno atualizado");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!nativeExecutor.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", nativeExecutor.optString("summary", "executor nativo interno pendente"));
            }
            result.put("message", nativeExecutor.optString("summary", "executor nativo interno atualizado"));
            return result;
        }

        if ("apk_core_linux_internal_probe".equals(type)
                || "apk_core_linux_internal_bootstrap".equals(type)
                || "apk_core_linux_executor_probe".equals(type)
                || "apk_core_linux_rootfs_manifest".equals(type)
                || "apk_core_linux_box64_manifest".equals(type)
                || "apk_core_linux_bedrock_preflight".equals(type)) {
            String action = "probe";
            if ("apk_core_linux_internal_bootstrap".equals(type)) action = "bootstrap";
            if ("apk_core_linux_executor_probe".equals(type)) action = "executor";
            if ("apk_core_linux_rootfs_manifest".equals(type)) action = "rootfs";
            if ("apk_core_linux_box64_manifest".equals(type)) action = "box64";
            if ("apk_core_linux_bedrock_preflight".equals(type)) action = "bedrock_preflight";
            JSONObject core = coreLinuxStaticSnapshot(action);
            safePutPayload(result, "coreLinuxInternal", core);
            internalDiagnosticsSummary = core.optString("summary", "Core Linux interno em modo seguro");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", core.optString("summary", "Core Linux interno verificado sem Python/Termux"));
            return result;
        }

        if ("apk_linux_runtime_probe".equals(type) || "apk_linux_rootfs_probe".equals(type) || "apk_linux_box64_probe".equals(type)) {
            String focus = "runtime";
            if ("apk_linux_rootfs_probe".equals(type)) focus = "rootfs";
            if ("apk_linux_box64_probe".equals(type)) focus = "box64";
            JSONObject linux = coreLinuxStaticSnapshot(focus);
            safePutPayload(result, "linuxRuntime", linux);
            internalDiagnosticsSummary = linux.optString("summary", "runtime Linux verificado em modo seguro");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", linux.optString("summary", "Core Linux Runtime verificado sem Python/Termux"));
            return result;
        }
        if ("apk_linux_prepare_directories".equals(type)) {
            prepareCoreLinuxRuntimeState();
            JSONObject linux = coreLinuxProvisionPlanSnapshot("prepare_directories");
            safePutPayload(result, "linuxProvision", linux);
            internalDiagnosticsSummary = linux.optString("summary", "provisioner preparado");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", "diretórios e planos do Core Linux Runtime preparados sem instalar nada");
            return result;
        }
        if ("apk_linux_provisioner_probe".equals(type) || "apk_linux_generate_setup_plan".equals(type)) {
            JSONObject linux = coreLinuxProvisionPlanSnapshot("apk_linux_generate_setup_plan".equals(type) ? "setup_plan" : "provisioner");
            safePutPayload(result, "linuxProvision", linux);
            internalDiagnosticsSummary = linux.optString("summary", "plano Linux gerado");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!linux.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", linux.optString("error", "provisioner Linux pendente"));
            }
            result.put("message", linux.optBoolean("ok", false) ? "plano do Core Linux Runtime gerado pelo APK" : "provisioner Linux ainda pendente");
            return result;
        }
        if ("apk_minecraft_bedrock_install_plan".equals(type) || "apk_minecraft_bedrock_properties_template".equals(type)) {
            JSONObject plan = bedrockInstallPlanSnapshot("apk_minecraft_bedrock_properties_template".equals(type) ? "properties_template" : "install_plan");
            safePutPayload(result, "bedrockPlan", plan);
            internalDiagnosticsSummary = plan.optString("summary", "plano Bedrock gerado");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!plan.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", plan.optString("error", "plano Bedrock pendente"));
            }
            result.put("message", plan.optBoolean("ok", false) ? "plano Bedrock gerado sem iniciar servidor" : "plano Bedrock pendente");
            return result;
        }
        if ("apk_minecraft_bedrock_requirements".equals(type)) {
            JSONObject requirements = bedrockServerLightweightTestSnapshot();
            safePutPayload(result, "bedrock", requirements);
            internalDiagnosticsSummary = requirements.optString("summary", "requisitos Bedrock avaliados");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!requirements.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", requirements.optString("error", "requisitos Bedrock pendentes"));
            }
            result.put("message", "requisitos do Bedrock avaliados sem instalar nada");
            return result;
        }
        if ("apk_minecraft_bedrock_probe".equals(type) || "apk_minecraft_bedrock_status".equals(type)) {
            JSONObject bedrock = bedrockServerLightweightTestSnapshot();
            safePutPayload(result, "bedrock", bedrock);
            internalDiagnosticsSummary = bedrock.optString("summary", "Bedrock diagnosticado");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!bedrock.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", bedrock.optString("error", "Bedrock não configurado"));
            }
            result.put("message", bedrock.optBoolean("ok", false) ? "Bedrock Manager diagnosticado pelo APK" : "Bedrock Manager ainda não configurado");
            return result;
        }
        if ("apk_minecraft_bedrock_prepare_files".equals(type)
                || "apk_minecraft_bedrock_start_plan".equals(type)
                || "apk_minecraft_bedrock_stop_plan".equals(type)
                || "apk_minecraft_bedrock_logs_status".equals(type)) {
            String focus = "status";
            if ("apk_minecraft_bedrock_prepare_files".equals(type)) focus = "prepare_properties";
            if ("apk_minecraft_bedrock_start_plan".equals(type)) focus = "start_plan";
            if ("apk_minecraft_bedrock_stop_plan".equals(type)) focus = "stop_plan";
            if ("apk_minecraft_bedrock_logs_status".equals(type)) focus = "logs_status";
            JSONObject manager = bedrockServerLightweightTestSnapshot();
            manager.put("focus", focus);
            safePutPayload(result, "bedrockManager", manager);
            internalDiagnosticsSummary = manager.optString("summary", "Bedrock Manager em modo seguro");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", manager.optString("summary", "Bedrock Manager verificado sem Python/Termux"));
            return result;
        }
        if ("apk_minecraft_bedrock_installer_status".equals(type)
                || "apk_minecraft_bedrock_validate_device".equals(type)
                || "apk_minecraft_bedrock_choose_strategy_plan".equals(type)
                || "apk_minecraft_bedrock_prepare_environment_plan".equals(type)
                || "apk_minecraft_bedrock_download_manifest".equals(type)
                || "apk_minecraft_bedrock_final_preflight".equals(type)) {
            String focus = "status";
            if ("apk_minecraft_bedrock_validate_device".equals(type)) focus = "validate_device";
            if ("apk_minecraft_bedrock_choose_strategy_plan".equals(type)) focus = "choose_strategy";
            if ("apk_minecraft_bedrock_prepare_environment_plan".equals(type)) focus = "prepare_environment";
            if ("apk_minecraft_bedrock_download_manifest".equals(type)) focus = "download_manifest";
            if ("apk_minecraft_bedrock_final_preflight".equals(type)) focus = "final_preflight";
            JSONObject wizard = bedrockServerLightweightTestSnapshot();
            wizard.put("focus", focus);
            wizard.put("installerIsolated", true);
            safePutPayload(result, "bedrockInstaller", wizard);
            internalDiagnosticsSummary = wizard.optString("summary", "instalador Bedrock em modo seguro");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", wizard.optString("summary", "instalador Bedrock verificado sem Python/Termux"));
            return result;
        }
        if ("apk_minecraft_bedrock_runtime_status".equals(type)
                || "apk_minecraft_bedrock_runtime_start".equals(type)
                || "apk_minecraft_bedrock_runtime_stop".equals(type)
                || "apk_minecraft_bedrock_runtime_logs".equals(type)
                || "apk_minecraft_bedrock_runner_status".equals(type)
                || "apk_minecraft_bedrock_runner_preflight".equals(type)
                || "apk_minecraft_bedrock_runner_start".equals(type)
                || "apk_minecraft_bedrock_runner_stop".equals(type)
                || "apk_minecraft_bedrock_console_tail".equals(type)
                || "apk_minecraft_bedrock_console_command".equals(type)
                || "apk_minecraft_bedrock_runtime_repair".equals(type)) {
            String action = "status";
            if ("apk_minecraft_bedrock_runtime_start".equals(type) || "apk_minecraft_bedrock_runner_start".equals(type)) action = "start";
            if ("apk_minecraft_bedrock_runtime_stop".equals(type) || "apk_minecraft_bedrock_runner_stop".equals(type)) action = "stop";
            if ("apk_minecraft_bedrock_runtime_logs".equals(type) || "apk_minecraft_bedrock_console_tail".equals(type)) action = "console_tail";
            if ("apk_minecraft_bedrock_runner_preflight".equals(type)) action = "preflight";
            if ("apk_minecraft_bedrock_console_command".equals(type)) action = "console_command_remote_blocked";
            if ("apk_minecraft_bedrock_runtime_repair".equals(type)) action = "repair";
            if ("stop".equals(action)) {
                try { stopBedrockService("job-stop-safe"); } catch (Throwable ignored) {}
            }
            JSONObject runtime = bedrockRuntimeStaticSnapshot(action);
            runtime.put("startBlocked", "start".equals(action));
            runtime.put("isolationMode", BEDROCK_RUNTIME_ISOLATED);
            safePutPayload(result, "bedrockRuntime", runtime);
            internalDiagnosticsSummary = runtime.optString("summary", "runtime Bedrock em modo seguro");
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", "start".equals(action) ? "runtime Bedrock bloqueado com segurança pelo APK" : runtime.optString("summary", "runtime Bedrock verificado sem Python/Termux"));
            return result;
        }
        if ("apk_download_small".equals(type)) {
            JSONObject download = downloadSmallJobPayload(serverUrl, jobPayload);
            safePutPayload(result, "download", download);
            if (!download.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", download.optString("error", "download pequeno falhou"));
            }
            result.put("message", download.optBoolean("ok", false) ? "download pequeno concluído pelo APK" : "download pequeno falhou");
            return result;
        }
        if ("apk_verify_file".equals(type)) {
            JSONObject verify = verifyCachedJobFile(jobPayload);
            safePutPayload(result, "verify", verify);
            if (!verify.optBoolean("ok", false)) {
                result.put("ok", false);
                result.put("error", verify.optString("error", "verificação de arquivo falhou"));
            }
            result.put("message", verify.optBoolean("ok", false) ? "arquivo interno verificado" : "verificação de arquivo falhou");
            return result;
        }
        result.put("ok", false);
        result.put("error", "job interno não permitido pelo APK: " + type);
        return result;
    }

    private JSONObject permissionsSnapshot() throws Exception {
        JSONObject permissions = new JSONObject();
        permissions.put("notifications", hasNotificationPermission() ? "granted" : "missing");
        permissions.put("install_updates", hasInstallPermission() ? "granted" : "missing");
        permissions.put("background", hasBatteryPermission() ? "allowed" : "restricted");
        permissions.put("all_required", hasRequiredAppPermissions());
        return permissions;
    }

    private JSONObject storageSnapshot() throws Exception {
        JSONObject storage = new JSONObject();
        File files = getFilesDir();
        File cache = getCacheDir();
        File runtime = internalRuntimePath == null || internalRuntimePath.trim().isEmpty() ? new File(files, "core-runtime") : new File(internalRuntimePath);
        File jobCache = new File(cache, "core-worker-jobs");
        JSONObject updates = updateArtifactsSnapshot();
        storage.put("files_bytes", directorySize(files));
        storage.put("cache_bytes", directorySize(cache));
        storage.put("runtime_bytes", directorySize(runtime));
        storage.put("job_cache_bytes", directorySize(jobCache));
        storage.put("job_cache_files", directoryFileCount(jobCache));
        storage.put("update_artifacts_bytes", updates.optLong("bytes", 0L));
        storage.put("update_artifacts_files", updates.optInt("files", 0));
        safePutPayload(storage, "update_artifacts", updates);
        storage.put("files_dir", files == null ? "" : files.getName());
        storage.put("cache_dir", cache == null ? "" : cache.getName());
        storage.put("scope", "app-specific-internal-and-external-app-specific");
        storage.put("summary", storageSummary(storage));
        return storage;
    }

    private String storageSummary(JSONObject storage) {
        try {
            long cacheBytes = storage.optLong("cache_bytes", 0L);
            long jobBytes = storage.optLong("job_cache_bytes", 0L);
            long updateBytes = storage.optLong("update_artifacts_bytes", 0L);
            if (updateBytes > 0L) {
                return "cache " + humanBytes(cacheBytes) + " · updates " + humanBytes(updateBytes);
            }
            return "cache " + humanBytes(cacheBytes) + " · jobs " + humanBytes(jobBytes);
        } catch (Throwable ignored) {
            return "cache interno ok";
        }
    }

    private long directorySize(File file) {
        if (file == null || !file.exists()) return 0L;
        if (file.isFile()) return Math.max(0L, file.length());
        long total = 0L;
        File[] children = file.listFiles();
        if (children != null) {
            for (File child : children) total += directorySize(child);
        }
        return total;
    }

    private int directoryFileCount(File file) {
        if (file == null || !file.exists()) return 0;
        if (file.isFile()) return 1;
        int total = 0;
        File[] children = file.listFiles();
        if (children != null) {
            for (File child : children) total += directoryFileCount(child);
        }
        return total;
    }

    private String humanBytes(long bytes) {
        if (bytes < 1024L) return bytes + " B";
        double kb = bytes / 1024.0;
        if (kb < 1024.0) return String.format(Locale.ROOT, "%.1f KiB", kb);
        double mb = kb / 1024.0;
        return String.format(Locale.ROOT, "%.1f MiB", mb);
    }

    private JSONObject deviceDiagnosticSnapshot() throws Exception {
        JSONObject device = new JSONObject();
        device.put("manufacturer", Build.MANUFACTURER);
        device.put("model", Build.MODEL);
        device.put("android_sdk", Build.VERSION.SDK_INT);
        safePutPayload(device, "battery", batterySnapshot());
        safePutPayload(device, "permissions", permissionsSnapshot());
        device.put("summary", quickBatteryLabel() + " · Android " + Build.VERSION.SDK_INT);
        return device;
    }

    private JSONObject networkDiagnosticSnapshot(String serverUrl) throws Exception {
        JSONObject network = networkSnapshot(serverUrl);
        boolean available = network.optBoolean("available", false);
        int ping = network.optInt("vps_ping_ms", -1);
        boolean ok = available && (serverUrl == null || serverUrl.trim().isEmpty() || ping >= 0);
        network.put("ok", ok);
        network.put("summary", quickNetworkLabel() + (ping >= 0 ? " · " + ping + "ms" : ""));
        if (!ok) network.put("error", available ? "VPS não respondeu ao ping TCP" : "sem internet ativa");
        return network;
    }

    private JSONObject pushDiagnosticSnapshot() throws Exception {
        JSONObject push = new JSONObject();
        push.put("enabled_in_build", FCM_ENABLED_IN_APK);
        push.put("state", fcmState == null ? "" : fcmState);
        push.put("token_registered_locally", prefs.getString("fcm_token", "").trim().length() > 0);
        push.put("permission", hasNotificationPermission() ? "granted" : "missing");
        push.put("fallback_local", true);
        push.put("summary", "push " + fcmCompactLabel() + " · permissão " + (hasNotificationPermission() ? "ok" : "pendente"));
        return push;
    }

    private JSONObject runtimeDiagnosticSnapshot() throws Exception {
        JSONObject runtime = runtimeSnapshot();
        runtime.put("last_diagnostic_at", internalDiagnosticsLastAt);
        runtime.put("diagnostics_summary", internalDiagnosticsSummary == null ? "" : internalDiagnosticsSummary);
        runtime.put("storage_summary", internalStorageSummary == null ? "" : internalStorageSummary);
        runtime.put("bridge_summary", internalBridgeSummary == null ? "" : internalBridgeSummary);
        runtime.put("job_history_text", internalJobHistoryText());
        return runtime;
    }

    private JSONObject workerBridgeStatusSnapshot() throws Exception {
        JSONObject bridge = new JSONObject();
        bridge.put("mode", "apk-native-python-linux-bedrock-installer");
        bridge.put("apk_internal_online", internalRuntimeOnline);
        bridge.put("apk_native_worker_online", nativeWorkerOnline);
        bridge.put("termux_worker_online", localAgentOnline);
        bridge.put("termux_agent_version", localAgentVersion == null ? "" : localAgentVersion);
        bridge.put("termux_profile", localAgentProfile == null ? "" : localAgentProfile);
        bridge.put("jobs_real_runtime", nativeWorkerOnline ? "apk-native-worker" : "apk-internal-queue");
        bridge.put("jobs_internal_runtime", "apk-native-safe-queue");
        bridge.put("termux_role", "fallback-legado");
        bridge.put("core_linux_summary", coreLinuxSummary == null ? "" : coreLinuxSummary);
        bridge.put("bedrock_summary", bedrockSummary == null ? "" : bedrockSummary);
        bridge.put("ready_for_termux_reduction", internalRuntimeOnline && hasPairing());
        String summary = nativeWorkerOnline ? "APK nativo pareado" : (internalRuntimeOnline ? "APK interno online" : "APK aguardando");
        summary += localAgentOnline ? " · Termux fallback online" : " · Termux fallback offline";
        bridge.put("summary", summary);
        return bridge;
    }

    private JSONObject coreLinuxStaticSnapshot(String focus) throws Exception {
        File base = coreLinuxDir();
        JSONObject nativeState = readJsonFile(new File(new File(base, "runtime"), "native-executor-state.json"));
        JSONObject out = CoreLinuxRuntimeManager.runtimeSnapshot(this, base, focus == null ? "status" : focus, nativeState);
        out.put("focus", focus == null ? "status" : focus);
        out.put("mode", "core-linux-runtime-v1-no-termux");
        out.put("pythonTouched", false);
        out.put("nativeStarted", false);
        out.put("bedrockStarted", false);
        coreLinuxSummary = out.optString("summary", coreLinuxSummary);
        coreLinuxState = out.optString("state", coreLinuxState);
        coreLinuxPrepared = out.optBoolean("ok", coreLinuxPrepared);
        coreLinuxLastCheckAt = System.currentTimeMillis();
        return out;
    }

    private JSONObject coreLinuxRootfsStaticSnapshot(String action) throws Exception {
        return CoreLinuxRuntimeManager.rootfsSnapshot(this, coreLinuxDir(), action == null ? "status" : action);
    }

    private JSONObject bedrockRuntimeStaticSnapshot(String focus) throws Exception {
        readBedrockServiceState();
        File runtime = new File(new File(coreLinuxDir(), "bedrock"), "runtime");
        File runnerState = new File(runtime, "runner-state.json");
        File console = new File(new File(coreLinuxDir(), "bedrock/logs"), "bedrock-console.log");
        JSONObject out = new JSONObject();
        out.put("ok", true);
        out.put("focus", focus == null ? "status" : focus);
        out.put("mode", "static-no-python");
        out.put("serviceActive", bedrockRuntimeServiceActive);
        out.put("state", bedrockRuntimeState == null ? "stopped" : bedrockRuntimeState);
        out.put("summary", bedrockRuntimeSummary == null ? "runtime Bedrock parado" : bedrockRuntimeSummary);
        out.put("runnerState", safeFileStatus(runnerState));
        out.put("consoleLog", safeFileStatus(console));
        out.put("pythonTouched", false);
        out.put("startedService", false);
        return out;
    }

    private JSONObject collectStatusBundle(String serverUrl) throws Exception {
        JSONObject bundle = new JSONObject();
        safePutPayload(bundle, "status", statusSnapshot());
        safePutPayload(bundle, "device", deviceDiagnosticSnapshot());
        safePutPayload(bundle, "network", networkDiagnosticSnapshot(serverUrl));
        safePutPayload(bundle, "push", pushDiagnosticSnapshot());
        safePutPayload(bundle, "update", updateSnapshot());
        safePutPayload(bundle, "runtime", runtimeDiagnosticSnapshot());
        safePutPayload(bundle, "linuxRuntime", coreLinuxStaticSnapshot("bundle"));
        safePutPayload(bundle, "bedrock", bedrockServerLightweightTestSnapshot());
        safePutPayload(bundle, "bedrockRuntime", bedrockRuntimeStaticSnapshot("status"));
        safePutPayload(bundle, "storage", storageSnapshot());
        safePutPayload(bundle, "bridge", workerBridgeStatusSnapshot());
        safePutPayload(bundle, "history", internalJobHistoryJson());
        bundle.put("summary", "status leve do APK coletado sem tocar rootfs/Python pesado");
        return bundle;
    }

    private JSONObject diagnosticSnapshot(String serverUrl) throws Exception {
        JSONObject diagnostic = new JSONObject();
        diagnostic.put("timestamp", System.currentTimeMillis());
        diagnostic.put("runtimeLabel", runtimeStatusLabel());
        diagnostic.put("lightJobs", internalLightJobsState == null ? "" : internalLightJobsState);
        diagnostic.put("lastLightJob", internalLightJobsLastSummary == null ? "" : internalLightJobsLastSummary);
        diagnostic.put("queue", internalLightJobsQueueSummary == null ? "" : internalLightJobsQueueSummary);
        diagnostic.put("lastJobFetchAt", internalLightJobsLastCheckAt);
        diagnostic.put("lastJobFetchReason", internalLightJobsLastFetchReason == null ? "" : internalLightJobsLastFetchReason);
        diagnostic.put("lastJobFetchAppVersion", internalLightJobsLastFetchAppVersion == null ? "" : internalLightJobsLastFetchAppVersion);
        diagnostic.put("lastJobFetchAppVersionCode", internalLightJobsLastFetchAppVersionCode);
        diagnostic.put("lastJobFetchHttpStatus", internalLightJobsLastFetchHttpStatus);
        diagnostic.put("jobsReturned", internalLightJobsLastReturnedCount);
        diagnostic.put("lastJobFetchError", internalLightJobsLastFetchError == null ? "" : internalLightJobsLastFetchError);
        safePutPayload(diagnostic, "jobHistory", internalJobHistoryJson());
        diagnostic.put("termuxWorkerOnline", localAgentOnline);
        diagnostic.put("localAgentVersion", localAgentVersion == null ? "" : localAgentVersion);
        safePutPayload(diagnostic, "battery", batterySnapshot());
        safePutPayload(diagnostic, "network", networkSnapshot(serverUrl));
        safePutPayload(diagnostic, "update", updateSnapshot());
        safePutPayload(diagnostic, "appStatus", appStatusSnapshot());
        safePutPayload(diagnostic, "permissions", permissionsSnapshot());
        safePutPayload(diagnostic, "storage", storageSnapshot());
        safePutPayload(diagnostic, "runtime", runtimeSnapshot());
        safePutPayload(diagnostic, "linuxRuntime", coreLinuxStaticSnapshot("diagnostic"));
        safePutPayload(diagnostic, "bedrock", bedrockServerLightweightTestSnapshot());
        safePutPayload(diagnostic, "bedrockRuntime", bedrockRuntimeStaticSnapshot("status"));
        safePutPayload(diagnostic, "bridge", workerBridgeStatusSnapshot());
        internalDiagnosticsSummary = "diagnóstico leve ok · rootfs/Bedrock pesado pausado";
        internalDiagnosticsLastAt = System.currentTimeMillis();
        return diagnostic;
    }

    private JSONObject checkUpdateForJob(String serverUrl) throws Exception {
        JSONObject update = new JSONObject();
        if (serverUrl == null || serverUrl.trim().isEmpty()) {
            update.put("ok", false);
            update.put("error", "VPS não configurada");
            return update;
        }
        HttpResult result = fetchLatestManifest(serverUrl);
        update.put("httpStatus", result.status);
        if (!result.ok()) {
            update.put("ok", false);
            update.put("error", compactResultBody(result.body));
            return update;
        }
        JSONObject body = new JSONObject(result.body);
        latestVersionName = body.optString("versionName", body.optString("version", ""));
        latestVersionCode = body.optInt("versionCode", -1);
        latestApkSha256 = body.optString("sha256", "");
        latestApkUrl = resolveUpdateUrl(serverUrl, body.optString("downloadUrl", body.optString("directApkUrl", body.optString("apkUrl", body.optString("url", "")))));
        latestNotificationId = body.optString("notificationId", latestNotificationId == null ? "" : latestNotificationId);
        latestUpdateAvailable = isLatestUpdateAvailable();
        update.put("ok", true);
        update.put("installedVersion", APP_VERSION);
        update.put("installedCode", BuildConfig.VERSION_CODE);
        update.put("latestVersion", latestVersionName);
        update.put("latestCode", latestVersionCode);
        update.put("available", latestUpdateAvailable);
        update.put("state", updateChecklistLabel());
        updateUpdateUi("APK " + APP_VERSION + " · " + (latestUpdateAvailable ? "atualização pronta" : "em dia"), latestUpdateAvailable, true);
        return update;
    }

    private JSONObject vpsConnectionTest(String serverUrl) throws Exception {
        JSONObject connection = new JSONObject();
        connection.put("serverConfigured", serverUrl != null && !serverUrl.trim().isEmpty());
        if (serverUrl == null || serverUrl.trim().isEmpty()) {
            connection.put("ok", false);
            connection.put("error", "VPS não configurada");
            return connection;
        }
        double tcp = measureTcpPingMs(serverUrl);
        connection.put("tcpPingMs", tcp >= 0 ? Math.round(tcp) : -1);
        HttpResult result = request("GET", serverUrl + "/core-worker/app/latest.json", null, null);
        connection.put("httpStatus", result.status);
        connection.put("ok", result.ok());
        if (!result.ok()) connection.put("error", compactResultBody(result.body));
        return connection;
    }

    private long clearInternalJobCache() {
        File dir = new File(getCacheDir(), "core-worker-jobs");
        return deleteChildren(dir);
    }

    private long deleteChildren(File file) {
        if (file == null || !file.exists()) return 0L;
        long total = 0L;
        if (file.isDirectory()) {
            File[] children = file.listFiles();
            if (children != null) {
                for (File child : children) total += deleteChildren(child);
            }
        }
        try {
            total += file.length();
        } catch (Throwable ignored) {
        }
        try {
            if (!file.equals(getCacheDir())) file.delete();
        } catch (Throwable ignored) {
        }
        return total;
    }

    private JSONObject downloadSmallJobPayload(String serverUrl, JSONObject jobPayload) throws Exception {
        JSONObject output = new JSONObject();
        String raw = jobPayload == null ? "" : jobPayload.optString("url", jobPayload.optString("path", "/core-worker/app/latest.json"));
        String url = resolveSafeJobUrl(serverUrl, raw);
        int maxBytes = jobPayload == null ? 262144 : jobPayload.optInt("maxBytes", 262144);
        if (maxBytes <= 0 || maxBytes > 262144) maxBytes = 262144;
        File dir = new File(getCacheDir(), "core-worker-jobs");
        if (!dir.exists()) dir.mkdirs();
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setConnectTimeout(6000);
        conn.setReadTimeout(9000);
        conn.setInstanceFollowRedirects(true);
        conn.setRequestProperty("Accept", "application/json,text/plain,*/*");
        int status = conn.getResponseCode();
        output.put("url", safeUrlForReport(url));
        output.put("httpStatus", status);
        if (status < 200 || status >= 300) {
            output.put("ok", false);
            output.put("error", compactResultBody(readAll(conn.getErrorStream())));
            conn.disconnect();
            return output;
        }
        InputStream input = conn.getInputStream();
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        File target = new File(dir, "small-" + System.currentTimeMillis() + ".bin");
        FileOutputStream out = new FileOutputStream(target, false);
        byte[] buffer = new byte[8192];
        int read;
        int total = 0;
        while ((read = input.read(buffer)) != -1) {
            total += read;
            if (total > maxBytes) {
                out.close();
                input.close();
                conn.disconnect();
                target.delete();
                throw new Exception("download excedeu limite seguro de " + maxBytes + " bytes");
            }
            digest.update(buffer, 0, read);
            out.write(buffer, 0, read);
        }
        out.flush();
        out.close();
        input.close();
        conn.disconnect();
        output.put("ok", true);
        output.put("bytes", total);
        String sha = bytesToHex(digest.digest());
        output.put("sha256", sha);
        output.put("cacheFile", target.getName());
        String expectedSha = jobPayload == null ? "" : jobPayload.optString("sha256", jobPayload.optString("expectedSha256", ""));
        if (expectedSha != null && expectedSha.trim().matches("(?i)[a-f0-9]{64}")) {
            boolean match = expectedSha.trim().equalsIgnoreCase(sha);
            output.put("sha256Match", match);
            if (!match) {
                output.put("ok", false);
                output.put("error", "sha256 diferente do esperado");
            }
        }
        return output;
    }

    private JSONObject verifyCachedJobFile(JSONObject jobPayload) throws Exception {
        JSONObject output = new JSONObject();
        String name = jobPayload == null ? "" : jobPayload.optString("file", jobPayload.optString("cacheFile", jobPayload.optString("name", "")));
        if (name == null || name.trim().isEmpty() || name.contains("/") || name.contains("\\") || name.contains("..")) {
            output.put("ok", false);
            output.put("error", "arquivo inválido");
            return output;
        }
        File target = new File(new File(getCacheDir(), "core-worker-jobs"), name.trim());
        if (!target.exists() || !target.isFile()) {
            output.put("ok", false);
            output.put("error", "arquivo não encontrado no cache interno");
            return output;
        }
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        InputStream input = new FileInputStream(target);
        byte[] buffer = new byte[8192];
        int read;
        long total = 0L;
        while ((read = input.read(buffer)) != -1) {
            total += read;
            digest.update(buffer, 0, read);
        }
        input.close();
        String sha = bytesToHex(digest.digest());
        output.put("ok", true);
        output.put("file", target.getName());
        output.put("bytes", total);
        output.put("sha256", sha);
        String expectedSha = jobPayload == null ? "" : jobPayload.optString("sha256", jobPayload.optString("expectedSha256", ""));
        if (expectedSha != null && expectedSha.trim().matches("(?i)[a-f0-9]{64}")) {
            boolean match = expectedSha.trim().equalsIgnoreCase(sha);
            output.put("sha256Match", match);
            if (!match) {
                output.put("ok", false);
                output.put("error", "sha256 diferente do esperado");
            }
        }
        return output;
    }

    private String resolveSafeJobUrl(String serverUrl, String raw) throws Exception {
        String value = raw == null || raw.trim().isEmpty() ? "/core-worker/app/latest.json" : raw.trim();
        if (value.startsWith("/")) {
            return serverUrl.replaceAll("/+$", "") + value;
        }
        URL base = new URL(serverUrl);
        URL target = new URL(value);
        if (!"http".equalsIgnoreCase(target.getProtocol()) && !"https".equalsIgnoreCase(target.getProtocol())) {
            throw new Exception("URL de job não permitida");
        }
        if (!base.getHost().equalsIgnoreCase(target.getHost())) {
            throw new Exception("download pequeno só permite a própria VPS");
        }
        return target.toString();
    }

    private String safeUrlForReport(String url) {
        try {
            URL parsed = new URL(url);
            return parsed.getProtocol() + "://" + parsed.getHost() + parsed.getPath();
        } catch (Throwable ignored) {
            return "url";
        }
    }

    private String bytesToHex(byte[] hash) {
        StringBuilder builder = new StringBuilder();
        if (hash == null) return "";
        for (byte b : hash) builder.append(String.format(Locale.ROOT, "%02x", b));
        return builder.toString();
    }

    private void postLightJobResult(String serverUrl, JSONObject job, JSONObject result) {
        try {
            JSONObject payload = new JSONObject();
            payload.put("jobId", job == null ? "" : job.optString("id", ""));
            payload.put("type", job == null ? "" : job.optString("type", ""));
            payload.put("installId", installId());
            payload.put("workerId", effectiveWorkerId());
            payload.put("appVersion", APP_VERSION);
            payload.put("appVersionCode", BuildConfig.VERSION_CODE);
            safePutPayload(payload, "result", result);
            request("POST", serverUrl + "/core-worker/app/jobs/result", payload, null);
        } catch (Throwable ignored) {
        }
    }

    private void reportFcmToken(String serverUrl, String token, String reason) {
        if (serverUrl == null || serverUrl.trim().isEmpty() || token == null || token.trim().isEmpty()) {
            return;
        }
        new Thread(() -> {
            try {
                JSONObject payload = statusSnapshot();
                payload.put("appVersion", APP_VERSION);
                payload.put("appVersionCode", BuildConfig.VERSION_CODE);
                payload.put("versionName", APP_VERSION);
                payload.put("versionCode", BuildConfig.VERSION_CODE);
                payload.put("workerId", effectiveWorkerId());
                payload.put("installId", installId());
                payload.put("deviceName", prefs.getString("device_name", ""));
                payload.put("fcmToken", token.trim());
                payload.put("state", "registered");
                payload.put("reason", reason == null ? "activity" : reason);
                payload.put("permission", hasNotificationPermission() ? "granted" : "missing");
                HttpResult result = request("POST", serverUrl + "/core-worker/app/fcm-token", payload, null);
                if (result.ok()) {
                    boolean refreshRequired = false;
                    try {
                        JSONObject response = new JSONObject(result.body);
                        JSONObject push = response.optJSONObject("push");
                        refreshRequired = response.optBoolean("refreshRequired", false) || (push != null && push.optBoolean("refreshRequired", false));
                    } catch (Throwable ignored) {
                        refreshRequired = false;
                    }
                    if (refreshRequired) {
                        markFcmState("renovando token", "VPS marcou o token antigo como inválido", false);
                        refreshFcmTokenAfterServerReject(serverUrl, reason);
                        return;
                    }
                    markFcmState("ativo", "token registrado na VPS", false);
                    reportAppState("fcm_token_registered", "token FCM registrado na VPS");
                } else {
                    markFcmState("token local · VPS não confirmou", "HTTP " + result.status + " em /fcm-token", false);
                }
            } catch (Throwable err) {
                markFcmState("token local · falha ao registrar", shortThrowable(err), false);
            }
        }, "core-worker-fcm-token").start();
    }

    private void refreshFcmTokenAfterServerReject(String serverUrl, String reason) {
        if (!FCM_ENABLED_IN_APK) {
            return;
        }
        long now = System.currentTimeMillis();
        long last = prefs.getLong("fcm_forced_refresh_at", 0L);
        if (now - last < 10L * 60L * 1000L) {
            markFcmState("aguardando token novo", "renovação recente já solicitada", false);
            return;
        }
        prefs.edit().putLong("fcm_forced_refresh_at", now).apply();
        try {
            FirebaseMessaging.getInstance().deleteToken().addOnCompleteListener(task -> {
                if (!task.isSuccessful()) {
                    Throwable err = task.getException();
                    markFcmState("token inválido · renovação falhou", err == null ? "deleteToken falhou" : shortThrowable(err), false);
                    return;
                }
                prefs.edit()
                        .remove("fcm_token")
                        .putString("fcm_state", "renovando token")
                        .apply();
                fcmTokenPreview = "";
                fcmState = "renovando token";
                mainHandler.postDelayed(() -> safeStartupTask(() -> registerFcmTokenAsync((reason == null ? "server_rejected_token" : reason) + "_refresh")), 1800L);
            });
        } catch (Throwable err) {
            markFcmState("token inválido · renovação indisponível", shortThrowable(err), false);
        }
    }

    private String fcmCompactLabel() {
        String state = fcmStatusLabel().toLowerCase(Locale.ROOT);
        if (state.startsWith("ativo")) return "ativo";
        if (state.contains("desativado")) return "desativado";
        if (state.contains("indispon")) return "indisponível";
        return "fallback local";
    }

    private String fcmStatusLabel() {
        if (!FCM_ENABLED_IN_APK) {
            return "desativado no build · fallback local ativo";
        }
        long disabledUntil = prefs.getLong("fcm_disabled_until", 0L);
        if (disabledUntil > System.currentTimeMillis()) {
            long minutes = Math.max(1L, (disabledUntil - System.currentTimeMillis()) / 60000L);
            return "pausado por segurança · nova tentativa em ~" + minutes + " min · fallback local ativo";
        }
        String state = fcmState == null ? "" : fcmState.trim();
        if (state.isEmpty()) {
            state = prefs.getString("fcm_state", "não verificado");
        }
        if ("ativo".equalsIgnoreCase(state)) {
            return fcmTokenPreview == null || fcmTokenPreview.trim().isEmpty() ? "ativo · fallback local ativo" : "ativo · token registrado · fallback local ativo";
        }
        if (Build.VERSION.SDK_INT >= 33 && !hasNotificationPermission()) {
            return state + " · sem permissão visível · fallback local ativo";
        }
        return state + " · fallback local ativo";
    }

    private void reportAppState(String state, String detail) {
        String serverUrl = normalizedServerUrl();
        if (serverUrl == null || serverUrl.trim().isEmpty()) return;
        new Thread(() -> {
            try {
                JSONObject payload = statusSnapshot();
                payload.put("notificationId", "app-state-" + APP_VERSION);
                payload.put("state", state);
                payload.put("delivered", true);
                payload.put("versionName", latestVersionName == null ? "" : latestVersionName);
                payload.put("versionCode", latestVersionCode);
                payload.put("detail", detail == null ? "" : detail);
                payload.put("permission", hasNotificationPermission() ? "granted" : "missing");
                safePutPayload(payload, "battery", batterySnapshot());
                safePutPayload(payload, "network", networkSnapshot(serverUrl));
                safePutPayload(payload, "update", updateSnapshot());
                safePutPayload(payload, "app_status", appStatusSnapshot());
                request("POST", serverUrl + "/core-worker/app/notification", payload, null);
            } catch (Throwable ignored) {
            }
        }).start();
    }

    private void reportUpdateNotification(String serverUrl, String state, boolean delivered, String detail) {
        try {
            String id = emptyFallback(latestNotificationId, "apk-" + latestVersionCode + "-" + latestVersionName);
            String prefKey = "notification_reported_" + sanitizePrefKey(id) + "_" + sanitizePrefKey(state);
            boolean transientEvent = isTransientUpdateEvent(state);
            if (!transientEvent && prefs.getBoolean(prefKey, false)) {
                return;
            }
            JSONObject payload = new JSONObject();
            payload.put("notificationId", id);
            payload.put("state", state);
            payload.put("delivered", delivered);
            payload.put("versionName", latestVersionName);
            payload.put("versionCode", latestVersionCode);
            payload.put("appVersion", APP_VERSION);
            payload.put("appVersionCode", BuildConfig.VERSION_CODE);
            payload.put("workerId", localAgentWorkerId);
            payload.put("installId", installId());
            payload.put("permission", hasNotificationPermission() ? "granted" : "missing");
            payload.put("detail", detail);
            HttpResult result = request("POST", serverUrl + "/core-worker/app/notification", payload, null);
            if (result.ok() && !transientEvent) {
                prefs.edit().putBoolean(prefKey, true).apply();
            }
        } catch (Throwable ignored) {
        }
    }

    private boolean isTransientUpdateEvent(String state) {
        return "fcm_received".equals(state)
                || "fcm_displayed".equals(state)
                || "fcm_permission_missing".equals(state)
                || "download_tap".equals(state)
                || "download_started".equals(state)
                || "download_verified".equals(state)
                || "download_failed".equals(state)
                || "install_permission_missing".equals(state)
                || "install_intent_opened".equals(state)
                || "install_direct_url_opened".equals(state)
                || "install_intent_failed".equals(state);
    }

    private String sanitizePrefKey(String value) {
        return String.valueOf(value == null ? "" : value).replaceAll("[^A-Za-z0-9_.-]+", "_");
    }

    private String installId() {
        String id = prefs.getString("install_id", "");
        if (id == null || id.trim().isEmpty()) {
            id = UUID.randomUUID().toString();
            prefs.edit().putString("install_id", id).apply();
        }
        return id;
    }

    private interface DownloadProgress {
        void onProgress(long done, long total);
    }

    private void downloadFile(String url, File target, DownloadProgress progress) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setConnectTimeout(9000);
        conn.setReadTimeout(30000);
        conn.setInstanceFollowRedirects(true);
        conn.setRequestProperty("Accept", "application/vnd.android.package-archive,*/*");
        int status = conn.getResponseCode();
        if (status < 200 || status >= 300) {
            String body = readAll(conn.getErrorStream());
            conn.disconnect();
            throw new Exception("HTTP " + status + " · " + compactResultBody(body));
        }
        long total = -1;
        try {
            total = conn.getContentLengthLong();
        } catch (Throwable ignored) {
            total = -1;
        }
        ensureParentDirectory(target);
        InputStream input = conn.getInputStream();
        FileOutputStream output;
        try {
            output = new FileOutputStream(target);
        } catch (Throwable firstOpenError) {
            ensureParentDirectory(target);
            output = new FileOutputStream(target);
        }
        byte[] buffer = new byte[32 * 1024];
        int read;
        long done = 0;
        long lastUi = 0;
        while ((read = input.read(buffer)) >= 0) {
            output.write(buffer, 0, read);
            done += read;
            long now = System.currentTimeMillis();
            if (progress != null && (now - lastUi > 700 || (total > 0 && done >= total))) {
                lastUi = now;
                progress.onProgress(done, total);
            }
        }
        output.flush();
        output.close();
        input.close();
        conn.disconnect();
        if (progress != null) {
            progress.onProgress(done, total);
        }
    }

    private String formatBytes(long value) {
        if (value < 0) return "?";
        if (value < 1024) return value + " B";
        double kb = value / 1024.0;
        if (kb < 1024) return String.format(Locale.ROOT, "%.1f KB", kb);
        double mb = kb / 1024.0;
        return String.format(Locale.ROOT, "%.1f MB", mb);
    }


    private void openApkInstaller(File apkFile) {
        runOnUiThread(() -> {
            String serverUrl = normalizedServerUrl();
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !getPackageManager().canRequestPackageInstalls()) {
                try {
                    Intent settings = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + getPackageName()));
                    startActivity(settings);
                    reportUpdateNotification(serverUrl, "install_permission_missing", false, "Android bloqueou instalação por fonte desconhecida");
                    setUpdateActionState("APK baixado e validado, mas o Android bloqueou instalação por fonte desconhecida.\nAutorize o Core Worker e toque em Atualizar novamente.", "Abrir instalador", true, false);
                    refreshLocalStatus("Autorize o Core Worker a instalar apps desconhecidos. Depois volte aqui e toque novamente em Atualizar. O APK já foi baixado e validado localmente.");
                    return;
                } catch (Throwable ignored) {
                    // Continua para tentar abrir o instalador local.
                }
            }

            Uri uri;
            try {
                uri = FileProvider.getUriForFile(this, getPackageName() + ".files", apkFile);
            } catch (Throwable exc) {
                reportUpdateNotification(serverUrl, "install_intent_failed", false, "FileProvider falhou: " + exc.getClass().getSimpleName() + ": " + String.valueOf(exc.getMessage()));
                setUpdateActionState("APK baixado, mas o FileProvider falhou ao preparar o instalador.\n" + exc.getClass().getSimpleName() + ": " + String.valueOf(exc.getMessage()), "Tentar novamente", true, false);
                refreshLocalStatus("Atualização baixada, mas não consegui preparar o arquivo para instalação: " + exc.getClass().getSimpleName());
                return;
            }

            try {
                Intent install = new Intent(Intent.ACTION_INSTALL_PACKAGE);
                install.setData(uri);
                install.putExtra(Intent.EXTRA_NOT_UNKNOWN_SOURCE, true);
                install.putExtra(Intent.EXTRA_RETURN_RESULT, false);
                install.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                install.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(install);
                reportUpdateNotification(serverUrl, "install_intent_opened", true, "instalador Android aberto com ACTION_INSTALL_PACKAGE");
                setUpdateActionState("Instalador do Android aberto. Conclua a instalação da atualização.", "Instalador aberto", true, false);
                refreshLocalStatus("APK baixado e validado. Abri o instalador do Android usando o arquivo local, sem mandar para site intermediário.");
                return;
            } catch (Throwable ignored) {
                // Tenta fallback ACTION_VIEW abaixo.
            }

            try {
                Intent view = new Intent(Intent.ACTION_VIEW);
                view.setDataAndType(uri, "application/vnd.android.package-archive");
                view.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                view.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(view);
                reportUpdateNotification(serverUrl, "install_intent_opened", true, "instalador Android aberto com ACTION_VIEW");
                setUpdateActionState("Instalador do Android aberto. Conclua a instalação da atualização.", "Instalador aberto", true, false);
                refreshLocalStatus("APK baixado e validado. Abri o instalador do Android usando ACTION_VIEW.");
                return;
            } catch (Throwable viewExc) {
                reportUpdateNotification(serverUrl, "install_intent_failed", false, viewExc.getClass().getSimpleName() + ": " + String.valueOf(viewExc.getMessage()));
                if (latestApkUrl != null && !latestApkUrl.trim().isEmpty()) {
                    try {
                        Intent direct = new Intent(Intent.ACTION_VIEW, Uri.parse(latestApkUrl));
                        direct.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                        startActivity(direct);
                        reportUpdateNotification(serverUrl, "install_direct_url_opened", true, "fallback abriu URL direta do arquivo APK");
                        setUpdateActionState("Não consegui abrir o instalador local, então abri a URL direta do arquivo APK como fallback.", "URL direta aberta", true, false);
                        refreshLocalStatus("Fallback usado: abri a URL direta do APK, não uma página intermediária.");
                        return;
                    } catch (Throwable urlExc) {
                        reportUpdateNotification(serverUrl, "install_intent_failed", false, "fallback URL falhou: " + urlExc.getClass().getSimpleName() + ": " + String.valueOf(urlExc.getMessage()));
                    }
                }
                setUpdateActionState("Atualização baixada, mas não consegui abrir o instalador.\n" + viewExc.getClass().getSimpleName() + ": " + String.valueOf(viewExc.getMessage()), "Tentar novamente", true, false);
                refreshLocalStatus("Atualização baixada, mas não consegui abrir o instalador local: " + viewExc.getClass().getSimpleName() + ". Verifique a permissão de instalar apps desconhecidos para o Core Worker.");
            }
        });
    }


    private void updateLatestUi(String value) {
        runOnUiThread(() -> {
            if (updateText != null) {
                updateText.setText(value);
            }
        });
    }

    private String resolveUpdateUrl(String serverUrl, String raw) {
        raw = raw == null ? "" : raw.trim();
        if (raw.startsWith("http://") || raw.startsWith("https://")) {
            return raw;
        }
        try {
            URL base = new URL(serverUrl);
            String root = base.getProtocol() + "://" + base.getHost();
            if (base.getPort() > 0) {
                root += ":" + base.getPort();
            }
            if (!raw.startsWith("/")) {
                raw = "/" + raw;
            }
            return root + raw;
        } catch (Throwable ignored) {
            return raw;
        }
    }

    private String changelogText(JSONArray array) {
        if (array == null || array.length() == 0) {
            return "";
        }
        StringBuilder builder = new StringBuilder();
        for (int i = 0; i < array.length(); i++) {
            if (i > 4) {
                builder.append("• mais mudanças no changelog.\n");
                break;
            }
            builder.append("• ").append(array.optString(i, "")).append('\n');
        }
        return builder.toString().trim();
    }

    private String sha256Of(File file) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        FileInputStream input = new FileInputStream(file);
        byte[] buffer = new byte[16 * 1024];
        int read;
        while ((read = input.read(buffer)) > 0) {
            digest.update(buffer, 0, read);
        }
        input.close();
        byte[] hash = digest.digest();
        StringBuilder builder = new StringBuilder();
        for (byte b : hash) {
            builder.append(String.format(Locale.ROOT, "%02x", b));
        }
        return builder.toString();
    }

    private void saveLocalFields(String profile) {
        String normalized = normalizeProfile(profile);
        prefs.edit()
                .putString("server_url", normalizedServerUrl())
                .putString("device_name", deviceNameInput.getText().toString().trim())
                .putString("profile", normalized)
                .apply();
        localAgentProfile = normalized;
        updateSystemChecklistText();
        updatePairingUi();
    }

    private void putProfilePayload(JSONObject payload, String profile) throws Exception {
        payload.put("profile", profile);
        payload.put("profile_label", profileLabel(profile));
        payload.put("roles", jsonArray(profileRoles(profile)));
        payload.put("capabilities", coreWorkerApkCapabilitiesArray());
        payload.put("supported_tasks", supportedLightJobsArray());
        payload.put("supportedTasks", supportedLightJobsArray());
        payload.put("app_jobs", supportedLightJobsArray());
        safePutPayload(payload, "coreLinux", coreLinuxPublicSnapshot());
        safePutPayload(payload, "nativeRuntime", nativeRuntimePublicSnapshot());
        JSONObject profileStatus = payload.optJSONObject("status");
        if (profileStatus == null) {
            profileStatus = new JSONObject();
        }
        profileStatus.put("profile", profile);
        profileStatus.put("profile_label", profileLabel(profile));
        profileStatus.put("apk_scope", "native-runtime-python-linux-bedrock-installer");
        profileStatus.put("runtime_mode", runtimeMode == null || runtimeMode.trim().isEmpty() ? "apk-native-python-linux-bedrock-installer" : runtimeMode);
        profileStatus.put("internal_runtime_state", internalRuntimeState == null ? "" : internalRuntimeState);
        profileStatus.put("runtime", runtimeSnapshot());
        payload.put("runtime_mode", runtimeMode == null || runtimeMode.trim().isEmpty() ? "apk-native-python-linux-bedrock-installer" : runtimeMode);
        payload.put("status", profileStatus);
    }

    private void safePutPayload(JSONObject target, String key, JSONObject value) {
        try {
            if (target != null && key != null && value != null) {
                target.put(key, value);
            }
        } catch (Throwable ignored) {
        }
    }

    private void safePutPayload(JSONObject target, String key, JSONArray value) {
        try {
            if (target != null && key != null && value != null) {
                target.put(key, value);
            }
        } catch (Throwable ignored) {
        }
    }

    private JSONObject updateSnapshot() throws Exception {
        JSONObject update = new JSONObject();
        update.put("installed_version", APP_VERSION);
        update.put("installed_code", BuildConfig.VERSION_CODE);
        update.put("latest_version", latestVersionName == null ? "" : latestVersionName);
        update.put("latest_code", latestVersionCode);
        update.put("available", latestUpdateAvailable);
        update.put("state", updateChecklistLabel());
        update.put("download_busy", updateDownloadBusy);
        safePutPayload(update, "artifacts", updateArtifactsSnapshot());
        return update;
    }

    private JSONObject appStatusSnapshot() throws Exception {
        JSONObject appStatus = new JSONObject();
        appStatus.put("ready", hasPairing() && (internalRuntimeOnline || localAgentOnline));
        appStatus.put("paired", hasPairing());
        appStatus.put("profile", appliedProfile());
        appStatus.put("profile_label", profileLabel(appliedProfile()));
        appStatus.put("last_error", emptyFallback(appStatusLastError, internalRuntimeLastError));
        appStatus.put("last_sent_at", appStatusLastSentAt);
        appStatus.put("foreground", true);
        appStatus.put("heartbeat_reason", emptyFallback(internalRuntimeHeartbeatState, "pendente"));
        return appStatus;
    }

    private JSONObject statusSnapshot() throws Exception {
        JSONObject status = new JSONObject();
        status.put("app", "foreground");
        status.put("apk_companion", true);
        status.put("android_sdk", Build.VERSION.SDK_INT);
        status.put("manufacturer", Build.MANUFACTURER);
        status.put("model", Build.MODEL);
        status.put("local_agent_online", localAgentOnline);
        status.put("native_worker_online", nativeWorkerOnline);
        status.put("native_worker_state", nativeWorkerState == null ? "" : nativeWorkerState);
        status.put("native_worker_last_heartbeat_at", nativeWorkerLastHeartbeatAt);
        status.put("native_boot_summary", nativeBootSummary == null ? "" : nativeBootSummary);
        status.put("native_shell_summary", nativeShellSummary == null ? "" : nativeShellSummary);
        status.put("native_python_summary", nativePythonSummary == null ? "" : nativePythonSummary);
        status.put("native_python_available", nativePythonAvailable);
        status.put("native_python_version", nativePythonVersion == null ? "" : nativePythonVersion);
        status.put("native_python_last_script", nativePythonLastScript == null ? "" : nativePythonLastScript);
        status.put("native_python_last_error", nativePythonLastError == null ? "" : nativePythonLastError);
        status.put("core_linux_summary", coreLinuxSummary == null ? "" : coreLinuxSummary);
        status.put("core_linux_state", coreLinuxState == null ? "" : coreLinuxState);
        status.put("core_linux_prepared", coreLinuxPrepared);
        try {
            safePutPayload(status, "core_linux_static", coreLinuxStaticSnapshot("status"));
        } catch (Throwable ignored) {
        }
        status.put("core_linux_heavy_probe_paused", false);
        status.put("core_linux_runtime_v1_enabled", true);
        status.put("bedrock_runtime_isolated", BEDROCK_RUNTIME_ISOLATED);
        status.put("bedrock_summary", bedrockSummary == null ? "" : bedrockSummary);
        status.put("bedrock_state", bedrockState == null ? "" : bedrockState);
        status.put("bedrock_ready", bedrockReady);
        status.put("bedrock_runtime_summary", bedrockRuntimeSummary == null ? "" : bedrockRuntimeSummary);
        status.put("bedrock_runtime_state", bedrockRuntimeState == null ? "" : bedrockRuntimeState);
        status.put("bedrock_runtime_service_active", bedrockRuntimeServiceActive);
        status.put("foreground_runtime_active", foregroundRuntimeActive);
        status.put("foreground_runtime_summary", foregroundRuntimeSummary == null ? "" : foregroundRuntimeSummary);
        status.put("foreground_runtime_last_tick_at", foregroundRuntimeLastTickAt);
        status.put("linux_install_strategy_summary", linuxInstallStrategySummary == null ? "" : linuxInstallStrategySummary);
        status.put("bedrock_installer_summary", bedrockInstallerSummary == null ? "" : bedrockInstallerSummary);
        status.put("bedrock_installer_state", bedrockInstallerState == null ? "" : bedrockInstallerState);
        status.put("bedrock_installer_next_action", bedrockInstallerNextAction == null ? "" : bedrockInstallerNextAction);
        status.put("termux_installed", isPackageInstalled("com.termux"));
        status.put("termux_api_installed", isPackageInstalled("com.termux.api"));
        status.put("termux_boot_installed", isPackageInstalled("com.termux.boot"));
        status.put("tailscale_installed", isPackageInstalled("com.tailscale.ipn"));
        status.put("fcm_state", fcmState);
        status.put("fcm_token_preview", fcmTokenPreview);
        status.put("runtime_mode", runtimeMode == null || runtimeMode.trim().isEmpty() ? "apk-native-python-linux-bedrock-page-runtime" : runtimeMode);
        status.put("internal_runtime_state", internalRuntimeState == null ? "" : internalRuntimeState);
        status.put("internal_runtime_online", internalRuntimeOnline);
        status.put("internal_runtime_heartbeat_state", internalRuntimeHeartbeatState == null ? "" : internalRuntimeHeartbeatState);
        status.put("internal_runtime_last_error", internalRuntimeLastError == null ? "" : internalRuntimeLastError);
        status.put("internal_light_jobs_state", internalLightJobsState == null ? "" : internalLightJobsState);
        status.put("internal_light_jobs_last_check_at", internalLightJobsLastCheckAt);
        status.put("internal_light_jobs_last_count", internalLightJobsLastCount);
        status.put("internal_light_jobs_last_summary", internalLightJobsLastSummary == null ? "" : internalLightJobsLastSummary);
        status.put("internal_light_jobs_last_fetch_reason", internalLightJobsLastFetchReason == null ? "" : internalLightJobsLastFetchReason);
        status.put("internal_light_jobs_last_fetch_app_version", internalLightJobsLastFetchAppVersion == null ? "" : internalLightJobsLastFetchAppVersion);
        status.put("internal_light_jobs_last_fetch_app_version_code", internalLightJobsLastFetchAppVersionCode);
        status.put("internal_light_jobs_last_fetch_http_status", internalLightJobsLastFetchHttpStatus);
        status.put("internal_light_jobs_last_returned_count", internalLightJobsLastReturnedCount);
        status.put("internal_light_jobs_last_fetch_error", internalLightJobsLastFetchError == null ? "" : internalLightJobsLastFetchError);
        status.put("internal_jobs_queue", internalLightJobsQueueSummary == null ? "" : internalLightJobsQueueSummary);
        status.put("internal_jobs_running", internalLightJobsRunningCount);
        status.put("internal_jobs_pending", internalLightJobsPendingCount);
        safePutPayload(status, "internal_jobs_history", internalJobHistoryJson());
        status.put("runtime", runtimeSnapshot());
        safePutPayload(status, "battery", batterySnapshot());
        safePutPayload(status, "network", networkSnapshot(normalizedServerUrl()));
        safePutPayload(status, "update", updateSnapshot());
        safePutPayload(status, "permissions", permissionsSnapshot());
        safePutPayload(status, "storage", storageSnapshot());
        safePutPayload(status, "app_status", appStatusSnapshot());
        if (localAgentOnline) {
            status.put("local_agent_version", localAgentVersion);
            status.put("local_agent_profile", localAgentProfile);
            status.put("local_agent_sshd", localAgentSshdSummary);
        }
        return status;
    }

    private JSONObject batterySnapshot() throws Exception {
        JSONObject battery = new JSONObject();
        BatteryManager manager = (BatteryManager) getSystemService(BATTERY_SERVICE);
        int percent = -1;
        if (manager != null) {
            percent = manager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
        }
        Intent intent = registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        int temperature = -1;
        int status = -1;
        int plugged = 0;
        if (intent != null) {
            temperature = intent.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, -1);
            status = intent.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
            plugged = intent.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0);
        }
        battery.put("available", percent >= 0);
        if (percent >= 0) {
            battery.put("percent", percent);
            battery.put("percentage", percent);
        }
        if (temperature >= 0) {
            battery.put("temperature_c", temperature / 10.0);
        }
        battery.put("charging", status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL);
        battery.put("status", batteryStatusLabel(status));
        battery.put("plugged", pluggedLabel(plugged));
        battery.put("source", "android-batterymanager");
        return battery;
    }

    private JSONObject networkSnapshot(String serverUrl) throws Exception {
        JSONObject networkJson = new JSONObject();
        networkJson.put("available", false);
        networkJson.put("type", "unknown");
        ConnectivityManager connectivity = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
        if (connectivity != null) {
            Network active = connectivity.getActiveNetwork();
            NetworkCapabilities caps = active == null ? null : connectivity.getNetworkCapabilities(active);
            if (caps != null) {
                networkJson.put("available", caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET));
                if (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) {
                    networkJson.put("type", "wifi");
                } else if (caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) {
                    networkJson.put("type", "mobile");
                } else if (caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                    networkJson.put("type", "vpn");
                } else {
                    networkJson.put("type", "other");
                }
                networkJson.put("vpn", caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN));
            }
        }
        double ping = measureTcpPingMs(serverUrl);
        if (ping >= 0) {
            networkJson.put("vps_ping_ms", Math.round(ping));
        }
        networkJson.put("private_network_hint", isLikelyTailscale(serverUrl) ? "tailscale_or_100_net" : "unknown");
        return networkJson;
    }

    private double measureTcpPingMs(String serverUrl) {
        try {
            URL url = new URL(serverUrl);
            String host = url.getHost();
            int port = url.getPort();
            if (port <= 0) {
                port = "https".equalsIgnoreCase(url.getProtocol()) ? 443 : 80;
            }
            long start = System.nanoTime();
            Socket socket = new Socket();
            try {
                socket.connect(new InetSocketAddress(host, port), 3000);
            } finally {
                try {
                    socket.close();
                } catch (Throwable ignored) {
                }
            }
            return (System.nanoTime() - start) / 1_000_000.0;
        } catch (Throwable ignored) {
            return -1;
        }
    }

    private boolean isLikelyTailscale(String serverUrl) {
        try {
            String host = new URL(serverUrl).getHost();
            return host != null && host.startsWith("100.");
        } catch (Throwable ignored) {
            return false;
        }
    }

    private HttpResult request(String method, String url, JSONObject payload, String token) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setRequestMethod(method);
        boolean localRequest = url.startsWith("http://127.0.0.1") || url.startsWith("http://localhost");
        conn.setConnectTimeout(localRequest ? 900 : 6000);
        conn.setReadTimeout(localRequest ? 1800 : 9000);
        conn.setRequestProperty("Accept", "application/json");
        if (token != null && !token.trim().isEmpty()) {
            conn.setRequestProperty("Authorization", "Bearer " + token.trim());
        }
        if (payload != null) {
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            OutputStream stream = conn.getOutputStream();
            BufferedWriter writer = new BufferedWriter(new OutputStreamWriter(stream, StandardCharsets.UTF_8));
            writer.write(payload.toString());
            writer.flush();
            writer.close();
            stream.close();
        }
        int status = conn.getResponseCode();
        InputStream input = status >= 200 && status < 400 ? conn.getInputStream() : conn.getErrorStream();
        String body = readAll(input);
        conn.disconnect();
        return new HttpResult(status, body == null ? "" : body);
    }

    private String readAll(InputStream input) throws Exception {
        if (input == null) {
            return "";
        }
        BufferedReader reader = new BufferedReader(new InputStreamReader(input, StandardCharsets.UTF_8));
        StringBuilder builder = new StringBuilder();
        String line;
        while ((line = reader.readLine()) != null) {
            if (builder.length() > 0) {
                builder.append('\n');
            }
            builder.append(line);
        }
        reader.close();
        return builder.toString();
    }

    private void checkLocalAgent(boolean userVisible) {
        runBusy(userVisible ? "Verificando este celular..." : "Verificando este celular...", () -> {
            boolean ok = updateLocalAgentStatus(true);
            sendInternalRuntimeHeartbeat(false, userVisible ? "manual_check" : "background_check");
            fetchAndRunLightJobs(false, userVisible ? "manual_check" : "background_check");
            updateSystemChecklistText();
            if (userVisible) {
                if (ok) {
                    show("Tudo pronto.\nWorker online · perfil " + profileLabel(appliedProfile()));
                } else {
                    show("Worker local offline. Abra o Termux por enquanto para acordar este celular.");
                }
            }
        });
    }

    private boolean updateLocalAgentStatus(boolean updateText) {
        try {
            HttpResult result = request("GET", LOCAL_AGENT_STATUS_URL, null, null);
            if (!result.ok()) {
                throw new Exception("HTTP " + result.status);
            }
            JSONObject body = new JSONObject(result.body);
            applyLocalAgentStatus(body);
            if (updateText) {
                showLocalAgentText();
            }
            return true;
        } catch (Throwable exc) {
            localAgentOnline = false;
            localAgentVersion = "";
            localAgentProfile = "";
            localAgentWorkerId = "";
            localAgentVpsConfigured = false;
            localAgentJobsConfigured = false;
            localAgentMessage = "offline";
            runOnUiThread(this::updatePairingUi);
            if (updateText) {
                showLocalAgentText();
            }
            return false;
        }
    }

    private boolean syncProfileToLocalAgent(String profile) {
        try {
            JSONObject payload = new JSONObject();
            payload.put("profile", profile);
            payload.put("profile_label", profileLabel(profile));
            payload.put("roles", jsonArray(profileRoles(profile)));
            payload.put("capabilities", jsonArray(profileRoles(profile)));
            payload.put("source", "core-worker-apk-companion");
            payload.put("apk_version", APP_VERSION);
            HttpResult result = request("POST", LOCAL_AGENT_PROFILE_URL, payload, null);
            if (!result.ok()) {
                throw new Exception("HTTP " + result.status);
            }
            JSONObject body = new JSONObject(result.body);
            applyLocalAgentStatus(body);
            showLocalAgentText();
            runOnUiThread(this::updatePairingUi);
            updateSystemChecklistText();
            return true;
        } catch (Throwable exc) {
            localAgentOnline = false;
            localAgentVersion = "";
            localAgentProfile = "";
            localAgentMessage = "offline ao aplicar perfil";
            runOnUiThread(this::updatePairingUi);
            showLocalAgentText();
            updateSystemChecklistText();
            return false;
        }
    }

    private void applyLocalAgentStatus(JSONObject body) {
        localAgentOnline = body.optBoolean("ok", true);
        localAgentVersion = body.optString("version", "");
        String reportedProfile = normalizeProfileOrEmpty(body.optString("profile", ""));
        if (reportedProfile.isEmpty()) {
            reportedProfile = normalizeProfileOrEmpty(body.optString("profile_label", ""));
        }
        localAgentProfile = reportedProfile;
        if (!reportedProfile.isEmpty()) {
            final String finalReportedProfile = reportedProfile;
            prefs.edit().putString("profile", finalReportedProfile).apply();
            runOnUiThread(() -> {
                updateProfileRadioSelection(finalReportedProfile);
                updateProfileHint(finalReportedProfile);
            });
        }
        localAgentWorkerId = body.optString("worker_id", localAgentWorkerId);
        localAgentVpsConfigured = body.optBoolean("vps_configured", false);
        localAgentJobsConfigured = body.optBoolean("jobs_configured", false);
        JSONObject statusObj = body.optJSONObject("status");
        if (statusObj != null) {
            String reportedMode = statusObj.optString("runtime_mode", "");
            if (!reportedMode.trim().isEmpty()) {
                runtimeMode = reportedMode.trim();
            }
        }
        String bodyMode = body.optString("runtime_mode", "");
        if (!bodyMode.trim().isEmpty()) {
            runtimeMode = bodyMode.trim();
        }
        localAgentSshdSummary = body.optString("sshd_summary", "");
        if (localAgentOnline) {
            String jobs = localAgentJobsConfigured ? "jobs ok" : "jobs pendentes";
            String vps = localAgentVpsConfigured ? "VPS ok" : "VPS pendente";
            String paired = autoPairFromLocalAgent() ? "pareado" : "pareamento local pendente";
            String sshd = localAgentSshdSummary == null || localAgentSshdSummary.trim().isEmpty() ? "wake ?" : localAgentSshdSummary.trim();
            localAgentMessage = "online · " + vps + " · " + jobs + " · " + paired + " · " + sshd;
        } else {
            localAgentMessage = "offline";
        }
    }

    private void showLocalAgentText() {
        runOnUiThread(() -> {
            if (localAgentText != null) {
                localAgentText.setText(localAgentLine());
            }
            updateSystemChecklistText();
            updatePairingUi();
            refreshLocalStatus(null);
        });
    }

    private String quickBatteryLabel() {
        try {
            JSONObject battery = batterySnapshot();
            if (!battery.optBoolean("available", false)) {
                return "bateria ?";
            }
            String label = battery.optInt("percent", -1) + "%";
            if (battery.has("temperature_c")) {
                label += " · " + Math.round(battery.optDouble("temperature_c", 0)) + "°C";
            }
            if (battery.optBoolean("charging", false)) {
                label += " · carregando";
            }
            return label;
        } catch (Throwable ignored) {
            return "bateria ?";
        }
    }

    private String quickNetworkLabel() {
        try {
            ConnectivityManager connectivity = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
            Network active = connectivity == null ? null : connectivity.getActiveNetwork();
            NetworkCapabilities caps = active == null ? null : connectivity.getNetworkCapabilities(active);
            if (caps == null || !caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)) {
                return "sem rede";
            }
            String label;
            if (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) {
                label = "Wi‑Fi";
            } else if (caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) {
                label = "móvel";
            } else if (caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                label = "VPN";
            } else {
                label = "rede";
            }
            if (caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN) && !"VPN".equals(label)) {
                label += "+VPN";
            }
            return label;
        } catch (Throwable ignored) {
            return "rede ?";
        }
    }

    private String localAgentLine() {
        String profile = appliedProfile();
        String internal = internalRuntimeOnline ? "Runtime APK online" : "Runtime APK aguardando";
        if (nativeWorkerOnline || prefs.getBoolean("paired_via_native_apk", false)) {
            return "✅ APK pronto para trabalhar\n" + profileLabel(profile) + " · Push " + fcmCompactLabel() + " · " + internal + " · " + emptyFallback(nativeWorkerState, "worker nativo");
        }
        if (!localAgentOnline) {
            return (internalRuntimeOnline ? "✅ APK conectado à VPS" : "⚠️ Aguardando pareamento")
                    + "\n" + internal + " · Termux é apenas fallback temporário.";
        }
        if (hasPairing()) {
            return "✅ Pronto para trabalhar\n" + profileLabel(profile) + " · Push " + fcmCompactLabel() + " · " + internal + " · APK " + updateChecklistLabel();
        }
        return "⚠️ Fallback Termux detectado\nConecte este celular direto pelo APK quando possível.";
    }


    private boolean hasPairing() {
        boolean pairedViaLocal = prefs.getBoolean("paired_via_local_agent", false);
        boolean pairedViaNative = prefs.getBoolean("paired_via_native_apk", false);
        String serverUrl = prefs.getString("server_url", DEFAULT_VPS_URL);
        String workerId = prefs.getString("worker_id", "");
        String token = prefs.getString("worker_token", "");
        boolean nativeSaved = pairedViaNative && serverUrl != null && !serverUrl.isEmpty() && workerId != null && !workerId.isEmpty() && token != null && !token.isEmpty();
        boolean saved = pairedViaLocal && serverUrl != null && !serverUrl.isEmpty() && workerId != null && !workerId.isEmpty();
        boolean local = localAgentOnline && localAgentVpsConfigured && localAgentWorkerId != null && !localAgentWorkerId.trim().isEmpty();
        return nativeSaved || saved || local;
    }

    private boolean autoPairFromLocalAgent() {
        if (!localAgentOnline || !localAgentVpsConfigured || localAgentWorkerId == null || localAgentWorkerId.trim().isEmpty()) {
            return false;
        }
        String serverUrl = normalizedServerUrl();
        prefs.edit()
                .putString("server_url", serverUrl)
                .putString("worker_id", localAgentWorkerId)
                .putBoolean("paired_via_local_agent", true)
                .apply();
        registerFcmTokenAsync("local_agent_pair_detected");
        return true;
    }

    private void autoVerifySavedPairing() {
        new Thread(() -> {
            try {
                if (prefs.getBoolean("paired_via_native_apk", false)) {
                    sendNativeWorkerHeartbeat(false, "auto_verify_saved_native_pairing");
                    show(null);
                }
                boolean ok = updateLocalAgentStatus(false);
                if (ok && autoPairFromLocalAgent()) {
                    vpsState = localAgentVpsConfigured ? "ok" : "pendente";
                    reportAppState("local_agent_seen", "APK detectou worker local já pareado: " + localAgentMessage);
                    show("Pareamento existente detectado automaticamente pelo worker local. Nenhum novo código é necessário.");
                } else {
                    reportAppState(ok ? "local_agent_unpaired" : "local_agent_offline", ok ? localAgentMessage : "worker local offline/inacessível pelo APK");
                    show(null);
                }
            } catch (Throwable ignored) {
                show(null);
            }
        }).start();
    }

    private void openTermux() {
        try {
            Intent launch = getPackageManager().getLaunchIntentForPackage("com.termux");
            if (launch == null) {
                throw new ActivityNotFoundException("Termux não encontrado");
            }
            startActivity(launch);
        } catch (Throwable exc) {
            refreshLocalStatus("Não consegui abrir o Termux automaticamente. Abra o Termux por enquanto; o runtime interno ainda está em preparação.");
        }
    }

    private void openTailscale() {
        try {
            Intent launch = getPackageManager().getLaunchIntentForPackage("com.tailscale.ipn");
            if (launch == null) {
                throw new ActivityNotFoundException("Tailscale não encontrado");
            }
            startActivity(launch);
        } catch (Throwable exc) {
            refreshLocalStatus("Não consegui abrir o Tailscale automaticamente. Abra o app Tailscale manualmente e conecte na mesma tailnet da VPS.");
        }
    }

    private void confirmClearPairing() {
        new AlertDialog.Builder(this)
                .setTitle("Esquecer conexão local?")
                .setMessage("Isso remove a conexão salva neste APK. O registro na VPS não é apagado automaticamente.")
                .setPositiveButton("Esquecer", (dialog, which) -> {
                    prefs.edit()
                            .remove("worker_token")
                            .remove("server_url")
                            .remove("profile")
                            .remove("paired_via_local_agent")
                            .remove("paired_via_native_apk")
                            .remove("native_worker_id")
                            .remove("worker_id")
                            .apply();
                    loadInputs();
                    updatePairingUi();
                    refreshLocalStatus("Conexão local removida.");
                })
                .setNegativeButton("Cancelar", null)
                .show();
    }

    private void runBusy(String message, WorkerRunnable runnable) {
        runOnUiThread(() -> {
            refreshLocalStatus(message);
            setButtonsEnabled(false);
        });
        new Thread(() -> {
            try {
                runnable.run();
            } catch (Throwable exc) {
                appStatusLastError = shortThrowable(exc);
                startupLog("runBusy:fail " + appStatusLastError);
                show("Erro: " + exc.getClass().getSimpleName() + " · " + String.valueOf(exc.getMessage()));
            } finally {
                runOnUiThread(() -> setButtonsEnabled(true));
            }
        }).start();
    }

    private void show(String message) {
        runOnUiThread(() -> {
            if (!activityDestroyed) refreshLocalStatus(message);
        });
    }

    private void toast(String message) {
        runOnUiThread(() -> {
            if (!activityDestroyed) Toast.makeText(this, message, Toast.LENGTH_SHORT).show();
        });
    }

    private void setButtonsEnabled(boolean enabled) {
        if (Looper.myLooper() != Looper.getMainLooper()) {
            mainHandler.post(() -> setButtonsEnabled(enabled));
            return;
        }
        if (activityDestroyed) {
            return;
        }
        if (prepareButton != null) prepareButton.setEnabled(enabled);
        if (termuxButton != null) termuxButton.setEnabled(enabled);
        if (tailscaleButton != null) tailscaleButton.setEnabled(enabled);
        if (testButton != null) testButton.setEnabled(enabled);
        if (pairButton != null) pairButton.setEnabled(enabled);
        if (saveProfileButton != null) saveProfileButton.setEnabled(enabled);
        if (profileToggleButton != null) profileToggleButton.setEnabled(enabled);
        if (technicalToggleButton != null) technicalToggleButton.setEnabled(enabled);
        if (heartbeatButton != null) heartbeatButton.setEnabled(enabled);
        if (updateCheckButton != null) updateCheckButton.setEnabled(enabled);
        if (updateCleanupButton != null) updateCleanupButton.setEnabled(enabled && !updateDownloadBusy);
        if (updateInstallButton != null) applyUpdateButtonState(latestUpdateAvailable, updateDownloadBusy ? "Baixando..." : "Atualizar agora", updateDownloadBusy);
        if (clearButton != null) clearButton.setEnabled(enabled);
        if (bedrockTestAllButton != null) bedrockTestAllButton.setEnabled(enabled && !bedrockProbeRunning.get());
        if (bedrockPrepareServerButton != null) bedrockPrepareServerButton.setEnabled(enabled);
        if (bedrockFilesButton != null) bedrockFilesButton.setEnabled(enabled);
        if (bedrockLogsButton != null) bedrockLogsButton.setEnabled(enabled);
        if (bedrockEulaButton != null) bedrockEulaButton.setEnabled(enabled);
        if (bedrockSendCommandButton != null) bedrockSendCommandButton.setEnabled(enabled);
        if (bedrockExpandTerminalButton != null) bedrockExpandTerminalButton.setEnabled(enabled);
        if (bedrockCopyTerminalButton != null) bedrockCopyTerminalButton.setEnabled(enabled);
    }

    private void refreshLocalStatus(String extra) {
        if (Looper.myLooper() != Looper.getMainLooper()) {
            final String safeExtra = extra;
            mainHandler.post(() -> refreshLocalStatus(safeExtra));
            return;
        }
        if (activityDestroyed || statusText == null) {
            return;
        }
        boolean hasExtra = extra != null && !extra.trim().isEmpty();
        if (hasExtra) {
            StringBuilder builder = new StringBuilder();
            builder.append(extra.trim());
            if (technicalExpanded) {
                builder.append("\n\nResumo técnico rápido\n");
                builder.append(checkLine("VPS", hasPairing() ? "conectada" : vpsChecklistLabel(normalizedServerUrl()))).append('\n');
                builder.append(checkLine("Worker", localAgentOnline ? "online" : "offline")).append('\n');
                builder.append(checkLine("Perfil", profileLabel(appliedProfile()))).append('\n');
                builder.append(checkLine("Atualizações", updateChecklistLabel())).append('\n');
                builder.append(checkLine("Push", fcmStatusLabel()));
            }
            statusText.setText(builder.toString());
            statusText.setVisibility(View.VISIBLE);
        } else {
            statusText.setText("");
            statusText.setVisibility(View.GONE);
        }
        if (hasExtra) {
            appendCoreTerminalEvent("status", extra.trim());
        }
        if (localAgentText != null) {
            localAgentText.setText(localAgentLine());
        }
        updateSystemChecklistText();
        updatePairingUi();
    }

    private String checkLine(String label, String value) {
        return "• " + label + ": " + value;
    }

    private String vpsChecklistLabel(String server) {
        if (server == null || server.trim().isEmpty()) {
            return "não definida";
        }
        return vpsState == null || vpsState.isEmpty() ? "não testada" : vpsState;
    }

    private String updateChecklistLabel() {
        if (latestUpdateAvailable) {
            return "nova versão disponível";
        }
        if (latestVersionCode >= 0) {
            return "em dia";
        }
        return "não verificada";
    }

    private String networkChecklistLabel(String server) {
        try {
            ConnectivityManager connectivity = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
            if (connectivity == null) {
                return "desconhecida";
            }
            Network active = connectivity.getActiveNetwork();
            NetworkCapabilities caps = active == null ? null : connectivity.getNetworkCapabilities(active);
            if (caps == null || !caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)) {
                return "sem rede";
            }
            if (caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                return "ok · VPN ativa";
            }
            if (isLikelyTailscale(server)) {
                return "rede ok · confirme Tailscale";
            }
            return "rede ok";
        } catch (Throwable ignored) {
            return "desconhecida";
        }
    }

    private void updateSystemChecklistText() {
        runOnUiThread(() -> {
            String[] blocks = prepareChecklistBlocks();
            if (technicalAppText != null) technicalAppText.setText(blocks[0]);
            if (technicalDeviceText != null) technicalDeviceText.setText(blocks[1]);
            if (technicalRuntimeText != null) technicalRuntimeText.setText(blocks[2]);
            if (technicalDiagnosticsText != null) technicalDiagnosticsText.setText(blocks[3]);
            if (technicalBedrockText != null) technicalBedrockText.setText(blocks[4]);
            if (technicalTermuxText != null) technicalTermuxText.setText(blocks[5]);
            if (technicalDependenciesText != null) technicalDependenciesText.setText(blocks[6]);
            if (systemChecklistText != null) {
                systemChecklistText.setText(prepareChecklistText());
            }
            refreshBedrockVisualState();
        });
    }

    private String[] prepareChecklistBlocks() {
        String server = normalizedServerUrl();
        try { batterySnapshot(); } catch (Throwable ignored) {}
        // Não faz ping/rede pesada aqui: este método roda na UI thread ao atualizar detalhes técnicos.
        String appBlock = "App\n"
                + checkLine("APK", APP_VERSION + " (" + BuildConfig.VERSION_CODE + ")") + "\n"
                + checkLine("Push", fcmStatusLabel()) + "\n"
                + checkLine("Perfil", profileLabel(appliedProfile())) + "\n"
                + checkLine("Atualizações", updateChecklistLabel()) + "\n"
                + checkLine("Diagnóstico", emptyFallback(internalDiagnosticsSummary, "aguardando"));

        String battery = quickBatteryLabel();
        String tempNote = "";
        try {
            JSONObject snap = batterySnapshot();
            double temp = snap.optDouble("temperature_c", -1);
            if (temp >= 42) tempNote = " · atenção";
        } catch (Throwable ignored) {}
        String deviceBlock = "Aparelho\n"
                + checkLine("Bateria", battery + tempNote) + "\n"
                + checkLine("Rede", quickNetworkLabel()) + "\n"
                + checkLine("Dispositivo", Build.MANUFACTURER + " " + Build.MODEL);

        String ageLabel = "pendente";
        if (internalRuntimeLastHeartbeatAt > 0L) {
            long age = Math.max(0L, (System.currentTimeMillis() - internalRuntimeLastHeartbeatAt) / 1000L);
            ageLabel = age < 60 ? "agora" : (age / 60L) + " min atrás";
        }
        String runtimeBlock = "Runtime\n"
                + checkLine("Modo", runtimeStatusLabel()) + "\n"
                + checkLine("Heartbeat APK", internalRuntimeOnline ? "online direto na VPS" : emptyFallback(internalRuntimeHeartbeatState, "pendente")) + "\n"
                + checkLine("Último heartbeat", ageLabel) + "\n"
                + checkLine("Jobs internos", emptyFallback(internalLightJobsState, "aguardando")) + "\n"
                + checkLine("Cobertura", emptyFallback(internalLightJobsCatalogSummary, "catálogo aguardando")) + "\n"
                + checkLine("Fila", emptyFallback(internalLightJobsQueueSummary, "aguardando")) + "\n"
                + checkLine("Persistente", emptyFallback(foregroundRuntimeSummary, "serviço aguardando")) + "\n"
                + checkLine("Jobs reais", nativePythonAvailable ? "APK Python interno" : (nativeWorkerOnline ? "APK nativo" : "APK interno · fallback legado")) + "\n"
                + checkLine("Shell", emptyFallback(nativeShellSummary, "controlado aguardando")) + "\n"
                + checkLine("Python", emptyFallback(nativePythonSummary, "aguardando health check")) + "\n"
                + checkLine("Linux runtime", emptyFallback(coreLinuxSummary, "preparando")) + "\n"
                + checkLine("Bedrock", emptyFallback(bedrockSummary, "não configurado"));

        String diagAge = "pendente";
        if (internalDiagnosticsLastAt > 0L) {
            long age = Math.max(0L, (System.currentTimeMillis() - internalDiagnosticsLastAt) / 1000L);
            diagAge = age < 60 ? "agora" : (age / 60L) + " min atrás";
        }
        String diagnosticsBlock = "Diagnósticos APK\n"
                + checkLine("Resumo", emptyFallback(internalDiagnosticsSummary, "aguardando")) + "\n"
                + checkLine("Armazenamento", emptyFallback(internalStorageSummary, "aguardando")) + "\n"
                + checkLine("Ponte", emptyFallback(internalBridgeSummary, "aguardando")) + "\n"
                + checkLine("Último job", emptyFallback(internalLightJobsLastSummary, "nenhum")) + "\n"
                + checkLine("Histórico", internalJobHistoryText()) + "\n"
                + checkLine("Atualizado", diagAge)
                + ((internalRuntimeLastError != null && !internalRuntimeLastError.trim().isEmpty()) ? "\n" + checkLine("Último erro APK", internalRuntimeLastError) : "");

        String bedrockBlock = "Servidor Minecraft Bedrock\n"
                + checkLine("Estado", emptyFallback(bedrockSummary, "não instalado")) + "\n"
                + checkLine("Manager", emptyFallback(bedrockState, "aguardando diagnóstico")) + "\n"
                + checkLine("Instalador", emptyFallback(bedrockInstallerSummary, "aguardando validação")) + "\n"
                + checkLine("Próxima ação", emptyFallback(bedrockInstallerNextAction, "validar requisitos")) + "\n"
                + checkLine("Pronto", bedrockReady ? "sim · aguardando runner" : "não · ambiente/servidor pendente") + "\n"
                + checkLine("Propriedades", "server.properties preparado pelo APK") + "\n"
                + checkLine("Runtime Bedrock", emptyFallback(bedrockRuntimeSummary, "parado · aguardando preflight")) + "\n"
                + checkLine("Execução", bedrockRuntimeServiceActive ? "serviço visível ativo · start real protegido" : "start/stop/logs preparados · serviço parado") + "\n"
                + checkLine("Segurança", "sem download automático · sem shell livre");

        String sshd = emptyFallback(localAgentSshdSummary, "não informado");
        if (sshd.toLowerCase(Locale.ROOT).contains("porta configurada não apareceu")) {
            sshd = "ativo · porta não detectada";
        }
        String termuxBlock = "Fallback legado Termux\n"
                + checkLine("Status", localAgentOnline ? "online, mas não principal" : "offline") + "\n"
                + checkLine("Jobs avançados", localAgentJobsConfigured ? "fallback disponível" : "não exigido para runtime APK") + "\n"
                + checkLine("SSHD", sshd);

        String depsBlock = "Migração sem Termux\n"
                + checkLine("Status aparelho", "APK nativo") + "\n"
                + checkLine("Boot/autostart", emptyFallback(nativeBootSummary, "APK nativo")) + "\n"
                + checkLine("Jobs internos", "APK nativo") + "\n"
                + checkLine("Jobs Python", nativePythonAvailable ? "APK + Python interno" : "aguardando health check") + "\n"
                + checkLine("Shell", emptyFallback(nativeShellSummary, "controlado aguardando")) + "\n"
                + checkLine("Linux runtime", emptyFallback(coreLinuxSummary, "base preparada")) + "\n"
                + checkLine("Instalação Linux", emptyFallback(linuxInstallStrategySummary, "aguardando plano")) + "\n"
                + checkLine("Instalador Bedrock", emptyFallback(bedrockInstallerSummary, "aguardando validação")) + "\n"
                + checkLine("Bedrock", emptyFallback(bedrockSummary, "diagnóstico pendente")) + "\n"
                + checkLine("Runtime Bedrock", emptyFallback(bedrockRuntimeSummary, "parado")) + "\n"
                + checkLine("Termux", localAgentOnline ? "fallback legado online" : "fallback legado opcional") + "\n"
                + checkLine("Rede privada", isPackageInstalled("com.tailscale.ipn") ? networkChecklistLabel(server) : "VPN externa ainda é etapa futura") + "\n"
                + checkLine("VPS", hasPairing() ? "conexão direta salva" : "pareamento pendente");

        return new String[]{appBlock, deviceBlock, runtimeBlock, diagnosticsBlock, bedrockBlock, termuxBlock, depsBlock};
    }

    private String prepareChecklistText() {
        StringBuilder builder = new StringBuilder();
        String[] blocks = prepareChecklistBlocks();
        for (int i = 0; i < blocks.length; i++) {
            if (i > 0) builder.append("\n\n");
            builder.append(blocks[i]);
        }
        return builder.toString();
    }

    private boolean isPackageInstalled(String packageName) {
        try {
            if (Build.VERSION.SDK_INT >= 33) {
                getPackageManager().getPackageInfo(packageName, PackageManager.PackageInfoFlags.of(0));
            } else {
                getPackageManager().getPackageInfo(packageName, 0);
            }
            return true;
        } catch (Throwable ignored) {
            return false;
        }
    }

    private String emptyFallback(String value, String fallback) {
        return value == null || value.trim().isEmpty() ? fallback : value.trim();
    }

    private String normalizedServerUrl() {
        return emptyFallback(DEFAULT_VPS_URL, "").trim().replaceAll("/+$", "");
    }

    private String serverDisplayLabel() {
        String label = emptyFallback(DEFAULT_VPS_LABEL, "VPS não configurada no build");
        String url = normalizedServerUrl();
        return url.isEmpty() ? "VPS não configurada no build" : label;
    }

    private String selectedProfile() {
        int id = profileGroup.getCheckedRadioButtonId();
        View selected = id == -1 ? null : findViewById(id);
        if (selected != null && selected.getTag() != null) {
            return normalizeProfile(String.valueOf(selected.getTag()));
        }
        return appliedProfile();
    }

    private String selectedProfileSafe() {
        try {
            return selectedProfile();
        } catch (Throwable ignored) {
            return appliedProfile();
        }
    }

    private String appliedProfile() {
        String fromAgent = normalizeProfileOrEmpty(localAgentProfile);
        if (!fromAgent.isEmpty()) {
            return fromAgent;
        }
        return normalizeProfile(prefs.getString("profile", "midia"));
    }

    private String normalizeProfile(String profile) {
        String normalized = normalizeProfileOrEmpty(profile);
        return normalized.isEmpty() ? "midia" : normalized;
    }

    private String normalizeProfileOrEmpty(String profile) {
        String value = profile == null ? "" : profile.trim().toLowerCase(Locale.ROOT);
        value = value.replace('í', 'i').replace('í', 'i').replace('á', 'a').replace('é', 'e').replace('ó', 'o').replace('ú', 'u');
        value = value.replaceAll("[^a-z0-9_-]+", "-").replaceAll("^-+|-+$", "");
        if ("normal".equals(value) || "media".equals(value) || "midia".equals(value)) return "midia";
        if ("leve".equals(value)) return "leve";
        if ("completo".equals(value)) return "completo";
        if ("builder".equals(value) || "build".equals(value) || "apk-builder".equals(value)) return "builder";
        if ("turbo".equals(value)) return "turbo";
        if ("bedrock".equals(value)) return "bedrock";
        return "";
    }

    private void updateProfileRadioSelection(String profile) {
        if (profileGroup == null) return;
        String normalized = normalizeProfile(profile);
        runOnUiThread(() -> {
            for (int i = 0; i < profileGroup.getChildCount(); i++) {
                View child = profileGroup.getChildAt(i);
                if (child instanceof RadioButton && normalized.equals(String.valueOf(child.getTag()))) {
                    int id = child.getId();
                    if (profileGroup.getCheckedRadioButtonId() != id) {
                        profileGroup.check(id);
                    }
                    break;
                }
            }
        });
    }

    private String defaultDeviceName() {
        String maker = cleanTitle(Build.MANUFACTURER);
        String model = cleanTitle(Build.MODEL);
        if (model.toLowerCase(Locale.ROOT).startsWith(maker.toLowerCase(Locale.ROOT))) {
            return model;
        }
        return maker + " " + model;
    }

    private String cleanTitle(String value) {
        value = value == null ? "Android" : value.trim();
        if (value.isEmpty()) {
            return "Android";
        }
        return value.substring(0, 1).toUpperCase(Locale.ROOT) + value.substring(1);
    }

    private String[] profileRoles(String profile) {
        if ("leve".equals(profile)) {
            return new String[]{"phone-worker", "diagnostics", "log-summary"};
        }
        if ("completo".equals(profile)) {
            return new String[]{"phone-worker", "diagnostics", "log-summary", "maintenance-plan", "zip-validate", "ffmpeg", "ffprobe", "tts-convert"};
        }
        if ("builder".equals(profile)) {
            return new String[]{"phone-worker", "diagnostics", "log-summary", "apk-builder", "zip-validate", "vps-assist", "cache-worker"};
        }
        if ("turbo".equals(profile)) {
            return new String[]{"phone-worker", "diagnostics", "log-summary", "maintenance-plan", "zip-validate", "ffmpeg", "ffprobe", "tts-convert", "apk-builder", "vps-assist", "cache-worker"};
        }
        if ("bedrock".equals(profile)) {
            return new String[]{"phone-worker", "diagnostics", "log-summary", "bedrock", "bedrock-logs", "bedrock-backup"};
        }
        return new String[]{"phone-worker", "diagnostics", "log-summary", "zip-validate", "ffmpeg", "ffprobe", "tts-convert"};
    }

    private String profileLabel(String profile) {
        String normalized = normalizeProfile(profile);
        if ("leve".equals(normalized)) return "Leve";
        if ("completo".equals(normalized)) return "Completo";
        if ("builder".equals(normalized)) return "Builder";
        if ("turbo".equals(normalized)) return "Turbo";
        if ("bedrock".equals(normalized)) return "Bedrock";
        return "Normal";
    }

    private String profileDescription(String profile) {
        String normalized = normalizeProfile(profile);
        if ("leve".equals(normalized)) {
            return "Economia de bateria para uso leve.";
        }
        if ("completo".equals(normalized)) {
            return "Tarefas extras sem exigir modo máximo.";
        }
        if ("builder".equals(normalized)) {
            return "Compila APK no celular quando solicitado.";
        }
        if ("turbo".equals(normalized)) {
            return "Máximo desempenho para ajudar a VPS.";
        }
        if ("bedrock".equals(normalized)) {
            return "Reservado para funções futuras.";
        }
        return "Recomendado para tarefas normais.";
    }

    private void updateProfileHint(String profile) {
        String normalized = normalizeProfile(profile);
        String summary = profileLabel(normalized) + "\n" + profileDescription(normalized);
        runOnUiThread(() -> {
            if (profileSummaryText != null) {
                profileSummaryText.setText(summary);
            }
            if (profileHintText != null) {
                profileHintText.setText(profileDescription(normalized));
            }
        });
    }

    private void updateProfileSelectionHint(String profile) {
        String normalized = normalizeProfile(profile);
        runOnUiThread(() -> {
            if (profileHintText != null) {
                profileHintText.setText("Selecionado: " + profileLabel(normalized) + "\n" + profileDescription(normalized));
            }
        });
    }

    private void collapseProfileDetails() {
        runOnUiThread(() -> {
            profileExpanded = false;
            if (profileDetailsContent != null) profileDetailsContent.setVisibility(View.GONE);
            if (profileToggleButton != null) profileToggleButton.setText("Alterar perfil");
        });
    }


    private JSONArray jsonArray(String[] values) {
        JSONArray array = new JSONArray();
        for (String value : values) {
            array.put(value);
        }
        return array;
    }

    private String compactResultBody(String body) {
        if (body == null || body.trim().isEmpty()) {
            return "ok";
        }
        try {
            JSONObject json = new JSONObject(body);
            if (json.has("error")) {
                return json.optString("error", "erro");
            }
            if (json.has("message")) {
                return json.optString("message", "ok");
            }
            if (json.optBoolean("ok", false)) {
                return "ok";
            }
        } catch (Throwable ignored) {
        }
        String compact = body.replace('\n', ' ').replace('\r', ' ').trim();
        if (compact.length() > 180) {
            return compact.substring(0, 180) + "...";
        }
        return compact;
    }

    private String batteryStatusLabel(int status) {
        if (status == BatteryManager.BATTERY_STATUS_CHARGING) return "CHARGING";
        if (status == BatteryManager.BATTERY_STATUS_DISCHARGING) return "DISCHARGING";
        if (status == BatteryManager.BATTERY_STATUS_FULL) return "FULL";
        if (status == BatteryManager.BATTERY_STATUS_NOT_CHARGING) return "NOT_CHARGING";
        return "UNKNOWN";
    }

    private String pluggedLabel(int plugged) {
        if ((plugged & BatteryManager.BATTERY_PLUGGED_USB) != 0) return "USB";
        if ((plugged & BatteryManager.BATTERY_PLUGGED_AC) != 0) return "AC";
        if (Build.VERSION.SDK_INT >= 17 && (plugged & BatteryManager.BATTERY_PLUGGED_WIRELESS) != 0) return "WIRELESS";
        return "UNPLUGGED";
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    private interface WorkerRunnable {
        void run() throws Exception;
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
