"""Configuração exclusiva do domínio de música.

Esta é a fonte de verdade das opções MUSIC_*, Lavalink de metadados e
provedores musicais usadas pela VPS. ``config.py`` só reexporta estes nomes
temporariamente para compatibilidade durante a modularização.
"""

from __future__ import annotations

import os


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _parse_int(value: str | None, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


# -----------------------------------------------------------------------------
# Música — configuração do domínio
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
# Se o Lavalink recebe o TTS/arquivo mas perde o voice state (state=None/voice_keys=[]),
# o bot pode cair para o TTS local direto em vez de silenciar a mensagem.
MUSIC_TTS_LAVALINK_FAILURE_LOCAL_FALLBACK = _parse_bool(os.getenv("MUSIC_TTS_LAVALINK_FAILURE_LOCAL_FALLBACK", "true"), True)
MUSIC_TTS_LAVALINK_LOCAL_FALLBACK_COOLDOWN_SECONDS = max(5.0, _parse_float(os.getenv("MUSIC_TTS_LAVALINK_LOCAL_FALLBACK_COOLDOWN_SECONDS", "45"), 45.0))
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
# Playlist virtual: limite da janela materializada, não limite lógico da coleção.
# O objetivo é manter consumo de RAM estável mesmo para playlists enormes.
MUSIC_PLAYLIST_WINDOW_SIZE = min(50, max(5, _parse_int(os.getenv("MUSIC_PLAYLIST_WINDOW_SIZE", "25"), 25)))
# Start-first: ao abrir uma playlist virtual materialize só o mínimo necessário
# para iniciar o áudio. O restante é preenchido em background depois que o
# primeiro play já foi confirmado pelo Phone Worker.
MUSIC_PLAYLIST_STARTUP_SIZE = min(
    MUSIC_PLAYLIST_WINDOW_SIZE,
    max(1, _parse_int(os.getenv("MUSIC_PLAYLIST_STARTUP_SIZE", "1"), 1)),
)
MUSIC_PLAYLIST_LOW_WATERMARK = min(
    MUSIC_PLAYLIST_WINDOW_SIZE - 1,
    max(1, _parse_int(os.getenv("MUSIC_PLAYLIST_LOW_WATERMARK", "8"), 8)),
)
MUSIC_SEARCH_RESULTS = max(1, min(10, _parse_int(os.getenv("MUSIC_SEARCH_RESULTS", "3"), 3)))
MUSIC_SEARCH_CHOICE_MEMORY_ENABLED = _parse_bool(os.getenv("MUSIC_SEARCH_CHOICE_MEMORY_ENABLED", "true"), True)
MUSIC_SEARCH_CHOICE_MEMORY_MAX_ENTRIES = max(1, min(100000, _parse_int(os.getenv("MUSIC_SEARCH_CHOICE_MEMORY_MAX_ENTRIES", "10000"), 10000)))
MUSIC_SEARCH_CHOICE_MEMORY_PATH = (os.getenv("MUSIC_SEARCH_CHOICE_MEMORY_PATH", "") or "").strip()
MUSIC_SEARCH_CHOICE_MEMORY_APPROX_ENABLED = _parse_bool(os.getenv("MUSIC_SEARCH_CHOICE_MEMORY_APPROX_ENABLED", "true"), True)
MUSIC_SEARCH_CHOICE_MEMORY_APPROX_MAX_EDITS = max(1, min(3, _parse_int(os.getenv("MUSIC_SEARCH_CHOICE_MEMORY_APPROX_MAX_EDITS", "2"), 2)))
MUSIC_SEARCH_CHOICE_MEMORY_APPROX_MIN_CHARS = max(4, min(24, _parse_int(os.getenv("MUSIC_SEARCH_CHOICE_MEMORY_APPROX_MIN_CHARS", "4"), 4)))
MUSIC_YOUTUBE_SEARCH_API_FIRST = _parse_bool(os.getenv("MUSIC_YOUTUBE_SEARCH_API_FIRST", "true"), True)
# Pesquisa textual do YouTube deve ser rápida: por padrão só lista metadata leve
# (API oficial se configurada, depois yt-dlp flat). Desative para permitir fallback
# pesado de busca completa quando a busca leve não encontrar nada.
MUSIC_YOUTUBE_SEARCH_FAST_ONLY = _parse_bool(os.getenv("MUSIC_YOUTUBE_SEARCH_FAST_ONLY", "true"), True)
MUSIC_YOUTUBE_SEARCH_TIMEOUT_SECONDS = max(2.0, _parse_float(os.getenv("MUSIC_YOUTUBE_SEARCH_TIMEOUT_SECONDS", "7.0"), 7.0))
MUSIC_YOUTUBE_SEARCH_USE_COOKIES = _parse_bool(os.getenv("MUSIC_YOUTUBE_SEARCH_USE_COOKIES", "false"), False)
# Link direto do YouTube entra rápido no queue/painel e resolve o stream só na hora
# de tocar. Isso evita o comando travar em yt-dlp antes de responder.
MUSIC_YOUTUBE_DIRECT_FAST_ENQUEUE = _parse_bool(os.getenv("MUSIC_YOUTUBE_DIRECT_FAST_ENQUEUE", "true"), True)
MUSIC_YOUTUBE_DIRECT_METADATA_TIMEOUT_SECONDS = max(0.5, _parse_float(os.getenv("MUSIC_YOUTUBE_DIRECT_METADATA_TIMEOUT_SECONDS", "1.4"), 1.4))
# Resolução local rápida: reduz combinações de clients/formatos do yt-dlp.
MUSIC_LOCAL_YOUTUBE_FAST_RESOLVE = _parse_bool(os.getenv("MUSIC_LOCAL_YOUTUBE_FAST_RESOLVE", "true"), True)
MUSIC_LOCAL_YOUTUBE_CLIENTS = (os.getenv("MUSIC_LOCAL_YOUTUBE_CLIENTS", "ios,android,web") or "ios,android,web").strip()
# Na VPS, YouTube costuma bloquear extrações sem cookie com "confirm you are not a bot".
# Para link direto/resultado do YouTube, usar cookies primeiro evita duas tentativas lentas
# sem cookie antes de chegar no caminho que realmente funciona.
MUSIC_LOCAL_YOUTUBE_COOKIES_FIRST = _parse_bool(os.getenv("MUSIC_LOCAL_YOUTUBE_COOKIES_FIRST", "true"), True)
MUSIC_LOCAL_YOUTUBE_RESOLVE_ATTEMPT_TIMEOUT_SECONDS = max(2.0, _parse_float(os.getenv("MUSIC_LOCAL_YOUTUBE_RESOLVE_ATTEMPT_TIMEOUT_SECONDS", "5.0"), 5.0))
MUSIC_LOCAL_YOUTUBE_NO_COOKIE_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("MUSIC_LOCAL_YOUTUBE_NO_COOKIE_TIMEOUT_SECONDS", "1.6"), 1.6))
# Limite total do caminho local do YouTube por faixa. Sem isso, várias
# combinações de clients/formatos podem deixar o painel preso em “resolvendo”.
MUSIC_LOCAL_YOUTUBE_RESOLVE_TOTAL_TIMEOUT_SECONDS = max(6.0, _parse_float(os.getenv("MUSIC_LOCAL_YOUTUBE_RESOLVE_TOTAL_TIMEOUT_SECONDS", "14.0"), 14.0))
# Resultado escolhido no YouTube tenta mirror LavaSrc por pouco tempo. Se o
# espelho não bater/abrir rápido, cai para yt-dlp local sem segurar o usuário.
MUSIC_YOUTUBE_LAVASRC_MIRROR_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("MUSIC_YOUTUBE_LAVASRC_MIRROR_TIMEOUT_SECONDS", "2.5"), 2.5))
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
        # Alta qualidade real, mas sem filtros rígidos. O yt-dlp escolhe o
        # melhor áudio possível; fallbacks específicos ficam no extractor.
        "bestaudio/best",
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
MUSIC_PANEL_REPOST_ON_TRACK_CHANGE = _parse_bool(os.getenv("MUSIC_PANEL_REPOST_ON_TRACK_CHANGE", "true"), True)
MUSIC_SOURCE_EMOJIS = {
    "youtube": "<:YouTube:1502490543891021827>",
    "spotify": "<:Spotify:1502490573205016676>",
    "deezer": "<:Deezer:1502490958997094420>",
    "soundcloud": "<:SoundCloud:1502491211485675631>",
}
MUSIC_SOURCE_EMOJI_FALLBACK = "🎵"
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
MUSIC_VOICE_STATUS_WATCHDOG_INTERVAL_SECONDS = max(15.0, _parse_float(os.getenv("MUSIC_VOICE_STATUS_WATCHDOG_INTERVAL_SECONDS", "45"), 45.0))
MUSIC_VOICE_STATUS_REASSERT_SECONDS = max(60.0, _parse_float(os.getenv("MUSIC_VOICE_STATUS_REASSERT_SECONDS", "240"), 240.0))
MUSIC_VOICE_STATUS_WRITE_RETRIES = max(1, min(5, _parse_int(os.getenv("MUSIC_VOICE_STATUS_WRITE_RETRIES", "3"), 3)))
MUSIC_VOICE_STATUS_RETRY_BASE_SECONDS = max(0.1, _parse_float(os.getenv("MUSIC_VOICE_STATUS_RETRY_BASE_SECONDS", "0.75"), 0.75))
MUSIC_VOICE_STATUS_GATEWAY_ACK_SECONDS = max(1.0, _parse_float(os.getenv("MUSIC_VOICE_STATUS_GATEWAY_ACK_SECONDS", "8"), 8.0))
MUSIC_MIN_LINK_METADATA_CONFIDENCE = (os.getenv("MUSIC_MIN_LINK_METADATA_CONFIDENCE", "medium") or "medium").strip().lower()
MUSIC_MAX_DURATION_MISMATCH_SECONDS = max(0.0, _parse_float(os.getenv("MUSIC_MAX_DURATION_MISMATCH_SECONDS", "45"), 45.0))
MUSIC_MAX_DURATION_MISMATCH_RATIO = max(0.0, _parse_float(os.getenv("MUSIC_MAX_DURATION_MISMATCH_RATIO", "0.25"), 0.25))
MUSIC_REJECT_WEAK_LINK_MATCHES = _parse_bool(os.getenv("MUSIC_REJECT_WEAK_LINK_MATCHES", "true"), True)
# Mirrors do LavaSrc usados para transformar metadata de Spotify/YouTube em áudio tocável.
# Spotify direto/spsearch fica fora do padrão porque o LavaSrc 4.8.x pode falhar com 403.
MUSIC_LAVASRC_MIRROR_PREFIXES = (os.getenv("MUSIC_LAVASRC_MIRROR_PREFIXES", "scsearch") or "scsearch").strip()


