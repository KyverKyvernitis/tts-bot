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

public class MainActivity extends Activity {
    private static final String APP_VERSION = "0.5.14";
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

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        migrateFcmSafetyStateForPatch52();
        buildUi();
        loadInputs();
        safeStartupTask(() -> CoreWorkerUpdateJobService.schedule(this, "activity_create"));
        safeStartupTask(() -> reportAppState("app_opened", "APK aberto; versão instalada " + APP_VERSION + " (" + BuildConfig.VERSION_CODE + ")"));
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
        prepareCard.addView(smallText("Visão rápida do aparelho."));

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
        updateCard.addView(smallText("APK e avisos de atualização."));
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
        technicalCard.addView(smallText("Logs, rede e opções avançadas."));
        technicalToggleButton = secondaryButton("Abrir detalhes técnicos");
        technicalToggleButton.setOnClickListener(v -> toggleTechnicalDetails());
        technicalCard.addView(technicalToggleButton);

        technicalDetailsContent = new LinearLayout(this);
        technicalDetailsContent.setOrientation(LinearLayout.VERTICAL);
        technicalDetailsContent.setVisibility(View.GONE);
        technicalCard.addView(technicalDetailsContent);

        systemChecklistText = smallText(prepareChecklistText());
        systemChecklistText.setTextColor(TEXT);
        systemChecklistText.setBackground(cardBackground(CARD_SOFT));
        systemChecklistText.setPadding(dp(10), dp(10), dp(10), dp(10));
        technicalDetailsContent.addView(systemChecklistText);

        termuxButton = secondaryButton("Abrir Termux");
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
            if (!updateLocalAgentStatus(true)) {
                show("Worker local offline. Abra o Termux e inicie o phone-worker antes de parear.\n\nO APK não vai criar um worker separado.");
                return;
            }

            JSONObject payload = new JSONObject();
            payload.put("code", code);
            payload.put("vps_url", serverUrl);
            payload.put("name", name);
            payload.put("device_name", name);
            putProfilePayload(payload, profile);
            payload.put("source", "core-worker-apk-companion");
            payload.put("apk_version", APP_VERSION);

