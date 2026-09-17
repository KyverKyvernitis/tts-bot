package dev.core.worker;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONObject;

import java.util.Locale;
import java.util.UUID;
import java.util.WeakHashMap;

/**
 * Mantém identidades separadas para o celular físico e para o runtime APK.
 *
 * Instalações antigas herdaram do Termux o mesmo worker_id. Durante o bootstrap,
 * isso fazia o heartbeat Android sobrescrever versão/roles/tasks do phone-worker.
 * O runtime APK passa a usar `<worker-id>-apk`, mantendo o token compartilhado
 * apenas para a transição autenticada. Pareamentos criados diretamente pelo APK
 * continuam usando o próprio ID sem sufixo.
 */
final class CoreWorkerRuntimeIdentity {
    // Pairing, clear, migration and heartbeat completion share this lock.
    static final Object LOCK = new Object();
    private static long generation;
    private static final WeakHashMap<SharedPreferences, String> pendingInstallIds = new WeakHashMap<>();
    private static final WeakHashMap<SharedPreferences, Boolean> pendingMigrations = new WeakHashMap<>();
    private static final String PREFS = "core_worker_private";
    private static final int TERMUX_PORT = 8766;
    private static final int APK_BOOTSTRAP_PORT = 8767;

    private CoreWorkerRuntimeIdentity() { }

    static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static String installId(SharedPreferences prefs) {
        synchronized (LOCK) {
            String pending = pendingInstallIds.get(prefs);
            String value = pending == null ? prefs.getString("install_id", "") : pending;
            if (value != null && !value.trim().isEmpty() && pending == null) return value;
            if (value == null || value.trim().isEmpty()) value = UUID.randomUUID().toString();
            pendingInstallIds.put(prefs, value);
            requireCommit(prefs.edit().putString("install_id", value));
            pendingInstallIds.remove(prefs);
            return value;
        }
    }

    static void requireCommit(SharedPreferences.Editor editor) {
        if (!editor.commit()) throw new IllegalStateException("falha ao persistir estado do Core Worker");
    }

    static long generation() {
        synchronized (LOCK) { return generation; }
    }

    static boolean isCurrent(SharedPreferences prefs, long expected, String token, String runtime) {
        synchronized (LOCK) {
            return expected == generation && prefs.getBoolean("agent_enabled", false)
                    && token != null && !token.isEmpty()
                    && token.equals(prefs.getString("worker_token", ""))
                    && runtime.equals(runtimeWorkerId(prefs));
        }
    }

    static void migrate(Context context) {
        if (context == null) return;
        synchronized (LOCK) {
            SharedPreferences prefs = prefs(context);
            String canonical = canonicalWorkerId(prefs);
            if (canonical.isEmpty()) return;
            String runtime = runtimeWorkerId(prefs);
            boolean sharedBootstrap = !runtime.equals(canonical);
            int oldPort = prefs.getInt("direct_http_port", TERMUX_PORT);
            int port = sharedBootstrap && (!prefs.getBoolean("direct_http_port_migrated_v072", false)
                    || oldPort == TERMUX_PORT) ? APK_BOOTSTRAP_PORT : oldPort;
            if (!pendingMigrations.containsKey(prefs) && runtime.equals(prefs.getString("runtime_worker_id", ""))
                    && canonical.equals(prefs.getString("physical_worker_id", ""))
                    && "apk".equals(prefs.getString("runtime_kind", ""))
                    && prefs.getBoolean("bootstrap_shared_worker_identity", false) == sharedBootstrap
                    && prefs.getBoolean("direct_http_port_migrated_v072", false)
                    && prefs.contains("direct_http_port") && oldPort == port
                    && (sharedBootstrap ? canonical.equals(prefs.getString("parent_worker_id", ""))
                                        : !prefs.contains("parent_worker_id"))) return;
            SharedPreferences.Editor editor = prefs.edit()
                    .putString("runtime_worker_id", runtime)
                    .putString("physical_worker_id", canonical)
                    .putString("runtime_kind", "apk")
                    .putBoolean("bootstrap_shared_worker_identity", sharedBootstrap)
                    .putInt("direct_http_port", port)
                    .putBoolean("direct_http_port_migrated_v072", true);
            if (sharedBootstrap) editor.putString("parent_worker_id", canonical);
            else editor.remove("parent_worker_id");
            pendingMigrations.put(prefs, true);
            requireCommit(editor);
            pendingMigrations.remove(prefs);
        }
    }

