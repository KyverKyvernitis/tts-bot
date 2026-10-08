from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "termux" / "phone-worker" / "scripts" / "validate-teto-assets.py"
SPEC = importlib.util.spec_from_file_location("teto_asset_validator_under_test", SCRIPT)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class TetoAssetValidatorTests(unittest.TestCase):
    def _renderer(self) -> Mock:
        renderer = Mock()
        renderer.status.return_value = {
            "ready": True, "root": "/selected/english", "voicebank_profile": "english-cvvc",
            "renderer_version": "revision-test", "phonemizer_version": "g2p-test",
        }
        renderer.synthesize.return_value = {
            "audio": b"RIFFtest-wave-bytes", "audio_format": "wav",
            "voicebank_profile": "english-cvvc", "coverage_percent": 87.5,
        }
        return renderer

    def _run(self, renderer: Mock, *extra: str) -> tuple[int, dict]:
        arguments = ["--voicebank", "/candidate/english", "--resampler", "resampler", *extra]
        stdout = io.StringIO()
        with patch.dict(os.environ, {}, clear=False), patch.object(
            validator, "TetoRenderer", return_value=renderer,
        ), contextlib.redirect_stdout(stdout):
            code = validator.main(arguments)
        return code, json.loads(stdout.getvalue())

    def test_render_test_saves_exact_wav_and_exposes_selected_bank(self):
        renderer = self._renderer()
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "sample.wav"
            code, report = self._run(
                renderer, "--mode", "auto", "--render-test", "--text", "Minha filha",
                "--output", str(output),
            )
            self.assertEqual(code, 0)
            self.assertEqual(output.read_bytes(), renderer.synthesize.return_value["audio"])
            self.assertEqual(report["render_test"]["output"], str(output.resolve()))
            self.assertEqual(report["status"]["root"], "/selected/english")
            self.assertNotIn("audio", report["render_test"])
            renderer.synthesize.assert_called_once_with(
                "Minha filha", timeout_seconds=30, max_audio_bytes=8 * 1024 * 1024,
            )

    def test_status_only_does_not_render_or_create_a_corpus(self):
        renderer = self._renderer()
        code, report = self._run(renderer)
        self.assertEqual(code, 0)
        renderer.synthesize.assert_not_called()
        self.assertNotIn("audit", report)
        self.assertNotIn("render_test", report)

    def test_legacy_render_test_works_without_output(self):
        renderer = self._renderer()
        code, report = self._run(renderer, "--render-test")
        self.assertEqual(code, 0)
        self.assertEqual(report["render_test"]["bytes"], len(renderer.synthesize.return_value["audio"]))
        self.assertNotIn("output", report["render_test"])

    def test_audit_saves_bounded_corpus_and_metadata(self):
        renderer = self._renderer()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "audit"
            code, report = self._run(renderer, "--audit-dir", str(directory))
            self.assertEqual(code, 0)
            self.assertEqual(len(list(directory.glob("*.wav"))), 10)
            self.assertEqual(len(list(directory.glob("*.json"))), 11)
            summary = json.loads((directory / "summary.json").read_text("utf-8"))
            self.assertEqual(summary["rendered"], 10)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(summary["status"]["voicebank_profile"], "english-cvvc")
            self.assertEqual(summary["status"]["renderer_version"], "revision-test")
            for call in renderer.synthesize.call_args_list:
                self.assertEqual(call.kwargs["timeout_seconds"], 30)
                self.assertEqual(call.kwargs["max_audio_bytes"], 8 * 1024 * 1024)
            self.assertEqual(report["audit"]["rendered"], 10)

    def test_failed_phrase_is_reported_nonzero_and_removes_stale_wav(self):
        renderer = self._renderer()
        rendered = renderer.synthesize.return_value
        renderer.synthesize.side_effect = [TimeoutError("deadline"), *([rendered] * 9)]
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stale = directory / "01-nasais-palatais.wav"
            stale.write_bytes(b"older audio")
            code, report = self._run(renderer, "--audit-dir", str(directory))
            self.assertEqual(code, 3)
            self.assertEqual(report["audit"]["failed"], 1)
            self.assertEqual(report["audit"]["rendered"], 9)
            self.assertIn("TimeoutError", report["audit"]["phrases"][0]["error"])
            self.assertFalse(stale.exists())
            self.assertTrue((directory / "01-nasais-palatais.json").is_file())

    def test_output_path_error_is_nonzero(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "missing" / "sample.wav"
            code, report = self._run(self._renderer(), "--render-test", "--output", str(output))
            self.assertEqual(code, 3)
            self.assertIn("FileNotFoundError", report["error"])
            self.assertFalse(output.exists())

    def test_audit_path_error_is_nonzero(self):
        renderer = self._renderer()
        with tempfile.TemporaryDirectory() as temp:
            existing_file = Path(temp) / "file"
            existing_file.write_text("not a directory", encoding="utf-8")
            code, report = self._run(renderer, "--audit-dir", str(existing_file))
            self.assertEqual(code, 3)
            self.assertIn("FileExistsError", report["error"])
            renderer.synthesize.assert_not_called()

    def test_oversized_audio_is_not_saved(self):
        renderer = self._renderer()
        renderer.synthesize.return_value = {"audio": b"x" * (validator.MAX_AUDIO_BYTES + 1)}
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "too-large.wav"
            code, report = self._run(renderer, "--render-test", "--output", str(output))
            self.assertEqual(code, 3)
            self.assertIn("limite de 8 MiB", report["error"])
            self.assertFalse(output.exists())

    def test_output_requires_render_test(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self._run(self._renderer(), "--output", "unused.wav")
        self.assertEqual(raised.exception.code, 2)

    def test_unavailable_bank_does_not_render(self):
        renderer = self._renderer()
        renderer.status.return_value = {"ready": False, "last_error": "missing bank"}
        code, report = self._run(renderer, "--audit-dir", "unused", "--render-test")
        self.assertEqual(code, 2)
        self.assertEqual(report["status"]["last_error"], "missing bank")
        renderer.synthesize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
