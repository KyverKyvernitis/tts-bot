import re
import unicodedata


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
