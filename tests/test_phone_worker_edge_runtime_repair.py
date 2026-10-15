"""Offline checks of Edge repair in the actual Phone Worker interpreter."""
import importlib.metadata
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


PHONE = Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker/phone_worker.py"


class EdgeRuntimeRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="edge-runtime-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("PHONE_WORKER_", "CORE_WORKER_"))}
        environment.update({"CORE_WORKER_PROFILE": "turbo",
                            "PHONE_WORKER_DEPS_STATE_DIR": str(self.root / "deps-state"),
                            "PHONE_WORKER_TURBO_DEPS_INSTALL_MODE": "off",
                            "PHONE_WORKER_HEAVY_PYTHON_DEPS_INSTALL": "false"})
        self.start_patch(patch.dict(os.environ, environment, clear=True))
        spec = importlib.util.spec_from_file_location("phone_edge_runtime_repair", PHONE)
        self.worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.worker)
        self.state = {"installed": False, "returncode": 0,
                      "install_importable": True, "version": "7.3.1"}
        self.threads, self.commands, self.invalidations, self.imports = [], [], [], []
        self.start_patch(patch.object(self.worker, "importlib", SimpleNamespace(
            import_module=self.edge_import, util=importlib.util,
            invalidate_caches=lambda: self.invalidations.append(True))))
        self.start_patch(patch.object(importlib.metadata, "version", side_effect=lambda name:
                                     self.state["version"] if name == "edge-tts" else "2.5.4"))
        owner = self

        class DeferredThread:
            def __init__(self, *, target, name, daemon):
                self.target, self.name, self.daemon = target, name, daemon

            def start(self):
                owner.threads.append(self)

        self.start_patch(patch.object(self.worker, "threading", SimpleNamespace(Thread=DeferredThread)))
        self.start_patch(patch.object(self.worker, "subprocess", SimpleNamespace(run=self.pip)))
        self.start_patch(patch.object(self.worker, "_android_tts_status", return_value={"enabled": False}))
        self.start_patch(patch.object(self.worker, "_teto_status", return_value={
            "ready": True, "enabled": True, "resources": {"ok": True}}))
        self.start_patch(patch.object(self.worker, "_phone_worker_music_bridge_module",
                                     side_effect=AssertionError("Edge repair touched music")))

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def edge_import(self, name):
        self.assertEqual(name, "edge_tts")
        self.imports.append(name)
        if not self.state["installed"]:
            raise ModuleNotFoundError("edge_tts absent in worker interpreter")
        return SimpleNamespace(Communicate=object)

    def pip(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if self.state["returncode"] == 0 and self.state["install_importable"]:
            self.state.update(installed=True, version="7.3.1")
        return SimpleNamespace(returncode=self.state["returncode"])

    def test_missing_edge_repairs_worker_interpreter_and_only_then_advertises(self):
        w = self.worker
        w._CONTROL_PLANE_SNAPSHOT_CACHE["old"] = {"available_engines": ["teto"]}
        before = w._turbo_dependency_snapshot()
        self.assertFalse(before["edge_tts"])
        self.assertNotIn("edge", w._tts_agent_available_engines(before))
        self.assertIn("teto", w._tts_agent_available_engines(before))
        self.assertTrue(before["edge_tts_repair"]["started"])
        self.assertEqual(len(self.threads), 1)
        self.assertFalse(self.commands)  # HTTP/heartbeat is not held by pip.
        self.assertEqual(w._ensure_edge_tts_runtime_dependency()["reason"], "already_running")
        self.assertTrue(self.threads[0].daemon)
        self.assertEqual(self.threads[0].name, "edge-tts-runtime-repair")
        self.threads[0].target()
        command, options = self.commands[0]
        self.assertEqual(command[:3], [w.sys.executable, "-m", "pip"])
        self.assertEqual(command[-1], "edge-tts==7.3.1")
        self.assertIn("--only-binary=:all:", command)
        self.assertGreaterEqual(options["timeout"], 45)
        self.assertFalse(options["check"])
        self.assertEqual(self.invalidations, [True, True])
        self.assertEqual(w._CONTROL_PLANE_SNAPSHOT_CACHE, {})
        self.assertFalse(w._EDGE_TTS_RUNTIME_INSTALL_RUNNING)
        after = w._turbo_dependency_snapshot()
        self.assertTrue(after["edge_tts"])
        self.assertIn("edge", w._tts_agent_available_engines(after))
        self.assertIn("teto", w._tts_agent_available_engines(after))
        self.assertEqual(w._ensure_edge_tts_runtime_dependency()["reason"], "available")
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(len(self.threads), 1)

    def test_explicit_disable_is_respected(self):
        for key, value in [
            ("PHONE_WORKER_AUTO_INSTALL_TTS_DEPS", "false"),
            ("PHONE_WORKER_TURBO_DEPS_INSTALL_MODE", "disabled"),
            ("PHONE_WORKER_SAFE_MODE", "true"),
            ("PHONE_WORKER_TTS_AGENT_ENABLED", "false"),
            ("CORE_WORKER_PROFILE", "basic"),
        ]:
            with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                self.assertFalse(self.worker._ensure_edge_tts_runtime_dependency()["started"])
                self.assertFalse(self.threads)
                self.assertFalse(self.commands)

    def check_failed_repair(self):
        w = self.worker
        self.assertTrue(w._ensure_edge_tts_runtime_dependency()["started"])
        self.threads[0].target()
        self.assertFalse(w._EDGE_TTS_RUNTIME_INSTALL_RUNNING)
        result = w._turbo_dependency_snapshot()
        self.assertFalse(result["edge_tts"])
        self.assertNotIn("edge", w._tts_agent_available_engines(result))
        self.assertEqual(result["edge_tts_repair"]["reason"], "cooldown")
        self.assertEqual(len(self.threads), 1)
        self.assertEqual(len(self.commands), 1)

    def test_pip_failure_keeps_edge_unavailable_and_retries_with_cooldown(self):
        self.state["returncode"] = 1
        self.check_failed_repair()

    def test_import_failure_keeps_edge_unavailable_and_retries_with_cooldown(self):
        self.state["install_importable"] = False
        self.check_failed_repair()

    def test_shared_dependency_validation_uses_the_validated_edge_pin(self):
        self.state["installed"] = True
        self.assertEqual(self.worker._module_import_ok("edge_tts"), (True, ""))
        self.state["version"] = "7.2.8"
        ok, error = self.worker._module_import_ok("edge_tts")
        self.assertFalse(ok)
        self.assertIn("7.3.1", error)

    def test_install_by_supervisor_before_background_run_avoids_duplicate_pip(self):
        self.assertTrue(self.worker._ensure_edge_tts_runtime_dependency()["started"])
        self.state["installed"] = True
        self.threads[0].target()
        self.assertFalse(self.commands)
        self.assertFalse(self.worker._EDGE_TTS_RUNTIME_INSTALL_RUNNING)

    def test_direct_worker_aligns_old_edge_before_importing_or_advertising(self):
        w = self.worker
        self.state.update(installed=True, version="7.2.8")
        before = w._turbo_dependency_snapshot()
        self.assertFalse(before["edge_tts"])
        self.assertNotIn("edge", w._tts_agent_available_engines(before))
        self.assertIn("teto", w._tts_agent_available_engines(before))
        self.assertTrue(before["edge_tts_repair"]["started"])
        self.assertFalse(self.imports)  # Old module is not cached before pip.
        self.threads[0].target()
        self.assertEqual(self.commands[0][0][-1], "edge-tts==7.3.1")
        after = w._turbo_dependency_snapshot()
        self.assertTrue(after["edge_tts"])
        self.assertIn("edge", w._tts_agent_available_engines(after))
        self.assertIn("teto", w._tts_agent_available_engines(after))

    def test_runtime_install_lock_is_shared_with_other_processes(self):
        w = self.worker
        code = """
import fcntl, sys
with open(sys.argv[1], 'a') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(23)
"""
        path = w._dependency_install_state_dir() / "edge-tts.install.lock"
        with w._edge_tts_runtime_install_lock():
            locked = subprocess.run([sys.executable, "-c", code, str(path)], timeout=5)
            self.assertEqual(locked.returncode, 23)
        unlocked = subprocess.run([sys.executable, "-c", code, str(path)], timeout=5)
        self.assertEqual(unlocked.returncode, 0)

    def test_supervisor_cleanup_preserves_wheel_only_edge_repair(self):
        for script_path in ["deploy/termux/phone-worker/start-phone-worker.sh",
                            "cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh"]:
            with self.subTest(script=script_path):
                source = (PHONE.parents[3] / script_path).read_text()
                function = "cleanup_stale_heavy_dependency_builds() {" + source.split(
                    "cleanup_stale_heavy_dependency_builds() {", 1)[1].split("\n}", 1)[0] + "\n}"
                # Exercise the real shell filter without touching host processes.
                shell = r'''
truthy() { return 0; }
heavy_python_deps_enabled() { return 1; }
log() { :; }
sleep() { :; }
kill() { printf 'kill %s %s\n' "$1" "$2"; }
ps() {
  printf '%s\n' \
    'u 101 1 0 ? 00:00 python -m pip install --only-binary=:all: edge-tts==7.3.1' \
    'u 202 1 0 ? 00:00 python pyproject_hooks build_wheel grpcio' \
    'u 303 1 0 ? 00:00 python phone_worker.py --port 8766' \
    'u 404 1 0 ? 00:00 python -m cogs.musica.runtime_telefone.agente.servidor'
}
'''
                result = subprocess.run(["bash", "-c", shell + function + "\ncleanup_stale_heavy_dependency_builds\n"],
                                        text=True, capture_output=True, check=True)
                self.assertEqual(result.stdout.splitlines(), ["kill -9 202"] * 3)


if __name__ == "__main__":
    unittest.main()
