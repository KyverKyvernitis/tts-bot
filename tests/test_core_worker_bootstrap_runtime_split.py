from __future__ import annotations

import json
import time
from pathlib import Path

from utility.commands.workers_registry import CoreWorkersRegistry, _hash_secret


ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / "android/core-worker-app/app/src/main/java/dev/core/worker"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _record(worker_id: str, token: str, *, source: str, platform: str, roles: list[str], tasks: list[str]) -> dict:
    now = time.time()
    return {
        "worker_id": worker_id,
        "name": "teste",
        "enabled": True,
        "token_hash": _hash_secret(token),
        "registered_at": now,
        "updated_at": now,
        "last_heartbeat_at": now,
        "source": source,
        "platform": platform,
        "roles": roles,
        "capabilities": roles,
        "supported_tasks": tasks,
        "status": {},
    }


def test_legacy_apk_collision_is_split_without_counting_two_phones(tmp_path: Path) -> None:
    token = "shared-bootstrap-token"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "version": 1,
        "pairings": {},
        "workers": {
            "phone-localhost-test": _record(
                "phone-localhost-test",
                token,
                source="core-worker-apk-agent-service-v2",
                platform="android",
                roles=["apk-worker", "diagnostics"],
                tasks=["apk_ping"],
            ),
        },
        "jobs": {},
    }), encoding="utf-8")

    registry = CoreWorkersRegistry(registry_path)
    heartbeat = registry.heartbeat({
        "worker_id": "phone-localhost-test",
        "source": "core-worker-apk-agent-service-v2",
        "platform": "android",
        "runtime_kind": "apk",
        "roles": ["apk-worker", "diagnostics"],
        "capabilities": ["apk-worker", "diagnostics"],
        "supported_tasks": ["apk_ping"],
    }, token=token)

    assert heartbeat["worker_id"] == "phone-localhost-test-apk"
    snapshot = registry.snapshot()
    workers = {item["worker_id"]: item for item in snapshot["workers"]}
    assert set(workers) == {"phone-localhost-test", "phone-localhost-test-apk"}
    assert workers["phone-localhost-test"]["runtime_kind"] == "termux"
    assert workers["phone-localhost-test"]["online"] is False
    assert workers["phone-localhost-test-apk"]["runtime_kind"] == "apk"
    assert workers["phone-localhost-test-apk"]["parent_worker_id"] == "phone-localhost-test"
    assert workers["phone-localhost-test-apk"]["online"] is True
    assert snapshot["summary"]["registered"] == 1
    assert snapshot["summary"]["runtime_registered"] == 2

    restored = registry.heartbeat({
        "worker_id": "phone-localhost-test",
        "source": "termux-phone-worker",
        "platform": "android-termux",
        "runtime_kind": "termux",
        "physical_worker_id": "phone-localhost-test",
        "version": "1.10.34",
        "roles": ["phone-worker", "apk-builder"],
        "capabilities": ["phone-worker", "apk-builder"],
        "supported_tasks": ["worker_update", "apk_build_debug", "apk_publish_last"],
    }, token=token)
    assert restored["worker_id"] == "phone-localhost-test"
    assert restored["worker"]["source"] == "termux-phone-worker"


