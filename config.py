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
# Event loop / logging — defaults seguros para VPS pequena
# -----------------------------------------------------------------------------
BOT_EVENT_LOOP_WATCHDOG_INTERVAL_SECONDS = _parse_float(os.getenv("BOT_EVENT_LOOP_WATCHDOG_INTERVAL_SECONDS", "1.0"), 1.0)
BOT_EVENT_LOOP_LAG_WARNING_SECONDS = _parse_float(os.getenv("BOT_EVENT_LOOP_LAG_WARNING_SECONDS", "1.5"), 1.5)
BOT_EVENT_LOOP_LAG_WARNING_COOLDOWN_SECONDS = _parse_float(os.getenv("BOT_EVENT_LOOP_LAG_WARNING_COOLDOWN_SECONDS", "30.0"), 30.0)
BOT_EVENT_LOOP_LAG_SEVERE_SECONDS = _parse_float(os.getenv("BOT_EVENT_LOOP_LAG_SEVERE_SECONDS", "8.0"), 8.0)
BOT_EVENT_LOOP_LAG_DIAGNOSTIC_COOLDOWN_SECONDS = _parse_float(os.getenv("BOT_EVENT_LOOP_LAG_DIAGNOSTIC_COOLDOWN_SECONDS", "90.0"), 90.0)

ON_COLOR = 0x57F287
OFF_COLOR = 0xED4245

GTTS_DEFAULT_LANGUAGE = os.getenv("GTTS_DEFAULT_LANGUAGE", "pt-br")

TTS_OPUS_PLAYBACK_ENABLED = _parse_bool(os.getenv("TTS_OPUS_PLAYBACK_ENABLED", "true"), True)
TTS_OPUS_PLAYBACK_COPY_CODEC = _parse_bool(os.getenv("TTS_OPUS_PLAYBACK_COPY_CODEC", "true"), True)
WORKER_VOICE_AGENT_DIRECT_TTS_PREBUILD_GCLOUD = False
WORKER_VOICE_AGENT_DIRECT_TTS_PREBUILD_MAX_MB = max(1, _parse_int(os.getenv("WORKER_VOICE_AGENT_DIRECT_TTS_PREBUILD_MAX_MB", "8"), 8))

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
TTS_EDGE_CONNECT_TIMEOUT_SECONDS = max(1, _parse_int(os.getenv("TTS_EDGE_CONNECT_TIMEOUT_SECONDS", "4"), 4))
TTS_EDGE_RECEIVE_TIMEOUT_SECONDS = max(1, _parse_int(os.getenv("TTS_EDGE_RECEIVE_TIMEOUT_SECONDS", "15"), 15))

