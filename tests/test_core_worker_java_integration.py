"""Host integration of real dispatchers, builder and Android service code.

Android lifecycle/OS and Chaquopy are controlled boundaries, not device tests.
CORE_WORKER_ANDROID_API_JAR supplies compile-time Android signatures (see handoff).
"""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / "android/core-worker-app/app/src/main/java/dev/core/worker"


@pytest.fixture(scope="session")
def integration_harness(tmp_path_factory):
    java, javac = shutil.which("java"), shutil.which("javac")
    ecj = os.environ.get("CORE_WORKER_ECJ_JAR", "")
    jars = [os.environ.get(key, "") for key in ("CORE_WORKER_JSON_JAR", "CORE_WORKER_ANDROID_API_JAR")]
    if not java or not (javac or Path(ecj).is_file()) or not all(Path(jar).is_file() for jar in jars):
        message = "Integration harness requires Java 17 compiler, CORE_WORKER_JSON_JAR and CORE_WORKER_ANDROID_API_JAR"
        if os.environ.get("CORE_WORKER_REQUIRE_JAVA_TESTS") == "1":
            pytest.fail(message)
        pytest.skip(message)
    output = tmp_path_factory.mktemp("core-worker-integration")
    classes = ["CoreWorkerRuntimeIdentity", "CoreWorkerAutoEnrollment", "CoreWorkerBackgroundIo",
               "CoreWorkerHttpTransport", "CoreWorkerUpdateArtifacts", "CoreWorkerSocketListener",
               "CoreWorkerDirectHttpServer", "LocalNativeTtsHttpServer", "CoreWorkerProcessRunner",
               "CoreWorkerDirectTaskExecutor", "CoreWorkerDirectSupport", "CoreWorkerDirectDataTasks",
               "CoreWorkerDirectMediaTasks", "CoreWorkerDirectSystemTasks", "CoreWorkerDirectTtsTasks",
               "CoreWorkerJobCatalog", "CoreWorkerApkBuildManager", "CoreWorkerApkToolchainFilesystem",
               "CoreWorkerAtomicTextFiles", "CoreLinuxRootfsFilesystem",
               "CoreWorkerRuntimeService", "CoreWorkerUpdateJobService"]
    sources = [JAVA / (name + ".java") for name in classes]
    sources += [ROOT / "tests/java_support" / name for name in (
        "android/content/SharedPreferences.java", "android/util/Base64.java", "android/util/AtomicFile.java")]
    sources += [ROOT / "tests/java_harness/dev/core/worker/FakePreferences.java"]
    sources += sorted((ROOT / "tests/java_integration").rglob("*.java"))
    command = [javac, "--release", "17"] if javac else [java, "-jar", ecj, "-17", "-nowarn"]
    compiled = subprocess.run(command + ["-encoding", "UTF-8", "-cp", os.pathsep.join(jars), "-d", str(output),
                                        *map(str, sources)], capture_output=True, text=True, timeout=90)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    return [java, "-ea", "-cp", os.pathsep.join([str(output), *jars]), "dev.core.worker.IntegrationHarness"]


@pytest.mark.parametrize("scenario", ["direct_catalog", "direct_tts", "direct_invalid", "builder_pin",
                                     "service_stop", "service_clear", "service_lease_commit", "service_poll_rejected",
                                     "update_generations"])
def test_java_integration(integration_harness, scenario):
    result = subprocess.run([*integration_harness, scenario], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