def test_old_apk_can_poll_child_and_submit_result_with_physical_id(tmp_path: Path) -> None:
    token = "shared-bootstrap-token"
    worker_id = "phone-localhost-result"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "version": 1,
        "pairings": {},
        "workers": {
            worker_id: _record(
                worker_id,
                token,
                source="termux-phone-worker",
                platform="android-termux",
                roles=["phone-worker", "apk-builder"],
                tasks=["worker_update", "apk_build_debug"],
            ),
        },
        "jobs": {},
    }), encoding="utf-8")
    registry = CoreWorkersRegistry(registry_path)

    registry.heartbeat({
        "worker_id": worker_id,
        "source": "core-worker-apk-agent-service-v2",
        "platform": "android",
        "roles": ["apk-worker", "diagnostics"],
        "capabilities": ["apk-worker", "diagnostics"],
        "supported_tasks": ["apk_ping"],
    }, token=token)
    child_id = f"{worker_id}-apk"
    created = registry.create_job(
        job_type="apk_ping",
        target_worker_id=child_id,
        ttl_seconds=300,
        lease_seconds=120,
        summary="legacy result bridge",
    )
    job_id = created["job"]["job_id"]

    polled = registry.poll_job({
        "worker_id": worker_id,
        "source": "core-worker-apk-agent-service-v2",
        "platform": "android",
        "roles": ["apk-worker", "diagnostics"],
        "capabilities": ["apk-worker", "diagnostics"],
        "supported_tasks": ["apk_ping"],
    }, token=token)
    assert polled["worker_id"] == child_id
    assert polled["job"]["job_id"] == job_id

    submitted = registry.submit_job_result({
        "worker_id": worker_id,
        "job_id": job_id,
        "status": "succeeded",
        "summary": "ok",
        "result": {"ok": True, "type": "apk_ping"},
    }, token=token)
    assert submitted["ok"] is True
    assert submitted["worker_id"] == child_id


def test_android_runtime_uses_child_identity_and_bootstrap_port() -> None:
    identity = _read(JAVA / "CoreWorkerRuntimeIdentity.java")
    service = _read(JAVA / "CoreWorkerRuntimeService.java")
    direct = _read(JAVA / "CoreWorkerDirectHttpServer.java")
    activity = _read(JAVA / "MainActivity.java")

    assert "APK_BOOTSTRAP_PORT = 8767" in identity
    assert 'return safe + "-apk"' in identity
    assert 'putString("parent_worker_id", canonical)' in identity
    assert "CoreWorkerRuntimeIdentity.migrate(getApplicationContext())" in service
    assert "CoreWorkerRuntimeIdentity.putRuntimeFields(getApplicationContext(), payload)" in service
    assert "CoreWorkerRuntimeIdentity.directHttpPort(context)" in direct
    assert "CoreWorkerRuntimeIdentity.markDedicatedApkPair(prefs, workerId)" in activity
    assert "CoreWorkerRuntimeIdentity.clear(editor)" in activity


def test_automation_routes_apk_trigger_to_termux_bootstrap() -> None:
    automation = _read(ROOT / "scripts/core-worker-automation.py")
    phone_worker = _read(ROOT / "deploy/termux/phone-worker/phone_worker.py")
    assert "def _bootstrap_worker_id_for_runtime" in automation
    assert "runtime APK não recebe worker_update" in automation
    assert "worker_update continua reservado ao Termux bootstrap" in automation
    assert 'PHONE_WORKER_VERSION = "1.10.36"' in phone_worker
    assert '"runtime_kind": "termux"' in phone_worker
    assert '"platform": "android-termux"' in phone_worker