# Lavalink/phone worker — música agora deve rodar fora da VPS.
# MUSIC_BACKEND fica aceitando valor antigo por compatibilidade, mas o fluxo
# musical usa MUSIC_WORKER_ONLY_ENABLED para bloquear fallback local pesado.
MUSIC_BACKEND = (os.getenv("MUSIC_BACKEND", "worker") or "worker").strip().lower()
LAVALINK_ENABLED = _parse_bool(os.getenv("LAVALINK_ENABLED", "false"), False)
LAVALINK_MODE = (os.getenv("LAVALINK_MODE", "off") or "off").strip().lower()
LAVALINK_HOST = (os.getenv("LAVALINK_HOST", "") or "").strip()
LAVALINK_PORT = _parse_int(os.getenv("LAVALINK_PORT", "2333"), 2333)
LAVALINK_PASSWORD = (os.getenv("LAVALINK_PASSWORD", "") or "").strip()
LAVALINK_SECURE = _parse_bool(os.getenv("LAVALINK_SECURE", "false"), False)
LAVALINK_NODE_NAME = (os.getenv("LAVALINK_NODE_NAME", "main") or "main").strip() or "main"
LAVALINK_TIMEOUT_SECONDS = max(2.0, _parse_float(os.getenv("LAVALINK_TIMEOUT_SECONDS", "8.0"), 8.0))

