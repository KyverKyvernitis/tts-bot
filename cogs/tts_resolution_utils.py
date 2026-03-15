import re
import unicodedata

import discord

from .tts_voice_common import _normalize_spaces, _shorten


def gcloud_language_priority(code: str) -> tuple[int, str]:
    value = str(code or '').strip()
    preferred = {
        'pt-BR': 0,
        'pt-PT': 1,
        'en-US': 2,
        'es-ES': 3,
        'es-US': 4,
        'fr-FR': 5,
        'de-DE': 6,
        'it-IT': 7,
        'ja-JP': 8,
    }
    base = value.split('-', 1)[0].lower() if value else ''
    base_order = {
        'pt': 0,
        'en': 1,
        'es': 2,
        'fr': 3,
        'de': 4,
        'it': 5,
        'ja': 6,
    }
    return (preferred.get(value, 100 + base_order.get(base, 100)), value.lower())


def build_gcloud_language_options_from_catalog(catalog: list[dict[str, object]], current_value: str | None = None, *, default_language: str = 'pt-BR') -> list[discord.SelectOption]:
    seen: set[str] = set()
    ordered_codes: list[str] = []
    preferred = [
        str(current_value or '').strip(),
        str(default_language or 'pt-BR').strip(),
        'pt-BR', 'pt-PT', 'en-US', 'es-ES', 'es-US', 'fr-FR', 'de-DE', 'it-IT', 'ja-JP',
    ]
    for code in preferred:
        if code and code not in seen:
            seen.add(code)
            ordered_codes.append(code)
    discovered = sorted({str(code) for entry in catalog for code in list(entry.get('language_codes', []) or []) if str(code or '').strip()}, key=gcloud_language_priority)
    for code in discovered:
        if code not in seen:
            seen.add(code)
            ordered_codes.append(code)
    options: list[discord.SelectOption] = []
    for code in ordered_codes[:25]:
        desc = 'Idioma disponível no Google Cloud'
        if code.startswith('pt-'):
            desc = 'Português disponível no Google Cloud'
        elif code.startswith('en-'):
            desc = 'Inglês disponível no Google Cloud'
        elif code.startswith('es-'):
            desc = 'Espanhol disponível no Google Cloud'
        options.append(discord.SelectOption(label=_shorten(code, 100), description=_shorten(desc, 100), value=code, default=(code == current_value)))
    return options


def gcloud_voice_priority(voice_name: str) -> tuple[int, str]:
    value = str(voice_name or '').strip()
    order = [('Studio', 0), ('Neural2', 1), ('Wavenet', 2), ('Standard', 3), ('Chirp3-HD', 4), ('Chirp3', 4)]
    family_rank = 99
    for token, rank in order:
        if token.lower() in value.lower():
            family_rank = rank
            break
    return (family_rank, value.lower())


def split_gcloud_voice_name(voice_name: str) -> tuple[str, str]:
    value = str(voice_name or '').strip()
    family = 'Google Cloud'
    for token, label in [('Studio', 'Studio'), ('Neural2', 'Neural2'), ('Wavenet', 'WaveNet'), ('Standard', 'Standard'), ('Chirp3-HD', 'Chirp 3 HD'), ('Chirp3', 'Chirp 3')]:
        if token.lower() in value.lower():
            family = label
            break
    tail = value.rsplit('-', 1)[-1] if '-' in value else ''
    variant = f'variante {tail}' if len(tail) <= 3 and tail.isalnum() else value
    return family, variant


def describe_gcloud_voice(voice_name: str) -> str:
    family, variant = split_gcloud_voice_name(voice_name)
    return _shorten(f'{family} · {variant}', 100)


def build_gcloud_voice_options_from_catalog(catalog: list[dict[str, object]], language_code: str, current_value: str | None = None, *, default_language: str = 'pt-BR', default_voice: str = 'pt-BR-Standard-A') -> list[discord.SelectOption]:
    language_code = str(language_code or '').strip() or str(default_language or 'pt-BR')
    filtered_names = sorted({str(entry.get('name') or '') for entry in catalog if language_code in list(entry.get('language_codes', []) or []) and str(entry.get('name') or '').strip()}, key=gcloud_voice_priority)
    ordered_names: list[str] = []
    seen: set[str] = set()
    preferred = [
        str(current_value or '').strip(),
        str(default_voice or 'pt-BR-Standard-A').strip(),
    ]
    for name in preferred:
        if name and name not in seen:
            seen.add(name)
            ordered_names.append(name)
    for name in filtered_names:
        if name not in seen:
            seen.add(name)
            ordered_names.append(name)
    options: list[discord.SelectOption] = []
    for name in ordered_names[:25]:
        family, variant = split_gcloud_voice_name(name)
        label = _shorten(f'{family} — {variant}', 100)
        options.append(discord.SelectOption(label=label, description=_shorten(name, 100), value=name, default=(name == current_value)))
    return options


