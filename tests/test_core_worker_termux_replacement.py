from __future__ import annotations

import re
from pathlib import Path

from utility.commands.workers_registry import CORE_WORKER_JOB_TYPES


ROOT = Path(__file__).resolve().parents[1]
ANDROID = ROOT / "android/core-worker-app"
JAVA = ANDROID / "app/src/main/java/dev/core/worker"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def java_array(source: str, name: str) -> list[str]:
    match = re.search(rf"{re.escape(name)}\s*=\s*new String\[\]\s*\{{(.*?)\}};", source, re.S)
    assert match, f"array {name} not found"
    return re.findall(r'"([a-z0-9_]+)"', match.group(1))


def all_android_java() -> str:
    return "\n".join(read(path) for path in sorted(JAVA.glob("*.java")))


def test_version_marks_the_termux_replacement_release() -> None:
    gradle = read(ANDROID / "app/build.gradle")
    assert "versionCode 118" in gradle
    assert 'versionName "0.7.0"' in gradle
    assert read(ANDROID / "README.md").startswith("# Core Worker 0.7.0 — runtime móvel sem Termux")


def test_android_runtime_has_no_legacy_termux_protocol_or_package_dependency() -> None:
    source = all_android_java()
    for forbidden in (
        "/core-worker/app/jobs/fetch",
        "/core-worker/app/jobs/result",
        "/core-worker/app/heartbeat",
        "127.0.0.1:8766/local/",
        'getLaunchIntentForPackage("com.termux")',
        'getPackageInfo("com.termux")',
    ):
        assert forbidden not in source
    manifest = read(ANDROID / "app/src/main/AndroidManifest.xml")
    assert 'android:name="com.termux"' not in manifest
    assert 'android:name="com.termux.api"' not in manifest
    assert 'android:name="com.termux.boot"' not in manifest


def test_direct_http_api_is_authenticated_and_compatible() -> None:
    server = read(JAVA / "CoreWorkerDirectHttpServer.java")
    for route in ("/health", "/status", "/task", "/tts-agent/health", "/tts-agent/synthesize.raw"):
        assert f'"{route}"' in server
    assert 'new InetSocketAddress("0.0.0.0", port)' in server
    assert "constantTimeEquals" in server
    assert 'headers.get("authorization")' in server
    assert 'safeHeader(headers, "x-phone-worker-token")' in server
    assert 'safeHeader(headers, "x-core-worker-token")' in server
    assert 'return json(401' in server
    assert '"/shell"' not in server
    assert '"/command"' not in server
    assert '"X-Core-Worker-Cache-Hit"' in server
    assert '"X-Core-Worker-Android-Synth-Ms"' in server


def test_direct_executor_has_allowlist_without_free_shell() -> None:
    executor = read(JAVA / "CoreWorkerDirectTaskExecutor.java")
    assert "static boolean supports(String rawTask)" in executor
    assert "ProcessBuilder" in executor  # somente FFmpeg/FFprobe privados e argv validado
    assert 'new String[] {"/system/bin/sh"' not in executor
    assert "Runtime.getRuntime().exec" not in executor
    assert "isSafeFfmpegArg" in executor
    assert "caminho ZIP suspeito" in executor
    assert "entrada grande demais" in executor
    assert '"emoji_recolor"' in executor
    assert '"tts_synthesize_piper"' in executor
    assert "BitmapFactory.decodeByteArray" in executor


def test_java_and_python_job_catalogs_match() -> None:
    catalog = read(JAVA / "CoreWorkerJobCatalog.java")
    internal = java_array(catalog, "APK_JOBS")
    direct = java_array(catalog, "DIRECT_REGISTRY_TASKS")
    remote = internal + direct
    assert len(internal) == 44
    assert len(direct) == 38
    assert len(remote) == len(set(remote))
    assert len(remote) <= 96
    assert set(remote).issubset(CORE_WORKER_JOB_TYPES)


def test_runtime_uses_authenticated_registry_and_durable_results() -> None:
    service = read(JAVA / "CoreWorkerRuntimeService.java")
    assert 'serverUrl + "/core-worker/jobs/poll"' in service
    assert 'serverUrl + "/core-worker/jobs/result"' in service
    assert 'serverUrl + "/core-worker/heartbeat"' in service
    assert 'payload.put("worker_id"' in service
    assert 'payload.put("job_id"' in service
    assert 'payload.put("status", ok ? "succeeded" : "failed")' in service
    assert 'payload.put("result"' in service
    assert "persistOutbox" in service
    assert "normalizeStoredEnvelope" in service
    assert 'status.put("termux_replaced", true)' in service


