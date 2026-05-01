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
DEVAI_PROVIDER_ORDER = _parse_csv(
    os.getenv("DEVAI_PROVIDER_ORDER", "groq,gemini,openrouter,cerebras,cloudflare,huggingface,pollinations")
)
DEVAI_PROVIDER_TIMEOUT_SECONDS = _parse_int(os.getenv("DEVAI_PROVIDER_TIMEOUT_SECONDS", "60"), 60)
DEVAI_MAX_OUTPUT_TOKENS = _parse_int(os.getenv("DEVAI_MAX_OUTPUT_TOKENS", "16000"), 16000)
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

DEVAI_GEMINI_MODEL = (os.getenv("DEVAI_GEMINI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash").strip()
DEVAI_GROQ_BASE_URL = (os.getenv("DEVAI_GROQ_BASE_URL", "https://api.groq.com/openai/v1") or "https://api.groq.com/openai/v1").strip()
DEVAI_GROQ_MODEL = (os.getenv("DEVAI_GROQ_MODEL", "openai/gpt-oss-120b") or "openai/gpt-oss-120b").strip()
# OpenRouter free dá acesso a Qwen3-Coder e DeepSeek R1 sem cartão.
# Use ":free" no sufixo pra forçar tier gratuito. Limites ~20 RPM, ~200 RPD/modelo.
DEVAI_OPENROUTER_BASE_URL = (os.getenv("DEVAI_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1") or "https://openrouter.ai/api/v1").strip()
DEVAI_OPENROUTER_MODEL = (os.getenv("DEVAI_OPENROUTER_MODEL", "qwen/qwen3-coder:free") or "qwen/qwen3-coder:free").strip()
DEVAI_OPENROUTER_REFERER = (os.getenv("DEVAI_OPENROUTER_REFERER", "") or "").strip()
# Cerebras: gpt-oss-120b virou indisponível pra free tier ("temporarily reduced").
# qwen-3-32b é stable, free tier e mais rápido pra prompts médios.
# Confirmar que o model id da sua conta é esse — se não for, troque pra "llama3.1-8b".
DEVAI_CEREBRAS_BASE_URL = (os.getenv("DEVAI_CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1") or "https://api.cerebras.ai/v1").strip()
DEVAI_CEREBRAS_MODEL = (os.getenv("DEVAI_CEREBRAS_MODEL", "qwen-3-32b") or "qwen-3-32b").strip()
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
# Cortes agressivos pra caber no free tier dos providers (Groq tem TPM 8000,
# Cloudflare context max 32768). Antes os defaults eram muito grandes e
# resultavam em HTTP 413/400 em quase todos os providers.
DEVAI_MAX_INDEX_CHARS = _parse_int(os.getenv("DEVAI_MAX_INDEX_CHARS", "5000"), 5000)
DEVAI_MAX_CONTEXT_FILES = _parse_int(os.getenv("DEVAI_MAX_CONTEXT_FILES", "3"), 3)
DEVAI_MAX_FILE_CONTEXT_CHARS = _parse_int(os.getenv("DEVAI_MAX_FILE_CONTEXT_CHARS", "8000"), 8000)
DEVAI_MAX_FILES_PER_PATCH = _parse_int(os.getenv("DEVAI_MAX_FILES_PER_PATCH", "5"), 5)
DEVAI_MAX_FILE_BYTES = _parse_int(os.getenv("DEVAI_MAX_FILE_BYTES", "220000"), 220000)
# Estimativa hardcoded: ~4 chars por token. Se o prompt total estimado
# passar deste limite, o cog corta diff/contexto progressivamente.
DEVAI_MAX_PROMPT_CHARS = _parse_int(os.getenv("DEVAI_MAX_PROMPT_CHARS", "28000"), 28000)

# Comentário automático da DevAI para patches aceitos pelo auto-updater de ZIP.
DEVAI_PATCH_REVIEW_ENABLED = _parse_bool(os.getenv("DEVAI_PATCH_REVIEW_ENABLED", "true"), True)
DEVAI_PATCH_REVIEW_MAX_FILES = _parse_int(os.getenv("DEVAI_PATCH_REVIEW_MAX_FILES", "5"), 5)
DEVAI_PATCH_REVIEW_MAX_CHARS_PER_FILE = _parse_int(os.getenv("DEVAI_PATCH_REVIEW_MAX_CHARS_PER_FILE", "5000"), 5000)
DEVAI_PATCH_REVIEW_MAX_DIFF_CHARS = _parse_int(os.getenv("DEVAI_PATCH_REVIEW_MAX_DIFF_CHARS", "8000"), 8000)
# Timeout duro pro review inteiro (montar prompt + chamar IA + render +
# enviar). Sem isso, um provider lento poderia segurar o `_analysis_lock`
# indefinidamente e bloquear o próximo review.
DEVAI_PATCH_REVIEW_TIMEOUT_SECONDS = _parse_int(os.getenv("DEVAI_PATCH_REVIEW_TIMEOUT_SECONDS", "120"), 120)

# Histórico de patches recentes injetado no prompt — evita a IA repetir
# tentativas que falharam ou propor solução já aplicada.
DEVAI_HISTORY_ITEMS = _parse_int(os.getenv("DEVAI_HISTORY_ITEMS", "5"), 5)
DEVAI_HISTORY_MAX_AGE_SECONDS = _parse_int(os.getenv("DEVAI_HISTORY_MAX_AGE_SECONDS", str(7 * 24 * 3600)), 7 * 24 * 3600)