def gcloud_voice_matches_language(voice_name: str, language_code: str) -> bool:
    voice = str(voice_name or '').strip().lower()
    language = str(language_code or '').strip().lower()
    if not voice or not language:
        return False
    return voice.startswith(language + '-')


def pick_first_gcloud_voice_for_language(catalog: list[dict[str, object]], language_code: str, *, default_language: str = 'pt-BR', default_voice: str = 'pt-BR-Standard-A') -> str:
    options = build_gcloud_voice_options_from_catalog(catalog, language_code, current_value=None, default_language=default_language, default_voice=default_voice)
    return str(options[0].value if options else '')


def normalize_rate_value(raw: str) -> str | None:
    value = str(raw).strip().replace('％', '%').replace('−', '-').replace('–', '-').replace('—', '-').replace(' ', '')
    if value.endswith('%'):
        value = value[:-1]
    if not value:
        return None
    if value[0] not in '+-':
        value = f'+{value}'
    if not value[1:].isdigit():
        return None
    return f"{value[0]}{value[1:]}%"


def normalize_pitch_value(raw: str) -> str | None:
    value = str(raw).strip().replace('−', '-').replace('–', '-').replace('—', '-').replace(' ', '')
    if value.lower().endswith('hz'):
        value = value[:-2]
    if not value:
        return None
    if value[0] not in '+-':
        value = f'+{value}'
    if not value[1:].isdigit():
        return None
    return f"{value[0]}{value[1:]}Hz"


def normalize_language_query(value: str) -> str:
    normalized = unicodedata.normalize('NFKD', str(value or '').strip().lower())
    normalized = ''.join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = re.sub(r'[^a-z0-9\-\s]', ' ', normalized)
    return re.sub(r'\s+', ' ', normalized).strip()


def resolve_gtts_language_input(raw_language: str, gtts_languages: dict[str, str], gtts_language_aliases: dict[str, str]) -> tuple[str | None, str | None]:
    value = str(raw_language or '').strip()
    if not value:
        return None, None

    normalized = normalize_language_query(value)
    candidates = [normalized]
    if normalized:
        candidates.extend({normalized.replace('_', '-'), normalized.replace(' ', '-'), normalized.replace('-', ' ')})

    for candidate in candidates:
        code = gtts_language_aliases.get(candidate)
        if code and code in gtts_languages:
            return code, gtts_languages.get(code)

    raw_code = value.strip().lower().replace('_', '-')
    if raw_code in gtts_languages:
        return raw_code, gtts_languages.get(raw_code)

    return None, None


def validate_gcloud_language_input(raw_value: str) -> tuple[str | None, str | None]:
    value = _normalize_spaces(str(raw_value or '')).replace(' ', '')
    if not value:
        return None, 'O idioma do Google Cloud não pode ficar vazio.'
    if len(value) > 16 or not re.fullmatch(r'[A-Za-z0-9-]+', value):
        return None, 'Use um código de idioma válido, como `pt-BR` ou `en-US`.'
    return value, None


def validate_gcloud_voice_input(raw_value: str) -> tuple[str | None, str | None]:
    value = _normalize_spaces(str(raw_value or ''))
    if not value:
        return None, 'A voz do Google Cloud não pode ficar vazia.'
    value = value.replace(' ', '')
    if len(value) > 64 or not re.fullmatch(r'[A-Za-z0-9._-]+', value):
        return None, 'Use um nome de voz válido, como `pt-BR-Standard-A`.'
    return value, None


def normalize_gcloud_rate_value(raw_value: str | float, *, default_rate: float = 1.0) -> str:
    try:
        numeric = float(str(raw_value).strip().replace(',', '.'))
    except Exception:
        numeric = float(default_rate or 1.0)
    numeric = max(0.25, min(2.0, numeric))
    return f"{numeric:.2f}".rstrip('0').rstrip('.')


def normalize_gcloud_pitch_value(raw_value: str | float, *, default_pitch: float = 0.0) -> str:
    try:
        numeric = float(str(raw_value).strip().replace(',', '.'))
    except Exception:
        numeric = float(default_pitch or 0.0)
    numeric = max(-20.0, min(20.0, numeric))
    if abs(numeric - round(numeric)) < 1e-9:
        return str(int(round(numeric)))
    return f"{numeric:.2f}".rstrip('0').rstrip('.')