# Lavalink auxiliar — pensado para o phone worker/celular via Tailscale.
# Em MUSIC_WORKER_ONLY_ENABLED=true, este node vira o node musical preferencial
# e não há fallback para execução pesada local na VPS.
AUX_LAVALINK_ENABLED = _parse_bool(os.getenv("AUX_LAVALINK_ENABLED", "false"), False)
AUX_LAVALINK_HOST = (os.getenv("AUX_LAVALINK_HOST", "") or "").strip()
AUX_LAVALINK_PORT = _parse_int(os.getenv("AUX_LAVALINK_PORT", "2333"), 2333)
AUX_LAVALINK_PASSWORD = (os.getenv("AUX_LAVALINK_PASSWORD", "") or "").strip()
AUX_LAVALINK_SECURE = _parse_bool(os.getenv("AUX_LAVALINK_SECURE", "false"), False)
AUX_LAVALINK_NODE_NAME = (os.getenv("AUX_LAVALINK_NODE_NAME", "phone") or "phone").strip() or "phone"
AUX_LAVALINK_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("AUX_LAVALINK_TIMEOUT_SECONDS", "3.0"), 3.0))
AUX_LAVALINK_COOLDOWN_SECONDS = max(10.0, _parse_float(os.getenv("AUX_LAVALINK_COOLDOWN_SECONDS", "300"), 300.0))


