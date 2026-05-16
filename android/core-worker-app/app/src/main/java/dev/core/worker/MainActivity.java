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
    private static final String APP_VERSION = "0.3.1";
    private static final String LOCAL_AGENT_STATUS_URL = "http://127.0.0.1:8766/local/status";
    private static final String LOCAL_AGENT_PROFILE_URL = "http://127.0.0.1:8766/local/profile";
    private static final String LOCAL_AGENT_PAIR_URL = "http://127.0.0.1:8766/local/pair";
    private static final String LOCAL_AGENT_HEARTBEAT_URL = "http://127.0.0.1:8766/local/heartbeat";
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
    private TextView localAgentText;
    private Button testButton;
    private Button pairButton;
    private Button saveProfileButton;
    private Button heartbeatButton;
    private Button localAgentButton;
    private Button termuxButton;
    private Button tailscaleButton;
    private Button clearButton;
    private volatile boolean localAgentOnline = false;
    private volatile String localAgentVersion = "";
    private volatile String localAgentProfile = "";
    private volatile String localAgentWorkerId = "";
    private volatile String localAgentMessage = "ainda não verificado";
    private volatile String vpsState = "não testada";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        ensureWorkerId();
        buildUi();
        loadInputs();
        refreshLocalStatus("Pronto. Conecte a rede, gere um código no Discord e toque em Conectar.");
        checkLocalAgent(false);
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
        subtitle.setText("APK privado e leve. Ele facilita conectar este celular, escolher o perfil e conversar com o worker local do Termux. O controle pesado continua no Discord/VPS.");
        subtitle.setTextColor(MUTED);
        subtitle.setTextSize(14);
        subtitle.setPadding(0, dp(8), 0, dp(14));
        root.addView(subtitle);

        LinearLayout connectCard = card();
        root.addView(connectCard);
        connectCard.addView(sectionTitle("1. Conectar este celular"));
        connectCard.addView(smallText("Gere um código no painel workers do Discord e cole aqui. O APK vai passar o código para o worker local do Termux, sem criar registro separado."));

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

        pairButton = button("Conectar este worker local");
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

        localAgentText = smallText("Worker local: ainda não verificado.");
        statusCard.addView(localAgentText);

        heartbeatButton = button("Atualizar status básico");
        heartbeatButton.setOnClickListener(v -> sendHeartbeat());
        statusCard.addView(heartbeatButton);

        localAgentButton = button("Verificar worker local");
        localAgentButton.setOnClickListener(v -> checkLocalAgent(true));
        statusCard.addView(localAgentButton);

        termuxButton = button("Abrir Termux");
        termuxButton.setOnClickListener(v -> openTermux());
        statusCard.addView(termuxButton);

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
            vpsState = result.ok() ? "ok" : "falha HTTP " + result.status;
            double ping = measureTcpPingMs(serverUrl);
            String message = result.ok() ? "VPS online" : "VPS respondeu HTTP " + result.status;
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
        String code = pairCodeInput.getText().toString().trim();
        String name = deviceNameInput.getText().toString().trim();
        String profile = selectedProfile();
        if (serverUrl.isEmpty() || code.isEmpty() || name.isEmpty()) {
            refreshLocalStatus("Preencha URL da VPS, código CORE e nome do celular.");
            return;
        }
        saveLocalFields(profile);

        runBusy("Pareando o worker local com a VPS...", () -> {
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
            vpsState = "ok";
            show("Worker local conectado com sucesso.\nPerfil: " + profileLabel(profile) + "\nRegistro usado: " + emptyFallback(workerId, "worker local") + "\n\nO APK não criou worker separado; quem envia heartbeat e executa jobs é o Termux worker.");
        });
    }

    private void updateOwnProfile() {
        String profile = selectedProfile();
        saveLocalFields(profile);
        runBusy("Salvando perfil deste celular...", () -> {
            boolean localSynced = syncProfileToLocalAgent(profile);
            String prefix = localSynced
                    ? "Perfil salvo no APK e enviado ao worker local: " + profileLabel(profile)
                    : "Perfil salvo no APK. Worker local offline; abra o Termux e inicie o worker.";
            if (hasPairing()) {
                sendHeartbeatInternal(true, prefix);
            } else {
                show(prefix + "\n\nEste celular ainda não está pareado com a VPS.");
            }
        });
    }

    private void sendHeartbeat() {
        saveLocalFields(selectedProfile());
        runBusy("Atualizando status básico...", () -> {
            updateLocalAgentStatus(false);
            sendHeartbeatInternal(true);
        });
    }

    private void sendHeartbeatInternal(boolean showResult) throws Exception {
        sendHeartbeatInternal(showResult, null);
    }

    private void sendHeartbeatInternal(boolean showResult, String successPrefix) throws Exception {
        if (!updateLocalAgentStatus(true)) {
            show("Worker local offline. Abra o Termux e inicie o phone-worker.\n\nO APK não envia heartbeat próprio para não criar registro duplicado.");
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
            String message = successPrefix == null ? "Status básico solicitado ao worker local." : successPrefix;
            message += synced ? "\nVPS recebeu heartbeat do Termux worker." : "\nWorker local respondeu, mas ainda não confirmou heartbeat na VPS.";
            show(message);
        } else {
            show("Worker local pareado. O Termux worker envia heartbeat e executa jobs.\nAgora confira o painel workers no Discord.");
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
        status.put("local_agent_online", localAgentOnline);
        if (localAgentOnline) {
            status.put("local_agent_version", localAgentVersion);
            status.put("local_agent_profile", localAgentProfile);
        }
        return status;
    }

    private JSONObject healthSnapshot() throws Exception {
        JSONObject health = new JSONObject();
        health.put("ok", true);
        health.put("apk_version", APP_VERSION);
        health.put("scope", "companion_local_agent_profile_only");
        health.put("local_agent_online", localAgentOnline);
        health.put("note", "APK leve: pareamento, perfil do próprio celular, status básico e integração local com Termux. Controle pesado fica no Discord/VPS.");
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
        runBusy(userVisible ? "Verificando worker local..." : "Verificando worker local...", () -> {
            boolean ok = updateLocalAgentStatus(true);
            if (userVisible) {
                if (ok) {
                    show("Worker local online.\nVersão: " + emptyFallback(localAgentVersion, "desconhecida") + "\nPerfil: " + emptyFallback(localAgentProfile, "não informado"));
                } else {
                    show("Worker local offline. Abra o Termux e inicie o phone-worker.");
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
        } catch (Exception exc) {
            localAgentOnline = false;
            localAgentVersion = "";
            localAgentProfile = "";
            localAgentWorkerId = "";
            localAgentMessage = "offline";
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
            return true;
        } catch (Exception exc) {
            localAgentOnline = false;
            localAgentVersion = "";
            localAgentProfile = "";
            localAgentMessage = "offline ao salvar perfil";
            showLocalAgentText();
            return false;
        }
    }

    private void applyLocalAgentStatus(JSONObject body) {
        localAgentOnline = body.optBoolean("ok", true);
        localAgentVersion = body.optString("version", "");
        localAgentProfile = body.optString("profile_label", body.optString("profile", ""));
        localAgentWorkerId = body.optString("worker_id", localAgentWorkerId);
        if (localAgentOnline) {
            String jobs = body.optBoolean("jobs_configured", false) ? "jobs ok" : "jobs pendentes";
            String vps = body.optBoolean("vps_configured", false) ? "VPS ok" : "VPS pendente";
            localAgentMessage = "online · " + vps + " · " + jobs;
        } else {
            localAgentMessage = "offline";
        }
    }

    private void showLocalAgentText() {
        runOnUiThread(() -> {
            if (localAgentText != null) {
                localAgentText.setText(localAgentLine());
            }
            refreshLocalStatus(null);
        });
    }

    private String localAgentLine() {
        if (!localAgentOnline) {
            return "Worker local: offline. Abra o Termux e inicie o phone-worker.";
        }
        String version = emptyFallback(localAgentVersion, "versão ?");
        String profile = emptyFallback(localAgentProfile, "perfil ?");
        return "Worker local: online · " + version + " · " + profile + " · " + localAgentMessage;
    }

    private boolean hasPairing() {
        boolean pairedViaLocal = prefs.getBoolean("paired_via_local_agent", false);
        String serverUrl = prefs.getString("server_url", "");
        String workerId = prefs.getString("worker_id", "");
        return pairedViaLocal && serverUrl != null && !serverUrl.isEmpty() && workerId != null && !workerId.isEmpty();
    }

    private void openTermux() {
        try {
            Intent launch = getPackageManager().getLaunchIntentForPackage("com.termux");
            if (launch == null) {
                throw new ActivityNotFoundException("Termux não encontrado");
            }
            startActivity(launch);
        } catch (Exception exc) {
            refreshLocalStatus("Não consegui abrir o Termux automaticamente. Abra o Termux manualmente e rode: ~/phone-worker/start-phone-worker.sh");
        }
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
                .setMessage("Isso remove a conexão salva no APK. O token real fica no Termux worker; o registro na VPS não é apagado automaticamente.")
                .setPositiveButton("Esquecer", (dialog, which) -> {
                    prefs.edit()
                            .remove("worker_token")
                            .remove("server_url")
                            .remove("profile")
                            .remove("paired_via_local_agent")
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
        if (localAgentButton != null) localAgentButton.setEnabled(enabled);
        if (termuxButton != null) termuxButton.setEnabled(enabled);
        if (tailscaleButton != null) tailscaleButton.setEnabled(enabled);
        if (clearButton != null) clearButton.setEnabled(enabled);
    }

    private void refreshLocalStatus(String extra) {
        String workerId = localAgentWorkerId != null && !localAgentWorkerId.trim().isEmpty() ? localAgentWorkerId : prefs.getString("worker_id", ensureWorkerId());
        boolean paired = hasPairing();
        String server = prefs.getString("server_url", normalizedServerUrl());
        String profile = prefs.getString("profile", selectedProfileSafe());
        StringBuilder builder = new StringBuilder();
        builder.append("Checklist rápido\n");
        builder.append(checkLine("Rede/Tailscale", networkChecklistLabel(server))).append('\n');
        builder.append(checkLine("VPS", vpsChecklistLabel(server))).append('\n');
        builder.append(checkLine("Worker local", localAgentOnline ? "ok" : localAgentMessage)).append('\n');
        builder.append(checkLine("Pareamento", paired ? "ok" : "pendente")).append("\n\n");
        builder.append("Status deste celular\n");
        builder.append("Conectado: ").append(paired ? "sim" : "não").append('\n');
        builder.append("Perfil: ").append(profileLabel(profile)).append('\n');
        builder.append("VPS: ").append(server == null || server.isEmpty() ? "não definida" : server).append('\n');
        builder.append("Worker ID: ").append(workerId).append('\n');
        if (prefs.getBoolean("paired_via_local_agent", false)) {
            builder.append("Modo: APK conectado ao Termux worker local\n");
        }
        builder.append("Versão APK: ").append(APP_VERSION).append('\n');
        builder.append("Agent local: ").append(localAgentOnline ? emptyFallback(localAgentVersion, "online") : "offline").append("\n\n");
        if (extra != null && !extra.trim().isEmpty()) {
            builder.append(extra);
        }
        statusText.setText(builder.toString());
        if (localAgentText != null) {
            localAgentText.setText(localAgentLine());
        }
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
        } catch (Exception ignored) {
            return "desconhecida";
        }
    }

    private String emptyFallback(String value, String fallback) {
        return value == null || value.trim().isEmpty() ? fallback : value.trim();
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
        } catch (Exception ignored) {
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