def test_direct_http_secret_is_delivered_only_after_authorized_apk_calls() -> None:
    webserver = read(ROOT / "webserver.py")
    assert "def _core_worker_apk_http_token" in webserver
    assert "def _core_worker_is_apk_payload" in webserver
    assert "if status != 200 or not isinstance(body, dict) or not body.get(\"ok\")" in webserver
    assert 'body["direct_http_token"] = token' in webserver
    assert webserver.count("_attach_core_worker_apk_http_token(status, body, payload)") == 2
    activity = read(JAVA / "MainActivity.java")
    assert '.putString("direct_http_token"' in activity
    assert '.remove("direct_http_token")' in activity


def test_vps_does_not_build_apk_in_replacement_mode() -> None:
    workers = read(ROOT / "utility/commands/workers.py")
    assert '"apk_build_debug", "apk_publish_last"' in workers
    assert "build/publicação de APK não fazem parte do worker móvel" in workers
    assert '"worker_update", "apk_build_debug", "apk_publish_last", "boot_repair"' in workers
    assert 'values: set[str] = {"apk-worker"}' in workers
    assert 'selected.add("apk-worker")' in workers
    assert 'self.selected_features.add("apk-worker")' in workers
    assert '"builder": "Builder"' not in workers


def test_config_routes_mobile_worker_to_apk_and_keeps_music_local() -> None:
    config = read(ROOT / "config.py")
    assert 'CORE_WORKER_APK_REPLACES_TERMUX = _parse_bool' in config
    assert 'MUSIC_BACKEND = "local" if CORE_WORKER_APK_REPLACES_TERMUX' in config
    assert 'MUSIC_AGENT_ENABLED = (not CORE_WORKER_APK_REPLACES_TERMUX)' in config
    assert 'MUSIC_WORKER_ONLY_ENABLED = (not CORE_WORKER_APK_REPLACES_TERMUX)' in config
    assert 'WORKER_VOICE_AGENT_ENABLED = (not CORE_WORKER_APK_REPLACES_TERMUX)' in config
    assert 'CORE_WORKER_APK_REPLACES_TERMUX and bool(PHONE_WORKER_HOST and PHONE_WORKER_TOKEN)' in config


def test_rootfs_and_bedrock_use_strict_real_gates() -> None:
    runner = read(JAVA / "CoreLinuxRunnerPreflightManager.java")
    bedrock = read(JAVA / "CoreWorkerBedrockService.java")
    runtime = read(JAVA / "CoreWorkerRuntimeService.java")
    assert 'new File(rootfsDir, "bin/sh").isFile()' in runner
    assert 'new File(rootfsDir, "lib/ld-linux-aarch64.so.1").isFile()' in runner
    assert 'lib/aarch64-linux-gnu/libc.so.6' in runner
    assert 'boolean strictFailure' in runner
    assert 'boolean bedrockRequirementsReady = runnerBaseRequirementsReady && box64Ready && bedrockServerReady && propertiesReady && eulaReady' in runner
    assert 'private static final boolean BEDROCK_RUNTIME_ISOLATED = false' in bedrock
    assert 'if (!p.eulaAccepted) p.blockers.put' in bedrock
    assert 'runnerPreflight.optBoolean("runnerExecutionAllowed", false)' in bedrock
    assert 'status.put("bedrock_start_allowed", coreLinux.optBoolean("bedrockStartAllowed", false))' in runtime


def test_direct_executor_preserves_assist_payload_contracts() -> None:
    executor = read(JAVA / "CoreWorkerDirectTaskExecutor.java")
    assert 'body.optDouble("timeout_seconds", 3.0)' in executor
    assert '.put("results", results)' in executor
    assert '.put("url", item.optString("target", target))' in executor
    assert '.put("files", results)' in executor
    assert '.put("total_bytes", total)' in executor
    assert 'private JSONObject maintenancePlan(JSONObject body)' in executor
    assert 'body.optJSONArray("entries")' in executor
    assert '.put("estimated_reclaimable", reclaimable)' in executor
    assert '.put("old_temp_candidates", jsonArray(oldTemp, 80))' in executor
    assert '.put("old_log_candidates", jsonArray(oldLogs, 80))' in executor


def test_welcome_emoji_offload_uses_the_apk_direct_api() -> None:
    source = read(ROOT / "cogs/welcome/core/media_mixin.py")
    assert 'CORE_WORKER_APK_REPLACES_TERMUX' in source
    assert '"task": "emoji_recolor"' in source
    assert 'replacement_enabled and bool(host and token)' in source


def test_legacy_automation_cannot_build_or_update_the_apk_worker() -> None:
    automation = read(ROOT / "scripts/core-worker-automation.py")
    webserver = read(ROOT / "webserver.py")
    assert 'if _env_bool("CORE_WORKER_APK_REPLACES_TERMUX", True):' in automation
    assert '"skipped": "external_build_required"' in automation
    assert 'pending.pop("apk_build", None)' in automation
    assert '"skipped": "termux_replaced"' in automation
    assert 'if _env_bool_web("CORE_WORKER_APK_REPLACES_TERMUX", True):' in webserver