# Fast path do Edge: a VPS mantém a posse da call e começa a reproduzir os
# fragmentos recebidos antes de a síntese completa terminar. O caminho antigo
# por arquivo continua disponível para rollback por variável de ambiente.
TTS_EDGE_VPS_FAST_PATH_ENABLED = _parse_bool(os.getenv("TTS_EDGE_VPS_FAST_PATH_ENABLED", "true"), True)
TTS_EDGE_STREAMING_ENABLED = _parse_bool(os.getenv("TTS_EDGE_STREAMING_ENABLED", "true"), True)
TTS_EDGE_STREAM_FIRST_AUDIO_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("TTS_EDGE_STREAM_FIRST_AUDIO_TIMEOUT_SECONDS", "4.0"), 4.0))
TTS_EDGE_STREAM_TOTAL_TIMEOUT_SECONDS = max(TTS_EDGE_STREAM_FIRST_AUDIO_TIMEOUT_SECONDS, _parse_float(os.getenv("TTS_EDGE_STREAM_TOTAL_TIMEOUT_SECONDS", "30.0"), 30.0))
TTS_EDGE_STREAM_PREBUFFER_MS = min(1200, max(100, _parse_int(os.getenv("TTS_EDGE_STREAM_PREBUFFER_MS", "220"), 220)))
TTS_EDGE_STREAM_QUEUE_MAX_CHUNKS = min(128, max(8, _parse_int(os.getenv("TTS_EDGE_STREAM_QUEUE_MAX_CHUNKS", "64"), 64)))
TTS_EDGE_STREAM_CHUNK_BYTES = min(64 * 1024, max(4 * 1024, _parse_int(os.getenv("TTS_EDGE_STREAM_CHUNK_BYTES", "16384"), 16384)))
TTS_EDGE_STREAM_PIPE_OPEN_TIMEOUT_SECONDS = max(2.0, _parse_float(os.getenv("TTS_EDGE_STREAM_PIPE_OPEN_TIMEOUT_SECONDS", "10.0"), 10.0))
TTS_EDGE_STREAM_PIPE_BYTES = min(1024 * 1024, max(64 * 1024, _parse_int(os.getenv("TTS_EDGE_STREAM_PIPE_BYTES", "131072"), 131072)))
TTS_EDGE_STREAM_CACHE_BUFFER_BYTES = min(1024 * 1024, max(16 * 1024, _parse_int(os.getenv("TTS_EDGE_STREAM_CACHE_BUFFER_BYTES", "131072"), 131072)))
# O áudio Edge destinado ao cache fica em memória somente até a síntese acabar;
# a gravação final ocorre fora do event loop. Dois MiB cobrem com folga as
# falas aceitas normalmente sem pressionar a VPS de 1 GB.
TTS_EDGE_STREAM_CACHE_MEMORY_MAX_BYTES = min(8 * 1024 * 1024, max(256 * 1024, _parse_int(os.getenv("TTS_EDGE_STREAM_CACHE_MEMORY_MAX_BYTES", "2097152"), 2097152)))
TTS_EDGE_ADAPTIVE_PREBUFFER_ENABLED = _parse_bool(os.getenv("TTS_EDGE_ADAPTIVE_PREBUFFER_ENABLED", "true"), True)
TTS_EDGE_ADAPTIVE_PREBUFFER_MIN_MS = min(TTS_EDGE_STREAM_PREBUFFER_MS, max(100, _parse_int(os.getenv("TTS_EDGE_ADAPTIVE_PREBUFFER_MIN_MS", "160"), 160)))
TTS_EDGE_ADAPTIVE_PREBUFFER_MAX_MS = max(TTS_EDGE_STREAM_PREBUFFER_MS, min(1200, _parse_int(os.getenv("TTS_EDGE_ADAPTIVE_PREBUFFER_MAX_MS", "400"), 400)))
TTS_EDGE_ADAPTIVE_PREBUFFER_STABLE_STREAMS = max(4, _parse_int(os.getenv("TTS_EDGE_ADAPTIVE_PREBUFFER_STABLE_STREAMS", "20"), 20))
TTS_EDGE_STREAM_STALL_THRESHOLD_MS = max(20.0, _parse_float(os.getenv("TTS_EDGE_STREAM_STALL_THRESHOLD_MS", "35.0"), 35.0))
TTS_EDGE_FFMPEG_MP3_INPUT_HINT_ENABLED = _parse_bool(os.getenv("TTS_EDGE_FFMPEG_MP3_INPUT_HINT_ENABLED", "true"), True)
TTS_EDGE_CIRCUIT_BREAKER_ENABLED = _parse_bool(os.getenv("TTS_EDGE_CIRCUIT_BREAKER_ENABLED", "true"), True)
TTS_EDGE_CIRCUIT_BREAKER_FAILURES = max(2, _parse_int(os.getenv("TTS_EDGE_CIRCUIT_BREAKER_FAILURES", "3"), 3))
TTS_EDGE_CIRCUIT_BREAKER_COOLDOWN_SECONDS = max(5.0, _parse_float(os.getenv("TTS_EDGE_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "15.0"), 15.0))
# Um prefetch global preserva dois dos três slots Edge padrão para falas atuais.
# A prioridade é promovida automaticamente quando o item especulativo vira a
# próxima fala real.
TTS_EDGE_PREFETCH_CONCURRENCY = max(1, _parse_int(os.getenv("TTS_EDGE_PREFETCH_CONCURRENCY", "1"), 1))

# gTTS: executor limitado mantém a concorrência física correta mesmo quando uma
# coroutine expira. Os timeouts nativos impedem thread zumbi em requests.
TTS_GTTS_CONCURRENCY = _parse_int(os.getenv("TTS_GTTS_CONCURRENCY", "2"), 2)
TTS_GTTS_TIMEOUT_SECONDS = max(5.0, _parse_float(os.getenv("TTS_GTTS_TIMEOUT_SECONDS", "20.0"), 20.0))
TTS_GTTS_CONNECT_TIMEOUT_SECONDS = max(0.5, _parse_float(os.getenv("TTS_GTTS_CONNECT_TIMEOUT_SECONDS", "3.5"), 3.5))
TTS_GTTS_READ_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("TTS_GTTS_READ_TIMEOUT_SECONDS", "8.0"), 8.0))
# gTTS 2.5.4 abre uma Session por parte. O executor dedicado permite manter
# uma sessão por thread, com rotação curta e rollback imediato por flag.
TTS_GTTS_PERSISTENT_SESSION_ENABLED = _parse_bool(os.getenv("TTS_GTTS_PERSISTENT_SESSION_ENABLED", "true"), True)
TTS_GTTS_SESSION_TTL_SECONDS = max(10.0, _parse_float(os.getenv("TTS_GTTS_SESSION_TTL_SECONDS", "90.0"), 90.0))
TTS_GTTS_SESSION_MAX_REQUESTS = max(4, _parse_int(os.getenv("TTS_GTTS_SESSION_MAX_REQUESTS", "256"), 256))
TTS_GTTS_STREAMING_ENABLED = _parse_bool(os.getenv("TTS_GTTS_STREAMING_ENABLED", "true"), True)
# Acima de 100 caracteres o gTTS divide o texto em mais de uma requisição; o
# stream permite tocar a primeira parte enquanto as próximas são sintetizadas.
TTS_GTTS_STREAM_MIN_CHARS = max(1, _parse_int(os.getenv("TTS_GTTS_STREAM_MIN_CHARS", "101"), 101))
TTS_GTTS_STREAM_FIRST_AUDIO_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("TTS_GTTS_STREAM_FIRST_AUDIO_TIMEOUT_SECONDS", "6.0"), 6.0))
TTS_CACHE_MAINTENANCE_DELAY_SECONDS = max(0.1, _parse_float(os.getenv("TTS_CACHE_MAINTENANCE_DELAY_SECONDS", "0.75"), 0.75))
TTS_LATENCY_SAMPLE_WINDOW = max(32, _parse_int(os.getenv("TTS_LATENCY_SAMPLE_WINDOW", "256"), 256))
TTS_PERSISTENT_STATS_FLUSH_SECONDS = max(0.05, _parse_float(os.getenv("TTS_PERSISTENT_STATS_FLUSH_SECONDS", "0.5"), 0.5))

