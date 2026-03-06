import os

TOKEN = os.getenv("TOKEN", "")
MONGO_URI = os.getenv("MONGO_URI", "")
PORT = int(os.getenv("PORT", "10000"))

TTS_ENABLED = os.getenv("TTS_ENABLED", "true").lower() == "true"
TTS_IDLE_DISCONNECT_SECONDS = int(os.getenv("TTS_IDLE_DISCONNECT_SECONDS", "180"))

VOICE_BOT_ID = int(os.getenv("VOICE_BOT_ID", "0") or "0")
BLOCK_VOICE_BOT_ID = int(os.getenv("BLOCK_VOICE_BOT_ID", "0") or "0")
ONLY_TTS_USER_ID = int(os.getenv("ONLY_TTS_USER_ID", "0") or "0")

GTTS_DEFAULT_LANGUAGE = os.getenv("GTTS_DEFAULT_LANGUAGE", "pt-br")
