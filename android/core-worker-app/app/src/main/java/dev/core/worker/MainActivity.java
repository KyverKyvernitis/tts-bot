package dev.core.worker;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.res.ColorStateList;
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
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import androidx.core.content.FileProvider;

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

public class MainActivity extends Activity {
    private static final String APP_VERSION = "0.5.27";
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
    private TextView technicalTermuxText;
    private TextView technicalDependenciesText;
    private TextView updateText;
    private LinearLayout permissionGateCard;
    private LinearLayout mainContent;
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
    private Button clearButton;
    private LinearLayout technicalDetailsContent;
    private Button technicalToggleButton;
    private boolean technicalExpanded = false;
    private TextView profileSummaryText;
    private LinearLayout profileDetailsContent;
    private Button profileToggleButton;
    private boolean profileExpanded = false;

    private volatile boolean localAgentOnline = false;
    private volatile String localAgentVersion = "";
    private volatile String localAgentProfile = "";
    private volatile String runtimeMode = "apk-native-python-first";
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
    private volatile int internalLightJobsLastCount = 0;
    private volatile String internalLightJobsLastSummary = "nenhum job executado ainda";
    private volatile int internalLightJobsRunningCount = 0;
    private volatile int internalLightJobsPendingCount = 0;
    private volatile String internalLightJobsQueueSummary = "fila aguardando";
    private volatile int internalLightJobsAutoTotal = 0;
    private volatile int internalLightJobsManualTotal = 0;
    private volatile String internalLightJobsCatalogSummary = "catálogo aguardando";
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

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        migrateFcmSafetyStateForPatch52();
        buildUi();
        loadInputs();
        safeStartupTask(this::prepareInternalRuntimePreview);
        safeStartupTask(this::prepareNativeRuntimeState);
        safeStartupTask(() -> CoreWorkerUpdateJobService.schedule(this, "activity_create"));
        safeStartupTask(() -> reportAppState("app_opened", "APK aberto; versão instalada " + APP_VERSION + " (" + BuildConfig.VERSION_CODE + ")"));
        safeStartupTask(() -> reportAppState("runtime_internal_ready", "runtime interno preparado em modo híbrido; heartbeat/status direto ativo"));
        safeStartupTask(() -> sendInternalRuntimeHeartbeat(false, "app_opened"));
        safeStartupTask(() -> sendNativeWorkerHeartbeat(false, "app_opened"));
        safeStartupTask(() -> fetchAndRunLightJobs(false, "app_opened"));
        safeStartupTask(this::updatePermissionGate);
        refreshLocalStatus("Pronto. O app verifica automaticamente se este celular já está pareado.");
        safeStartupTask(() -> checkLocalAgent(false));
        safeStartupTask(this::autoVerifySavedPairing);
        safeStartupTask(this::autoCheckForUpdate);
        scheduleFcmTokenRegistration("activity_create");
    }

    @Override
    protected void onResume() {
        super.onResume();
        safeStartupTask(this::updatePermissionGate);
        safeStartupTask(() -> CoreWorkerUpdateJobService.schedule(this, "activity_resume"));
        safeStartupTask(this::autoVerifySavedPairing);
        safeStartupTask(this::autoCheckForUpdate);
        safeStartupTask(() -> sendInternalRuntimeHeartbeat(false, "activity_resume"));
        safeStartupTask(() -> sendNativeWorkerHeartbeat(false, "activity_resume"));
        safeStartupTask(() -> fetchAndRunLightJobs(false, "activity_resume"));
        scheduleFcmTokenRegistration("activity_resume");
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        safeStartupTask(this::updatePermissionGate);
        refreshLocalStatus(requestCode == 4103 ? "Permissão de notificação atualizada. Verifique as demais permissões necessárias." : null);
        if (requestCode == 4103) {
            safeStartupTask(() -> CoreWorkerUpdateJobService.schedule(this, "notification_permission_result"));
            scheduleFcmTokenRegistration("notification_permission_result");
            safeStartupTask(this::autoCheckForUpdate);
        }
    }

    private interface SafeStartupRunnable {
        void run() throws Exception;
    }

    private void safeStartupTask(SafeStartupRunnable runnable) {
        try {
            runnable.run();
        } catch (Throwable ignored) {
        }
    }

    private void buildUi() {
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setBackgroundColor(BG);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(16), dp(18), dp(16), dp(24));
        scroll.addView(root, new ScrollView.LayoutParams(
                ScrollView.LayoutParams.MATCH_PARENT,
                ScrollView.LayoutParams.WRAP_CONTENT
        ));

        TextView title = new TextView(this);
        title.setText("Core Worker");
        title.setTextColor(TEXT);
        title.setTextSize(31);
        title.setGravity(Gravity.START);
        title.setTypeface(null, 1);
        root.addView(title);

        TextView subtitle = new TextView(this);
        subtitle.setText("Seu celular está ajudando a VPS do bot.");
        subtitle.setTextColor(MUTED);
        subtitle.setTextSize(14);
        subtitle.setPadding(0, dp(5), 0, dp(12));
        root.addView(subtitle);

        buildPermissionGate(root);

        mainContent = new LinearLayout(this);
        mainContent.setOrientation(LinearLayout.VERTICAL);
        root.addView(mainContent, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        ));

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
        prepareCard.addView(sectionTitle("Status"));
        prepareCard.addView(smallText("Resumo do APK interno e do worker local."));

        localAgentText = smallText("Este celular ainda não foi verificado.");
        localAgentText.setTextColor(TEXT);
        localAgentText.setBackground(cardBackground(CARD_SOFT));
        localAgentText.setPadding(dp(12), dp(10), dp(12), dp(10));
        prepareCard.addView(localAgentText);

        prepareButton = primaryButton("Verificar agora");
        prepareButton.setOnClickListener(v -> checkLocalAgent(true));
        prepareCard.addView(prepareButton);

        connectCard = cardWithTopMargin(mainContent);
        connectTitleText = sectionTitle("Conexão");
        connectCard.addView(connectTitleText);
        connectHintText = smallText("Pareie uma vez. Depois o app lembra a VPS principal.");
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
        updateCard.addView(smallText("APK publicado, push e fallback local."));
        updateText = smallText("APK " + APP_VERSION + " · ainda não verificado.");
        updateText.setTextColor(TEXT);
        updateText.setBackground(cardBackground(CARD_SOFT));
        updateText.setPadding(dp(10), dp(10), dp(10), dp(10));
        updateCard.addView(updateText);

        updateCheckButton = secondaryButton("Verificar atualização");
        updateCheckButton.setOnClickListener(v -> checkForUpdate());
        updateCard.addView(updateCheckButton);


        LinearLayout technicalCard = cardWithTopMargin(mainContent);
        technicalCard.addView(sectionTitle("Detalhes técnicos"));
        technicalCard.addView(smallText("Status avançado, runtime, rede e dependências ficam recolhidos aqui."));
        technicalToggleButton = secondaryButton("Abrir detalhes técnicos");
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

        clearButton = dangerButton("Esquecer conexão local");
        clearButton.setBackground(makeButtonBackground(Color.rgb(91, 50, 57), BUTTON_DISABLED_BG));
        clearButton.setTextColor(new ColorStateList(new int[][]{new int[]{-android.R.attr.state_enabled}, new int[]{}}, new int[]{BUTTON_DISABLED_TEXT, TEXT}));
        clearButton.setOnClickListener(v -> confirmClearPairing());
        technicalDetailsContent.addView(clearButton);

        statusText = new TextView(this);
        statusText.setTextColor(TEXT);
        statusText.setTextSize(13);
        statusText.setPadding(dp(14), dp(14), dp(14), dp(14));
        statusText.setBackground(cardBackground(CARD));
        LinearLayout.LayoutParams statusParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        statusParams.setMargins(0, dp(10), 0, 0);
        mainContent.addView(statusText, statusParams);

        setContentView(scroll);
    }

    private void buildPermissionGate(LinearLayout root) {
        permissionGateCard = card();
        permissionGateCard.setBackground(cardBackground(CARD_SOFT));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        params.setMargins(0, 0, 0, dp(14));
        root.addView(permissionGateCard, params);

        permissionGateCard.addView(sectionTitle("Permissões necessárias"));
        TextView intro = smallText("Permita notificações, instalação de APK e uso em segundo plano.");
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
        return hasNotificationPermission() && hasInstallPermission() && hasBatteryPermission();
    }

    private void updatePermissionGate() {
        runOnUiThread(() -> {
            boolean notificationOk = hasNotificationPermission();
            boolean installOk = hasInstallPermission();
            boolean batteryOk = hasBatteryPermission();
            boolean allOk = notificationOk && installOk && batteryOk;

            if (permissionStatusText != null) {
                StringBuilder builder = new StringBuilder();
                builder.append(permissionLine("Notificações", notificationOk, "avisar APK novo publicado pela VPS")).append('\n');
                builder.append(permissionLine("Instalar atualizações", installOk, "abrir o APK baixado da VPS")).append('\n');
                builder.append(permissionLine("Segundo plano/bateria", batteryOk, "manter checagens locais mais confiáveis"));
                permissionStatusText.setText(builder.toString());
            }

            if (permissionGateCard != null) {
                permissionGateCard.setVisibility(allOk ? View.GONE : View.VISIBLE);
            }
            if (mainContent != null) {
                mainContent.setVisibility(allOk ? View.VISIBLE : View.GONE);
            }
            if (notificationPermissionButton != null) {
                notificationPermissionButton.setVisibility(notificationOk ? View.GONE : View.VISIBLE);
            }
            if (installPermissionButton != null) {
                installPermissionButton.setVisibility(installOk ? View.GONE : View.VISIBLE);
            }
            if (batteryPermissionButton != null) {
                batteryPermissionButton.setVisibility(batteryOk ? View.GONE : View.VISIBLE);
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
            technicalDetailsContent.setVisibility(technicalExpanded ? View.VISIBLE : View.GONE);
        }
        if (technicalToggleButton != null) {
            technicalToggleButton.setText(technicalExpanded ? "Fechar detalhes" : "Abrir detalhes técnicos");
        }
        refreshLocalStatus(null);
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
                File filesBase = getExternalFilesDir(null);
                if (filesBase == null) {
                    filesBase = getCacheDir();
                }
                File updateDir = new File(filesBase, "updates");
                if (!updateDir.exists() && !updateDir.mkdirs()) {
                    String detail = "Não consegui criar a pasta local de atualização.";
                    show(detail);
                    setUpdateActionState("Falha: não consegui criar pasta local para baixar o APK.", "Tentar novamente", true, false);
                    reportUpdateNotification(serverUrl, "download_failed", false, "falha criando pasta local de atualização");
                    return;
                }
                File apkFile = new File(updateDir, safeLocalApkName());
                downloadFile(latestApkUrl, apkFile, (done, total) -> {
                    String progress = total > 0
                            ? "Baixando " + version + "... " + Math.max(0, Math.min(100, (int) ((done * 100L) / total))) + "% · " + formatBytes(done) + " / " + formatBytes(total)
                            : "Baixando " + version + "... " + formatBytes(done);
                    setUpdateActionState(progress, "Baixando...", true, true);
                });
                setUpdateActionState("Download concluído. Validando APK...", "Validando...", true, true);
                if (latestApkSha256 != null && !latestApkSha256.trim().isEmpty()) {
                    String actual = sha256Of(apkFile);
                    if (!actual.equalsIgnoreCase(latestApkSha256.trim())) {
                        apkFile.delete();
                        String detail = "Atualização baixada, mas o hash não confere. Instalação bloqueada por segurança.";
                        show(detail);
                        setUpdateActionState("Falha: hash SHA-256 diferente do latest.json.\nInstalação bloqueada por segurança.", "Tentar novamente", true, false);
                        reportUpdateNotification(serverUrl, "download_failed", false, "sha256 divergente no APK baixado");
                        return;
                    }
                }
                reportUpdateNotification(serverUrl, "download_verified", true, "APK baixado direto e sha256 validado");
                updateUpdateUi("Atualização baixada e verificada. Vou abrir o instalador do Android.\nArquivo: " + apkFile.getName() + "\nSe aparecer bloqueio, permita instalar apps desconhecidos para o Core Worker.", true, true);
                setUpdateActionState("APK baixado e validado. Abrindo instalador do Android...", "Abrindo...", true, true);
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
            payload.put("roles", new JSONArray().put("apk-native").put("diagnostics").put("internal-jobs"));
            payload.put("capabilities", new JSONArray().put("apk-native").put("android-status").put("native-boot").put("safe-shell-probe").put("python-embedded"));
            payload.put("supported_tasks", new JSONArray());
            payload.put("app_jobs", supportedLightJobsArray());
            JSONObject status = payload.optJSONObject("status");
            if (status == null) status = new JSONObject();
            status.put("apk_native_worker", true);
            status.put("termux_required_now", false);
            status.put("termux_role", "fallback-temporario");
            status.put("migration_stage", "apk-native-runtime-python-phase2");
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
        new Thread(() -> sendNativeWorkerHeartbeatInternal(showResult, reason), "core-worker-native-heartbeat").start();
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
            payload.put("roles", new JSONArray().put("apk-native").put("diagnostics").put("internal-jobs"));
            payload.put("capabilities", new JSONArray().put("apk-native").put("android-status").put("native-boot").put("safe-shell-probe").put("python-embedded"));
            payload.put("supported_tasks", new JSONArray());
            payload.put("app_jobs", supportedLightJobsArray());
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
            if (!Python.isStarted()) {
                Python.start(new AndroidPlatform(this));
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
                || "log_summarizer".equals(script);
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
            meta.put("mode", "apk-native-python-first");
            meta.put("active", true);
            meta.put("internal_runtime", "apk-native-runtime");
            meta.put("apk_version", APP_VERSION);
            meta.put("version_code", BuildConfig.VERSION_CODE);
            meta.put("created_by", "core-worker-apk");
            meta.put("summary", "Runtime interno ativo para status, boot, jobs seguros e shell controlado no sandbox do app. Termux fica só como fallback temporário.");
            meta.put("migration_stage", "apk-native-python-first");
            writeTextFile(state, meta.toString());
            internalRuntimeState = "preparado · heartbeat ativo";
            internalRuntimePath = runtimeDir.getAbsolutePath();
            runtimeMode = "apk-native-python-first";
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

    private JSONObject runtimeSnapshot() throws Exception {
        JSONObject runtime = new JSONObject();
        runtime.put("mode", runtimeMode == null || runtimeMode.trim().isEmpty() ? "apk-native-python-first" : runtimeMode.trim());
        runtime.put("current_worker", nativeWorkerOnline ? "apk-native-worker" : (localAgentOnline ? "termux-fallback" : "apk-internal-heartbeat"));
        runtime.put("internal_runtime", "apk-native-runtime");
        runtime.put("internal_runtime_state", internalRuntimeState == null ? "" : internalRuntimeState);
        runtime.put("internal_runtime_online", internalRuntimeOnline);
        runtime.put("internal_runtime_heartbeat_state", internalRuntimeHeartbeatState == null ? "" : internalRuntimeHeartbeatState);
        runtime.put("internal_runtime_last_heartbeat_at", internalRuntimeLastHeartbeatAt);
        runtime.put("internal_runtime_last_error", internalRuntimeLastError == null ? "" : internalRuntimeLastError);
        runtime.put("internal_runtime_path", internalRuntimePath == null ? "" : internalRuntimePath);
        runtime.put("termux_required_now", false);
        runtime.put("termux_fallback_available", localAgentOnline);
        runtime.put("advanced_jobs_require_termux", true);
        runtime.put("jobs_runtime", "apk-native-python-first");
        runtime.put("migration_stage", "apk-native-runtime-python-phase2");
        runtime.put("light_jobs_state", internalLightJobsState == null ? "" : internalLightJobsState);
        runtime.put("light_jobs_last_check_at", internalLightJobsLastCheckAt);
        runtime.put("light_jobs_last_count", internalLightJobsLastCount);
        runtime.put("light_jobs_last_summary", internalLightJobsLastSummary == null ? "" : internalLightJobsLastSummary);
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
        runtime.put("summary", "APK assume status, boot, jobs internos e shell controlado; Termux fica como fallback para jobs avançados/build.");
        return runtime;
    }

    private String runtimeStatusLabel() {
        String state = internalRuntimeState == null || internalRuntimeState.trim().isEmpty() ? "não preparado" : internalRuntimeState.trim();
        String hb = internalRuntimeOnline ? "APK online" : "APK aguardando sync";
        return "APK nativo · " + hb + " · " + state;
    }

    private void sendInternalRuntimeHeartbeat(boolean showResult, String reason) {
        new Thread(() -> sendInternalRuntimeHeartbeatInternal(showResult, reason), "core-worker-apk-heartbeat").start();
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
            payload.put("runtime_mode", "apk-native-python-first");
            payload.put("internal_runtime", "apk-native-runtime");
            payload.put("internal_runtime_state", internalRuntimeState == null ? "" : internalRuntimeState);
            payload.put("internal_runtime_path", internalRuntimePath == null ? "" : internalRuntimePath);
            payload.put("workerId", effectiveWorkerId());
            payload.put("installId", installId());
            payload.put("deviceName", deviceNameInput == null ? defaultDeviceName() : deviceNameInput.getText().toString().trim());
            payload.put("appVersion", APP_VERSION);
            payload.put("appVersionCode", BuildConfig.VERSION_CODE);
            payload.put("profile", appliedProfile());
            payload.put("profileLabel", profileLabel(appliedProfile()));
            payload.put("localAgentOnline", localAgentOnline);
            payload.put("termuxWorkerOnline", localAgentOnline);
            payload.put("nativeWorkerOnline", nativeWorkerOnline);
            payload.put("jobsRuntime", "apk-native-python-first");
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
        new Thread(() -> {
            try {
                JSONObject payload = statusSnapshot();
                payload.put("installId", installId());
                payload.put("workerId", effectiveWorkerId());
                payload.put("reason", reason == null ? "background" : reason);
                payload.put("supportedJobs", supportedLightJobsArray());
                HttpResult response = request("POST", serverUrl + "/core-worker/app/jobs/fetch", payload, null);
                internalLightJobsLastCheckAt = System.currentTimeMillis();
                if (!response.ok()) {
                    internalLightJobsState = "falha HTTP " + response.status;
                    internalRuntimeLastError = compactResultBody(response.body);
                    updateSystemChecklistText();
                    return;
                }
                JSONObject body = new JSONObject(response.body);
                JSONArray jobs = body.optJSONArray("jobs");
                int count = jobs == null ? 0 : jobs.length();
                internalLightJobsLastCount = count;
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
                appStatusLastError = internalRuntimeLastError;
                updateSystemChecklistText();
            }
        }, "core-worker-apk-light-jobs").start();
    }

    private JSONArray supportedLightJobsArray() {
        return new JSONArray()
                .put("apk_ping")
                .put("apk_status_refresh")
                .put("apk_report_logs")
                .put("apk_diagnostic")
                .put("apk_check_update")
                .put("apk_test_vps_connection")
                .put("apk_upload_report")
                .put("apk_upload_app_logs")
                .put("apk_clear_app_cache")
                .put("apk_cache_cleanup")
                .put("apk_sync_profile")
                .put("apk_sync_runtime_state")
                .put("apk_download_small")
                .put("apk_verify_file")
                .put("apk_job_history")
                .put("apk_device_diagnostic")
                .put("apk_network_diagnostic")
                .put("apk_push_diagnostic")
                .put("apk_update_diagnostic")
                .put("apk_runtime_diagnostic")
                .put("apk_storage_diagnostic")
                .put("apk_worker_bridge_status")
                .put("apk_collect_status_bundle")
                .put("apk_cleanup_runtime_cache")
                .put("apk_refresh_runtime")
                .put("apk_force_status_bundle")
                .put("apk_test_notification")
                .put("apk_repair_local_state")
                .put("apk_reset_job_history")
                .put("apk_trim_cache")
                .put("apk_sync_profile_now")
                .put("apk_verify_update_state")
                .put("apk_native_worker_status")
                .put("apk_native_boot_status")
                .put("apk_local_shell_probe")
                .put("apk_python_runtime_probe")
                .put("apk_python_health_check")
                .put("apk_python_runtime_info")
                .put("apk_python_status_bundle")
                .put("apk_python_storage_check")
                .put("apk_python_log_summary");
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
            safePutPayload(logs, "history", internalJobHistoryJson());
            safePutPayload(logs, "status", statusSnapshot());
            result.put("reportKind", "app-internal-logs");
            safePutPayload(result, "logs", logs);
            result.put("message", "logs do APK enviados para a VPS");
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
            safePutPayload(result, "network", network);
            boolean ok = network.optBoolean("ok", false);
            internalDiagnosticsSummary = ok ? "rede ok · " + quickNetworkLabel() : "rede com atenção";
            internalDiagnosticsLastAt = System.currentTimeMillis();
            if (!ok) {
                result.put("ok", false);
                result.put("error", network.optString("error", "rede indisponível"));
            }
            result.put("message", ok ? "diagnóstico de rede concluído pelo APK" : "diagnóstico de rede encontrou problema");
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
            safePutPayload(result, "storage", storage);
            internalStorageSummary = storageSummary(storage);
            internalDiagnosticsSummary = "armazenamento " + internalStorageSummary;
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", "diagnóstico de armazenamento interno concluído");
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
            safePutPayload(result, "bundle", bundle);
            internalDiagnosticsSummary = "pacote de status enviado";
            internalDiagnosticsLastAt = System.currentTimeMillis();
            result.put("message", "pacote completo de status do APK enviado para a VPS");
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
        storage.put("files_bytes", directorySize(files));
        storage.put("cache_bytes", directorySize(cache));
        storage.put("runtime_bytes", directorySize(runtime));
        storage.put("job_cache_bytes", directorySize(jobCache));
        storage.put("job_cache_files", directoryFileCount(jobCache));
        storage.put("files_dir", files == null ? "" : files.getName());
        storage.put("cache_dir", cache == null ? "" : cache.getName());
        storage.put("scope", "app-specific-internal");
        storage.put("summary", storageSummary(storage));
        return storage;
    }

    private String storageSummary(JSONObject storage) {
        try {
            long cacheBytes = storage.optLong("cache_bytes", 0L);
            long jobBytes = storage.optLong("job_cache_bytes", 0L);
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
        bridge.put("mode", "apk-native-python-first");
        bridge.put("apk_internal_online", internalRuntimeOnline);
        bridge.put("apk_native_worker_online", nativeWorkerOnline);
        bridge.put("termux_worker_online", localAgentOnline);
        bridge.put("termux_agent_version", localAgentVersion == null ? "" : localAgentVersion);
        bridge.put("termux_profile", localAgentProfile == null ? "" : localAgentProfile);
        bridge.put("jobs_real_runtime", nativeWorkerOnline ? "apk-native-worker" : "apk-internal-queue");
        bridge.put("jobs_internal_runtime", "apk-native-safe-queue");
        bridge.put("termux_role", "fallback-temporario");
        bridge.put("ready_for_termux_reduction", internalRuntimeOnline && hasPairing());
        String summary = nativeWorkerOnline ? "APK nativo pareado" : (internalRuntimeOnline ? "APK interno online" : "APK aguardando");
        summary += localAgentOnline ? " · Termux fallback online" : " · Termux fallback offline";
        bridge.put("summary", summary);
        return bridge;
    }

    private JSONObject collectStatusBundle(String serverUrl) throws Exception {
        JSONObject bundle = new JSONObject();
        safePutPayload(bundle, "status", statusSnapshot());
        safePutPayload(bundle, "device", deviceDiagnosticSnapshot());
        safePutPayload(bundle, "network", networkDiagnosticSnapshot(serverUrl));
        safePutPayload(bundle, "push", pushDiagnosticSnapshot());
        safePutPayload(bundle, "update", updateSnapshot());
        safePutPayload(bundle, "runtime", runtimeDiagnosticSnapshot());
        safePutPayload(bundle, "storage", storageSnapshot());
        safePutPayload(bundle, "bridge", workerBridgeStatusSnapshot());
        safePutPayload(bundle, "history", internalJobHistoryJson());
        bundle.put("summary", "status completo do APK coletado sem Termux");
        return bundle;
    }

    private JSONObject diagnosticSnapshot(String serverUrl) throws Exception {
        JSONObject diagnostic = new JSONObject();
        diagnostic.put("timestamp", System.currentTimeMillis());
        diagnostic.put("runtimeLabel", runtimeStatusLabel());
        diagnostic.put("lightJobs", internalLightJobsState == null ? "" : internalLightJobsState);
        diagnostic.put("lastLightJob", internalLightJobsLastSummary == null ? "" : internalLightJobsLastSummary);
        diagnostic.put("queue", internalLightJobsQueueSummary == null ? "" : internalLightJobsQueueSummary);
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
        safePutPayload(diagnostic, "bridge", workerBridgeStatusSnapshot());
        internalDiagnosticsSummary = "diagnóstico completo ok";
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
        InputStream input = conn.getInputStream();
        FileOutputStream output = new FileOutputStream(target);
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
        payload.put("capabilities", jsonArray(profileRoles(profile)));
        payload.put("supported_tasks", new JSONArray());
        payload.put("app_jobs", supportedLightJobsArray());
        JSONObject profileStatus = payload.optJSONObject("status");
        if (profileStatus == null) {
            profileStatus = new JSONObject();
        }
        profileStatus.put("profile", profile);
        profileStatus.put("profile_label", profileLabel(profile));
        profileStatus.put("apk_scope", "native-runtime-python-phase2");
        profileStatus.put("runtime_mode", runtimeMode == null || runtimeMode.trim().isEmpty() ? "apk-native-python-first" : runtimeMode);
        profileStatus.put("internal_runtime_state", internalRuntimeState == null ? "" : internalRuntimeState);
        profileStatus.put("runtime", runtimeSnapshot());
        payload.put("runtime_mode", runtimeMode == null || runtimeMode.trim().isEmpty() ? "apk-native-python-first" : runtimeMode);
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
        status.put("termux_installed", isPackageInstalled("com.termux"));
        status.put("termux_api_installed", isPackageInstalled("com.termux.api"));
        status.put("termux_boot_installed", isPackageInstalled("com.termux.boot"));
        status.put("tailscale_installed", isPackageInstalled("com.tailscale.ipn"));
        status.put("fcm_state", fcmState);
        status.put("fcm_token_preview", fcmTokenPreview);
        status.put("runtime_mode", runtimeMode == null || runtimeMode.trim().isEmpty() ? "apk-native-python-first" : runtimeMode);
        status.put("internal_runtime_state", internalRuntimeState == null ? "" : internalRuntimeState);
        status.put("internal_runtime_online", internalRuntimeOnline);
        status.put("internal_runtime_heartbeat_state", internalRuntimeHeartbeatState == null ? "" : internalRuntimeHeartbeatState);
        status.put("internal_runtime_last_error", internalRuntimeLastError == null ? "" : internalRuntimeLastError);
        status.put("internal_light_jobs_state", internalLightJobsState == null ? "" : internalLightJobsState);
        status.put("internal_light_jobs_last_check_at", internalLightJobsLastCheckAt);
        status.put("internal_light_jobs_last_count", internalLightJobsLastCount);
        status.put("internal_light_jobs_last_summary", internalLightJobsLastSummary == null ? "" : internalLightJobsLastSummary);
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
            updatePairingUi();
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
            updatePairingUi();
            updateSystemChecklistText();
            return true;
        } catch (Throwable exc) {
            localAgentOnline = false;
            localAgentVersion = "";
            localAgentProfile = "";
            localAgentMessage = "offline ao aplicar perfil";
            updatePairingUi();
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
            prefs.edit().putString("profile", reportedProfile).apply();
            updateProfileRadioSelection(reportedProfile);
            updateProfileHint(reportedProfile);
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
        refreshLocalStatus(message);
        setButtonsEnabled(false);
        new Thread(() -> {
            try {
                runnable.run();
            } catch (Throwable exc) {
                show("Erro: " + exc.getClass().getSimpleName() + " · " + String.valueOf(exc.getMessage()));
            } finally {
                runOnUiThread(() -> setButtonsEnabled(true));
            }
        }).start();
    }

    private void show(String message) {
        runOnUiThread(() -> refreshLocalStatus(message));
    }

    private void toast(String message) {
        runOnUiThread(() -> Toast.makeText(this, message, Toast.LENGTH_SHORT).show());
    }

    private void setButtonsEnabled(boolean enabled) {
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
        if (updateInstallButton != null) applyUpdateButtonState(latestUpdateAvailable, updateDownloadBusy ? "Baixando..." : "Atualizar agora", updateDownloadBusy);
        if (clearButton != null) clearButton.setEnabled(enabled);
    }

    private void refreshLocalStatus(String extra) {
        if (statusText == null) {
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
            if (technicalTermuxText != null) technicalTermuxText.setText(blocks[4]);
            if (technicalDependenciesText != null) technicalDependenciesText.setText(blocks[5]);
            if (systemChecklistText != null) {
                systemChecklistText.setText(prepareChecklistText());
            }
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
                + checkLine("Jobs reais", nativeWorkerOnline ? "APK nativo" : "APK interno · fallback Termux") + "\n"
                + checkLine("Shell", emptyFallback(nativeShellSummary, "controlado aguardando")) + "\n"
                + checkLine("Python", emptyFallback(nativePythonSummary, "aguardando health check"));

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

        String sshd = emptyFallback(localAgentSshdSummary, "não informado");
        if (sshd.toLowerCase(Locale.ROOT).contains("porta configurada não apareceu")) {
            sshd = "ativo · porta não detectada";
        }
        String termuxBlock = "Fallback Termux\n"
                + checkLine("Status", localAgentOnline ? "online como fallback" : "offline") + "\n"
                + checkLine("Jobs avançados", localAgentJobsConfigured ? "fallback disponível" : "não dependente para runtime básico") + "\n"
                + checkLine("SSHD", sshd);

        String depsBlock = "Migração sem Termux\n"
                + checkLine("Status aparelho", "APK nativo") + "\n"
                + checkLine("Boot/autostart", emptyFallback(nativeBootSummary, "APK nativo")) + "\n"
                + checkLine("Jobs internos", "APK nativo") + "\n"
                + checkLine("Jobs Python", nativePythonAvailable ? "APK + Python interno" : "aguardando health check") + "\n"
                + checkLine("Shell", emptyFallback(nativeShellSummary, "controlado aguardando")) + "\n"
                + checkLine("Python", emptyFallback(nativePythonSummary, "runtime embutido aguardando")) + "\n"
                + checkLine("Termux", localAgentOnline ? "fallback temporário online" : "não exigido para status/boot/jobs internos") + "\n"
                + checkLine("Rede privada", isPackageInstalled("com.tailscale.ipn") ? networkChecklistLabel(server) : "VPN externa ainda é etapa futura") + "\n"
                + checkLine("VPS", hasPairing() ? "conexão direta salva" : "pareamento pendente");

        return new String[]{appBlock, deviceBlock, runtimeBlock, diagnosticsBlock, termuxBlock, depsBlock};
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
