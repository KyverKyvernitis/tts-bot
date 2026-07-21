package dev.core.worker;

import org.json.JSONArray;

import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.Set;

/**
 * Catálogo único dos jobs que o APK executa sem depender de Activity ou Termux.
 *
 * A ordem é estável porque o painel da VPS também usa esta lista para renderizar
 * capacidades e comparar versões do agente.
 */
public final class CoreWorkerJobCatalog {
    private static final String[] SUPPORTED_JOBS = new String[] {
            "apk_ping",
            "apk_status_refresh",
            "apk_upload_app_logs",
            "apk_diagnostic",
            "apk_check_update",
            "apk_test_vps_connection",
            "apk_sync_runtime_state",
            "apk_job_history",
            "apk_device_diagnostic",
            "apk_push_diagnostic",
            "apk_update_diagnostic",
            "apk_runtime_diagnostic",
            "apk_worker_bridge_status",
            "apk_test_notification",
            "apk_repair_local_state",
            "apk_reset_job_history",
            "apk_trim_cache",
            "apk_update_storage_cleanup",
            "apk_sync_profile",
            "apk_sync_profile_now",
            "apk_verify_update_state",
            "apk_native_worker_status",
            "apk_native_boot_status",
            "apk_local_shell_probe",
            "apk_core_linux_native_executor_probe",
            "apk_core_linux_native_executor_test",
            "apk_core_linux_native_runtime_status",
            "apk_core_linux_rootfs_status",
            "apk_core_linux_rootfs_prepare",
            "apk_core_linux_rootfs_validate",
            "apk_core_linux_rootfs_preflight",
            "apk_core_linux_rootfs_clean_staging",
            "apk_core_linux_rootfs_import_status",
            "apk_core_linux_rootfs_import_validate",
            "apk_core_linux_rootfs_import_abort",
            "apk_core_linux_rootfs_real_status",
            "apk_core_linux_rootfs_glibc_preflight",
            "apk_core_linux_runner_status",
            "apk_core_linux_runner_preflight",
            "apk_core_linux_runner_requirements",
            "apk_core_linux_runtime_smoke_test",
            "apk_core_linux_rootfs_smoke_test",
            "apk_core_linux_box64_preflight",
            "apk_core_linux_box64_smoke_test"
    };

    private static final String[] CAPABILITIES = new String[] {
            "apk-native",
            "android-status",
            "native-boot",
            "safe-shell-probe",
            "internal-jobs",
            "background-job-agent",
            "durable-job-results",
            "core-linux-runtime",
            "core-linux-rootfs-manager",
            "core-linux-rootfs-import-v1",
            "core-linux-runner-preflight-v11",
            "core-linux-base-tools-smoke-v12",
            "core-linux-rootfs-proot-smoke-v13.3",
            "core-linux-box64-intake-preflight-v14.2.1",
            "core-linux-box64-glibc-preflight-v15.3.1",
            "core-linux-rootfs-glibc-intake-preflight-v16.1",
            "core-linux-embedded-binaries-intake-v11",
            "core-linux-runtime-v1",
            "android-native-tts-bridge"
    };

    private static final Set<String> SUPPORTED_SET;

    static {
        LinkedHashSet<String> values = new LinkedHashSet<>();
        Collections.addAll(values, SUPPORTED_JOBS);
        SUPPORTED_SET = Collections.unmodifiableSet(values);
    }

    private CoreWorkerJobCatalog() {
    }

    public static JSONArray supportedJobs() {
        JSONArray out = new JSONArray();
        for (String value : SUPPORTED_JOBS) {
            out.put(value);
        }
        return out;
    }

    public static JSONArray capabilities() {
        JSONArray out = new JSONArray();
        for (String value : CAPABILITIES) {
            out.put(value);
        }
        return out;
    }

    public static boolean supports(String type) {
        return type != null && SUPPORTED_SET.contains(type.trim());
    }

    public static int size() {
        return SUPPORTED_JOBS.length;
    }
}