# Em uma call fria, prepara um frame do FFmpeg enquanto a conexão Discord ainda
# termina. O frame só é contado quando o Discord realmente o solicita.
TTS_FFMPEG_PRIME_ENABLED = _parse_bool(os.getenv("TTS_FFMPEG_PRIME_ENABLED", "true"), True)
TTS_FFMPEG_PRIME_TIMEOUT_SECONDS = max(0.25, _parse_float(os.getenv("TTS_FFMPEG_PRIME_TIMEOUT_SECONDS", "1.5"), 1.5))
# voice_channel.connect() já recebe self_deaf. Em entradas frias do TTS, a
# confirmação do estado e a persistência podem terminar depois que o playback
# fica apto, sem atrasar o primeiro frame.
TTS_DEFER_POST_CONNECT_MAINTENANCE_ENABLED = _parse_bool(
    os.getenv("TTS_DEFER_POST_CONNECT_MAINTENANCE_ENABLED", "true"),
    True,
)
TTS_POST_CONNECT_SETTLE_DELAY_SECONDS = max(
    0.0,
    min(1.0, _parse_float(os.getenv("TTS_POST_CONNECT_SETTLE_DELAY_SECONDS", "0.12"), 0.12)),
)

# FFmpeg enxuto para reprodução
TTS_FFMPEG_BEFORE_OPTIONS = (os.getenv("TTS_FFMPEG_BEFORE_OPTIONS", "-nostdin") or "-nostdin").strip()
TTS_FFMPEG_OPTIONS = (os.getenv("TTS_FFMPEG_OPTIONS", "-vn -loglevel error") or "-vn -loglevel error").strip()


# O APK assume o runtime móvel. O Termux pode continuar temporariamente apenas
# como builder bootstrap do primeiro APK com toolchain interno; nunca buildamos na VPS.
CORE_WORKER_APK_REPLACES_TERMUX = _parse_bool(os.getenv("CORE_WORKER_APK_REPLACES_TERMUX", "true"), True)
CORE_WORKER_TERMUX_BOOTSTRAP_BUILDER_ENABLED = _parse_bool(
    os.getenv("CORE_WORKER_TERMUX_BOOTSTRAP_BUILDER_ENABLED", "true"), True
)