# Conexão direta com o Phone Worker usada exclusivamente pela música.
# Não depende do modo APK/Termux do Core Worker: se host+token existem, a cog
# pode consultar o agente como fallback do registry. Desative somente com
# MUSIC_PHONE_WORKER_DIRECT_ENABLED=false.
PHONE_WORKER_HOST = (os.getenv("PHONE_WORKER_HOST", "") or "").strip()
PHONE_WORKER_PORT = _parse_int(os.getenv("PHONE_WORKER_PORT", "8766"), 8766)
PHONE_WORKER_SCHEME = (os.getenv("PHONE_WORKER_SCHEME", "http") or "http").strip().lower() or "http"
PHONE_WORKER_TOKEN = (os.getenv("PHONE_WORKER_TOKEN", "") or "").strip()
MUSIC_PHONE_WORKER_DIRECT_ENABLED = _parse_bool(os.getenv("MUSIC_PHONE_WORKER_DIRECT_ENABLED", "true"), True)
PHONE_WORKER_ENABLED = bool(MUSIC_PHONE_WORKER_DIRECT_ENABLED and PHONE_WORKER_HOST and PHONE_WORKER_TOKEN)


# Music Agent no phone worker — padrão da música. A VPS fica como plano de UI/status
# e o Phone Worker assume voz/player/yt-dlp quando disponível. Lavalink fica só em metadados.
MUSIC_AGENT_ENABLED = _parse_bool(os.getenv("MUSIC_AGENT_ENABLED", "true"), True)
MUSIC_AGENT_COMMAND_TIMEOUT_SECONDS = max(2.0, _parse_float(os.getenv("MUSIC_AGENT_COMMAND_TIMEOUT_SECONDS", "18.0"), 18.0))
MUSIC_AGENT_STATUS_TIMEOUT_SECONDS = max(0.5, _parse_float(os.getenv("MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", "5.0"), 5.0))
MUSIC_AGENT_PLAY_STATUS_WATCH_SECONDS = max(5.0, _parse_float(os.getenv("MUSIC_AGENT_PLAY_STATUS_WATCH_SECONDS", "30.0"), 30.0))
MUSIC_AGENT_STATUS_POLL_SECONDS = max(0.4, _parse_float(os.getenv("MUSIC_AGENT_STATUS_POLL_SECONDS", "0.75"), 0.75))
# Poll adaptativo: preparação usa STATUS_POLL para confirmar início rápido;
# reprodução estável pode usar um intervalo maior sem afetar a UX.
MUSIC_AGENT_PANEL_POLL_SECONDS = max(1.0, min(4.0, _parse_float(os.getenv("MUSIC_AGENT_PANEL_POLL_SECONDS", "2.0"), 2.0)))
MUSIC_AGENT_PAUSED_POLL_SECONDS = max(MUSIC_AGENT_PANEL_POLL_SECONDS, min(6.0, _parse_float(os.getenv("MUSIC_AGENT_PAUSED_POLL_SECONDS", "3.0"), 3.0)))
# O monitor consulta o estado remoto com frequência, mas não precisa editar a
# mesma mensagem do Discord a cada poll. Atualizações sem mudança ficam limitadas
# a um refresh periódico para manter capacidade de autorreparo do painel.
MUSIC_AGENT_PANEL_REFRESH_SECONDS = max(10.0, min(300.0, _parse_float(os.getenv("MUSIC_AGENT_PANEL_REFRESH_SECONDS", "30.0"), 30.0)))
MUSIC_AGENT_IDLE_DISCONNECT_SECONDS = max(15.0, _parse_float(os.getenv("MUSIC_AGENT_IDLE_DISCONNECT_SECONDS", os.getenv("MUSIC_IDLE_DISCONNECT_SECONDS", "120")), 120.0))
MUSIC_LOADING_REACTION_EMOJI = (os.getenv("MUSIC_LOADING_REACTION_EMOJI", "<a:areia:1496606578395189473>") or "<a:areia:1496606578395189473>").strip()
MUSIC_AGENT_MIN_VERSION = (os.getenv("MUSIC_AGENT_MIN_VERSION", "0.3.23") or "0.3.23").strip()
MUSIC_AGENT_MAX_SESSIONS_PER_WORKER = max(1, _parse_int(os.getenv("MUSIC_AGENT_MAX_SESSIONS_PER_WORKER", "2"), 2))
MUSIC_AGENT_PLAYING_CONFIRM_SECONDS = max(2.0, _parse_float(os.getenv("MUSIC_AGENT_PLAYING_CONFIRM_SECONDS", "12.0"), 12.0))
MUSIC_AGENT_TTS_DUCK_VOLUME_PERCENT = max(0, min(100, _parse_int(os.getenv("MUSIC_AGENT_TTS_DUCK_VOLUME_PERCENT", "8"), 8)))
MUSIC_AGENT_TTS_ROUTE_ENABLED = _parse_bool(os.getenv("MUSIC_AGENT_TTS_ROUTE_ENABLED", "true"), True)
MUSIC_AGENT_TTS_TIMEOUT_SECONDS = max(3.0, _parse_float(os.getenv("MUSIC_AGENT_TTS_TIMEOUT_SECONDS", "30.0"), 30.0))
MUSIC_AGENT_DIRECT_CONFIRM_SECONDS = max(0.15, _parse_float(os.getenv("MUSIC_AGENT_DIRECT_CONFIRM_SECONDS", "0.35"), 0.35))
MUSIC_AGENT_PREFETCH_ENABLED = _parse_bool(os.getenv("MUSIC_AGENT_PREFETCH_ENABLED", "true"), True)
MUSIC_AGENT_PREFETCH_TOP_RESULTS = max(0, min(3, _parse_int(os.getenv("MUSIC_AGENT_PREFETCH_TOP_RESULTS", "1"), 1)))
MUSIC_AGENT_RESOLVE_CACHE_TTL_SECONDS = max(0.0, _parse_float(os.getenv("MUSIC_AGENT_RESOLVE_CACHE_TTL_SECONDS", "300.0"), 300.0))
MUSIC_AGENT_METADATA_CACHE_TTL_SECONDS = max(0.0, _parse_float(os.getenv("MUSIC_AGENT_METADATA_CACHE_TTL_SECONDS", "21600.0"), 21600.0))
MUSIC_AGENT_STREAM_CACHE_TTL_SECONDS = max(0.0, _parse_float(os.getenv("MUSIC_AGENT_STREAM_CACHE_TTL_SECONDS", "180.0"), 180.0))
MUSIC_AGENT_PREFETCH_TIMEOUT_SECONDS = max(3.0, _parse_float(os.getenv("MUSIC_AGENT_PREFETCH_TIMEOUT_SECONDS", "18.0"), 18.0))
MUSIC_SEARCH_PROVIDER_TIMEOUT_SECONDS = max(0.2, min(10.0, _parse_float(os.getenv("MUSIC_SEARCH_PROVIDER_TIMEOUT_SECONDS", "1.5"), 1.5)))
MUSIC_SEARCH_PROVIDER_FAST_BUDGET_SECONDS = max(0.15, min(2.0, _parse_float(os.getenv("MUSIC_SEARCH_PROVIDER_FAST_BUDGET_SECONDS", "0.65"), 0.65)))
MUSIC_SEARCH_PROVIDER_CIRCUIT_FAILURES = max(1, min(5, _parse_int(os.getenv("MUSIC_SEARCH_PROVIDER_CIRCUIT_FAILURES", "2"), 2)))
MUSIC_SEARCH_PROVIDER_CIRCUIT_COOLDOWN_SECONDS = max(1.0, min(300.0, _parse_float(os.getenv("MUSIC_SEARCH_PROVIDER_CIRCUIT_COOLDOWN_SECONDS", "30.0"), 30.0)))
# HTTP persistente: os providers compartilham conexoes DNS/TCP/TLS em vez de
# abrir uma conexao nova a cada busca. Limites pequenos preservam RAM na VPS.
MUSIC_SEARCH_HTTP_POOL_LIMIT = max(2, min(24, _parse_int(os.getenv("MUSIC_SEARCH_HTTP_POOL_LIMIT", "8"), 8)))
MUSIC_SEARCH_HTTP_POOL_LIMIT_PER_HOST = max(1, min(MUSIC_SEARCH_HTTP_POOL_LIMIT, _parse_int(os.getenv("MUSIC_SEARCH_HTTP_POOL_LIMIT_PER_HOST", "4"), 4)))
MUSIC_SEARCH_HTTP_KEEPALIVE_SECONDS = max(5.0, min(120.0, _parse_float(os.getenv("MUSIC_SEARCH_HTTP_KEEPALIVE_SECONDS", "30.0"), 30.0)))
MUSIC_SEARCH_HTTP_DNS_CACHE_SECONDS = max(30.0, min(1800.0, _parse_float(os.getenv("MUSIC_SEARCH_HTTP_DNS_CACHE_SECONDS", "300.0"), 300.0)))
# API-first: a YouTube Data API e a unica fonte da primeira tentativa.
# Se vier ao menos um resultado utilizavel, nao consultamos o Phone Worker.
# A reproducao continua sendo resolvida exclusivamente pelo yt-dlp apos a escolha.
MUSIC_SEARCH_API_FIRST_ENABLED = _parse_bool(os.getenv("MUSIC_SEARCH_API_FIRST_ENABLED", "true"), True)
MUSIC_SEARCH_API_FIRST_TIMEOUT_SECONDS = max(0.10, min(1.5, _parse_float(os.getenv("MUSIC_SEARCH_API_FIRST_TIMEOUT_SECONDS", "0.30"), 0.30)))
# Guard local conservador para search.list. O valor é número de chamadas, não
# unidades de quota; deixa margem para outros usos da mesma chave/projeto.
MUSIC_SEARCH_YOUTUBE_API_QUOTA_GUARD_ENABLED = _parse_bool(os.getenv("MUSIC_SEARCH_YOUTUBE_API_QUOTA_GUARD_ENABLED", "true"), True)
MUSIC_SEARCH_YOUTUBE_API_DAILY_SOFT_CALLS = max(0, min(10000, _parse_int(os.getenv("MUSIC_SEARCH_YOUTUBE_API_DAILY_SOFT_CALLS", "80"), 80)))
MUSIC_WORKER_DIRECT_CACHE_TTL_SECONDS = max(0.0, _parse_float(os.getenv("MUSIC_WORKER_DIRECT_CACHE_TTL_SECONDS", "90.0"), 90.0))
MUSIC_AGENT_BOOTSTRAP_ON_PLAY = _parse_bool(os.getenv("MUSIC_AGENT_BOOTSTRAP_ON_PLAY", "true"), True)
MUSIC_AGENT_MISSING_TOKEN_MESSAGE = (
    os.getenv("MUSIC_AGENT_MISSING_TOKEN_MESSAGE", "Sistema de música indisponível no momento: O worker está online, mas a música ainda não está pronta")
    or "Sistema de música indisponível no momento: O worker está online, mas a música ainda não está pronta"
).strip()

