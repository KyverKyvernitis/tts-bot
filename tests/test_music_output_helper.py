"""Standalone quality helper: no installed service, secrets or network required."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "scripts/testar-saida-musica.py"
spec = importlib.util.spec_from_file_location("music_output_helper_tested", PATH)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class Response(io.BytesIO):
    status = 200


class OutputHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.values = {"MUSIC_AGENT_TOKEN": "SENTINEL-MUSIC-TOKEN"}

    def response_opener(self, payload):
        opener = mock.Mock()
        raw = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
        opener.open.return_value = Response(raw)
        return opener

    def test_existing_envs_and_custom_music_location_are_literal_and_read_only(self):
        music = self.home / "custom/music.env"
        music.parent.mkdir()
        music.write_text("MUSIC_AGENT_TOKEN='file-token'\nMUSIC_AGENT_PORT=8781\n")
        phone = self.home / ".phone-worker.env"
        marker = self.home / "shell-must-not-run"
        phone.write_text(f"export MUSIC_AGENT_ENV='{music}'\nLITERAL='$(touch {marker})'\n")
        before = {p: p.read_bytes() for p in (phone, music)}
        result = helper.environment({"MUSIC_AGENT_TOKEN": "exported-token"}, home=self.home)
        self.assertEqual(result["MUSIC_AGENT_TOKEN"], "exported-token")
        self.assertEqual(result["MUSIC_AGENT_PORT"], "8781")
        self.assertIn("$(touch", result["LITERAL"])
        self.assertFalse(marker.exists())
        for p, raw in before.items():
            self.assertEqual(p.read_bytes(), raw)

    def test_default_worker_directory_and_json_quoted_values(self):
        folder = self.home / "phone-worker/secrets"
        folder.mkdir(parents=True)
        (folder / "music-agent.env").write_text('MUSIC_AGENT_TOKEN="existing-token"\nMUSIC_AGENT_HOST="127.0.0.1"\n')
        values = helper.environment({}, home=self.home)
        self.assertEqual(values["MUSIC_AGENT_TOKEN"], "existing-token")
        self.assertEqual(helper.endpoint(values), "http://127.0.0.1:8780/command")

    def test_no_worker_or_server_is_imported_from_installed_release(self):
        release = self.home / "release"
        release.mkdir()
        (release / "phone_worker.py").write_text("raise AssertionError('must never import worker')\n")
        with mock.patch("importlib.util.spec_from_file_location", side_effect=AssertionError("dynamic import")):
            result = helper.environment({"PHONE_WORKER_RELEASE_DIR": str(release)}, home=self.home)
        self.assertEqual(result["PHONE_WORKER_RELEASE_DIR"], str(release))

    def test_missing_token_does_not_create_env_or_call_network(self):
        values = helper.environment({}, home=self.home)
        opener = mock.Mock()
        with self.assertRaisesRegex(helper.HelperError, "nenhum token foi criado"):
            helper.request_command({"guild_id": 1}, values, opener=opener)
        opener.open.assert_not_called()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_env_read_is_bounded(self):
        path = self.home / ".phone-worker.env"
        path.write_bytes(b"x" * (helper.MAX_ENV_BYTES + 1))
        with self.assertRaisesRegex(helper.HelperError, "limite"):
            helper.environment({}, home=self.home)

    def test_status_has_no_mutation_settings(self):
        args = helper.parser().parse_args(["status", "--guild", "927002914449424404"])
        self.assertEqual(helper.command_body(args), {"action": "audio_output", "guild_id": 927002914449424404})

    def test_profile_protection_and_reset_payloads(self):
        parse = helper.parser().parse_args
        cases = [
            (["profile", "--guild", "7", "--profile", "quality_256"], {"settings": {"profile": "quality_256"}}),
            (["protection", "--guild", "7", "--fec", "on", "--loss", "0.05"], {"settings": {"fec": True, "expected_packet_loss": 0.05}}),
            (["protection", "--guild", "7", "--fec", "off", "--loss", "0"], {"settings": {"fec": False, "expected_packet_loss": 0.0}}),
            (["direct", "--guild", "7", "--mode", "auto"], {"settings": {"opus_direct": "auto"}}),
            (["direct", "--guild", "7", "--mode", "off"], {"settings": {"opus_direct": "off"}}),
            (["reset", "--guild", "7"], {"reset": True}),
        ]
        for argv, payload in cases:
            with self.subTest(argv=argv):
                self.assertEqual(helper.command_body(parse(argv)), {"action": "audio_output", "guild_id": 7, **payload})

    def test_invalid_guild_and_loss_are_rejected_before_loading_env(self):
        cases = [
            ["status"], ["status", "--guild", "0"], ["status", "--guild", "-1"],
            ["status", "--guild", "18446744073709551616"],
            ["profile", "--guild", "1", "--profile", "ultra"],
            ["direct", "--guild", "1"], ["direct", "--guild", "1", "--mode", "on"],
        ]
        cases += [["protection", "--guild", "1", "--fec", "on", "--loss", value]
                  for value in ("nan", "inf", "-0.1", "1.1", "15%")]
        for argv in cases:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                 mock.patch.object(helper, "environment", side_effect=AssertionError("must validate first")), \
                 self.assertRaises(SystemExit):
                helper.main(argv)

    def test_non_loopback_and_invalid_ports_never_send_token(self):
        for host in ("vps.example.org", "192.168.0.1", "https://127.0.0.1", "127.0.0.1@evil", "127.0.0.1/path"):
            opener = mock.Mock()
            with self.subTest(host=host), self.assertRaises(helper.HelperError):
                helper.request_command({"guild_id": 1}, {**self.values, "MUSIC_AGENT_HOST": host}, opener=opener)
            opener.open.assert_not_called()
        for port in ("0", "65536", "abc"):
            with self.subTest(port=port), self.assertRaises(helper.HelperError):
                helper.endpoint({"MUSIC_AGENT_PORT": port})

    def test_wildcard_binds_map_to_loopback_and_ipv6_is_bracketed(self):
        for host, expected in (("0.0.0.0", "127.0.0.1"), ("localhost", "127.0.0.1"),
                               ("::", "[::1]"), ("[::1]", "[::1]")):
            self.assertEqual(helper.endpoint({"MUSIC_AGENT_HOST": host}), f"http://{expected}:8780/command")

    def test_request_uses_authenticated_local_post_and_timeout(self):
        opener = self.response_opener({"ok": True, "guild_id": 7})
        body = {"action": "audio_output", "guild_id": 7}
        self.assertTrue(helper.request_command(body, self.values, opener=opener)["ok"])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8780/command")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer SENTINEL-MUSIC-TOKEN")
        self.assertEqual(json.loads(request.data), body)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], helper.TIMEOUT_SECONDS)

    def test_proxy_and_redirect_are_disabled(self):
        opener = self.response_opener({"ok": True, "guild_id": 1})
        with mock.patch.object(helper.urllib.request, "build_opener", return_value=opener) as build:
            helper.request_command({"guild_id": 1}, self.values)
        handlers = build.call_args.args
        self.assertEqual(next(x for x in handlers if isinstance(x, urllib.request.ProxyHandler)).proxies, {})
        redirects = next(x for x in handlers if isinstance(x, helper.NoRedirect))
        self.assertIsNone(redirects.redirect_request(None, None, 302, "found", {}, "https://evil.example"))

    def test_http_redirect_failure_does_not_expose_location_or_token(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError("https://evil.example/SENTINEL-MUSIC-TOKEN", 302,
                                                        "Bearer SENTINEL-MUSIC-TOKEN", {}, None)
        with self.assertRaises(helper.HelperError) as error:
            helper.request_command({"guild_id": 1}, self.values, opener=opener)
        self.assertIn("HTTP 302", str(error.exception))
        self.assertNotIn("SENTINEL", str(error.exception))
        self.assertNotIn("evil.example", str(error.exception))

    def test_network_exception_is_safe_even_when_it_contains_credentials(self):
        for error in (urllib.error.URLError("Bearer SENTINEL-MUSIC-TOKEN"), TimeoutError("secret-url"), OSError("private")):
            opener = mock.Mock()
            opener.open.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertRaises(helper.HelperError) as caught:
                helper.request_command({"guild_id": 1}, self.values, opener=opener)
            self.assertNotIn("SENTINEL", str(caught.exception))
            self.assertNotIn("secret-url", str(caught.exception))

    def test_response_limits_bad_json_constants_and_guild_identity(self):
        for payload in (b"x" * (helper.MAX_RESPONSE_BYTES + 1), b"[]", b"not-json", b'{"ok":NaN}', b'{"guild_id":2}'):
            with self.subTest(payload=payload[:20]), self.assertRaises(helper.HelperError):
                helper.request_command({"guild_id": 1}, self.values, opener=self.response_opener(payload))

    def test_only_quality_metadata_reaches_terminal_and_known_secrets_are_redacted(self):
        raw = {"ok": True, "version": "0.3.89", "guild_id": 7,
               "audio_output": {"settings": {"profile": "quality_192", "token": "SENTINEL-MUSIC-TOKEN"},
                                "requested": {"bitrate_kbps": 192, "copy_bitrate_kbps": 256,
                                              "fec": True, "expected_packet_loss": .15},
                                "applied": {"profile": "balanced", "bitrate_kbps": 64},
                                "preparation": {"candidate": True, "cache_enabled": True,
                                                "cache": {"hits": 1, "max_bytes": 268435456,
                                                          "path": "/private/cache"}},
                                "passthrough": {"processed_active": True, "processed_bitrate_kbps": 160,
                                                "opus_transport": True, "output_mode": "opus_processed"},
                                "source_url": "https://private.invalid/track", "reason": "SENTINEL-MUSIC-TOKEN"},
               "sync": {"status": "queued", "error": "private traceback"},
               "capabilities": {"profiles": ["balanced", "quality_192", "quality_256"],
                                "passthrough": {"eligible": False, "reasons": ["pcm_mixer_required"]}},
               "env": {"MUSIC_AGENT_TOKEN": "SENTINEL-MUSIC-TOKEN"}}
        output = helper.public_result(raw, self.values)
        text = json.dumps(output)
        for forbidden in ("SENTINEL-MUSIC-TOKEN", "source_url", "private.invalid", "traceback", '"env"', '"token"'):
            self.assertNotIn(forbidden, text)
        self.assertEqual(output["audio_output"]["requested"]["bitrate_kbps"], 192)
        self.assertEqual(output["audio_output"]["requested"]["copy_bitrate_kbps"], 256)
        self.assertEqual(output["audio_output"]["applied"]["bitrate_kbps"], 64)
        self.assertEqual(output["audio_output"]["preparation"]["cache"]["hits"], 1)
        self.assertTrue(output["audio_output"]["passthrough"]["processed_active"])
        self.assertNotIn("/private/cache", text)
        self.assertEqual(output["sync"]["status"], "queued")

    def test_cli_success_and_failure_status_codes_are_meaningful(self):
        for ok in (True, False):
            output = io.StringIO()
            with mock.patch.object(helper, "environment", return_value=self.values), \
                 mock.patch.object(helper, "request_command", return_value={"ok": ok, "guild_id": 1}), \
                 contextlib.redirect_stdout(output):
                code = helper.cli(["status", "--guild", "1"])
            self.assertEqual(code, 0 if ok else 1)
            self.assertEqual(json.loads(output.getvalue())["ok"], ok)

    def test_redaction_happens_before_string_truncation(self):
        secret = "private-" + "x" * 300
        output = helper.metadata_only({"reason": secret}, {"SERVICE_API_KEY": secret})
        self.assertEqual(output["reason"], "[redacted]")

    def test_direct_payload_only_changes_mode_and_keeps_protection_untouched(self):
        args = helper.parser().parse_args(["direct", "--guild", "7", "--mode", "auto"])
        payload = helper.command_body(args)
        self.assertEqual(payload["settings"], {"opus_direct": "auto"})
        self.assertNotIn("fec", payload["settings"])
        self.assertNotIn("profile", payload["settings"])

    def test_http_400_is_a_command_error_and_only_401_suggests_auth(self):
        for status, expected in ((400, "command_error"), (401, "autenticação recusada")):
            opener = mock.Mock()
            opener.open.side_effect = helper.urllib.error.HTTPError(
                "http://127.0.0.1:8780/command", status,
                "SENTINEL-MUSIC-TOKEN", {}, None)
            with self.subTest(status=status), self.assertRaises(helper.HelperError) as caught:
                helper.request_command({"guild_id": 7}, self.values, opener=opener)
            message = str(caught.exception)
            self.assertIn(expected, message)
            self.assertNotIn("SENTINEL", message)
            if status == 400:
                self.assertNotIn("token", message)

    def test_persistence_metadata_is_visible_without_exposing_local_paths(self):
        persistence = {'persistent': True, 'ready': True, 'stored': True, 'mode': 'off',
                       'error_type': '', 'path': '/private/local/preferences.json'}
        response = {'ok': True, 'guild_id': 7,
                    'audio_output': {'persistence': persistence},
                    'capabilities': {'opus_direct_persistent': True}}
        result = helper.public_result(response, self.values)
        self.assertEqual(result['audio_output']['persistence'],
                         {key: value for key, value in persistence.items() if key != 'path'})
        self.assertTrue(result['capabilities']['opus_direct_persistent'])
        self.assertNotIn('/private', json.dumps(result))

    def test_opus_direct_status_fields_remain_visible_without_source_secrets(self):
        direct = {"mode": "auto", "state": "pcm_fallback", "reason": "tts_overlay",
                  "copied_packets": 17, "encoded_packets": 8, "latched": True,
                  "packet_duration_ms": 20, "source_bitrate_kbps": 128,
                  "preskip_samples": 312, "trimmed_tail_samples": 648,
                  "packet_validation_required": True, "candidate": True,
                  "validated_packets": 25, "copy_active": False, "copy_supported": True,
                  "fallback_encoder": {"bitrate_kbps": 192, "fec": True, "expected_packet_loss": .15},
                  "source_url": "https://private.invalid/secret"}
        raw = {"ok": True, "version": "0.3.90", "guild_id": 7,
               "audio_output": {"settings": {"opus_direct": "auto"}, "passthrough": direct}}
        public = helper.public_result(raw, self.values)
        self.assertEqual(public["audio_output"]["settings"], {"opus_direct": "auto"})
        self.assertEqual(public["audio_output"]["passthrough"],
                         {key: value for key, value in direct.items() if key != "source_url"})
        self.assertNotIn("private.invalid", json.dumps(public))

    def test_real_hybrid_status_shape_preserves_packet_metrics_and_trim_limits(self):
        direct = {"state": "copy", "reason": "", "eligible": True, "copy_active": True,
                  "packet_validation_required": False, "copied_packets": 20, "copied_bytes": 5600,
                  "source_bitrate_kbps": 112, "encoded_packets": 0, "fallback_latched": False,
                  "copy_packet_limit_bytes": 480, "copy_fec": None,
                  "fallback_encoder": {"bitrate_kbps": 192, "fec": True, "expected_packet_loss": .15},
                  "source": {"sample_rate_hz": 48000, "channels": 2, "input_sample_rate_hz": 44100,
                             "pre_skip_samples": 312, "channel_mapping": 0, "output_gain_q8": 0,
                             "end_granule_samples": 48312, "ogg_pages": 4, "audio_packets": 51,
                             "trim_mode": "raw_packets_no_sample_exact_trim", "tail_padding_samples": 648,
                             "raw_duration_samples": 48960, "decoded_samples": 48960,
                             "token": "SENTINEL-MUSIC-TOKEN", "source_url": "https://private.invalid"}}
        result = helper.metadata_only(direct, self.values)
        expected_source = {key: value for key, value in direct['source'].items()
                           if key not in {'token', 'source_url'}}
        self.assertEqual(result, {**direct, 'source': expected_source})
        self.assertNotIn("SENTINEL", json.dumps(result))
        self.assertNotIn("private.invalid", json.dumps(result))

    def test_real_capabilities_and_runtime_metrics_remain_visible(self):
        config_path = ROOT / "cogs/musica/runtime_telefone/agente/configuracao.py"
        config_spec = importlib.util.spec_from_file_location("output_helper_config_contract", config_path)
        config = importlib.util.module_from_spec(config_spec)
        config_spec.loader.exec_module(config)
        capabilities = config.audio_output_capabilities()
        runtime = {"discord_py_version": "2.7.0", "libopus_version": "libopus 1.6",
                   "encoder_controls": {"bitrate": True, "fec": True, "expected_packet_loss": True},
                   "complexity": None, "vbr": None}
        metrics = {"audio_frame_count": 50, "audio_bytes": 192000, "source_stall_count": 0,
                   "partial_frame_count": 0, "source_deadline_overruns": 1, "mix_limited_frames": 2}
        applied = {"profile": "quality_192", "bitrate_kbps": 96, "state": "applied",
                   "unsupported_controls": [], "error_type": None, "revision": 1}
        raw = {"ok": True, "guild_id": 1, "version": "0.3.89", "capabilities": capabilities,
               "audio_output": {"runtime": runtime, "metrics": metrics, "applied": applied},
               "sync": {"state": "queued", "requested": {"bitrate_kbps": 96}, "applied": {}}}
        public = helper.public_result(raw, self.values)
        self.assertEqual(public["capabilities"], capabilities)
        self.assertEqual(public["audio_output"]["runtime"], runtime)
        self.assertEqual(public["audio_output"]["metrics"], metrics)
        self.assertEqual(public["audio_output"]["applied"], applied)
        self.assertEqual(public["sync"], raw["sync"])

    def test_cli_errors_have_no_traceback_or_raw_exception(self):
        output = io.StringIO()
        with mock.patch.object(helper, "environment", side_effect=RuntimeError("SENTINEL-MUSIC-TOKEN")), \
             contextlib.redirect_stderr(output):
            code = helper.cli(["status", "--guild", "1"])
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError", output.getvalue())
        self.assertNotIn("SENTINEL", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())


if __name__ == "__main__":
    unittest.main()