    static void markChildApkPair(SharedPreferences prefs, SharedPreferences.Editor editor, String parentWorkerId) {
        synchronized (LOCK) {
            String parent = safeId(parentWorkerId);
            if (parent.isEmpty()) throw new IllegalArgumentException("parent vazio");
            commitPair(prefs, editor.putString("pairing_owner", "parent-child")
                    .putString("runtime_worker_id", apkChildId(parent))
                    .putString("physical_worker_id", parent)
                    .putString("parent_worker_id", parent)
                    .putString("runtime_kind", "apk")
                    .putBoolean("bootstrap_shared_worker_identity", true)
                    .putInt("direct_http_port", APK_BOOTSTRAP_PORT)
                    .putBoolean("direct_http_port_migrated_v072", true));
        }
    }

    static void markDedicatedApkPair(SharedPreferences prefs, SharedPreferences.Editor editor, String workerId) {
        synchronized (LOCK) {
            String safe = safeId(workerId);
            if (safe.isEmpty()) throw new IllegalArgumentException("worker vazio");
            commitPair(prefs, editor.putString("pairing_owner", "apk")
                    .putString("runtime_worker_id", safe)
                    .putString("physical_worker_id", safe)
                    .putString("runtime_kind", "apk")
                    .remove("parent_worker_id")
                    .putBoolean("bootstrap_shared_worker_identity", false)
                    .putInt("direct_http_port", TERMUX_PORT)
                    .putBoolean("direct_http_port_migrated_v072", true));
        }
    }

    private static void commitPair(SharedPreferences prefs, SharedPreferences.Editor editor) {
        generation++;
        try {
            requireCommit(editor);
        } catch (RuntimeException error) {
            // SharedPreferences can update memory even if its disk write fails.
            // Never leave that unconfirmed credential usable by the agent.
            clearEditor(prefs.edit()).commit();
            throw error;
        }
    }

    static void clear(SharedPreferences prefs) {
        synchronized (LOCK) {
            generation++;
            requireCommit(clearEditor(prefs.edit()));
        }
    }

    static void stopAgent(SharedPreferences prefs, SharedPreferences.Editor editor) {
        synchronized (LOCK) {
            generation++;
            requireCommit(editor.putBoolean("agent_enabled", false));
        }
    }

    private static SharedPreferences.Editor clearEditor(SharedPreferences.Editor editor) {
        return editor.putBoolean("agent_enabled", false)
                .putBoolean("foreground_runtime_active", false)
                .putBoolean("job_executor_ready", false)
                .remove("worker_token").remove("direct_http_token")
                .remove("server_url").remove("profile")
                .remove("paired_via_local_agent").remove("paired_via_native_apk")
                .remove("auto_enrolled_apk").remove("auto_enrollment_state")
                .remove("auto_enrollment_challenge").remove("auto_enrollment_challenge_created_at")
                .remove("legacy_termux_online").remove("native_worker_id").remove("worker_id")
                .remove("pairing_owner").remove("runtime_worker_id")
                .remove("physical_worker_id").remove("parent_worker_id")
                .remove("runtime_kind").remove("bootstrap_shared_worker_identity")
                .remove("direct_http_effective_port").remove("direct_http_port_migrated_v072");
    }

    static String canonicalWorkerId(Context context) {
        return canonicalWorkerId(prefs(context));
    }

