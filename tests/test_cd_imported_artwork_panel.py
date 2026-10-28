"""Imported cover bytes feed both the panel attachment and application emoji."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import zipfile

from PIL import Image
import pytest

from cogs.tts import cd_covers
from cogs.tts.interface import painel_canto as panel
from cogs.tts.interface.application_emojis import ApplicationSelectEmojis
from osaka_cd import bundled, runtime
from osaka_cd.store import RuntimeStore
from test_cd_panel import cog_for, walk


@pytest.mark.asyncio
async def test_imported_cover_supplies_artist_attachment_and_reusable_menu_emoji(tmp_path):
    legacy = tmp_path / 'legacy'
    data = legacy / 'cogs/tts'
    (data / 'cd/previews').mkdir(parents=True)
    (data / 'cd_song_scores.json').write_text('[]')
    (data / 'cd_song_videos.json').write_text('{"songs":{}}')
    (data / 'cd/previews/index.json').write_text('{}')
    store = RuntimeStore(tmp_path / 'state', legacy)
    # This fixture represents the provider result. Production downloads and
    # normalizes it with catalog_import and the existing ArchiveMixin.
    image = io.BytesIO()
    Image.new('RGB', (480, 360), 'turquoise').save(image, format='JPEG')
    image = image.getvalue()
    digest = hashlib.sha256(image).hexdigest()
    video_id = 'abcdefghijk'
    with zipfile.ZipFile(bundled._PACKAGE) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    videos = json.loads(files['cogs/tts/cd_song_videos.json'])
    videos['songs']['ievan-polkka'].update(video_id=video_id,
        title='Ievan Polkka · Otomania feat. Hatsune Miku',
        artist='Otomania feat. Hatsune Miku', thumbnail_sha256=digest,
        view_count=123, view_count_source='verified-test-provider',
        selection_audit={'inspected_candidates': 1})
    files['cogs/tts/cd_song_videos.json'] = json.dumps(videos).encode()
    files[f'cogs/tts/cd/thumbnails/{video_id}.jpg'] = image
    package = tmp_path / 'imported.zip'
    with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    receipt = store.receive(package)
    result = store.process(receipt['job_id'])
    assert result['applied'], result

    remote = []

    async def create_emoji(*, name, image):
        with Image.open(io.BytesIO(image)) as decoded:
            assert decoded.size == (128, 128)
            assert decoded.format == 'JPEG'
            decoded.load()
        emoji = SimpleNamespace(name=name, id=9001)
        remote.append(emoji)
        return emoji

    bot = SimpleNamespace(user=None,
        fetch_application_emojis=AsyncMock(side_effect=lambda: list(remote)),
        create_application_emoji=AsyncMock(side_effect=create_emoji))
    manager = ApplicationSelectEmojis('osakacd', cd_covers._cover_bytes,
        lambda: ('ievan-polkka',), version_for_id=cd_covers.cover_identity)
    cog = cog_for({'ttv_melody_id': 'ievan-polkka'})
    cog.bot = bot
    view = None
    with runtime.unpin_snapshot(), runtime.pin_snapshot(store.snapshot()), \
            patch.object(runtime, 'snapshot', side_effect=store.snapshot), \
            patch.object(panel, '_CD_EMOJIS', manager), \
            patch('urllib.request.urlopen', side_effect=AssertionError('panel fetched artwork')):
        try:
            view = panel.CDPanelView(cog, 10, 20)
            await view.refresh()
            await manager._task
            assert manager.diagnostics('ievan-polkka')['state'] == 'ready', manager.diagnostics('ievan-polkka')
            view._rebuild()
            assert cd_covers.cover_revision('ievan-polkka') == digest[:12]
            files = view.panel_files()
            try:
                assert len(files) == 1
                assert files[0].filename == 'ttv-cd-ievan-polkka.jpg'
                assert files[0].fp.read() == image
            finally:
                for file in files:
                    file.close()
            components = list(walk(view.to_components()))
            contents = '\n'.join(item.get('content', '') for item in components)
            assert '**Ievan Polkka**' in contents
            assert 'Otomania feat. Hatsune Miku' in contents
            thumbnails = [item for item in components if item.get('type') == 11]
            assert thumbnails[0]['media']['url'] == 'attachment://ttv-cd-ievan-polkka.jpg'
            options = next(item['options'] for item in components if item.get('type') == 3)
            selected = next(item for item in options if item['value'] == 'ievan-polkka')
            assert str(selected['emoji']['id']) == '9001'
            manager.schedule(bot)
            assert bot.create_application_emoji.await_count == 1
            assert bot.fetch_application_emojis.await_count == 1
        finally:
            manager.close()
            if view is not None:
                view.stop()