def test_bootstrap_build_stays_on_termux_until_apk_self_builder_is_ready(tmp_path: Path) -> None:
    token = "shared-bootstrap-token"
    worker_id = "phone-localhost-builder"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "version": 1,
        "pairings": {},
        "workers": {
            worker_id: _record(
                worker_id,
                token,
                source="termux-phone-worker",
                platform="android-termux",
                roles=["phone-worker", "apk-builder"],
                tasks=["worker_update", "apk_build_debug", "apk_publish_last"],
            ),
        },
        "jobs": {},
    }), encoding="utf-8")
    registry = CoreWorkersRegistry(registry_path)

    # O APK legado ainda usa o ID físico, mas deve virar runtime filho sem
    # sobrescrever as capacidades do Termux bootstrap.
    registry.heartbeat({
        "worker_id": worker_id,
        "source": "core-worker-apk-agent-service-v2",
        "platform": "android",
        "roles": ["apk-worker", "diagnostics"],
        "capabilities": ["apk-worker", "diagnostics"],
        "supported_tasks": ["apk_ping", "apk_builder_status"],
        "status": {"apk_self_builder": {"ready": False, "publishReady": False}},
    }, token=token)
    child_id = f"{worker_id}-apk"

    created = registry.create_job(
        job_type="apk_build_debug",
        payload={"selfBuilderRequired": True},
        required_capabilities=["apk-builder"],
        ttl_seconds=300,
        lease_seconds=300,
        summary="bootstrap 0.7.2",
    )
    assert created["job"]["preferred_worker_id"] == worker_id

    child_poll = registry.poll_job({
        "worker_id": worker_id,
        "source": "core-worker-apk-agent-service-v2",
        "platform": "android",
        "roles": ["apk-worker", "diagnostics"],
        "capabilities": ["apk-worker", "diagnostics"],
        "supported_tasks": ["apk_ping", "apk_builder_status"],
    }, token=token)
    assert child_poll["worker_id"] == child_id
    assert child_poll["job"] is None

    termux_poll = registry.poll_job({
        "worker_id": worker_id,
        "source": "termux-phone-worker",
        "platform": "android-termux",
        "runtime_kind": "termux",
        "roles": ["phone-worker", "apk-builder"],
        "capabilities": ["phone-worker", "apk-builder"],
        "supported_tasks": ["worker_update", "apk_build_debug", "apk_publish_last"],
    }, token=token)
    assert termux_poll["worker_id"] == worker_id
    assert termux_poll["job"]["type"] == "apk_build_debug"


def test_automation_maps_apk_child_to_physical_termux_runtime() -> None:
    import importlib.util

    path = ROOT / "scripts/core-worker-automation.py"
    spec = importlib.util.spec_from_file_location("core_worker_automation_runtime_split_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    snapshot = {
        "workers": [
            {
                "worker_id": "phone-test",
                "source": "termux-phone-worker",
                "runtime_kind": "termux",
                "roles": ["phone-worker", "apk-builder"],
            },
            {
                "worker_id": "phone-test-apk",
                "source": "core-worker-apk-agent-service-v2",
                "runtime_kind": "apk",
                "parent_worker_id": "phone-test",
                "physical_worker_id": "phone-test",
                "roles": ["apk-worker"],
            },
        ]
    }
    assert module._bootstrap_worker_id_for_runtime(snapshot, "phone-test-apk") == "phone-test"


def test_failed_direct_update_keeps_bootstrap_pending(monkeypatch) -> None:
    import importlib.util

    path = ROOT / "scripts/core-worker-automation.py"
    spec = importlib.util.spec_from_file_location("core_worker_automation_pending_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    snapshot = {
        "workers": [
            {
                "worker_id": "phone-test",
                "name": "teste",
                "enabled": True,
                "online": False,
                "source": "termux-bootstrap-awaiting-heartbeat",
                "runtime_kind": "termux",
                "roles": ["phone-worker", "apk-builder"],
                "capabilities": ["phone-worker", "apk-builder"],
                "supported_tasks": ["worker_update", "apk_build_debug"],
            },
            {
                "worker_id": "phone-test-apk",
                "name": "teste · APK",
                "enabled": True,
                "online": True,
                "source": "core-worker-apk-agent-service-v2",
                "runtime_kind": "apk",
                "parent_worker_id": "phone-test",
                "physical_worker_id": "phone-test",
                "roles": ["apk-worker"],
            },
        ]
    }
    pending = {}
    monkeypatch.setattr(module, "_load_registry_snapshot", lambda: snapshot)
    monkeypatch.setattr(module, "_build_worker_update_payload", lambda: {"version": "1.10.34", "files": []})
    monkeypatch.setattr(module, "_direct_phone_worker_update_if_needed", lambda *a, **k: {
        "ok": False,
        "skipped": True,
        "port_conflict": True,
        "summary": "porta direta responde ao APK",
    })
    monkeypatch.setattr(module, "_load_pending", lambda: dict(pending))
    monkeypatch.setattr(module, "_save_pending", lambda value: (pending.clear(), pending.update(value)))

    result = module.queue_agent_updates(only_worker_id="phone-test-apk")
    assert result["pending"] is True
    assert "agent_update" in pending
    assert result["direct_update"]["port_conflict"] is True


