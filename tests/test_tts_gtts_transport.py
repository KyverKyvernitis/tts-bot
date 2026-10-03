"""Transporte gTTS preserva a verificação TLS e as configurações do requests."""
from __future__ import annotations

import base64
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from gtts.tts import gTTSError
import requests

from test_tts_helpers import TTSAudioMixin, tts_audio


class GTTSTransportTests(unittest.TestCase):
    def _iterate(self, session, *, proxies=None, persistent=True):
        audio = b"simulated-mp3-bytes"
        line = b'jQ1olc","[\\"' + base64.b64encode(audio) + b'\\"]'
        response = SimpleNamespace(
            raise_for_status=Mock(),
            iter_lines=lambda **kwargs: iter((line,)),
            close=Mock(),
        )
        session.send = Mock(return_value=response)
        prepared = requests.Request("POST", "https://tts.example.test/synthesize", data=b"fixed-test-text").prepare()
        tts = SimpleNamespace(_prepare_requests=lambda: [prepared],
                              stream=lambda: iter(()), timeout=(3.5, 8.0))
        class Probe(TTSAudioMixin):
            def _get_gtts_thread_session(self):
                if not persistent:
                    raise AssertionError("sessão persistente usada quando desativada")
                return session, dict(proxies or {})
        with patch.object(tts_audio, "TTS_GTTS_PERSISTENT_SESSION_ENABLED", persistent), \
             patch.object(tts_audio.requests, "Session", return_value=session):
            result = b"".join(Probe()._iter_gtts_audio_chunks(tts))
        self.assertEqual(result, audio)
        response.close.assert_called_once()
        return session.send.call_args.kwargs

    def test_requests_ca_bundle_is_applied_to_prepared_requests(self):
        with requests.Session() as session, patch.dict("os.environ", {"REQUESTS_CA_BUNDLE": "/test/requests-ca.pem"}, clear=True):
            options = self._iterate(session)
        self.assertEqual(options["verify"], "/test/requests-ca.pem")
        self.assertTrue(options["stream"])
        self.assertEqual(options["timeout"], (3.5, 8.0))

    def test_curl_ca_bundle_is_used_when_requests_bundle_is_unset(self):
        with requests.Session() as session, patch.dict("os.environ", {"CURL_CA_BUNDLE": "/test/curl-ca.pem"}, clear=True):
            options = self._iterate(session)
        self.assertEqual(options["verify"], "/test/curl-ca.pem")

    def test_tls_verification_remains_enabled_without_a_custom_ca(self):
        with requests.Session() as session, patch.dict("os.environ", {}, clear=True):
            options = self._iterate(session)
        self.assertIs(options["verify"], True)

    def test_session_trust_env_false_ignores_environment_ca_and_proxy(self):
        with requests.Session() as session, patch.dict("os.environ", {
            "REQUESTS_CA_BUNDLE": "/test/requests-ca.pem",
            "HTTPS_PROXY": "http://proxy.example.test:8080",
        }, clear=True):
            session.trust_env = False
            options = self._iterate(session)
        self.assertIs(options["verify"], True)
        self.assertEqual(options["proxies"], {})

    def test_explicit_verified_tls_overrides_session_verify_false(self):
        with requests.Session() as session, patch.dict("os.environ", {}, clear=True):
            session.verify = False
            options = self._iterate(session)
        self.assertIs(options["verify"], True)

    def test_session_and_environment_proxy_settings_are_merged_once(self):
        with requests.Session() as session, patch.dict("os.environ", {"HTTPS_PROXY": "http://proxy.example.test:8080"}, clear=True):
            session.proxies = {"http": "http://session-proxy.example.test:8080"}
            session.cert = "/test/client-certificate.pem"
            options = self._iterate(session)
        self.assertEqual(options["proxies"]["https"], "http://proxy.example.test:8080")
        self.assertEqual(options["proxies"]["http"], "http://session-proxy.example.test:8080")
        self.assertEqual(options["cert"], "/test/client-certificate.pem")
        self.assertIs(options["verify"], True)

    def test_legacy_send_only_session_preserves_verified_tls(self):
        options = self._iterate(SimpleNamespace())
        self.assertIs(options["verify"], True)
        self.assertEqual(options["proxies"], {})

    def test_disabled_persistence_still_applies_ca_and_closes_session_on_success(self):
        with requests.Session() as session, patch.dict("os.environ", {"REQUESTS_CA_BUNDLE": "/test/requests-ca.pem"}, clear=True):
            session.close = Mock(wraps=session.close)
            options = self._iterate(session, persistent=False)
            session.close.assert_called_once()
        self.assertEqual(options["verify"], "/test/requests-ca.pem")

    def _temporary_fixture(self):
        session = requests.Session()
        session.close = Mock(wraps=session.close)
        audio = b"simulated-mp3-bytes"
        line = b'jQ1olc","[\\"' + base64.b64encode(audio) + b'\\"]'
        response = SimpleNamespace(raise_for_status=Mock(), iter_lines=lambda **kwargs: iter((line, line)),
                                   close=Mock(), status_code=200, reason="OK")
        session.send = Mock(return_value=response)
        prepared = requests.Request("POST", "https://tts.example.test/synthesize", data=b"fixed-test-text").prepare()
        tts = SimpleNamespace(_prepare_requests=lambda: [prepared], timeout=(3.5, 8.0),
                              stream=Mock(side_effect=AssertionError("stream legado não deve ser usado")),
                              lang="pt", tld="com", lang_check=True, text="texto fixo de teste")
        probe = TTSAudioMixin()
        probe._get_gtts_thread_session = Mock(side_effect=AssertionError("reúso desativado"))
        probe._invalidate_gtts_thread_session = Mock(side_effect=AssertionError("reúso desativado"))
        return session, response, tts, probe, audio

    def test_disabled_persistence_closes_session_and_response_when_generator_closes(self):
        session, response, tts, probe, audio = self._temporary_fixture()
        with patch.object(tts_audio, "TTS_GTTS_PERSISTENT_SESSION_ENABLED", False), \
             patch.object(tts_audio.requests, "Session", return_value=session):
            chunks = probe._iter_gtts_audio_chunks(tts)
            self.assertEqual(next(chunks), audio)
            chunks.close()
        response.close.assert_called_once()
        session.close.assert_called_once()
        tts.stream.assert_not_called()

    def test_disabled_persistence_preserves_cancellation_without_retry_or_leak(self):
        session, response, tts, probe, audio = self._temporary_fixture()
        tts._tts_stop_requested = threading.Event()
        with patch.object(tts_audio, "TTS_GTTS_PERSISTENT_SESSION_ENABLED", False), \
             patch.object(tts_audio.requests, "Session", return_value=session):
            chunks = probe._iter_gtts_audio_chunks(tts)
            self.assertEqual(next(chunks), audio)
            tts._tts_stop_requested.set()
            with self.assertRaises(TimeoutError):
                next(chunks)
        session.send.assert_called_once()
        response.close.assert_called_once()
        session.close.assert_called_once()

    def test_disabled_persistence_closes_each_session_on_exhausted_retries(self):
        first, _response, tts, probe, _audio = self._temporary_fixture()
        second = requests.Session()
        second.close = Mock(wraps=second.close)
        first.send = Mock(side_effect=requests.exceptions.ConnectionError("simulated transport failure"))
        second.send = Mock(side_effect=requests.exceptions.ConnectionError("simulated transport failure"))
        with patch.object(tts_audio, "TTS_GTTS_PERSISTENT_SESSION_ENABLED", False), \
             patch.object(tts_audio.requests, "Session", side_effect=(first, second)) as factory:
            with self.assertRaises(gTTSError):
                list(probe._iter_gtts_audio_chunks(tts))
        self.assertEqual(factory.call_count, 2)
        first.close.assert_called_once()
        second.close.assert_called_once()
        tts.stream.assert_not_called()

    def test_disabled_persistence_closes_session_when_response_stream_fails(self):
        session, response, tts, probe, _audio = self._temporary_fixture()
        response.raise_for_status.side_effect = requests.exceptions.HTTPError("simulated response failure")
        with patch.object(tts_audio, "TTS_GTTS_PERSISTENT_SESSION_ENABLED", False), \
             patch.object(tts_audio.requests, "Session", return_value=session):
            with self.assertRaises(gTTSError):
                list(probe._iter_gtts_audio_chunks(tts))
        response.close.assert_called_once()
        session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