PHONE_WORKER_HOST = (os.getenv("PHONE_WORKER_HOST", "") or "").strip()
PHONE_WORKER_PORT = _parse_int(os.getenv("PHONE_WORKER_PORT", "8766"), 8766)
PHONE_WORKER_SCHEME = (os.getenv("PHONE_WORKER_SCHEME", "http") or "http").strip().lower() or "http"
PHONE_WORKER_TOKEN = (os.getenv("PHONE_WORKER_TOKEN", "") or "").strip()
# Mantém os consumidores existentes apontando para a API compatível do APK.
# Uma configuração antiga PHONE_WORKER_ENABLED=false não deve desligar o APK
# quando host e token diretos já estão presentes.
PHONE_WORKER_ENABLED = (
    CORE_WORKER_APK_REPLACES_TERMUX and bool(PHONE_WORKER_HOST and PHONE_WORKER_TOKEN)
) or _parse_bool(os.getenv("PHONE_WORKER_ENABLED", "false"), False)
PHONE_WORKER_ZIP_VALIDATE_ENABLED = _parse_bool(os.getenv("PHONE_WORKER_ZIP_VALIDATE_ENABLED", "true"), True)
PHONE_WORKER_ZIP_VALIDATE_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("PHONE_WORKER_ZIP_VALIDATE_TIMEOUT_SECONDS", "5.0"), 5.0))
PHONE_WORKER_ZIP_VALIDATE_MAX_MB = max(1, _parse_int(os.getenv("PHONE_WORKER_ZIP_VALIDATE_MAX_MB", "24"), 24))
PHONE_WORKER_UPDATE_LOG_SUMMARY_ENABLED = _parse_bool(os.getenv("PHONE_WORKER_UPDATE_LOG_SUMMARY_ENABLED", "true"), True)
PHONE_WORKER_UPDATE_LOG_SUMMARY_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("PHONE_WORKER_UPDATE_LOG_SUMMARY_TIMEOUT_SECONDS", "4.0"), 4.0))
PHONE_WORKER_MAINTENANCE_PLAN_ENABLED = _parse_bool(os.getenv("PHONE_WORKER_MAINTENANCE_PLAN_ENABLED", "true"), True)

# Benchmark restrito de TTS: usado só para provar se o worker turbo realmente
# sintetiza mais rápido que a VPS. Não altera o fluxo normal de TTS.
TTS_TURBO_BENCHMARK_ENABLED = _parse_bool(os.getenv("TTS_TURBO_BENCHMARK_ENABLED", "true"), True)
TTS_TURBO_BENCHMARK_GUILD_ID = _parse_int(os.getenv("TTS_TURBO_BENCHMARK_GUILD_ID", "927002914449424404"), 927002914449424404)
TTS_TURBO_BENCHMARK_TRIGGER_TEXT = (os.getenv("TTS_TURBO_BENCHMARK_TRIGGER_TEXT", "teste") or "teste").strip().lower()
TTS_TURBO_BENCHMARK_TIMEOUT_SECONDS = max(1.5, _parse_float(os.getenv("TTS_TURBO_BENCHMARK_TIMEOUT_SECONDS", "12.0"), 12.0))
TTS_TURBO_BENCHMARK_MAX_AUDIO_MB = max(1, _parse_int(os.getenv("TTS_TURBO_BENCHMARK_MAX_AUDIO_MB", "4"), 4))

# ATTS: Android TTS nativo do APK/phone-worker. O prefixo público oficial é %.
# Mantemos os nomes antigos TTS_PIPER_EXPERIMENT_* apenas como alias de migração.
TTS_ATTS_PREFIX = (os.getenv("TTS_ATTS_PREFIX") or os.getenv("TTS_PIPER_EXPERIMENT_PREFIX") or "%").strip() or "%"
TTS_ATTS_ENABLED = _parse_bool(os.getenv("TTS_ATTS_ENABLED", os.getenv("TTS_PIPER_EXPERIMENT_ENABLED", "true")), True)