    static String canonicalWorkerId(SharedPreferences prefs) {
        if (prefs == null) return "";
        String value = safeId(prefs.getString("worker_id", ""));
        if (!value.isEmpty()) return value;
        return safeId(prefs.getString("native_worker_id", ""));
    }

    static String runtimeWorkerId(Context context) {
        SharedPreferences prefs = prefs(context);
        migrate(context);
        return runtimeWorkerId(prefs);
    }

    static String runtimeWorkerId(SharedPreferences prefs) {
        if (prefs == null) return "";
        String saved = safeId(prefs.getString("runtime_worker_id", ""));
        if (!saved.isEmpty()) return saved;
        String canonical = canonicalWorkerId(prefs);
        if (canonical.isEmpty()) return "";
        if (isDedicatedApkPair(prefs, canonical)) return canonical;
        return apkChildId(canonical);
    }

    static String parentWorkerId(Context context) {
        migrate(context);
        SharedPreferences prefs = prefs(context);
        String value = safeId(prefs.getString("parent_worker_id", ""));
        if (!value.isEmpty()) return value;
        String canonical = canonicalWorkerId(prefs);
        String runtime = runtimeWorkerId(prefs);
        return !canonical.isEmpty() && !canonical.equals(runtime) ? canonical : "";
    }

    static boolean sharedBootstrapIdentity(Context context) {
        migrate(context);
        SharedPreferences prefs = prefs(context);
        String canonical = canonicalWorkerId(prefs);
        String runtime = runtimeWorkerId(prefs);
        return !canonical.isEmpty() && !runtime.isEmpty() && !canonical.equals(runtime);
    }

    static int directHttpPort(Context context) {
        migrate(context);
        SharedPreferences prefs = prefs(context);
        String canonical = canonicalWorkerId(prefs);
        String runtime = runtimeWorkerId(prefs);
        int fallback = canonical.isEmpty()
                ? APK_BOOTSTRAP_PORT
                : (!runtime.isEmpty() && !canonical.equals(runtime) ? APK_BOOTSTRAP_PORT : TERMUX_PORT);
        int value = prefs.getInt("direct_http_port", fallback);
        return value >= 1024 && value <= 65535 ? value : fallback;
    }

    static int effectiveDirectHttpPort(Context context) {
        SharedPreferences prefs = prefs(context);
        int configured = directHttpPort(context);
        int value = prefs.getInt("direct_http_effective_port", configured);
        return value >= 1024 && value <= 65535 ? value : configured;
    }

    static void putRuntimeFields(Context context, JSONObject payload) throws Exception {
        if (context == null || payload == null) return;
        String runtime = runtimeWorkerId(context);
        String physical = canonicalWorkerId(context);
        String parent = parentWorkerId(context);
        payload.put("worker_id", runtime);
        payload.put("id", runtime);
        payload.put("workerId", runtime);
        payload.put("runtime_kind", "apk");
        payload.put("platform", "android");
        payload.put("physical_worker_id", physical.isEmpty() ? runtime : physical);
        if (!parent.isEmpty()) payload.put("parent_worker_id", parent);
        payload.put("bootstrap_shared_worker_identity", !parent.isEmpty());
    }

    private static boolean isDedicatedApkPair(SharedPreferences prefs, String canonical) {
        String owner = String.valueOf(prefs.getString("pairing_owner", "")).trim().toLowerCase(Locale.ROOT);
        return "apk".equals(owner) || canonical.startsWith("apk-");
    }

    private static String apkChildId(String canonical) {
        String safe = safeId(canonical);
        if (safe.endsWith("-apk")) return safe;
        if (safe.length() > 60) safe = safe.substring(0, 60);
        return safe + "-apk";
    }

    private static String safeId(String value) {
        String clean = value == null ? "" : value.trim().toLowerCase(Locale.ROOT);
        clean = clean.replaceAll("[^a-z0-9_.:-]+", "-").replaceAll("^[-._:]+|[-._:]+$", "");
        return clean.length() <= 64 ? clean : clean.substring(0, 64);
    }
}
