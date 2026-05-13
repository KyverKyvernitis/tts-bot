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

TRIGGER_WORD = (os.getenv("TRIGGER_WORD", "pinto") or "pinto").lower().strip()
MUTE_TOGGLE_WORD = (os.getenv("MUTE_TOGGLE_WORD", "rola") or "rola").lower().strip()

TARGET_USER_ID = _parse_int(os.getenv("TARGET_USER_ID", "0"), 0)

TTS_ENABLED = _parse_bool(os.getenv("TTS_ENABLED", "true"), True)
BLOCK_VOICE_BOT_ID = _parse_int(os.getenv("BLOCK_VOICE_BOT_ID", "0"), 0)
ONLY_TTS_USER_ID = _parse_int(os.getenv("ONLY_TTS_USER_ID", "0"), 0)

# Usuário que recebe DM quando a primeira conexão de voz de uma guild falha no boot.
# Se vazio, o bot tenta usar o dono do aplicativo Discord automaticamente.
TTS_VOICE_FAILURE_DM_USER_ID = _parse_int(
    os.getenv("TTS_VOICE_FAILURE_DM_USER_ID", os.getenv("BOT_OWNER_ID", os.getenv("OWNER_ID", "0"))),
    0,
)

# Reentra automaticamente na última call lembrada depois de restart/queda.
# Desative apenas se quiser impedir qualquer restore automático de voz.
TTS_VOICE_AUTO_RESTORE_ENABLED = _parse_bool(os.getenv("TTS_VOICE_AUTO_RESTORE_ENABLED", "true"), True)

PORT = _parse_int(os.getenv("PORT", "10000"), 10000)

MONGODB_URI = (os.getenv("MONGODB_URI", "") or "").strip()
MONGODB_DB = (os.getenv("MONGODB_DB", "chat_revive") or "chat_revive").strip()
MONGODB_COLLECTION = (os.getenv("MONGODB_COLLECTION", "settings") or "settings").strip()

GUILD_IDS = _parse_guild_ids(os.getenv("GUILD_IDS", ""))

# -----------------------------------------------------------------------------
# CallKeeper — bots auxiliares que mantêm uma call ocupada em 1 servidor
# -----------------------------------------------------------------------------
CALLKEEPER_GUILD_ID = _parse_int(os.getenv("CALLKEEPER_GUILD_ID", "0"), 0)
CALLKEEPER_CHANNEL_ID = _parse_int(os.getenv("CALLKEEPER_CHANNEL_ID", "0"), 0)
CALLKEEPER_BOT_TOKENS = [
    token
    for token in (
        (os.getenv("CALLKEEPER_BOT_1_TOKEN", "") or "").strip(),
        (os.getenv("CALLKEEPER_BOT_2_TOKEN", "") or "").strip(),
        (os.getenv("CALLKEEPER_BOT_3_TOKEN", "") or "").strip(),
    )
    if token
]
CALLKEEPER_WATCHDOG_INTERVAL_SECONDS = _parse_float(os.getenv("CALLKEEPER_WATCHDOG_INTERVAL_SECONDS", "1.0"), 1.0)
CALLKEEPER_EVENT_DEBOUNCE_SECONDS = _parse_float(os.getenv("CALLKEEPER_EVENT_DEBOUNCE_SECONDS", "0.20"), 0.20)
CALLKEEPER_DISCONNECTED_BOT_COOLDOWN_SECONDS = _parse_float(os.getenv("CALLKEEPER_DISCONNECTED_BOT_COOLDOWN_SECONDS", "3.0"), 3.0)

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

# Tamanho máximo de texto para considerar cacheável
TTS_CACHEABLE_TEXT_MAX_LENGTH = _parse_int(os.getenv("TTS_CACHEABLE_TEXT_MAX_LENGTH", "320"), 320)

# Intervalo mínimo entre podas completas da pasta tmp_audio
TTS_TEMP_PRUNE_INTERVAL_SECONDS = _parse_int(os.getenv("TTS_TEMP_PRUNE_INTERVAL_SECONDS", "20"), 20)

# Warmup conservador no boot do cog TTS
TTS_BOOT_WARMUP_ENABLED = _parse_bool(os.getenv("TTS_BOOT_WARMUP_ENABLED", "true"), True)

# Alertas informativos de engine (sem cooldown/fallback automático da engine)
TTS_ENGINE_ALERT_COOLDOWN_SECONDS = _parse_int(os.getenv("TTS_ENGINE_ALERT_COOLDOWN_SECONDS", "900"), 900)
TTS_ENGINE_FAILURE_ALERT_THRESHOLD = _parse_int(os.getenv("TTS_ENGINE_FAILURE_ALERT_THRESHOLD", "3"), 3)
TTS_ENGINE_SLOW_WARN_SECONDS = _parse_float(os.getenv("TTS_ENGINE_SLOW_WARN_SECONDS", "8.0"), 8.0)

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
TTS_SYNTH_CONCURRENCY = _parse_int(os.getenv("TTS_SYNTH_CONCURRENCY", "3"), 3)

# Tempo máximo de espera da síntese Edge antes de fallback
TTS_EDGE_TIMEOUT_SECONDS = _parse_int(os.getenv("TTS_EDGE_TIMEOUT_SECONDS", "10"), 10)

# Concorrência máxima do gTTS
TTS_GTTS_CONCURRENCY = _parse_int(os.getenv("TTS_GTTS_CONCURRENCY", "1"), 1)