# Kasane Teto: engine UTAU exclusiva do phone worker. O bot nunca tenta
# carregar voicebank/resampler na VPS; quando o worker não estiver pronto, usa
# a configuração normal do usuário como fallback.
TTS_TETO_ENABLED = _parse_bool(os.getenv("TTS_TETO_ENABLED", "true"), True)
TTS_TETO_PREFIX = (os.getenv("TTS_TETO_PREFIX", "'") or "'").strip() or "'"
TTS_TETO_ENGINE = "teto"
TTS_TETO_MAX_TEXT_LENGTH = max(16, _parse_int(os.getenv("TTS_TETO_MAX_TEXT_LENGTH", "180"), 180))
TTS_TETO_WORKER_TIMEOUT_SECONDS = max(2.0, _parse_float(os.getenv("TTS_TETO_WORKER_TIMEOUT_SECONDS", "25"), 25.0))
TTS_TETO_MAX_AUDIO_MB = max(1, _parse_int(os.getenv("TTS_TETO_MAX_AUDIO_MB", "8"), 8))
TTS_PIPER_EXPERIMENT_ENABLED = TTS_ATTS_ENABLED
TTS_PIPER_EXPERIMENT_GUILD_ID = _parse_int(os.getenv("TTS_PIPER_EXPERIMENT_GUILD_ID", "0"), 0)
TTS_PIPER_EXPERIMENT_PREFIX = TTS_ATTS_PREFIX
TTS_PIPER_EXPERIMENT_ENGINE = "android_native"
TTS_PIPER_WORKER_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("TTS_PIPER_WORKER_TIMEOUT_SECONDS", "6.0"), 6.0))
TTS_PIPER_MAX_TEXT_LENGTH = max(16, _parse_int(os.getenv("TTS_PIPER_MAX_TEXT_LENGTH", "600"), 600))
TTS_PIPER_MAX_AUDIO_MB = max(1, _parse_int(os.getenv("TTS_PIPER_MAX_AUDIO_MB", "8"), 8))
TTS_PIPER_MODEL_NAME = (os.getenv("TTS_PIPER_MODEL_NAME", "turbo-default") or "turbo-default").strip() or "turbo-default"
TTS_PIPER_VPS_CACHE_SIZE = max(32, _parse_int(os.getenv("TTS_PIPER_VPS_CACHE_SIZE", "2048"), 2048))
TTS_PIPER_VPS_CACHE_MAX_MB = max(64, _parse_int(os.getenv("TTS_PIPER_VPS_CACHE_MAX_MB", "2048"), 2048))

# Cache do phone-worker turbo como segunda camada opcional do TTS.
# A VPS consulta esse cache com timeout curto; se falhar ou der miss, sintetiza localmente normalmente.
TTS_TURBO_WORKER_CACHE_ENABLED = _parse_bool(os.getenv("TTS_TURBO_WORKER_CACHE_ENABLED", "true"), True)
TTS_TURBO_WORKER_CACHE_LOOKUP_TIMEOUT_SECONDS = max(0.15, _parse_float(os.getenv("TTS_TURBO_WORKER_CACHE_LOOKUP_TIMEOUT_SECONDS", "0.65"), 0.65))
TTS_TURBO_WORKER_CACHE_STORE_TIMEOUT_SECONDS = max(0.5, _parse_float(os.getenv("TTS_TURBO_WORKER_CACHE_STORE_TIMEOUT_SECONDS", "2.5"), 2.5))
TTS_TURBO_WORKER_CACHE_MAX_AUDIO_MB = max(1, _parse_int(os.getenv("TTS_TURBO_WORKER_CACHE_MAX_AUDIO_MB", "8"), 8))
TTS_TURBO_WORKER_CACHE_STORE_BACKGROUND = _parse_bool(os.getenv("TTS_TURBO_WORKER_CACHE_STORE_BACKGROUND", "true"), True)
# Upload de cache é secundário: mantém uma fila pequena e serializada para não
# competir com síntese, playback e memória da VPS durante bursts.
TTS_TURBO_WORKER_CACHE_STORE_CONCURRENCY = max(1, _parse_int(os.getenv("TTS_TURBO_WORKER_CACHE_STORE_CONCURRENCY", "1"), 1))
TTS_TURBO_WORKER_CACHE_STORE_MAX_PENDING = max(TTS_TURBO_WORKER_CACHE_STORE_CONCURRENCY, _parse_int(os.getenv("TTS_TURBO_WORKER_CACHE_STORE_MAX_PENDING", "6"), 6))
TTS_TURBO_WORKER_CACHE_MISS_COOLDOWN_SECONDS = max(1.0, _parse_float(os.getenv("TTS_TURBO_WORKER_CACHE_MISS_COOLDOWN_SECONDS", "45.0"), 45.0))
TTS_TURBO_WORKER_CACHE_ERROR_COOLDOWN_SECONDS = max(1.0, _parse_float(os.getenv("TTS_TURBO_WORKER_CACHE_ERROR_COOLDOWN_SECONDS", "10.0"), 10.0))
TTS_TURBO_WORKER_CACHE_INDEX_MAX_ENTRIES = max(128, _parse_int(os.getenv("TTS_TURBO_WORKER_CACHE_INDEX_MAX_ENTRIES", "4096"), 4096))