# Phone Worker — celular via Tailscale.
# Música é um domínio independente do Core Worker APK: substituir o Termux do Core
# Worker não pode desligar o Music Agent. A VPS não reproduz música localmente.
MUSIC_WORKER_ONLY_ENABLED = _parse_bool(os.getenv("MUSIC_WORKER_ONLY_ENABLED", "true"), True)
MUSIC_WORKER_UNAVAILABLE_MESSAGE = (
    os.getenv("MUSIC_WORKER_UNAVAILABLE_MESSAGE", "Sistema de música indisponível no momento: Nenhum worker online")
    or "Sistema de música indisponível no momento: Nenhum worker online"
).strip()
MUSIC_WORKER_NO_CAPACITY_MESSAGE = (
    os.getenv(
        "MUSIC_WORKER_NO_CAPACITY_MESSAGE",
        "Sistema de música indisponível no momento: Há worker online, mas nenhum está apto para música",
    )
    or "Sistema de música indisponível no momento: Há worker online, mas nenhum está apto para música"
).strip()
MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE = (
    os.getenv("MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE", "Sistema de música indisponível no momento: O worker está online, mas a música ainda não está pronta")
    or "Sistema de música indisponível no momento: O worker está online, mas a música ainda não está pronta"
).strip()
MUSIC_WORKER_REQUIRE_TURBO = _parse_bool(os.getenv("MUSIC_WORKER_REQUIRE_TURBO", "true"), True)
MUSIC_WORKER_REQUIRED_ROLES = (os.getenv("MUSIC_WORKER_REQUIRED_ROLES", "phone-worker") or "phone-worker").strip()
MUSIC_WORKER_REQUIRED_CAPABILITIES = (os.getenv("MUSIC_WORKER_REQUIRED_CAPABILITIES", "ffmpeg,ffprobe") or "ffmpeg,ffprobe").strip()
# Segredos/cookies do yt-dlp em modo worker-only ficam no phone worker, não na VPS.
# Use um caminho local do celular, por exemplo:
# ~/phone-worker/secrets/youtube-cookies.txt
MUSIC_WORKER_YTDLP_COOKIES_FILE = (
    os.getenv("MUSIC_WORKER_YTDLP_COOKIES_FILE", os.getenv("PHONE_WORKER_MUSIC_YTDLP_COOKIES_FILE", ""))
    or ""
).strip()
MUSIC_WORKER_YTDLP_TIMEOUT_SECONDS = max(5.0, _parse_float(os.getenv("MUSIC_WORKER_YTDLP_TIMEOUT_SECONDS", "32.0"), 32.0))
MUSIC_WORKER_YTDLP_SEARCH_TIMEOUT_SECONDS = max(4.0, _parse_float(os.getenv("MUSIC_WORKER_YTDLP_SEARCH_TIMEOUT_SECONDS", "7.0"), 7.0))
MUSIC_WORKER_YTDLP_JS_RUNTIMES = (
    os.getenv("MUSIC_WORKER_YTDLP_JS_RUNTIMES", os.getenv("PHONE_WORKER_MUSIC_YTDLP_JS_RUNTIMES", "node"))
    or "node"
).strip()
MUSIC_WORKER_YTDLP_DEFAULT_SEARCH = (
    os.getenv("MUSIC_WORKER_YTDLP_DEFAULT_SEARCH", os.getenv("PHONE_WORKER_MUSIC_YTDLP_DEFAULT_SEARCH", "ytsearch"))
    or "ytsearch"
).strip()
MUSIC_WORKER_STREAM_CONNECT_TIMEOUT_SECONDS = max(3.0, _parse_float(os.getenv("MUSIC_WORKER_STREAM_CONNECT_TIMEOUT_SECONDS", "90.0"), 90.0))
MUSIC_WORKER_STREAM_READY_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("MUSIC_WORKER_STREAM_READY_TIMEOUT_SECONDS", "90.0"), 90.0))
MUSIC_WORKER_STREAM_PREBUFFER_SECONDS = max(0.1, _parse_float(os.getenv("MUSIC_WORKER_STREAM_PREBUFFER_SECONDS", "8.0"), 8.0))
MUSIC_WORKER_STREAM_MAX_BUFFER_SECONDS = max(1.0, _parse_float(os.getenv("MUSIC_WORKER_STREAM_MAX_BUFFER_SECONDS", "30.0"), 30.0))
MUSIC_WORKER_STREAM_UNDERRUN_LOG_EVERY = max(1, _parse_int(os.getenv("MUSIC_WORKER_STREAM_UNDERRUN_LOG_EVERY", "25"), 25))
MUSIC_WORKER_LAVALINK_HOST = (os.getenv("MUSIC_WORKER_LAVALINK_HOST", "") or "").strip()
MUSIC_WORKER_LAVALINK_PORT = _parse_int(os.getenv("MUSIC_WORKER_LAVALINK_PORT", "2333"), 2333)
MUSIC_WORKER_LAVALINK_PASSWORD = (os.getenv("MUSIC_WORKER_LAVALINK_PASSWORD", "") or "").strip()
MUSIC_WORKER_LAVALINK_SECURE = _parse_bool(os.getenv("MUSIC_WORKER_LAVALINK_SECURE", "false"), False)
MUSIC_WORKER_LAVALINK_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("MUSIC_WORKER_LAVALINK_TIMEOUT_SECONDS", "3.0"), 3.0))
# Quando true, exige que o heartbeat do worker já informe status.music_node/lavalink saudável.
# Fica false por compatibilidade com agents antigos; o backend Lavalink ainda valida o node.
MUSIC_WORKER_REQUIRE_MUSIC_NODE_STATUS = _parse_bool(os.getenv("MUSIC_WORKER_REQUIRE_MUSIC_NODE_STATUS", "false"), False)
MUSIC_WORKER_CONFIGURED_HEALTHCHECK_ENABLED = _parse_bool(os.getenv("MUSIC_WORKER_CONFIGURED_HEALTHCHECK_ENABLED", "true"), True)
MUSIC_WORKER_CONFIGURED_HEALTH_TIMEOUT_SECONDS = max(0.3, _parse_float(os.getenv("MUSIC_WORKER_CONFIGURED_HEALTH_TIMEOUT_SECONDS", "3.5"), 3.5))
# Em worker-only, o node auxiliar do celular vira o node preferencial/único da música
# quando AUX_LAVALINK_* estiver configurado. Sem AUX, usa LAVALINK_* como node do worker.
MUSIC_WORKER_LAVALINK_USE_AUX = _parse_bool(os.getenv("MUSIC_WORKER_LAVALINK_USE_AUX", "true"), True)

