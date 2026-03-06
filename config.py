import os

TOKEN = os.getenv("DISCORD_TOKEN")

TARGET_ROLE_ID = int(os.getenv("ROLE_ID", "0"))
DISABLE_TIME = int(os.getenv("DISABLE_TIME", "14400"))

TRIGGER_WORD = os.getenv("TRIGGER_WORD", "").lower().strip()
MUTE_TOGGLE_WORD = os.getenv("MUTE_TOGGLE_WORD", "rola").lower().strip()
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID", "0"))

TTS_ENABLED = os.getenv("TTS_ENABLED", "true").lower().strip() in ("1", "true", "yes", "y", "on")

# ✅ ALTERADO: tira espaços e evita virar 0 por string vazia
BLOCK_VOICE_BOT_ID = int((os.getenv("BLOCK_VOICE_BOT_ID", "0") or "0").strip())

PORT = int(os.getenv("PORT", "10000"))

MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DB = os.getenv("MONGODB_DB", "chat_revive").strip()
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "settings").strip()

GUILD_IDS = [
    1313883930637762560,
    1349910251117350923,
]

ON_COLOR = 0x57F287
OFF_COLOR = 0xED4245
