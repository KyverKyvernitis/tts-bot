#!/usr/bin/env python3
"""Read-only local CD/emoji diagnostics. No Discord requests or credentials."""
from pathlib import Path
import argparse
import hashlib
import importlib.util
import io
import json
import sys
import stat

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def inspect_cover(path, expected, *, pillow_available):
    result = {'path': str(path), 'expected_sha256': expected}
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return {**result, 'state': 'not_regular_file'}
        if not 0 < info.st_size <= 2 * 1024 * 1024:
            return {**result, 'state': 'invalid_size', 'bytes': info.st_size}
        with path.open('rb') as file:
            data = file.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            return {**result, 'state': 'invalid_size'}
        digest = hashlib.sha256(data).hexdigest()
        result.update(bytes=len(data), actual_sha256=digest, hash_matches=digest == expected)
        if digest != expected:
            return {**result, 'state': 'hash_mismatch'}
        if not data.startswith(b'\xff\xd8\xff') or not data.endswith(b'\xff\xd9'):
            return {**result, 'state': 'invalid_jpeg_signature'}
        if not pillow_available:
            return {**result, 'state': 'hash_ready_decode_unavailable'}
        from cogs.tts.cd_covers import _valid_jpeg
        return {**result, 'state': 'ready' if _valid_jpeg(data) else 'invalid_jpeg'}
    except FileNotFoundError:
        return {**result, 'state': 'missing_file'}
    except OSError as error:
        return {**result, 'state': 'read_failed', 'error_type': type(error).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('id', nargs='?', default='rolling-girl')
    args = parser.parse_args()
    from osaka_cd.runtime import pin_snapshot
    with pin_snapshot() as selected:
        diagnose(args.id, selected)


def diagnose(identity, selected):
    from osaka_cd.runtime import data_root
    from cogs.tts import cd_covers
    from cogs.tts.interface.application_emojis import emoji_name, square_emoji
    pillow_available = importlib.util.find_spec('PIL') is not None
    result = {'id': identity, 'discord_api_called': False,
              'python_executable': sys.executable, 'pillow_available': pillow_available,
              'catalog_path': str(data_root(cd_covers._DATA) / 'cd_song_videos.json'),
              'runtime_generation': selected.get('generation'),
              'video': cd_covers.video_catalog().get(identity, {}).get('video_id'),
              'manifest_cover_identity': cd_covers.cover_identity(identity),
              'local_validated_revision': cd_covers.cover_revision(identity),
              'panel_can_use_remote_thumbnail': bool(cd_covers.remote_thumbnail_url(identity))}
    emoji_identity = identity + (':' + result['manifest_cover_identity'] if result['manifest_cover_identity'] else '')
    result['expected_emoji_name'] = emoji_name('osakacd', emoji_identity)
    result['local_cover_candidates'] = [inspect_cover(path, expected, pillow_available=pillow_available)
                                        for path, expected in cd_covers._cover_sources(identity)]
    image = cd_covers._cover_bytes(identity)
    result['local_cover_ready'] = image is not None
    if image:
        result['local_cover_sha256'] = hashlib.sha256(image).hexdigest()
        try:
            output = square_emoji(image)
            from PIL import Image
            with Image.open(io.BytesIO(output)) as picture:
                picture.load()
                result['emoji_image'] = {'valid': True, 'format': picture.format,
                                         'size': list(picture.size), 'bytes': len(output)}
        except Exception as exc:
            result['emoji_image'] = {'valid': False, 'error_type': type(exc).__name__}
    else:
        states = [item['state'] for item in result['local_cover_candidates']]
        if 'hash_ready_decode_unavailable' in states:
            result['local_cover_ready'] = None
            result['reason_code'] = 'pillow_unavailable_in_this_python'
            result['reason'] = ('Arquivo e SHA-256 conferem, mas este Python não tem Pillow. '
                                'Execute o diagnóstico com o Python do serviço do bot para verificar a imagem.')
        elif not result['manifest_cover_identity']:
            result['reason_code'] = 'thumbnail_hash_missing'
            result['reason'] = 'O catálogo não declara um hash de capa válido para este CD.'
        else:
            result['reason_code'] = next(iter(states), 'local_cover_unavailable')
            result['reason'] = ('Confira local_cover_candidates: a capa não foi aprovada neste processo; '
                                'a thumbnail remota ou um emoji já registrado podem continuar visíveis.')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