# FFmpeg enxuto para reprodução
TTS_FFMPEG_BEFORE_OPTIONS = (os.getenv("TTS_FFMPEG_BEFORE_OPTIONS", "-nostdin") or "-nostdin").strip()
TTS_FFMPEG_OPTIONS = (os.getenv("TTS_FFMPEG_OPTIONS", "-vn -loglevel error") or "-vn -loglevel error").strip()

# -----------------------------------------------------------------------------
# Música — player leve integrado ao TTS
# -----------------------------------------------------------------------------
MUSIC_DEFAULT_VOLUME = _parse_float(os.getenv("MUSIC_DEFAULT_VOLUME", "0.55"), 0.55)
MUSIC_TTS_VOLUME = _parse_float(os.getenv("MUSIC_TTS_VOLUME", "1.0"), 1.0)
# Quando TTS toca junto com música local/yt-dlp, reduza a música para 5%
# do volume normal e restaure automaticamente ao fim do TTS.
MUSIC_TTS_LOCAL_DUCK_PERCENT = max(0.0, min(100.0, _parse_float(os.getenv("MUSIC_TTS_LOCAL_DUCK_PERCENT", "5"), 5.0)))
# Para música via Lavalink, o TTS usa o próprio node: pausa a faixa atual, toca
# o áudio curto e restaura a música na posição anterior.
MUSIC_LAVALINK_TTS_PAUSE_ENABLED = _parse_bool(os.getenv("MUSIC_LAVALINK_TTS_PAUSE_ENABLED", "true"), True)
MUSIC_LAVALINK_TTS_PAUSE_GRACE_SECONDS = max(0.2, _parse_float(os.getenv("MUSIC_LAVALINK_TTS_PAUSE_GRACE_SECONDS", "0.35"), 0.35))
MUSIC_TTS_PUBLIC_BASE_URL = (
    os.getenv("MUSIC_TTS_PUBLIC_BASE_URL", os.getenv("PUBLIC_BASE_URL", os.getenv("WEB_PUBLIC_BASE_URL", os.getenv("RENDER_EXTERNAL_URL", ""))))
    or ""
).strip().rstrip("/")
# URL local que o Lavalink usa para buscar TTS gerado pelo próprio bot.
# O padrão usa a porta do webserver/waitress no mesmo host, evitando DNS, HTTPS e
# proxy externo. A URL pública continua como fallback.
MUSIC_TTS_INTERNAL_BASE_URL = (
    os.getenv("MUSIC_TTS_INTERNAL_BASE_URL", f"http://127.0.0.1:{os.getenv('PORT', '10000')}")
    or ""
).strip().rstrip("/")
MUSIC_LAVALINK_TTS_INTERNAL_FIRST = _parse_bool(os.getenv("MUSIC_LAVALINK_TTS_INTERNAL_FIRST", "true"), True)
MUSIC_LAVALINK_TTS_URL_PROBE_TIMEOUT_SECONDS = max(0.25, _parse_float(os.getenv("MUSIC_LAVALINK_TTS_URL_PROBE_TIMEOUT_SECONDS", "1.75"), 1.75))
MUSIC_LAVALINK_TTS_FILE_FALLBACK = _parse_bool(os.getenv("MUSIC_LAVALINK_TTS_FILE_FALLBACK", "false"), False)
MUSIC_LAVALINK_TTS_URL_TTL_SECONDS = max(30, _parse_int(os.getenv("MUSIC_LAVALINK_TTS_URL_TTL_SECONDS", "240"), 240))
# Formato preferido para o áudio temporário de TTS usado pelo Lavalink.
# OGG/Opus é menor e costuma carregar mais rápido que MP3; MP3 fica como fallback
# por compatibilidade quando a conversão ou o loadtracks do Lavalink falhar.
MUSIC_TTS_AUDIO_FORMAT = (os.getenv("MUSIC_TTS_AUDIO_FORMAT", "opus") or "opus").strip().lower()
MUSIC_TTS_AUDIO_FALLBACK_FORMAT = (os.getenv("MUSIC_TTS_AUDIO_FALLBACK_FORMAT", "mp3") or "mp3").strip().lower()
MUSIC_TTS_OPUS_BITRATE = (os.getenv("MUSIC_TTS_OPUS_BITRATE", "48k") or "48k").strip()
MUSIC_TTS_OPUS_SAMPLE_RATE = max(8000, _parse_int(os.getenv("MUSIC_TTS_OPUS_SAMPLE_RATE", "48000"), 48000))
MUSIC_TTS_OPUS_CHANNELS = min(2, max(1, _parse_int(os.getenv("MUSIC_TTS_OPUS_CHANNELS", "1"), 1)))
MUSIC_TTS_CONVERT_TIMEOUT_SECONDS = max(2.0, _parse_float(os.getenv("MUSIC_TTS_CONVERT_TIMEOUT_SECONDS", "8.0"), 8.0))
# Suavização das transições do TTS tocado pelo Lavalink.
# O áudio curto recebe silêncio/fade antes de ser publicado, e a música faz uma
# rampa breve de volume antes/depois da interrupção para evitar clicks/flicker.
MUSIC_TTS_PREROLL_SILENCE_MS = max(0, _parse_int(os.getenv("MUSIC_TTS_PREROLL_SILENCE_MS", "140"), 140))
MUSIC_TTS_POSTROLL_SILENCE_MS = max(0, _parse_int(os.getenv("MUSIC_TTS_POSTROLL_SILENCE_MS", "180"), 180))
MUSIC_TTS_FADE_IN_MS = max(0, _parse_int(os.getenv("MUSIC_TTS_FADE_IN_MS", "45"), 45))
MUSIC_TTS_FADE_OUT_MS = max(0, _parse_int(os.getenv("MUSIC_TTS_FADE_OUT_MS", "70"), 70))
MUSIC_TTS_MP3_BITRATE = (os.getenv("MUSIC_TTS_MP3_BITRATE", "96k") or "96k").strip()
MUSIC_TTS_RESUME_SEEK_AHEAD_MS = max(0, _parse_int(os.getenv("MUSIC_TTS_RESUME_SEEK_AHEAD_MS", "120"), 120))
MUSIC_TTS_LAVALINK_VOLUME_RAMP_ENABLED = _parse_bool(os.getenv("MUSIC_TTS_LAVALINK_VOLUME_RAMP_ENABLED", "true"), True)
MUSIC_TTS_LAVALINK_VOLUME_RAMP_MS = max(0, _parse_int(os.getenv("MUSIC_TTS_LAVALINK_VOLUME_RAMP_MS", "180"), 180))
MUSIC_TTS_LAVALINK_RAMP_FLOOR_PERCENT = max(0, min(100, _parse_int(os.getenv("MUSIC_TTS_LAVALINK_RAMP_FLOOR_PERCENT", "5"), 5)))
MUSIC_IDLE_DISCONNECT_SECONDS = _parse_int(os.getenv("MUSIC_IDLE_DISCONNECT_SECONDS", "120"), 120)
MUSIC_QUEUE_MAXSIZE = min(100, max(1, _parse_int(os.getenv("MUSIC_QUEUE_MAXSIZE", "100"), 100)))
MUSIC_MAX_PLAYLIST_ITEMS = min(100, max(1, _parse_int(os.getenv("MUSIC_MAX_PLAYLIST_ITEMS", "100"), 100)))
MUSIC_SEARCH_RESULTS = _parse_int(os.getenv("MUSIC_SEARCH_RESULTS", "5"), 5)
MUSIC_YTDLP_TIMEOUT_SECONDS = _parse_float(os.getenv("MUSIC_YTDLP_TIMEOUT_SECONDS", "20"), 20.0)
MUSIC_EXTRACT_SOCKET_TIMEOUT_SECONDS = max(3.0, _parse_float(os.getenv("MUSIC_EXTRACT_SOCKET_TIMEOUT_SECONDS", "8"), 8.0))
MUSIC_YTDLP_RETRIES = max(0, _parse_int(os.getenv("MUSIC_YTDLP_RETRIES", "1"), 1))
MUSIC_FRAGMENT_RETRIES = max(0, _parse_int(os.getenv("MUSIC_FRAGMENT_RETRIES", "1"), 1))
MUSIC_EXTRACTOR_RETRIES = max(0, _parse_int(os.getenv("MUSIC_EXTRACTOR_RETRIES", "1"), 1))
MUSIC_PLAYLIST_LAZY_LOAD = _parse_bool(os.getenv("MUSIC_PLAYLIST_LAZY_LOAD", "true"), True)
MUSIC_STREAM_START_RETRIES = max(0, _parse_int(os.getenv("MUSIC_STREAM_START_RETRIES", "1"), 1))
MUSIC_HISTORY_MAXSIZE = _parse_int(os.getenv("MUSIC_HISTORY_MAXSIZE", "25"), 25)
MUSIC_CONTROL_VOTE_SECONDS = _parse_int(os.getenv("MUSIC_CONTROL_VOTE_SECONDS", "45"), 45)
MUSIC_YTDLP_COOKIES_FILE = (os.getenv("MUSIC_YTDLP_COOKIES_FILE", os.getenv("YTDLP_COOKIES_FILE", "")) or "").strip()
MUSIC_API_SEARCH_ENABLED = _parse_bool(os.getenv("MUSIC_API_SEARCH_ENABLED", "true"), True)
MUSIC_API_TIMEOUT_SECONDS = _parse_float(os.getenv("MUSIC_API_TIMEOUT_SECONDS", "5.0"), 5.0)
MUSIC_METADATA_CACHE_TTL_SECONDS = _parse_int(os.getenv("MUSIC_METADATA_CACHE_TTL_SECONDS", "300"), 300)
MUSIC_LAVALINK_SEARCH_CACHE_TTL_SECONDS = max(0, _parse_int(os.getenv("MUSIC_LAVALINK_SEARCH_CACHE_TTL_SECONDS", "90"), 90))
MUSIC_STREAM_CACHE_TTL_SECONDS = _parse_int(os.getenv("MUSIC_STREAM_CACHE_TTL_SECONDS", "480"), 480)
MUSIC_CACHE_MAX_ITEMS = _parse_int(os.getenv("MUSIC_CACHE_MAX_ITEMS", "160"), 160)
MUSIC_PREFETCH_NEXT = _parse_bool(os.getenv("MUSIC_PREFETCH_NEXT", "true"), True)
# Em VPS fraca, pré-resolver a próxima música imediatamente pode disputar CPU
# com o áudio atual. Por padrão, o prefetch só roda depois de alguns segundos
# e, em músicas com duração conhecida, perto do fim.
MUSIC_PREFETCH_MIN_DELAY_SECONDS = max(0.0, _parse_float(os.getenv("MUSIC_PREFETCH_MIN_DELAY_SECONDS", "18"), 18.0))
MUSIC_PREFETCH_BEFORE_END_SECONDS = max(5.0, _parse_float(os.getenv("MUSIC_PREFETCH_BEFORE_END_SECONDS", "45"), 45.0))
MUSIC_LIMITER_ENABLED = _parse_bool(os.getenv("MUSIC_LIMITER_ENABLED", "true"), True)
MUSIC_YTDLP_FORMAT = (
    os.getenv(
        "MUSIC_YTDLP_FORMAT",
        # Alta qualidade real: prioriza Opus áudio-only, depois M4A, e só
        # então fallbacks amplos. Opus reduz reencode/latência no Discord;
        # M4A costuma ser a alternativa estável quando Opus não existe.
        "bestaudio[acodec=opus][vcodec=none]/bestaudio[ext=webm][vcodec=none]/bestaudio[ext=m4a][vcodec=none]/bestaudio[vcodec=none]/bestaudio/best",
    )
    or "bestaudio/best"
).strip()
YOUTUBE_API_KEY = (os.getenv("YOUTUBE_API_KEY", os.getenv("GOOGLE_YOUTUBE_API_KEY", "")) or "").strip()
SPOTIFY_CLIENT_ID = (os.getenv("SPOTIFY_CLIENT_ID", "") or "").strip()
SPOTIFY_CLIENT_SECRET = (os.getenv("SPOTIFY_CLIENT_SECRET", "") or "").strip()
# Refresh token de usuário para playlists/álbuns privados ou colaborativos do Spotify.
# Client Credentials continua sendo usado para busca/faixas públicas quando não houver refresh token.
SPOTIFY_REFRESH_TOKEN = (os.getenv("SPOTIFY_REFRESH_TOKEN", "") or "").strip()
SPOTIFY_REDIRECT_URI = (os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback") or "http://127.0.0.1:8888/callback").strip()
SPOTIFY_MARKET = (os.getenv("SPOTIFY_MARKET", "BR") or "BR").strip().upper()
# Fallback público/não oficial para ler metadata de links públicos do Spotify
# quando a Web API oficial retornar 403 para playlists públicas em apps novos.
SPOTIFY_PUBLIC_FALLBACK_ENABLED = _parse_bool(os.getenv("SPOTIFY_PUBLIC_FALLBACK_ENABLED", "true"), True)
SPOTIFY_PUBLIC_FALLBACK_MAX_TRACKS = max(1, _parse_int(os.getenv("SPOTIFY_PUBLIC_FALLBACK_MAX_TRACKS", "100"), 100))
DEEZER_API_ENABLED = _parse_bool(os.getenv("DEEZER_API_ENABLED", "false"), False)
SOUNDCLOUD_API_ENABLED = _parse_bool(os.getenv("SOUNDCLOUD_API_ENABLED", "false"), False)
SOUNDCLOUD_API_TOKEN = (os.getenv("SOUNDCLOUD_API_TOKEN", "") or "").strip()
SOUNDCLOUD_CLIENT_ID = (os.getenv("SOUNDCLOUD_CLIENT_ID", "") or "").strip()
SOUNDCLOUD_API_BASE_URL = (os.getenv("SOUNDCLOUD_API_BASE_URL", "https://api.soundcloud.com/tracks") or "https://api.soundcloud.com/tracks").strip()
MUSIC_FFMPEG_BEFORE_OPTIONS = (
    os.getenv("MUSIC_FFMPEG_BEFORE_OPTIONS", "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 -reconnect_on_network_error 1 -reconnect_on_http_error 403,404,408,429,5xx -reconnect_delay_max 5 -rw_timeout 10000000 -multiple_requests 1")
    or "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 -reconnect_on_network_error 1 -reconnect_on_http_error 403,404,408,429,5xx -reconnect_delay_max 5 -rw_timeout 10000000 -multiple_requests 1"
).strip()
MUSIC_FFMPEG_OPTIONS = (
    os.getenv("MUSIC_FFMPEG_OPTIONS", "-vn -sn -dn -loglevel error -ar 48000 -ac 2")
    or "-vn -sn -dn -loglevel error -ar 48000 -ac 2"
).strip()
MUSIC_TTS_FFMPEG_OPTIONS = (os.getenv("MUSIC_TTS_FFMPEG_OPTIONS", "-vn -loglevel error") or "-vn -loglevel error").strip()
MUSIC_MAX_GLOBAL_EXTRACTORS = max(1, _parse_int(os.getenv("MUSIC_MAX_GLOBAL_EXTRACTORS", "1"), 1))
MUSIC_MAX_GLOBAL_PREFETCH = max(0, _parse_int(os.getenv("MUSIC_MAX_GLOBAL_PREFETCH", "1"), 1))
MUSIC_DISABLE_PREFETCH_ABOVE_PLAYERS = max(0, _parse_int(os.getenv("MUSIC_DISABLE_PREFETCH_ABOVE_PLAYERS", "2"), 2))
# Qualidade dinâmica: com só 1 servidor tocando, usa melhor áudio-only
# disponível sem teto de abr; com 2+ servidores, limita bitrate para poupar
# CPU/rede da VPS.
MUSIC_AUDIO_MODE = (os.getenv("MUSIC_AUDIO_MODE", "auto") or "auto").strip().lower()
MUSIC_HIGH_QUALITY_MAX_ACTIVE_GUILDS = max(1, _parse_int(os.getenv("MUSIC_HIGH_QUALITY_MAX_ACTIVE_GUILDS", "1"), 1))
MUSIC_HIGH_QUALITY_MAX_ABR = max(96, _parse_int(os.getenv("MUSIC_HIGH_QUALITY_MAX_ABR", "256"), 256))  # mantido para compat/env antigo
MUSIC_MAX_AUDIO_BITRATE_STABLE = max(64, _parse_int(os.getenv("MUSIC_MAX_AUDIO_BITRATE_STABLE", "160"), 160))
MUSIC_HEAVY_LOAD_MAX_ABR = max(64, _parse_int(os.getenv("MUSIC_HEAVY_LOAD_MAX_ABR", "128"), 128))
MUSIC_AUTO_BITRATE_ENABLED = _parse_bool(os.getenv("MUSIC_AUTO_BITRATE_ENABLED", "true"), True)
MUSIC_AUTO_BITRATE_MAX = max(8000, _parse_int(os.getenv("MUSIC_AUTO_BITRATE_MAX", "384000"), 384000))
MUSIC_AUTO_BITRATE_MIN_GAIN = max(0, _parse_int(os.getenv("MUSIC_AUTO_BITRATE_MIN_GAIN", "16000"), 16000))
MUSIC_PANEL_UPDATE_THROTTLE_SECONDS = max(0.05, _parse_float(os.getenv("MUSIC_PANEL_UPDATE_THROTTLE_SECONDS", "2.0"), 2.0))
MUSIC_VOICE_STATUS_ENABLED = _parse_bool(os.getenv("MUSIC_VOICE_STATUS_ENABLED", "true"), True)
MUSIC_VOICE_STATUS_TEMPLATE = (
    os.getenv(
        "MUSIC_VOICE_STATUS_TEMPLATE",
        "{source_emoji} <a:2574_Rainbow_Heart:1381731924162384023> {title}, {author} ({requester})",
    )
    or "{source_emoji} <a:2574_Rainbow_Heart:1381731924162384023> {title}, {author} ({requester})"
).strip()
MUSIC_VOICE_STATUS_IDLE = (os.getenv("MUSIC_VOICE_STATUS_IDLE", "") or "").strip()
MUSIC_VOICE_STATUS_UPDATE_INTERVAL_SECONDS = max(15.0, _parse_float(os.getenv("MUSIC_VOICE_STATUS_UPDATE_INTERVAL_SECONDS", "60"), 60.0))
MUSIC_MIN_LINK_METADATA_CONFIDENCE = (os.getenv("MUSIC_MIN_LINK_METADATA_CONFIDENCE", "medium") or "medium").strip().lower()
MUSIC_MAX_DURATION_MISMATCH_SECONDS = max(0.0, _parse_float(os.getenv("MUSIC_MAX_DURATION_MISMATCH_SECONDS", "45"), 45.0))
MUSIC_MAX_DURATION_MISMATCH_RATIO = max(0.0, _parse_float(os.getenv("MUSIC_MAX_DURATION_MISMATCH_RATIO", "0.25"), 0.25))
MUSIC_REJECT_WEAK_LINK_MATCHES = _parse_bool(os.getenv("MUSIC_REJECT_WEAK_LINK_MATCHES", "true"), True)
# Mirrors do LavaSrc usados para transformar metadata de Spotify/YouTube em áudio tocável.
# Spotify direto/spsearch fica fora do padrão porque o LavaSrc 4.8.x pode falhar com 403.
MUSIC_LAVASRC_MIRROR_PREFIXES = (os.getenv("MUSIC_LAVASRC_MIRROR_PREFIXES", "scsearch") or "scsearch").strip()

# Lavalink — suporte invisível/diagnóstico para migração futura do player.
# Patch atual NÃO troca o player real: MUSIC_BACKEND permanece local por padrão.
MUSIC_BACKEND = (os.getenv("MUSIC_BACKEND", "local") or "local").strip().lower()
LAVALINK_ENABLED = _parse_bool(os.getenv("LAVALINK_ENABLED", "false"), False)
LAVALINK_MODE = (os.getenv("LAVALINK_MODE", "off") or "off").strip().lower()
LAVALINK_HOST = (os.getenv("LAVALINK_HOST", "") or "").strip()
LAVALINK_PORT = _parse_int(os.getenv("LAVALINK_PORT", "2333"), 2333)
LAVALINK_PASSWORD = (os.getenv("LAVALINK_PASSWORD", "") or "").strip()
LAVALINK_SECURE = _parse_bool(os.getenv("LAVALINK_SECURE", "false"), False)
LAVALINK_NODE_NAME = (os.getenv("LAVALINK_NODE_NAME", "main") or "main").strip() or "main"
LAVALINK_TIMEOUT_SECONDS = max(2.0, _parse_float(os.getenv("LAVALINK_TIMEOUT_SECONDS", "8.0"), 8.0))
# Node de áudio compatível com Lavalink API.
# Qualquer valor legado de MUSIC_NODE_PROVIDER cai para Lavalink.
_MUSIC_NODE_PROVIDER_RAW = (os.getenv("MUSIC_NODE_PROVIDER", "lavalink") or "lavalink").strip().lower()
MUSIC_NODE_PROVIDER = _MUSIC_NODE_PROVIDER_RAW if _MUSIC_NODE_PROVIDER_RAW in {"lavalink", "auto"} else "lavalink"
AUDIO_NODE_FAILURE_COOLDOWN_SECONDS = max(5.0, _parse_float(os.getenv("AUDIO_NODE_FAILURE_COOLDOWN_SECONDS", "45"), 45.0))
AUDIO_NODE_STARTUP_WAIT_SECONDS = max(0.0, _parse_float(os.getenv("AUDIO_NODE_STARTUP_WAIT_SECONDS", "90"), 90.0))
AUDIO_NODE_STARTUP_WAIT_REQUIRED = _parse_bool(os.getenv("AUDIO_NODE_STARTUP_WAIT_REQUIRED", "true"), True)

# -----------------------------------------------------------------------------
# DevAI — analisa logs, pede correção para providers gratuitos/fallbacks e
# oferece patch .zip pelo webhook. Nunca aplica patch automaticamente.
# -----------------------------------------------------------------------------

def _parse_csv(value: str) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


DEVAI_ENABLED = _parse_bool(os.getenv("DEVAI_ENABLED", "false"), False)
DEVAI_WEBHOOK_URL = (os.getenv("DEVAI_WEBHOOK_URL", os.getenv("ALERT_WEBHOOK_URL", "")) or "").strip()
DEVAI_COMMENT_CHANNEL_ID = _parse_int(os.getenv("DEVAI_COMMENT_CHANNEL_ID", "0"), 0)
DEVAI_OWNER_IDS = _parse_guild_ids(os.getenv("DEVAI_OWNER_IDS", os.getenv("BOT_OWNER_ID", os.getenv("OWNER_ID", ""))))

# Ordem padrão atualizada (Apr/2026): Groq primeiro porque é o mais rápido +
# generoso no free tier; Gemini 2.5 Flash em seguida (1M context, ainda free);
# OpenRouter como passe-livre pra Qwen3-Coder free; Cerebras pra modelos 70B+
# rápidos (1M tokens/dia free); Cloudflare/HF como fallbacks; Pollinations
# como último recurso (qualidade variável).
# Provider order padrão (geração de patch). Inclui modelos pequenos no
# final pra evitar gerar patches alucinando.
DEVAI_PROVIDER_ORDER = _parse_csv(
    os.getenv("DEVAI_PROVIDER_ORDER", "groq,gemini,openrouter,cerebras,cloudflare,huggingface,pollinations")
)
# Provider order EXCLUSIVO pra review de patches. Modelos médios (Qwen-30B,
# Llama-32B em Cloudflare, Pollinations) alucinam removals que não existem,
# então ficam fora dessa lista. Se nada nesta cadeia responder, melhor o
# fallback simples ("DevAI não conseguiu chamar nenhum provider") do que
# inventar problemas inexistentes que o dono vai correr atrás.
DEVAI_REVIEW_PROVIDER_ORDER = _parse_csv(
    os.getenv("DEVAI_REVIEW_PROVIDER_ORDER", "gemini,groq,openrouter,cerebras")
)
# Timeout 60s era apertado pro Gemini 2.5 Pro (raciocina mais que Flash).
# Pro free tier costuma responder em 20-50s, mas pode chegar a 90s pra
# diffs grandes. 120s dá margem confortável.
DEVAI_PROVIDER_TIMEOUT_SECONDS = _parse_int(os.getenv("DEVAI_PROVIDER_TIMEOUT_SECONDS", "120"), 120)
# Output tokens. Gemini 2.5 Pro suporta até 65k de output. Subindo de 16k →
# 32k pra caber arquivos médios inteiros (cog.py do tts-bot tem ~1900 linhas
# que é ~30k tokens). Modelos menores que não suportam tanto fazem cap
# automático em `_output_tokens_for`.
DEVAI_MAX_OUTPUT_TOKENS = _parse_int(os.getenv("DEVAI_MAX_OUTPUT_TOKENS", "32000"), 32000)
DEVAI_TEMPERATURE = _parse_float(os.getenv("DEVAI_TEMPERATURE", "0.15"), 0.15)
# Tenta UMA rodada de "repair" (re-pedir JSON pra IA) quando o JSON vem inválido
# ou o Python não compila. Modelos pequenos quase sempre acertam na 2ª tentativa.
DEVAI_REPAIR_ENABLED = _parse_bool(os.getenv("DEVAI_REPAIR_ENABLED", "true"), True)

# Chaves já usadas por outros módulos podem existir no .env; a DevAI só lê aqui.
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY", "") or "").strip()
GROQ_API_KEY = (os.getenv("GROQ_API_KEY", "") or "").strip()
CLOUDFLARE_API_TOKEN = (os.getenv("CLOUDFLARE_API_TOKEN", "") or "").strip()
CLOUDFLARE_ACCOUNT_ID = (os.getenv("CLOUDFLARE_ACCOUNT_ID", "") or "").strip()
HUGGINGFACE_API_KEY = (os.getenv("HUGGINGFACE_API_KEY", "") or "").strip()
POLLINATIONS_API_KEY = (os.getenv("POLLINATIONS_API_KEY", "") or "").strip()
# Novas chaves pros providers adicionados em Apr/2026.
OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY", "") or "").strip()
CEREBRAS_API_KEY = (os.getenv("CEREBRAS_API_KEY", "") or "").strip()

# Gemini 2.5 Pro: 5 RPM, 100 RPD, 250K TPM no free tier (Apr/2026). Mais
# lento que Flash mas raciocina muito melhor — crucial pra patch review onde
# Flash estava alucinando "removeu import os" sobre código onde o import
# está claramente presente. Pro custa 1 RPM contra 2 RPM do Flash, mas com
# 100 RPD ainda comporta dezenas de reviews por dia.
DEVAI_GEMINI_MODEL = (os.getenv("DEVAI_GEMINI_MODEL", "gemini-2.5-pro") or "gemini-2.5-pro").strip()
DEVAI_GROQ_BASE_URL = (os.getenv("DEVAI_GROQ_BASE_URL", "https://api.groq.com/openai/v1") or "https://api.groq.com/openai/v1").strip()
DEVAI_GROQ_MODEL = (os.getenv("DEVAI_GROQ_MODEL", "openai/gpt-oss-120b") or "openai/gpt-oss-120b").strip()
# OpenRouter free dá acesso a Qwen3-Coder e DeepSeek R1 sem cartão.
# Use ":free" no sufixo pra forçar tier gratuito. Limites ~20 RPM, ~200 RPD/modelo.
DEVAI_OPENROUTER_BASE_URL = (os.getenv("DEVAI_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1") or "https://openrouter.ai/api/v1").strip()
DEVAI_OPENROUTER_MODEL = (os.getenv("DEVAI_OPENROUTER_MODEL", "qwen/qwen3-coder:free") or "qwen/qwen3-coder:free").strip()
DEVAI_OPENROUTER_REFERER = (os.getenv("DEVAI_OPENROUTER_REFERER", "") or "").strip()
# Cerebras: `qwen-3-32b` foi deprecado em 16/02/2026. Production models
# atuais (Apr/2026): `llama3.1-8b` (estável, free tier viável), `gpt-oss-120b`
# (rate limits reduzidos por alta demanda), `zai-glm-4.7` (idem).
# Llama 3.1 8B é menos capaz que 32B, mas em review de patch o que importa
# é seguir as regras do system prompt — modelos pequenos com prompt forte
# funcionam ok se a regra "cite a linha do diff antes de afirmar" for
# respeitada. Se preferir mais qualidade, troque pra `gpt-oss-120b` no .env
# (mas pode dar 503 por sobrecarga).
DEVAI_CEREBRAS_BASE_URL = (os.getenv("DEVAI_CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1") or "https://api.cerebras.ai/v1").strip()
DEVAI_CEREBRAS_MODEL = (os.getenv("DEVAI_CEREBRAS_MODEL", "llama3.1-8b") or "llama3.1-8b").strip()
DEVAI_CLOUDFLARE_BASE_URL = (os.getenv("DEVAI_CLOUDFLARE_BASE_URL", "") or "").strip()
# Llama-3.1-8B é fraco demais pra retornar arquivo Python completo.
# Qwen2.5-Coder-32B é o estado-da-arte open-source pra código no Workers AI.
# Alternativas: "@cf/openai/gpt-oss-120b" ou "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b".
DEVAI_CLOUDFLARE_MODEL = (os.getenv("DEVAI_CLOUDFLARE_MODEL", "@cf/qwen/qwen2.5-coder-32b-instruct") or "@cf/qwen/qwen2.5-coder-32b-instruct").strip()
DEVAI_HUGGINGFACE_BASE_URL = (os.getenv("DEVAI_HUGGINGFACE_BASE_URL", "https://router.huggingface.co/v1") or "https://router.huggingface.co/v1").strip()
# Qwen3-Coder MoE é mais rápido que o 2.5-Coder e melhor em multi-arquivo.
# Se quiser o mais poderoso: "deepseek-ai/DeepSeek-V3.2".
DEVAI_HUGGINGFACE_MODEL = (os.getenv("DEVAI_HUGGINGFACE_MODEL", "Qwen/Qwen3-Coder-30B-A3B-Instruct") or "Qwen/Qwen3-Coder-30B-A3B-Instruct").strip()
DEVAI_POLLINATIONS_BASE_URL = (os.getenv("DEVAI_POLLINATIONS_BASE_URL", "https://gen.pollinations.ai/v1") or "https://gen.pollinations.ai/v1").strip()
# `openclaw` foi removido da Pollinations entre Apr/2026. `openai-large` (GPT-4o)
# ou `mistral` continuam disponíveis. Verifique em https://text.pollinations.ai/models.
DEVAI_POLLINATIONS_MODEL = (os.getenv("DEVAI_POLLINATIONS_MODEL", "openai-large") or "openai-large").strip()

DEVAI_LOG_PATHS = (os.getenv("DEVAI_LOG_PATHS", "logs/*.log,bot.log,logs/bot.log,logs/updater.log") or "logs/*.log,bot.log,logs/bot.log,logs/updater.log").strip()
DEVAI_SCAN_EXISTING_LOGS_ON_BOOT = _parse_bool(os.getenv("DEVAI_SCAN_EXISTING_LOGS_ON_BOOT", "false"), False)
DEVAI_POLL_INTERVAL_SECONDS = _parse_float(os.getenv("DEVAI_POLL_INTERVAL_SECONDS", "8.0"), 8.0)
DEVAI_COOLDOWN_SECONDS = _parse_int(os.getenv("DEVAI_COOLDOWN_SECONDS", "300"), 300)
DEVAI_MAX_LOG_LINES = _parse_int(os.getenv("DEVAI_MAX_LOG_LINES", "180"), 180)
DEVAI_MAX_LOG_CHARS = _parse_int(os.getenv("DEVAI_MAX_LOG_CHARS", "18000"), 18000)
DEVAI_INDEX_MAX_AGE_SECONDS = _parse_int(os.getenv("DEVAI_INDEX_MAX_AGE_SECONDS", "1800"), 1800)
# Cortes ainda mais agressivos pra caber no Groq free tier que tem TPM 8000
# (~32k chars total, mas tem que descontar output 16k = ~16k chars de prompt).
# Mantemos margem de segurança pra 14k chars no prompt — isso libera ~10k pra
# diff/contexto + ~4k pra system prompt + schema + headers.
DEVAI_MAX_INDEX_CHARS = _parse_int(os.getenv("DEVAI_MAX_INDEX_CHARS", "3000"), 3000)
DEVAI_MAX_CONTEXT_FILES = _parse_int(os.getenv("DEVAI_MAX_CONTEXT_FILES", "2"), 2)
DEVAI_MAX_FILE_CONTEXT_CHARS = _parse_int(os.getenv("DEVAI_MAX_FILE_CONTEXT_CHARS", "5000"), 5000)
DEVAI_MAX_FILES_PER_PATCH = _parse_int(os.getenv("DEVAI_MAX_FILES_PER_PATCH", "5"), 5)
DEVAI_MAX_FILE_BYTES = _parse_int(os.getenv("DEVAI_MAX_FILE_BYTES", "220000"), 220000)
# Limite total do prompt — corte adaptativo do meio se passar.
# 14000 chars ~= 3500 tokens, deixando margem pro output (16k) caber em 8000 TPM
# do Groq. Gemini Pro/Cerebras suportam muito mais que isso, então é
# "lowest common denominator".
DEVAI_MAX_PROMPT_CHARS = _parse_int(os.getenv("DEVAI_MAX_PROMPT_CHARS", "14000"), 14000)

# Comentário automático da DevAI para patches aceitos pelo auto-updater de ZIP.
DEVAI_PATCH_REVIEW_ENABLED = _parse_bool(os.getenv("DEVAI_PATCH_REVIEW_ENABLED", "true"), True)
DEVAI_PATCH_REVIEW_MAX_FILES = _parse_int(os.getenv("DEVAI_PATCH_REVIEW_MAX_FILES", "5"), 5)
DEVAI_PATCH_REVIEW_MAX_CHARS_PER_FILE = _parse_int(os.getenv("DEVAI_PATCH_REVIEW_MAX_CHARS_PER_FILE", "3000"), 3000)
# Diff cap reduzido pra caber em 14k chars de prompt total (descontando
# system prompt 2k + schema 1k + headers/contexto 4k = ~7k disponível pra diff).
DEVAI_PATCH_REVIEW_MAX_DIFF_CHARS = _parse_int(os.getenv("DEVAI_PATCH_REVIEW_MAX_DIFF_CHARS", "6000"), 6000)
# Timeout duro pro review inteiro (montar prompt + chamar IA + render +
# enviar). Sem isso, um provider lento poderia segurar o `_analysis_lock`
# indefinidamente e bloquear o próximo review.
DEVAI_PATCH_REVIEW_TIMEOUT_SECONDS = _parse_int(os.getenv("DEVAI_PATCH_REVIEW_TIMEOUT_SECONDS", "120"), 120)

# Histórico de patches recentes injetado no prompt — evita a IA repetir
# tentativas que falharam ou propor solução já aplicada.
DEVAI_HISTORY_ITEMS = _parse_int(os.getenv("DEVAI_HISTORY_ITEMS", "5"), 5)
DEVAI_HISTORY_MAX_AGE_SECONDS = _parse_int(os.getenv("DEVAI_HISTORY_MAX_AGE_SECONDS", str(7 * 24 * 3600)), 7 * 24 * 3600)
# --- Music/TTS recovery defaults ---
MUSIC_LAVALINK_PREMATURE_END_MIN_SECONDS = float(os.getenv("MUSIC_LAVALINK_PREMATURE_END_MIN_SECONDS", "45"))
MUSIC_LAVALINK_PREMATURE_END_REMAINING_SECONDS = float(os.getenv("MUSIC_LAVALINK_PREMATURE_END_REMAINING_SECONDS", "35"))
MUSIC_LAVALINK_PREMATURE_END_MAX_RECOVERIES = int(os.getenv("MUSIC_LAVALINK_PREMATURE_END_MAX_RECOVERIES", "1"))
MUSIC_LAVALINK_TTS_TIMEOUT_PADDING_SECONDS = float(os.getenv("MUSIC_LAVALINK_TTS_TIMEOUT_PADDING_SECONDS", "18"))
MUSIC_TTS_SESSION_CLEANUP_GRACE_SECONDS = float(os.getenv("MUSIC_TTS_SESSION_CLEANUP_GRACE_SECONDS", "1.5"))
MUSIC_RESOLVING_STALE_SECONDS = float(os.getenv("MUSIC_RESOLVING_STALE_SECONDS", "45"))
# Evita segunda rodada automática quando o primeiro patch da IA já veio com
# sintaxe Python inválida (casos como `def def`). O erro ainda é reportado,
# mas não gasta mais CPU/API tentando reparar lixo óbvio.
DEVAI_REPAIR_SYNTAX_FAILURES = _parse_bool(os.getenv("DEVAI_REPAIR_SYNTAX_FAILURES", "false"), False)
