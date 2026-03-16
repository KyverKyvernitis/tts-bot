import os


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _parse_int(value: str, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_float(value: str, default: float = 0.0) -> float:
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def _parse_guild_ids(value: str) -> list[int]:
    if not value:
        return []

    result: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            pass
    return result


TOKEN = (os.getenv("DISCORD_TOKEN", "") or "").strip()

TARGET_ROLE_ID = _parse_int(os.getenv("ROLE_ID", "0"), 0)
DISABLE_TIME = _parse_int(os.getenv("DISABLE_TIME", "14400"), 14400)

TRIGGER_WORD = (os.getenv("TRIGGER_WORD", "") or "").lower().strip()
MUTE_TOGGLE_WORD = (os.getenv("MUTE_TOGGLE_WORD", "rola") or "rola").lower().strip()

TARGET_USER_ID = _parse_int(os.getenv("TARGET_USER_ID", "0"), 0)

TTS_ENABLED = _parse_bool(os.getenv("TTS_ENABLED", "true"), True)
BLOCK_VOICE_BOT_ID = _parse_int(os.getenv("BLOCK_VOICE_BOT_ID", "0"), 0)
ONLY_TTS_USER_ID = _parse_int(os.getenv("ONLY_TTS_USER_ID", "0"), 0)

PORT = _parse_int(os.getenv("PORT", "10000"), 10000)

MONGODB_URI = (os.getenv("MONGODB_URI", "") or "").strip()
MONGODB_DB = (os.getenv("MONGODB_DB", "chat_revive") or "chat_revive").strip()
MONGODB_COLLECTION = (os.getenv("MONGODB_COLLECTION", "settings") or "settings").strip()

GUILD_IDS = _parse_guild_ids(os.getenv("GUILD_IDS", ""))

ON_COLOR = 0x57F287
OFF_COLOR = 0xED4245

GTTS_DEFAULT_LANGUAGE = os.getenv("GTTS_DEFAULT_LANGUAGE", "pt-br")

GOOGLE_CLOUD_TTS_PREFIX = (os.getenv("GOOGLE_CLOUD_TTS_PREFIX", "'") or "'").strip() or "'"
GOOGLE_CLOUD_TTS_LANGUAGE_CODE = (os.getenv("GOOGLE_CLOUD_TTS_LANGUAGE_CODE", "pt-BR") or "pt-BR").strip()
GOOGLE_CLOUD_TTS_VOICE_NAME = (os.getenv("GOOGLE_CLOUD_TTS_VOICE_NAME", "pt-BR-Standard-A") or "pt-BR-Standard-A").strip()
GOOGLE_CLOUD_TTS_SPEAKING_RATE = _parse_float(os.getenv("GOOGLE_CLOUD_TTS_SPEAKING_RATE", "1.0"), 1.0)
GOOGLE_CLOUD_TTS_PITCH = _parse_float(os.getenv("GOOGLE_CLOUD_TTS_PITCH", "0.0"), 0.0)

# TTS tuning recomendado
# Mantém a call "quente" por mais tempo após falar, reduzindo reconexões
TTS_WARM_HOLD_SECONDS = _parse_int(os.getenv("TTS_WARM_HOLD_SECONDS", "30"), 30)

# Tempo máximo de inatividade antes de avaliar desconexão
TTS_IDLE_DISCONNECT_SECONDS = _parse_int(os.getenv("TTS_IDLE_DISCONNECT_SECONDS", "240"), 240)

# Cache curto de áudio para textos repetidos
TTS_AUDIO_CACHE_SIZE = _parse_int(os.getenv("TTS_AUDIO_CACHE_SIZE", "256"), 256)

# Tempo de vida do cache de áudio
TTS_AUDIO_CACHE_TTL_SECONDS = _parse_int(os.getenv("TTS_AUDIO_CACHE_TTL_SECONDS", "1800"), 1800)

# Pasta raiz para todos os arquivos temporários do TTS
TTS_TEMP_DIR = (os.getenv("TTS_TEMP_DIR", os.path.join(BASE_DIR, "tmp_audio")) or os.path.join(BASE_DIR, "tmp_audio")).strip()

# Limites globais da pasta tmp_audio
TTS_TEMP_MAX_MB = _parse_int(os.getenv("TTS_TEMP_MAX_MB", "256"), 256)
TTS_TEMP_MAX_FILES = _parse_int(os.getenv("TTS_TEMP_MAX_FILES", "256"), 256)

# Logs detalhados de debug do TTS
TTS_DEBUG_LOGS = _parse_bool(os.getenv("TTS_DEBUG_LOGS", "false"), False)

# Máximo de itens por fila de guild
TTS_QUEUE_MAXSIZE = _parse_int(os.getenv("TTS_QUEUE_MAXSIZE", "20"), 20)

# Quantas sínteses de áudio podem acontecer ao mesmo tempo no processo
TTS_SYNTH_CONCURRENCY = _parse_int(os.getenv("TTS_SYNTH_CONCURRENCY", "2"), 2)

# Tempo máximo de espera da síntese Edge antes de fallback
TTS_EDGE_TIMEOUT_SECONDS = _parse_int(os.getenv("TTS_EDGE_TIMEOUT_SECONDS", "10"), 10)

# Concorrência máxima do gTTS
TTS_GTTS_CONCURRENCY = _parse_int(os.getenv("TTS_GTTS_CONCURRENCY", "1"), 1)

# FFmpeg enxuto para reprodução
TTS_FFMPEG_BEFORE_OPTIONS = (os.getenv("TTS_FFMPEG_BEFORE_OPTIONS", "-nostdin") or "-nostdin").strip()
TTS_FFMPEG_OPTIONS = (os.getenv("TTS_FFMPEG_OPTIONS", "-vn -loglevel error") or "-vn -loglevel error").strip()