# TTS Agent: rota normal do TTS pelo phone-worker quando ele estiver online/saudável.
# O health loop mantém o modo em cache; a fala não faz probe antes de sintetizar.
TTS_WORKER_AGENT_ENABLED = _parse_bool(os.getenv("TTS_WORKER_AGENT_ENABLED", "true"), True)
TTS_WORKER_AGENT_HEALTH_INTERVAL_SECONDS = max(5.0, _parse_float(os.getenv("TTS_WORKER_AGENT_HEALTH_INTERVAL_SECONDS", "20"), 20.0))
TTS_WORKER_AGENT_HEALTH_TIMEOUT_SECONDS = max(0.4, _parse_float(os.getenv("TTS_WORKER_AGENT_HEALTH_TIMEOUT_SECONDS", "2.5"), 2.5))
TTS_WORKER_AGENT_STALE_SECONDS = max(10.0, _parse_float(os.getenv("TTS_WORKER_AGENT_STALE_SECONDS", "75"), 75.0))
TTS_WORKER_AGENT_FAILURE_THRESHOLD = max(1, _parse_int(os.getenv("TTS_WORKER_AGENT_FAILURE_THRESHOLD", "2"), 2))
TTS_WORKER_AGENT_FAILURE_COOLDOWN_SECONDS = max(5.0, _parse_float(os.getenv("TTS_WORKER_AGENT_FAILURE_COOLDOWN_SECONDS", "45"), 45.0))
TTS_WORKER_AGENT_SYNTH_TIMEOUT_SECONDS = max(2.0, _parse_float(os.getenv("TTS_WORKER_AGENT_SYNTH_TIMEOUT_SECONDS", "10"), 10.0))
TTS_WORKER_AGENT_BUSY_RETRY_ATTEMPTS = max(0, _parse_int(os.getenv("TTS_WORKER_AGENT_BUSY_RETRY_ATTEMPTS", "2"), 2))
TTS_WORKER_AGENT_BUSY_RETRY_DELAY_SECONDS = max(0.05, _parse_float(os.getenv("TTS_WORKER_AGENT_BUSY_RETRY_DELAY_SECONDS", "0.35"), 0.35))
TTS_WORKER_AGENT_MAX_AUDIO_MB = max(1, _parse_int(os.getenv("TTS_WORKER_AGENT_MAX_AUDIO_MB", "8"), 8))
TTS_WORKER_AGENT_MAX_TEXT_LENGTH = max(64, _parse_int(os.getenv("TTS_WORKER_AGENT_MAX_TEXT_LENGTH", "1200"), 1200))
TTS_WORKER_AGENT_PREFERRED_ENGINE = (os.getenv("TTS_WORKER_AGENT_PREFERRED_ENGINE", "auto") or "auto").strip().lower().replace("-", "_") or "auto"
# Health leve + rota adaptativa: não deixa o TTS esperar worker offline/lento.
TTS_WORKER_AGENT_HEALTH_FAILURE_THRESHOLD = max(1, _parse_int(os.getenv("TTS_WORKER_AGENT_HEALTH_FAILURE_THRESHOLD", "3"), 3))
TTS_WORKER_AGENT_RAW_AUDIO_ENABLED = _parse_bool(os.getenv("TTS_WORKER_AGENT_RAW_AUDIO_ENABLED", "true"), True)
TTS_WORKER_AGENT_ADAPTIVE_ROUTING_ENABLED = _parse_bool(os.getenv("TTS_WORKER_AGENT_ADAPTIVE_ROUTING_ENABLED", "true"), True)
TTS_WORKER_AGENT_ALWAYS_WORKER_ENGINES = (os.getenv("TTS_WORKER_AGENT_ALWAYS_WORKER_ENGINES", "android_native,teto") or "android_native,teto").strip().lower().replace("-", "_")
TTS_WORKER_AGENT_GTTS_MIN_WORKER_CHARS = max(0, _parse_int(os.getenv("TTS_WORKER_AGENT_GTTS_MIN_WORKER_CHARS", "120"), 120))
TTS_WORKER_AGENT_WORKER_SLOW_MARGIN = max(1.0, _parse_float(os.getenv("TTS_WORKER_AGENT_WORKER_SLOW_MARGIN", "1.15"), 1.15))
TTS_WORKER_AGENT_WORKER_MIN_ADVANTAGE_MS = max(0.0, _parse_float(os.getenv("TTS_WORKER_AGENT_WORKER_MIN_ADVANTAGE_MS", "120"), 120.0))

