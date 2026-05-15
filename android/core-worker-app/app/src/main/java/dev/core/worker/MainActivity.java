package dev.core.worker;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Bundle;
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

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.OutputStreamWriter;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.UUID;

public class MainActivity extends Activity {
    private static final String APP_VERSION = "0.2.0";
    private static final String PREFS = "core_worker_private";
    private static final int BG = Color.rgb(11, 16, 32);
    private static final int CARD = Color.rgb(21, 27, 46);
    private static final int TEXT = Color.rgb(247, 248, 252);
    private static final int MUTED = Color.rgb(183, 190, 212);
    private static final int ACCENT = Color.rgb(110, 168, 254);

    private SharedPreferences prefs;
    private EditText serverUrlInput;
    private EditText pairCodeInput;
    private EditText deviceNameInput;
    private RadioGroup profileGroup;
    private TextView statusText;
    private TextView profileHintText;
    private Button testButton;
    private Button pairButton;
    private Button saveProfileButton;
    private Button heartbeatButton;
    private Button tailscaleButton;
    private Button clearButton;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        ensureWorkerId();
        buildUi();
        loadInputs();
        refreshLocalStatus("Pronto. Conecte o Tailscale, gere um código no Discord e toque em Conectar.");
    }

    private void buildUi() {
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setBackgroundColor(BG);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(18), dp(18), dp(18), dp(28));
        scroll.addView(root, new ScrollView.LayoutParams(
                ScrollView.LayoutParams.MATCH_PARENT,
                ScrollView.LayoutParams.WRAP_CONTENT
        ));

        TextView title = new TextView(this);
        title.setText("Core Worker");
        title.setTextColor(TEXT);
        title.setTextSize(28);
        title.setGravity(Gravity.START);
        title.setTypeface(null, 1);
        root.addView(title);

        TextView subtitle = new TextView(this);
        subtitle.setText("APK privado e leve. Ele só facilita conectar este celular, escolher o perfil e enviar status básico. O controle pesado continua no Discord/VPS.");
        subtitle.setTextColor(MUTED);
        subtitle.setTextSize(14);
        subtitle.setPadding(0, dp(8), 0, dp(14));
        root.addView(subtitle);

        LinearLayout connectCard = card();
        root.addView(connectCard);
        connectCard.addView(sectionTitle("1. Conectar este celular"));
        connectCard.addView(smallText("Gere um código no painel workers do Discord e cole aqui. No futuro, este processo será automático pelo APK/QR."));

        serverUrlInput = input("URL da VPS", "http://100.x.x.x:10000");
        connectCard.addView(label("URL da VPS"));
        connectCard.addView(serverUrlInput);

        pairCodeInput = input("Código CORE-XXXX", "CORE-XXXXXXXX");
        pairCodeInput.setAllCaps(true);
        connectCard.addView(label("Código de pareamento"));
        connectCard.addView(pairCodeInput);

        deviceNameInput = input("Nome do celular", defaultDeviceName());
        connectCard.addView(label("Nome deste celular"));
        connectCard.addView(deviceNameInput);

        testButton = button("Testar VPS");
        testButton.setOnClickListener(v -> testServer());
        connectCard.addView(testButton);

        pairButton = button("Conectar / parear celular");
        pairButton.setOnClickListener(v -> pairWorker());
        connectCard.addView(pairButton);

        LinearLayout profileCard = card();
        LinearLayout.LayoutParams profileParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        profileParams.setMargins(0, dp(14), 0, 0);
        root.addView(profileCard, profileParams);

        profileCard.addView(sectionTitle("2. Perfil deste celular"));
        profileCard.addView(smallText("Aqui você escolhe o que este celular pode oferecer para a VPS. O APK altera apenas o próprio celular, não gerencia outros workers."));

        profileGroup = new RadioGroup(this);
        profileGroup.setOrientation(RadioGroup.VERTICAL);
        profileGroup.setPadding(0, dp(6), 0, dp(6));
        addProfileRadio("leve", "Leve · diagnósticos e logs");
        addProfileRadio("midia", "Mídia · FFmpeg, TTS, logs e ZIP");
        addProfileRadio("completo", "Completo · mídia + manutenção");
        addProfileRadio("bedrock", "Bedrock · Minecraft Bedrock futuro");
        profileGroup.setOnCheckedChangeListener((group, checkedId) -> {
            String profile = selectedProfile();
            prefs.edit().putString("profile", profile).apply();
            updateProfileHint(profile);
            refreshLocalStatus("Perfil local selecionado: " + profileLabel(profile) + ". Toque em Salvar perfil para enviar ao painel.");
        });
        profileCard.addView(profileGroup);

        profileHintText = smallText("");
        profileCard.addView(profileHintText);

        saveProfileButton = button("Salvar perfil deste celular");
        saveProfileButton.setOnClickListener(v -> updateOwnProfile());
        profileCard.addView(saveProfileButton);

        LinearLayout statusCard = card();
        LinearLayout.LayoutParams statusCardParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        statusCardParams.setMargins(0, dp(14), 0, 0);
        root.addView(statusCard, statusCardParams);

        statusCard.addView(sectionTitle("3. Status básico"));
        statusCard.addView(smallText("Use estes botões só para confirmar conexão. Jobs, failover e controle avançado ficam no painel Discord."));

        heartbeatButton = button("Atualizar status básico");
        heartbeatButton.setOnClickListener(v -> sendHeartbeat());
        statusCard.addView(heartbeatButton);

        tailscaleButton = button("Abrir Tailscale");
        tailscaleButton.setOnClickListener(v -> openTailscale());
        statusCard.addView(tailscaleButton);

        clearButton = button("Esquecer conexão local");
        clearButton.setOnClickListener(v -> confirmClearPairing());
        statusCard.addView(clearButton);

        statusText = new TextView(this);
        statusText.setTextColor(TEXT);
        statusText.setTextSize(14);
        statusText.setPadding(dp(14), dp(14), dp(14), dp(14));
        statusText.setBackgroundColor(CARD);
        LinearLayout.LayoutParams statusParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        statusParams.setMargins(0, dp(14), 0, 0);
        root.addView(statusText, statusParams);

        setContentView(scroll);
    }

    private LinearLayout card() {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(14), dp(14), dp(14), dp(14));
        card.setBackgroundColor(CARD);
        return card;
    }

    private TextView sectionTitle(String value) {
        TextView title = new TextView(this);
        title.setText(value);
        title.setTextColor(TEXT);
        title.setTextSize(17);
        title.setTypeface(null, 1);
        title.setPadding(0, 0, 0, dp(6));
        return title;
    }

    private TextView smallText(String value) {
        TextView text = new TextView(this);
        text.setText(value);
        text.setTextColor(MUTED);
        text.setTextSize(13);
        text.setPadding(0, dp(2), 0, dp(6));
        return text;
    }

    private TextView label(String value) {
        TextView label = new TextView(this);
        label.setText(value);
        label.setTextColor(MUTED);
        label.setTextSize(13);
        label.setPadding(0, dp(10), 0, dp(4));
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
        edit.setPadding(dp(10), dp(8), dp(10), dp(8));
        return edit;
    }

    private Button button(String text) {
        Button button = new Button(this);
        button.setText(text);
        button.setAllCaps(false);
        button.setTextColor(Color.WHITE);
        button.setTextSize(14);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        params.setMargins(0, dp(8), 0, 0);
        button.setLayoutParams(params);
        return button;
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
        serverUrlInput.setText(prefs.getString("server_url", ""));
        pairCodeInput.setText("");
        deviceNameInput.setText(prefs.getString("device_name", defaultDeviceName()));
        String profile = prefs.getString("profile", "midia");
        for (int i = 0; i < profileGroup.getChildCount(); i++) {
            View child = profileGroup.getChildAt(i);
            if (child instanceof RadioButton && profile.equals(String.valueOf(child.getTag()))) {
                ((RadioButton) child).setChecked(true);
                break;
            }
        }
        updateProfileHint(profile);
    }

    private void testServer() {
        String serverUrl = normalizedServerUrl();
        if (serverUrl.isEmpty()) {
            refreshLocalStatus("Informe a URL da VPS antes de testar.");
            return;
        }
        runBusy("Testando VPS...", () -> {
            HttpResult result = request("GET", serverUrl + "/health", null, null);
            double ping = measureTcpPingMs(serverUrl);
            String message = "VPS respondeu HTTP " + result.status;
            if (ping >= 0) {
                message += " · ping TCP " + Math.round(ping) + "ms";
            }
            message += "\n\n" + result.body;
            show(message);
        });
    }

    private void pairWorker() {
        String serverUrl = normalizedServerUrl();
        String code = pairCodeInput.getText().toString().trim();
        String name = deviceNameInput.getText().toString().trim();
        String profile = selectedProfile();
        if (serverUrl.isEmpty() || code.isEmpty() || name.isEmpty()) {
            refreshLocalStatus("Preencha URL da VPS, código CORE e nome do celular.");
            return;
        }
        saveLocalFields(profile);

        runBusy("Pareando este celular com a VPS...", () -> {
            JSONObject payload = basePayload();
            payload.put("code", code);
            payload.put("name", name);
            payload.put("device_name", name);
            payload.put("worker_id", prefs.getString("worker_id", ensureWorkerId()));
            putProfilePayload(payload, profile);
            payload.put("source", "core-worker-apk-companion");
            payload.put("version", APP_VERSION);

            HttpResult result = request("POST", serverUrl + "/core-worker/pair", payload, null);
            if (!result.ok()) {
                show("Falha ao conectar: HTTP " + result.status + "\n\n" + result.body);
                return;
            }
            JSONObject body = new JSONObject(result.body);
            String token = body.optString("token", "");
            String workerId = body.optString("worker_id", prefs.getString("worker_id", ""));
            if (token.isEmpty() || workerId.isEmpty()) {
                show("A VPS respondeu sem token/worker_id.\n\n" + result.body);
                return;
            }
            prefs.edit()
                    .putString("server_url", serverUrl)
                    .putString("device_name", name)
                    .putString("profile", profile)
                    .putString("worker_id", workerId)
                    .putString("worker_token", token)
                    .apply();
            show("Celular conectado com sucesso.\nPerfil: " + profileLabel(profile) + "\nToken salvo localmente no APK.\n\nEnviando status inicial...");
            sendHeartbeatInternal(false);
        });
    }

    private void updateOwnProfile() {
        String profile = selectedProfile();
        saveLocalFields(profile);
        runBusy("Salvando perfil deste celular...", () -> sendHeartbeatInternal(true, "Perfil salvo no painel: " + profileLabel(profile)));
    }

    private void sendHeartbeat() {
        saveLocalFields(selectedProfile());
        runBusy("Atualizando status básico...", () -> sendHeartbeatInternal(true));
    }

    private void sendHeartbeatInternal(boolean showResult) throws Exception {
        sendHeartbeatInternal(showResult, null);
    }

    private void sendHeartbeatInternal(boolean showResult, String successPrefix) throws Exception {
        String serverUrl = prefs.getString("server_url", normalizedServerUrl());
        String token = prefs.getString("worker_token", "");
        String workerId = prefs.getString("worker_id", "");
        if (serverUrl == null || serverUrl.isEmpty() || token.isEmpty() || workerId.isEmpty()) {
            show("Este celular ainda não está conectado. Gere um código no painel workers e toque em Conectar.");
            return;
        }
        String profile = prefs.getString("profile", "midia");
        JSONObject payload = basePayload();
        payload.put("worker_id", workerId);
        payload.put("name", prefs.getString("device_name", defaultDeviceName()));
        putProfilePayload(payload, profile);
        payload.put("version", APP_VERSION);
        payload.put("source", "core-worker-apk-companion");
        HttpResult result = request("POST", serverUrl + "/core-worker/heartbeat", payload, token);
        if (!result.ok()) {
            show("Falha ao atualizar: HTTP " + result.status + "\n\n" + result.body);
            return;
        }
        if (showResult) {
            String message = successPrefix == null ? "Status básico atualizado." : successPrefix;
            message += "\nHTTP " + result.status + "\n\n" + compactResultBody(result.body);
            show(message);
        } else {
            show("Celular conectado e status inicial enviado.\nAgora confira o painel workers no Discord.");
        }
    }

    private void saveLocalFields(String profile) {
        prefs.edit()
                .putString("server_url", normalizedServerUrl())
                .putString("device_name", deviceNameInput.getText().toString().trim())
                .putString("profile", profile)
                .apply();
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

    private JSONObject basePayload() throws Exception {
        String serverUrl = prefs.getString("server_url", normalizedServerUrl());
        JSONObject payload = new JSONObject();
        payload.put("worker_id", prefs.getString("worker_id", ensureWorkerId()));
        payload.put("name", prefs.getString("device_name", defaultDeviceName()));
        payload.put("endpoint", "android-app");
        payload.put("battery", batterySnapshot());
        payload.put("network", networkSnapshot(serverUrl));
        payload.put("status", statusSnapshot());
        payload.put("health", healthSnapshot());
        return payload;
    }

    private JSONObject statusSnapshot() throws Exception {
        JSONObject status = new JSONObject();
        status.put("app", "foreground");
        status.put("apk_companion", true);
        status.put("android_sdk", Build.VERSION.SDK_INT);
        status.put("manufacturer", Build.MANUFACTURER);
        status.put("model", Build.MODEL);
        return status;
    }

    private JSONObject healthSnapshot() throws Exception {
        JSONObject health = new JSONObject();
        health.put("ok", true);
        health.put("apk_version", APP_VERSION);
        health.put("scope", "companion_onboarding_profile_only");
        health.put("note", "APK leve: pareamento, perfil do próprio celular e status básico. Controle pesado fica no Discord/VPS.");
        return health;
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
        networkJson.put("tailscale_hint", isLikelyTailscale(serverUrl) ? "ts app ok" : "n/a");
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
                } catch (Exception ignored) {
                }
            }
            return (System.nanoTime() - start) / 1_000_000.0;
        } catch (Exception ignored) {
            return -1;
        }
    }

    private boolean isLikelyTailscale(String serverUrl) {
        try {
            String host = new URL(serverUrl).getHost();
            return host != null && host.startsWith("100.");
        } catch (Exception ignored) {
            return false;
        }
    }

    private HttpResult request(String method, String url, JSONObject payload, String token) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setRequestMethod(method);
        conn.setConnectTimeout(6000);
        conn.setReadTimeout(9000);
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

    private void openTailscale() {
        try {
            Intent launch = getPackageManager().getLaunchIntentForPackage("com.tailscale.ipn");
            if (launch == null) {
                throw new ActivityNotFoundException("Tailscale não encontrado");
            }
            startActivity(launch);
        } catch (Exception exc) {
            refreshLocalStatus("Não consegui abrir o Tailscale automaticamente. Abra o app Tailscale manualmente e conecte na mesma tailnet da VPS.");
        }
    }

    private void confirmClearPairing() {
        new AlertDialog.Builder(this)
                .setTitle("Esquecer conexão local?")
                .setMessage("Isso remove o token salvo no APK. O registro na VPS não é apagado automaticamente.")
                .setPositiveButton("Esquecer", (dialog, which) -> {
                    prefs.edit()
                            .remove("worker_token")
                            .remove("server_url")
                            .remove("profile")
                            .apply();
                    loadInputs();
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
            } catch (Exception exc) {
                show("Erro: " + exc.getClass().getSimpleName() + " · " + String.valueOf(exc.getMessage()));
            } finally {
                runOnUiThread(() -> setButtonsEnabled(true));
            }
        }).start();
    }

    private void show(String message) {
        runOnUiThread(() -> refreshLocalStatus(message));
    }

    private void setButtonsEnabled(boolean enabled) {
        if (testButton != null) testButton.setEnabled(enabled);
        if (pairButton != null) pairButton.setEnabled(enabled);
        if (saveProfileButton != null) saveProfileButton.setEnabled(enabled);
        if (heartbeatButton != null) heartbeatButton.setEnabled(enabled);
        if (tailscaleButton != null) tailscaleButton.setEnabled(enabled);
        if (clearButton != null) clearButton.setEnabled(enabled);
    }

    private void refreshLocalStatus(String extra) {
        String workerId = prefs.getString("worker_id", ensureWorkerId());
        String token = prefs.getString("worker_token", "");
        String server = prefs.getString("server_url", normalizedServerUrl());
        String profile = prefs.getString("profile", selectedProfileSafe());
        StringBuilder builder = new StringBuilder();
        builder.append("Status deste celular\n");
        builder.append("Conectado: ").append(token == null || token.isEmpty() ? "não" : "sim").append('\n');
        builder.append("Perfil: ").append(profileLabel(profile)).append('\n');
        builder.append("VPS: ").append(server == null || server.isEmpty() ? "não definida" : server).append('\n');
        builder.append("Worker ID: ").append(workerId).append('\n');
        builder.append("Versão APK: ").append(APP_VERSION).append("\n\n");
        builder.append(extra == null ? "" : extra);
        statusText.setText(builder.toString());
    }

    private String normalizedServerUrl() {
        String raw = serverUrlInput == null ? prefs.getString("server_url", "") : serverUrlInput.getText().toString();
        raw = raw == null ? "" : raw.trim();
        while (raw.endsWith("/")) {
            raw = raw.substring(0, raw.length() - 1);
        }
        return raw;
    }

    private String selectedProfile() {
        int id = profileGroup.getCheckedRadioButtonId();
        View selected = id == -1 ? null : findViewById(id);
        if (selected != null && selected.getTag() != null) {
            return String.valueOf(selected.getTag());
        }
        return "midia";
    }

    private String selectedProfileSafe() {
        try {
            return selectedProfile();
        } catch (Exception ignored) {
            return prefs.getString("profile", "midia");
        }
    }

    private String ensureWorkerId() {
        String existing = prefs == null ? "" : prefs.getString("worker_id", "");
        if (existing != null && !existing.trim().isEmpty()) {
            return existing;
        }
        String base = (Build.MANUFACTURER + "-" + Build.MODEL).toLowerCase(Locale.ROOT);
        base = base.replaceAll("[^a-z0-9_.:-]+", "-").replaceAll("^-+|-+$", "");
        if (base.length() < 3) {
            base = "android";
        }
        String id = "apk-" + base + "-" + UUID.randomUUID().toString().substring(0, 8);
        if (prefs != null) {
            prefs.edit().putString("worker_id", id).apply();
        }
        return id;
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
        if ("bedrock".equals(profile)) {
            return new String[]{"phone-worker", "diagnostics", "log-summary", "bedrock", "bedrock-logs", "bedrock-backup"};
        }
        return new String[]{"phone-worker", "diagnostics", "log-summary", "zip-validate", "ffmpeg", "ffprobe", "tts-convert"};
    }

    private String profileLabel(String profile) {
        if ("leve".equals(profile)) return "Leve";
        if ("completo".equals(profile)) return "Completo";
        if ("bedrock".equals(profile)) return "Bedrock";
        return "Mídia";
    }

    private String profileDescription(String profile) {
        if ("leve".equals(profile)) {
            return "Funções: diagnósticos e logs. Bom para celular fraco ou reserva.";
        }
        if ("completo".equals(profile)) {
            return "Funções: mídia, ZIP, TTS, FFmpeg e manutenção. Bom para celular principal.";
        }
        if ("bedrock".equals(profile)) {
            return "Funções futuras de Minecraft Bedrock. Não assume servidor Java.";
        }
        return "Funções: logs, ZIP, FFmpeg, FFprobe e TTS/cache. Perfil recomendado.";
    }

    private void updateProfileHint(String profile) {
        if (profileHintText != null) {
            profileHintText.setText(profileDescription(profile));
        }
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
        String compact = body.replace('\n', ' ').replace('\r', ' ').trim();
        if (compact.length() > 320) {
            return compact.substring(0, 320) + "...";
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