# Uso do phone-worker fora do /vps: preparação de áudio TTS para Lavalink.
# A VPS sempre mantém fallback local.
MUSIC_TTS_PHONE_WORKER_CONVERT_ENABLED = _parse_bool(os.getenv("MUSIC_TTS_PHONE_WORKER_CONVERT_ENABLED", "true"), True)
MUSIC_TTS_PHONE_WORKER_CONVERT_TIMEOUT_SECONDS = max(0.8, _parse_float(os.getenv("MUSIC_TTS_PHONE_WORKER_CONVERT_TIMEOUT_SECONDS", "3.5"), 3.5))
MUSIC_TTS_PHONE_WORKER_CONVERT_MAX_MB = max(1, _parse_int(os.getenv("MUSIC_TTS_PHONE_WORKER_CONVERT_MAX_MB", "8"), 8))

# Node de áudio compatível com Lavalink API.
# Qualquer valor legado de MUSIC_NODE_PROVIDER cai para Lavalink.
_MUSIC_NODE_PROVIDER_RAW = (os.getenv("MUSIC_NODE_PROVIDER", "lavalink") or "lavalink").strip().lower()
MUSIC_NODE_PROVIDER = _MUSIC_NODE_PROVIDER_RAW if _MUSIC_NODE_PROVIDER_RAW in {"lavalink", "auto"} else "lavalink"
AUDIO_NODE_FAILURE_COOLDOWN_SECONDS = max(5.0, _parse_float(os.getenv("AUDIO_NODE_FAILURE_COOLDOWN_SECONDS", "45"), 45.0))
AUDIO_NODE_STARTUP_WAIT_SECONDS = max(0.0, _parse_float(os.getenv("AUDIO_NODE_STARTUP_WAIT_SECONDS", "90"), 90.0))
AUDIO_NODE_STARTUP_WAIT_REQUIRED = _parse_bool(os.getenv("AUDIO_NODE_STARTUP_WAIT_REQUIRED", "true"), True)

