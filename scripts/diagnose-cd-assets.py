#!/usr/bin/env python3
"""Read-only local CD/emoji diagnostics. No Discord requests or credentials."""
from pathlib import Path
import argparse
import hashlib
import io
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('id', nargs='?', default='rolling-girl')
    args = parser.parse_args()
    from cogs.tts import cd_covers
    from cogs.tts.interface.application_emojis import emoji_name, square_emoji
    result = {'id': args.id, 'discord_api_called': False,
              'video': cd_covers.video_catalog().get(args.id, {}).get('video_id'),
              'manifest_cover_identity': cd_covers.cover_identity(args.id),
              'local_validated_revision': cd_covers.cover_revision(args.id),
              'panel_can_use_remote_thumbnail': bool(cd_covers.remote_thumbnail_url(args.id))}
    identity = args.id + (':' + result['manifest_cover_identity'] if result['manifest_cover_identity'] else '')
    result['expected_emoji_name'] = emoji_name('osakacd', identity)
    image = cd_covers._cover_bytes(args.id)
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
        result['reason'] = ('JPEG local ausente, inválido ou diferente do hash do manifesto; '
                            'a thumbnail remota do painel pode continuar visível.')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