            HttpResult result = request("POST", LOCAL_AGENT_PAIR_URL, payload, null);
            if (!result.ok()) {
                show("Falha ao parear pelo worker local: HTTP " + result.status + "\n\n" + compactResultBody(result.body));
                return;
            }
            JSONObject body = new JSONObject(result.body);
            if (!body.optBoolean("ok", false)) {
                show("O worker local não conseguiu parear.\n\n" + compactResultBody(result.body));
                return;
            }
            String workerId = body.optString("worker_id", localAgentWorkerId);
            prefs.edit()
                    .putString("server_url", serverUrl)
                    .putString("device_name", name)
                    .putString("profile", profile)
                    .putString("worker_id", workerId)
                    .putBoolean("paired_via_local_agent", true)
                    .remove("worker_token")
                    .apply();
            applyLocalAgentStatus(body);
            showLocalAgentText();
            updatePairingUi();
            registerFcmTokenAsync("pair_success");
            vpsState = "ok";
            show("Celular conectado.\nPerfil: " + profileLabel(profile) + "\nWorker: " + emptyFallback(workerId, "local"));
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
                    : "Perfil salvo no APK. Worker local offline; abra o Termux para sincronizar.";
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
            sendHeartbeatInternal(true);
        });
    }

    private void sendHeartbeatInternal(boolean showResult) throws Exception {
        sendHeartbeatInternal(showResult, null);
    }

    private void sendHeartbeatInternal(boolean showResult, String successPrefix) throws Exception {
        if (!updateLocalAgentStatus(true)) {
            show("Worker local offline. Abra o Termux para sincronizar.");
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
            message += synced ? "\nVPS recebeu heartbeat do Termux worker." : "\nWorker local respondeu, mas ainda não confirmou heartbeat na VPS.";
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

    private void reportFcmToken(String serverUrl, String token, String reason) {
        if (serverUrl == null || serverUrl.trim().isEmpty() || token == null || token.trim().isEmpty()) {
            return;
        }
        new Thread(() -> {
            try {
                JSONObject payload = statusSnapshot();
                payload.put("fcmToken", token.trim());
                payload.put("state", "registered");
                payload.put("reason", reason == null ? "activity" : reason);
                payload.put("permission", hasNotificationPermission() ? "granted" : "missing");
                HttpResult result = request("POST", serverUrl + "/core-worker/app/fcm-token", payload, null);
                if (result.ok()) {
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
            return fcmTokenPreview == null || fcmTokenPreview.trim().isEmpty() ? "ativo · fallback local ativo" : "ativo · token " + fcmTokenPreview + " · fallback local ativo";
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
        JSONObject profileStatus = payload.optJSONObject("status");
        if (profileStatus == null) {
            profileStatus = new JSONObject();
        }
        profileStatus.put("profile", profile);
        profileStatus.put("profile_label", profileLabel(profile));
        profileStatus.put("apk_scope", "onboarding_profile_only");
        payload.put("status", profileStatus);
    }

    private JSONObject statusSnapshot() throws Exception {
        JSONObject status = new JSONObject();
        status.put("app", "foreground");
        status.put("apk_companion", true);
        status.put("android_sdk", Build.VERSION.SDK_INT);
        status.put("manufacturer", Build.MANUFACTURER);
        status.put("model", Build.MODEL);
        status.put("local_agent_online", localAgentOnline);
        status.put("termux_installed", isPackageInstalled("com.termux"));
        status.put("termux_api_installed", isPackageInstalled("com.termux.api"));
        status.put("termux_boot_installed", isPackageInstalled("com.termux.boot"));
        status.put("tailscale_installed", isPackageInstalled("com.tailscale.ipn"));
        status.put("fcm_state", fcmState);
        status.put("fcm_token_preview", fcmTokenPreview);
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
            updateSystemChecklistText();
            if (userVisible) {
                if (ok) {
                    show("Tudo pronto.\nWorker online · perfil " + profileLabel(appliedProfile()));
                } else {
                    show("Worker local offline. Abra o Termux para acordar este celular.");
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

    private String localAgentLine() {
        String profile = appliedProfile();
        if (!localAgentOnline) {
            return "⚠️ Aguardando worker local\nAbra o Termux para acordar este celular.";
        }
        if (hasPairing()) {
            return "✅ Pronto para trabalhar\n" + profileLabel(profile) + " · Push " + fcmCompactLabel() + " · APK " + updateChecklistLabel();
        }
        return "⚠️ Worker detectado\nConecte este celular à VPS principal.";
    }


    private boolean hasPairing() {
        boolean pairedViaLocal = prefs.getBoolean("paired_via_local_agent", false);
        String serverUrl = prefs.getString("server_url", DEFAULT_VPS_URL);
        String workerId = prefs.getString("worker_id", "");
        boolean saved = pairedViaLocal && serverUrl != null && !serverUrl.isEmpty() && workerId != null && !workerId.isEmpty();
        boolean local = localAgentOnline && localAgentVpsConfigured && localAgentWorkerId != null && !localAgentWorkerId.trim().isEmpty();
        return saved || local;
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
            refreshLocalStatus("Não consegui abrir o Termux automaticamente. Abra o Termux; o autostart do Core Worker deve iniciar o watchdog local.");
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
                .setMessage("Isso remove a conexão salva no APK. O token real fica no Termux worker; o registro na VPS não é apagado automaticamente.")
                .setPositiveButton("Esquecer", (dialog, which) -> {
                    prefs.edit()
                            .remove("worker_token")
                            .remove("server_url")
                            .remove("profile")
                            .remove("paired_via_local_agent")
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
            if (systemChecklistText != null) {
                systemChecklistText.setText(prepareChecklistText());
            }
        });
    }

    private String prepareChecklistText() {
        String server = normalizedServerUrl();
        StringBuilder builder = new StringBuilder();
        builder.append("App\n");
        builder.append(checkLine("APK", APP_VERSION + " (" + BuildConfig.VERSION_CODE + ")")).append('\n');
        builder.append(checkLine("Push", fcmStatusLabel())).append('\n');
        builder.append(checkLine("Fallback", "JobScheduler local ativo")).append("\n\n");

        builder.append("Worker\n");
        builder.append(checkLine("Status", localAgentOnline ? "online" : "offline")).append('\n');
        builder.append(checkLine("Perfil", profileLabel(appliedProfile()))).append('\n');
        builder.append(checkLine("Jobs locais", localAgentJobsConfigured ? "ok" : "pendentes")).append('\n');
        builder.append(checkLine("SSHD", emptyFallback(localAgentSshdSummary, "não informado"))).append("\n\n");

        builder.append("Rede e Termux\n");
        builder.append(checkLine("VPS local", localAgentVpsConfigured ? "configurada" : "pendente")).append('\n');
        builder.append(checkLine("Rede privada", isPackageInstalled("com.tailscale.ipn") ? networkChecklistLabel(server) : "Tailscale externo ainda necessário")).append('\n');
        builder.append(checkLine("Termux", isPackageInstalled("com.termux") ? "instalado" : "precisa instalar")).append('\n');
        builder.append(checkLine("Termux:API", isPackageInstalled("com.termux.api") ? "instalado" : "precisa instalar")).append('\n');
        builder.append(checkLine("Termux:Boot", isPackageInstalled("com.termux.boot") ? "instalado" : "recomendado"));
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