# Worker Voice Agent roadmap: the VPS remains the bot/control plane, while the
# turbo worker becomes the direct audio/voice plane when this staged path is ready.
WORKER_VOICE_AGENT_ENABLED = (not CORE_WORKER_APK_REPLACES_TERMUX) and _parse_bool(os.getenv("WORKER_VOICE_AGENT_ENABLED", "true"), True)
WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED", "true"), True)
WORKER_VOICE_AGENT_DIRECT_TTS_AUTO_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_DIRECT_TTS_AUTO_ENABLED", "true"), True)
# gTTS mantém a call na VPS por padrão. O worker adaptativo ainda pode sintetizar
# e devolver bytes, mas sem pagar a transferência de propriedade da voz.
WORKER_VOICE_AGENT_DIRECT_GTTS_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_DIRECT_GTTS_ENABLED", "false"), False)
WORKER_VOICE_AGENT_DIRECT_TTS_MAX_CHARS = max(16, _parse_int(os.getenv("WORKER_VOICE_AGENT_DIRECT_TTS_MAX_CHARS", "600"), 600))
WORKER_VOICE_AGENT_DIRECT_TTS_TIMEOUT_SECONDS = max(3.0, _parse_float(os.getenv("WORKER_VOICE_AGENT_DIRECT_TTS_TIMEOUT_SECONDS", "30"), 30.0))
WORKER_VOICE_AGENT_DIRECT_TTS_FAILURE_COOLDOWN_SECONDS = max(5.0, _parse_float(os.getenv("WORKER_VOICE_AGENT_DIRECT_TTS_FAILURE_COOLDOWN_SECONDS", "45"), 45.0))
WORKER_VOICE_AGENT_SHARED_SESSION_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_SHARED_SESSION_ENABLED", "true"), True)
WORKER_VOICE_AGENT_SESSION_REPORT_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_SESSION_REPORT_ENABLED", "true"), True)
WORKER_VOICE_AGENT_SESSION_REPORT_TIMEOUT_SECONDS = max(0.6, _parse_float(os.getenv("WORKER_VOICE_AGENT_SESSION_REPORT_TIMEOUT_SECONDS", "1.5"), 1.5))
WORKER_VOICE_AGENT_SESSION_TTL_SECONDS = max(30.0, _parse_float(os.getenv("WORKER_VOICE_AGENT_SESSION_TTL_SECONDS", "180"), 180.0))
WORKER_VOICE_AGENT_SESSION_REPORT_MIN_INTERVAL_SECONDS = max(3.0, _parse_float(os.getenv("WORKER_VOICE_AGENT_SESSION_REPORT_MIN_INTERVAL_SECONDS", "15"), 15.0))
WORKER_VOICE_AGENT_HANDOFF_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_HANDOFF_ENABLED", "true"), True)
WORKER_VOICE_AGENT_HANDOFF_TTL_SECONDS = max(10.0, _parse_float(os.getenv("WORKER_VOICE_AGENT_HANDOFF_TTL_SECONDS", "60"), 60.0))
WORKER_VOICE_AGENT_HANDOFF_TIMEOUT_SECONDS = max(0.6, _parse_float(os.getenv("WORKER_VOICE_AGENT_HANDOFF_TIMEOUT_SECONDS", "1.5"), 1.5))
WORKER_VOICE_AGENT_TRANSFER_CONTROL_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_TRANSFER_CONTROL_ENABLED", "true"), True)
WORKER_VOICE_AGENT_TRANSFER_PREPARE_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_TRANSFER_PREPARE_ENABLED", "true"), True)
WORKER_VOICE_AGENT_TRANSFER_TIMEOUT_SECONDS = max(0.6, _parse_float(os.getenv("WORKER_VOICE_AGENT_TRANSFER_TIMEOUT_SECONDS", "1.5"), 1.5))
WORKER_VOICE_AGENT_TRANSFER_LEASE_TTL_SECONDS = max(10.0, _parse_float(os.getenv("WORKER_VOICE_AGENT_TRANSFER_LEASE_TTL_SECONDS", "45"), 45.0))
# Dry-run de conexão continua disponível para diagnóstico/manual, mas não deve abrir
# uma segunda conexão Voice WS/UDP automaticamente enquanto a VPS é dona da voz.
WORKER_VOICE_AGENT_CONNECTION_DRY_RUN_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_CONNECTION_DRY_RUN_ENABLED", "true"), True)
WORKER_VOICE_AGENT_CONNECTION_AUTO_PROBE_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_CONNECTION_AUTO_PROBE_ENABLED", "false"), False)
WORKER_VOICE_AGENT_CONNECTION_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("WORKER_VOICE_AGENT_CONNECTION_TIMEOUT_SECONDS", "4.0"), 4.0))
WORKER_VOICE_AGENT_CONNECTION_REPORT_TIMEOUT_SECONDS = max(0.6, _parse_float(os.getenv("WORKER_VOICE_AGENT_CONNECTION_REPORT_TIMEOUT_SECONDS", "1.5"), 1.5))

