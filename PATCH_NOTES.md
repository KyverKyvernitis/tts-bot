# Patch ATTS panel / Piper legacy removal

- Adds ATTS to the public TTS panel and settings menus.
- Makes %texto route through android_native / ATTS.
- Adds ATTS settings: language, voice, rate, pitch.
- Adds ATTS prefix storage/configuration.
- Removes Piper from normal TTS Agent advertised engines, benchmark list and public/default flows.
- Keeps legacy Piper functions isolated for compatibility/rollback only.

Validation:
- python3 -m py_compile selected files
- python3 -m pytest tests/test_tts_helpers.py tests/test_tts_message_flow.py -q