def test_manual_jobs_selected_on_apk_child_route_to_termux_bootstrap(tmp_path: Path) -> None:
    token = "shared-bootstrap-token"
    parent_id = "phone-manual-route"
    child_id = f"{parent_id}-apk"
    now = time.time()
    parent = _record(
        parent_id,
        token,
        source="termux-phone-worker",
        platform="android-termux",
        roles=["phone-worker", "apk-builder"],
        tasks=["worker_update", "apk_build_debug", "apk_publish_last"],
    )
    child = _record(
        child_id,
        token,
        source="core-worker-apk-agent-service-v2",
        platform="android",
        roles=["apk-worker", "diagnostics"],
        tasks=["apk_builder_status"],
    )
    child.update({
        "runtime_kind": "apk",
        "parent_worker_id": parent_id,
        "physical_worker_id": parent_id,
        "status": {"apk_self_builder": {"ready": False, "publishReady": False}},
        "updated_at": now,
        "last_heartbeat_at": now,
    })
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "version": 1,
        "pairings": {},
        "workers": {parent_id: parent, child_id: child},
        "jobs": {},
    }), encoding="utf-8")
    registry = CoreWorkersRegistry(registry_path)

    update = registry.create_job(
        job_type="worker_update",
        target_worker_id=child_id,
        required_capabilities=["phone-worker"],
        ttl_seconds=300,
        lease_seconds=120,
    )
    assert update["job"]["target_worker_id"] == parent_id

    build = registry.create_job(
        job_type="apk_build_debug",
        target_worker_id=child_id,
        required_capabilities=["apk-builder"],
        payload={"selfBuilderRequired": True},
        ttl_seconds=300,
        lease_seconds=300,
    )
    assert build["job"]["target_worker_id"] == parent_id


def test_shared_runtime_switches_future_builds_to_apk_after_real_preflight(tmp_path: Path) -> None:
    token = "shared-bootstrap-token"
    parent_id = "phone-ready-switch"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "version": 1,
        "pairings": {},
        "workers": {
            parent_id: _record(
                parent_id,
                token,
                source="termux-phone-worker",
                platform="android-termux",
                roles=["phone-worker", "apk-builder"],
                tasks=["worker_update", "apk_build_debug", "apk_publish_last"],
            ),
        },
        "jobs": {},
    }), encoding="utf-8")
    registry = CoreWorkersRegistry(registry_path)
    child_id = f"{parent_id}-apk"

    heartbeat = registry.heartbeat({
        "worker_id": child_id,
        "parent_worker_id": parent_id,
        "physical_worker_id": parent_id,
        "source": "core-worker-apk-agent-service-v2",
        "platform": "android",
        "runtime_kind": "apk",
        "roles": ["apk-worker", "apk-builder", "apk-self-builder"],
        "capabilities": ["apk-worker", "apk-builder", "apk-self-builder", "apk-publisher"],
        "supported_tasks": ["apk_builder_status", "apk_build_debug", "apk_publish_last"],
        "status": {"apk_self_builder": {"ready": True, "publishReady": True}},
    }, token=token)
    assert heartbeat["worker_id"] == child_id

    created = registry.create_job(
        job_type="apk_build_debug",
        payload={"selfBuilderRequired": True},
        required_capabilities=["apk-builder"],
        ttl_seconds=300,
        lease_seconds=300,
    )
    assert created["job"]["preferred_worker_id"] == child_id
