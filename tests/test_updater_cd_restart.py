"""Exercise the updater's real impact and restart branches without services.

Only OS/service, logging, timing and health boundaries are replaced. The Bash
classification, fast-reload decision, deploy branch and restart budget run from
the production sources, including the exact paths shipped in the v9.5 archive.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "updater" / "core"
V95_PATHS = (
    "osaka_cd/lyrics.py",
    "osaka_cd/render.py",
    "docs/cd-v9.5-frase.md",
    "tests/test_cd_phrase_length.py",
    "tests/test_cd_adaptive_lyrics.py",
    "tests/test_cd_message_harmony_mode.py",
    "tests/test_cd_arrangement_audio_contract.py",
    "tests/test_cd_routing_and_persistence.py",
)


def _function(source: str, name: str) -> str:
    pattern = re.compile(r"^" + re.escape(name) + r"\(\) \{\n", re.MULTILINE)
    match = pattern.search(source)
    if match is None:
        raise AssertionError(f"Production Bash function not found: {name}")
    following = re.search(r"^[A-Za-z_][A-Za-z_0-9]*\(\) \{\n",
                          source[match.end():], re.MULTILINE)
    end = match.end() + following.start() if following else len(source)
    return source[match.start():end]


@unittest.skipUnless(shutil.which("bash"), "Bash is required for the updater")
class CDUpdaterRestartTests(unittest.TestCase):
    def _run(self, paths, *, historical=False, repeat_restart=False):
        changes = (CORE / "mudancas.sh").read_text(encoding="utf-8")
        classify = _function(changes, "classify_changed_files")
        if historical:
            # The v9.5 ZIP did not change this classifier. Its installed version
            # lacked all three imported-library patterns now added by the fix.
            for pattern in ("|osaka_cd/*.py", "|updater/api_cd.py", "|updater/__init__.py"):
                self.assertIn(pattern, classify)
                classify = classify.replace(pattern, "")
        source = classify + _function(changes, "fast_reload_modules_for_changed_files")
        application = (CORE / "aplicacao.sh").read_text(encoding="utf-8")
        source += _function(application, "restart_bot_service_once")
        source += _function(application, "deploy_bot")
        script = source + r'''
logger() { :; }
systemctl() { printf 'systemctl %s\n' "$*" >> "$CD_TEST_TRACE"; }
service_restart_count() { printf '0\n'; }
update_now_ms() { printf '1000\n'; }
append_update_timing_ms() { :; }
bot_health_profile_for_changed_files() { printf 'standard\n'; }
verify_bot_after_restart() { printf 'verify %s\n' "$*" >> "$CD_TEST_TRACE"; }
refresh_bot_health_status() { printf 'refresh\n' >> "$CD_TEST_TRACE"; }
has_real_warning_text() { return 1; }
cogs_have_failures() { return 1; }

CHANGED_FILES_RAW="$CD_TEST_PATHS"
ROLLBACK_IN_PROGRESS=0
ROLLBACK_CONTROL_MODE=0
LOCAL_CANDIDATE_MODE=0
REMOTE_CANDIDATE_MODE=0
BOT_RESTARTS_DEPLOY=0
BOT_RESTARTS_ROLLBACK=0
BOT_WARNINGS_STATUS=''
BOT_COGS_STATUS=''
BOT_HEALTH_DETAIL_STATUS=''
FAST_RELOAD_STATUS=''
UPDATE_HAS_WARNINGS=0
LOG_TAG=cd-updater-test
SERVICE=tts-bot
classify_changed_files
printf 'BOT=%s COMMANDS=%s PHONE_SYNC=%s CORE_WORKER=%s REQUIREMENTS=%s\n' \
  "$BOT_CHANGED" "$APP_COMMANDS_MAY_HAVE_CHANGED" "$PHONE_WORKER_SYNC_REQUIRED" \
  "$CORE_WORKER_AUTOMATION_REQUIRED" "$REQUIREMENTS_CHANGED"
deploy_bot
SECOND_RC=0
if [[ "$CD_TEST_REPEAT" == 1 ]]; then
  set +e
  restart_bot_service_once
  SECOND_RC=$?
  set -e
fi
printf 'RESTARTS=%s SECOND_RC=%s\n' "$BOT_RESTARTS_DEPLOY" "$SECOND_RC"
'''
        with tempfile.TemporaryDirectory(prefix="cd-updater-restart-") as folder:
            trace = Path(folder) / "boundary.log"
            environment = dict(os.environ, CD_TEST_PATHS="\n".join(paths),
                               CD_TEST_TRACE=str(trace), CD_TEST_REPEAT=str(int(repeat_restart)),
                               HOT_RELOAD_ALLOW="", HOT_RELOAD_DENY="music,dashboard_sync,terminal_cmd")
            result = subprocess.run(["bash", "-eu", "-o", "pipefail", "-c", script],
                                    cwd=ROOT, env=environment, capture_output=True, text=True,
                                    check=True, timeout=10)
            values = {key: int(value) for token in result.stdout.split()
                      for key, value in [token.split("=", 1)]}
            calls = trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []
            return values, calls

    def test_exact_v95_paths_previously_skipped_restart_and_now_restart_once(self):
        previous, old_calls = self._run(V95_PATHS, historical=True)
        self.assertEqual(previous["BOT"], 0)
        self.assertEqual(previous["RESTARTS"], 0)
        self.assertEqual(old_calls, ["refresh"])

        current, calls = self._run(V95_PATHS)
        self.assertEqual(current["BOT"], 1)
        self.assertEqual(current["RESTARTS"], 1)
        self.assertEqual(calls[:2], ["systemctl reset-failed tts-bot", "systemctl restart tts-bot"])
        self.assertEqual(len([line for line in calls if line.startswith("verify ")]), 1)
        self.assertNotIn("refresh", calls)

    def test_nested_cd_phonetics_and_api_modules_reload_imported_code(self):
        for path in ("osaka_cd/lyrics.py", "osaka_cd/render.py",
                     "osaka_cd/phonetics/phonemizer.py", "osaka_cd/phonetics/ptbr_g2p.py",
                     "updater/api_cd.py", "updater/__init__.py"):
            with self.subTest(path=path):
                values, calls = self._run([path])
                self.assertEqual(values["BOT"], 1)
                self.assertEqual(calls.count("systemctl restart tts-bot"), 1)

    def test_cd_code_does_not_request_command_sync_or_phone_worker_update(self):
        values, _ = self._run(["osaka_cd/lyrics.py", "osaka_cd/render.py", "updater/api_cd.py"])
        self.assertEqual({key: values[key] for key in
                          ("COMMANDS", "PHONE_SYNC", "CORE_WORKER", "REQUIREMENTS")},
                         {"COMMANDS": 0, "PHONE_SYNC": 0, "CORE_WORKER": 0, "REQUIREMENTS": 0})

    def test_docs_tests_and_noncode_cd_library_assets_keep_health_only(self):
        for path in ("docs/cd-v9.5-frase.md", "tests/test_cd_phrase_length.py",
                     "osaka_cd/README.md", "osaka_cd/assets/cover.jpg",
                     "osaka_cd/fixtures/arrangement.json", "osaka_cd/lyrics.pyc"):
            with self.subTest(path=path):
                values, calls = self._run([path])
                self.assertEqual(values["BOT"], 0)
                self.assertEqual(values["RESTARTS"], 0)
                self.assertEqual(calls, ["refresh"])

    def test_existing_nested_cog_impact_still_restarts_and_checks_commands(self):
        values, calls = self._run(["cogs/tts/mensagens/preparacao.py"])
        self.assertEqual(values["BOT"], 1)
        self.assertEqual(values["COMMANDS"], 1)
        self.assertEqual(calls.count("systemctl restart tts-bot"), 1)

    def test_existing_catalog_asset_classification_is_unchanged(self):
        path = "cogs/tts/cd/packages/rolling-girl.json"
        previous, previous_calls = self._run([path], historical=True)
        current, current_calls = self._run([path])
        self.assertEqual(previous, current)
        self.assertEqual(previous["BOT"], 1)
        self.assertEqual(previous["COMMANDS"], 0)
        # Epoch timestamps differ between subprocesses; compare restart effects.
        self.assertEqual([line for line in previous_calls if line.startswith("systemctl ")],
                         [line for line in current_calls if line.startswith("systemctl ")])

    def test_real_restart_budget_blocks_a_second_service_restart(self):
        values, calls = self._run(["osaka_cd/lyrics.py"], repeat_restart=True)
        self.assertEqual(values["RESTARTS"], 1)
        self.assertEqual(values["SECOND_RC"], 75)
        self.assertEqual(calls.count("systemctl restart tts-bot"), 1)


if __name__ == "__main__":
    unittest.main()