# Texto longo começa a tocar mais rápido: divide em blocos naturais e enfileira
# partes menores, permitindo prefetch do próximo áudio durante o playback atual.
TTS_LONG_TEXT_CHUNK_ENABLED = _parse_bool(os.getenv("TTS_LONG_TEXT_CHUNK_ENABLED", "true"), True)
TTS_LONG_TEXT_CHUNK_MAX_CHARS = max(160, _parse_int(os.getenv("TTS_LONG_TEXT_CHUNK_MAX_CHARS", "420"), 420))
# Edge já entrega áudio progressivamente e aceita até 4096 bytes escapados por
# conexão. O limite conservador reduz novas conexões/FFmpegs em textos longos.
TTS_EDGE_LONG_TEXT_CHUNK_MAX_BYTES = min(3800, max(512, _parse_int(os.getenv("TTS_EDGE_LONG_TEXT_CHUNK_MAX_BYTES", "3000"), 3000)))
TTS_LONG_TEXT_CHUNK_MAX_PARTS = max(1, _parse_int(os.getenv("TTS_LONG_TEXT_CHUNK_MAX_PARTS", "8"), 8))
# Índice do cache local é verificado periodicamente, em vez de varrer todos os
# arquivos após cada fala.
TTS_CACHE_INDEX_SWEEP_INTERVAL_SECONDS = max(5.0, _parse_float(os.getenv("TTS_CACHE_INDEX_SWEEP_INTERVAL_SECONDS", "30"), 30.0))
TTS_CACHE_INDEX_SWEEP_MAX_ENTRIES = max(4, _parse_int(os.getenv("TTS_CACHE_INDEX_SWEEP_MAX_ENTRIES", "32"), 32))

# TTS shared streaming: compressed audio budgets, bounded decoder overlap.
TTS_STREAM_MEMORY_BUDGET_BYTES = max(1048576, _parse_int(os.getenv("TTS_STREAM_MEMORY_BUDGET_BYTES", "16777216"), 16777216))
TTS_FFMPEG_OVERLAP_ENABLED = _parse_bool(os.getenv("TTS_FFMPEG_OVERLAP_ENABLED", "true"), True)
TTS_FFMPEG_OVERLAP_CONCURRENCY = max(1, min(4, _parse_int(os.getenv("TTS_FFMPEG_OVERLAP_CONCURRENCY", "2"), 2)))
# Optional: background Opus preparation consumes CPU; enable after measuring the VPS.
TTS_PREPARED_OPUS_CACHE_ENABLED = _parse_bool(os.getenv("TTS_PREPARED_OPUS_CACHE_ENABLED", "false"), False)
TTS_PREPARED_OPUS_CACHE_MAX_BYTES = max(524288, _parse_int(os.getenv("TTS_PREPARED_OPUS_CACHE_MAX_BYTES", "8388608"), 8388608))
