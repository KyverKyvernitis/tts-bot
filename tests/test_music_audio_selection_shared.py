"""Shared playback/archive policy with yt-dlp and subprocess boundaries adapted.

Uses the production selector and archive method; no network requests, Discord
connection, or external yt-dlp installation are needed for these checks.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from cogs.musica.runtime_telefone.agente import utilitarios as selection


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_SOURCE = ROOT / 'cogs/musica/runtime_telefone/agente/validade_stream.py'


def audio(identifier, codec='opus', bitrate=128, *, language='en', **extra):
    return dict(format_id=identifier, url=f'https://media.invalid/{identifier}',
                acodec=codec, vcodec='none', abr=bitrate, asr=48000,
                audio_channels=2, language=language, **extra)


def chosen(formats, original, **settings):
    selector = selection.shared_audio_format_selector(
        lambda context: iter([original]),
        format_selector=settings.get('format_selector', selection.DEFAULT_YTDLP_AUDIO_FORMAT),
        format_sort=settings.get('format_sort', selection.DEFAULT_YTDLP_AUDIO_SORT))
    return list(selector(dict(formats=formats)))


class SharedSelectionTests(unittest.TestCase):
    def test_comparable_native_opus_reuses_complete_extracted_format(self):
        original = audio('aac', 'mp4a.40.2', 128)
        opus = audio('opus', bitrate=128,
                     http_headers={'Authorization': 'test-only'},
                     fragments=[{'url': 'https://media.invalid/part'}],
                     protocol='http_dash_segments')
        selected, = chosen([original, opus], original)
        self.assertIs(selected, opus)
        self.assertIs(selected['fragments'], opus['fragments'])
        self.assertIs(selected['http_headers'], opus['http_headers'])
        live = selection.select_playback_stream(
            dict(formats=[original, opus], requested_downloads=[original]),
            format_selector=selection.DEFAULT_YTDLP_AUDIO_FORMAT,
            format_sort=selection.DEFAULT_YTDLP_AUDIO_SORT)
        self.assertEqual(live['stream_url'], selected['url'])

    def test_long_format_identifier_does_not_lose_selected_format(self):
        original = audio('aac', 'aac', 128)
        opus = audio('opus-' + 'x' * 100)
        self.assertIs(chosen([original, opus], original)[0], opus)

    def test_clearly_higher_bitrate_aac_is_retained(self):
        original = audio('aac', 'aac', 256)
        opus = audio('opus', bitrate=96)
        self.assertIs(chosen([opus, original], original)[0], original)

    def test_language_and_extractor_preferences_are_preserved(self):
        original = audio('original', 'aac', 128, preference=1,
                         language_preference=2, source_preference=3, quality=4)
        for field, value in [('language', 'pt'), ('preference', 0),
                             ('language_preference', 1), ('source_preference', 2),
                             ('quality', 3), ('has_drm', True), ('vcodec', 'avc1')]:
            with self.subTest(field=field):
                alternative = audio('opus')
                alternative.update({field: value})
                self.assertIs(chosen([original, alternative], original)[0], original)

    def test_explicit_format_or_sort_is_respected(self):
        original = audio('custom', 'aac', 128)
        opus = audio('opus')
        for settings in [dict(format_selector='140'), dict(format_sort='asr,abr'),
                         dict(format_selector='bestaudio[acodec=aac]')]:
            with self.subTest(settings=settings):
                self.assertIs(chosen([original, opus], original, **settings)[0], original)

    def test_missing_or_unsupported_candidates_keep_baseline_object(self):
        original = audio('original', 'aac', 128)
        for formats in [[], [None, 'invalid'], [audio('opus', has_drm=True)],
                        [{**audio('opus'), 'url': 'file:///not-used'}]]:
            with self.subTest(formats=formats):
                self.assertIs(chosen(formats, original)[0], original)

    def test_baseline_selector_is_called_once_per_context(self):
        calls = []
        original = audio('original')
        def baseline(context):
            calls.append(context)
            yield original
        select = selection.shared_audio_format_selector(
            baseline, format_selector=selection.DEFAULT_YTDLP_AUDIO_FORMAT,
            format_sort=selection.DEFAULT_YTDLP_AUDIO_SORT)
        context = {'formats': [original]}
        self.assertEqual(list(select(context)), [original])
        self.assertEqual(calls, [context])


class ArchiveEntrypointTests(unittest.TestCase):
    def run_download(self, *, fmt='bestaudio/best', sort=None, fail=False):
        calls = SimpleNamespace(parsed=[], instances=[], baseline=[], downloads=[])
        original = audio('aac', 'aac', 128)
        opus = audio('opus', bitrate=128, http_headers={'Referer': 'test-only'})
        class DownloadError(Exception):
            pass
        class Downloader:
            def __init__(self, options):
                self.options = options
                calls.instances.append(self)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def build_format_selector(self, selector):
                calls.baseline.append(selector)
                return lambda context: iter([original])
            def download(self, urls):
                calls.downloads.append(urls)
                if fail:
                    raise DownloadError('simulated download failure')
                policy = self.options['format']
                self.selected = list(policy({'formats': [original, opus]})) if callable(policy) else None
                return 0
        def parse_options(arguments):
            calls.parsed.append(arguments)
            return SimpleNamespace(
                ydl_opts=dict(format=fmt, format_sort=sort,
                              cookiefile='cookie-test-only', socket_timeout=12),
                urls=['https://media.invalid/page'])
        fake = SimpleNamespace(parse_options=parse_options, YoutubeDL=Downloader,
                               DownloadError=DownloadError)
        arguments = ['--ignore-config', '--cookies', 'cookie-test-only', '-f', fmt,
                     'https://media.invalid/page']
        with patch.dict(sys.modules, yt_dlp=fake):
            result = selection.archive_download_main(arguments)
        self.assertEqual(calls.parsed, [arguments])
        self.assertEqual(calls.downloads, [['https://media.invalid/page']])
        self.assertEqual(calls.instances[0].options['cookiefile'], 'cookie-test-only')
        self.assertEqual(calls.instances[0].options['socket_timeout'], 12)
        return result, calls, original, opus

    def test_default_download_uses_shared_policy_inside_single_download(self):
        result, calls, _, opus = self.run_download(sort=['abr', 'acodec', 'asr'])
        self.assertEqual(result, 0)
        self.assertEqual(calls.baseline, ['bestaudio/best'])
        self.assertEqual(len(calls.instances), 1)
        self.assertIs(calls.instances[0].selected[0], opus)

    def test_explicit_settings_keep_ytdlp_selector(self):
        for fmt, sort in [('140', ['abr', 'acodec', 'asr']),
                          ('bestaudio/best', ['asr', 'abr'])]:
            with self.subTest(fmt=fmt, sort=sort):
                result, calls, _, _ = self.run_download(fmt=fmt, sort=sort)
                self.assertEqual(result, 0)
                self.assertEqual(calls.baseline, [])
                self.assertEqual(calls.instances[0].options['format'], fmt)

    def test_download_error_returns_existing_nonzero_contract(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result, _, _, _ = self.run_download(fail=True)
        self.assertEqual(result, 1)
        self.assertEqual(stderr.getvalue(), 'simulated download failure\n')


def archive_agent_class():
    tree = ast.parse(ARCHIVE_SOURCE.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and any(getattr(method, 'name', '') == '_archive_download' for method in node.body))
    method = next(node for node in cls.body if getattr(node, 'name', '') == '_archive_download')
    wrapper = ast.ClassDef(name='Agent', bases=[], keywords=[], body=[method], decorator_list=[])
    namespace = dict(asyncio=asyncio, contextlib=contextlib, json=json, os=os,
                     Path=Path, shutil=shutil, sys=sys, urlsplit=urlsplit,
                     DEFAULT_YTDLP_AUDIO_FORMAT=selection.DEFAULT_YTDLP_AUDIO_FORMAT,
                     DEFAULT_YTDLP_AUDIO_SORT=selection.DEFAULT_YTDLP_AUDIO_SORT,
                     formato_audio_configurado=selection.formato_audio_configurado,
                     __file__=str(ARCHIVE_SOURCE), _AUDIO_EXTS={'.ogg', '.m4a'},
                     ArchiveDownloadFailure=RuntimeError, CatalogNoMatchError=RuntimeError)
    module = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
    exec(compile(module, str(ARCHIVE_SOURCE), 'exec'), namespace)
    return namespace['Agent']


class ArchiveInvocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_staged_bytes_are_reused_without_spawn_or_format_change(self):
        agent = archive_agent_class()()
        item = {'track': {'webpage_url': 'https://media.invalid/old'}, 'emoji': 'audio', 'key': 'test'}
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            original = folder / 'audio.m4a'
            data = b'original staged archive bytes'
            original.write_bytes(data)
            (folder / '.download-name').write_text(original.name)
            found = await agent._archive_download(item, folder)
            self.assertEqual(found, original)
            self.assertEqual(original.read_bytes(), data)

    async def test_future_download_preserves_options_and_configured_format(self):
        for selector in ['bestaudio/best', '140', selection.LEGACY_YTDLP_AUDIO_FORMAT]:
            with self.subTest(selector=selector), tempfile.TemporaryDirectory() as root:
                folder = Path(root)
                cookies = folder / 'cookies.txt'
                cookies.write_text('test-only')
                calls = []
                agent = archive_agent_class()()
                agent.ytdlp_format = selector
                agent.ytdlp_sort = 'abr,acodec,asr'
                agent.cookies_file = str(cookies)
                agent.js_runtimes = 'node,quickjs'
                agent._archive_active = 'test'
                agent._archive_playback_active = lambda: True
                async def spawn(*args, **kwargs):
                    calls.append((args, kwargs))
                    return SimpleNamespace(returncode=0)
                async def communicate(proc, destination, **kwargs):
                    (destination / 'audio.ogg').write_bytes(b'new audio')
                    return b'', b''
                agent._archive_spawn = spawn
                agent._archive_download_communicate = communicate
                item = {'track': {'webpage_url': 'https://media.invalid/page', 'duration': 120},
                        'emoji': 'audio', 'key': 'test'}
                result = await agent._archive_download(item, folder)
                self.assertEqual(result, folder / 'audio.ogg')
                self.assertEqual(len(calls), 1)
                arguments, kwargs = calls[0]
                self.assertEqual(arguments[:3], (sys.executable,
                    str(ARCHIVE_SOURCE.with_name('utilitarios.py')), '--archive-download'))
                for flag, value in [('--cookies', str(cookies)), ('--socket-timeout', '12'),
                                    ('--format-sort', 'abr,acodec,asr'), ('--limit-rate', '512K'),
                                    ('-f', selection.formato_audio_configurado(selector))]:
                    self.assertEqual(arguments[arguments.index(flag) + 1], value)
                self.assertEqual(arguments.count('--js-runtimes'), 2)
                self.assertEqual(arguments[-1], 'https://media.invalid/page')
                self.assertEqual(kwargs['stderr'], asyncio.subprocess.PIPE)


if __name__ == '__main__':
    unittest.main()