# -----------------------------------------------------------------------------
# Music/TTS recovery defaults
# -----------------------------------------------------------------------------

MUSIC_LAVALINK_PREMATURE_END_MIN_SECONDS = float(os.getenv("MUSIC_LAVALINK_PREMATURE_END_MIN_SECONDS", "45"))
MUSIC_LAVALINK_PREMATURE_END_REMAINING_SECONDS = float(os.getenv("MUSIC_LAVALINK_PREMATURE_END_REMAINING_SECONDS", "35"))
MUSIC_LAVALINK_PREMATURE_END_MAX_RECOVERIES = int(os.getenv("MUSIC_LAVALINK_PREMATURE_END_MAX_RECOVERIES", "1"))
MUSIC_LAVALINK_TTS_TIMEOUT_PADDING_SECONDS = float(os.getenv("MUSIC_LAVALINK_TTS_TIMEOUT_PADDING_SECONDS", "18"))
MUSIC_TTS_SESSION_CLEANUP_GRACE_SECONDS = float(os.getenv("MUSIC_TTS_SESSION_CLEANUP_GRACE_SECONDS", "1.5"))
MUSIC_RESOLVING_STALE_SECONDS = float(os.getenv("MUSIC_RESOLVING_STALE_SECONDS", "45"))


__all__ = tuple(
    name
    for name in globals()
    if (
        name.startswith("MUSIC_")
        or name.startswith("LAVALINK_")
        or name.startswith("AUX_LAVALINK_")
        or name == "YOUTUBE_API_KEY"
        or name.startswith("SPOTIFY_")
        or name.startswith("DEEZER_")
        or name.startswith("SOUNDCLOUD_")
        or name.startswith("AUDIO_NODE_")
    )
)
