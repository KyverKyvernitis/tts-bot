# Patch Core Linux Runtime v1 sem Termux

- Adds `CoreLinuxRuntimeManager` for a safe Core Linux v1 flow inside the APK.
- Makes the APK advertise real `supported_tasks` instead of an empty list.
- Enables safe rootfs scaffold status/prepare/validate/clean-staging jobs without Termux, Python, shell or Bedrock start.
- Adds `apk_core_linux_runtime_smoke_test` to validate JNI executor + rootfs scaffold + runtime state end-to-end.
- Keeps Bedrock/Box64/real distro startup blocked for future patches.
- Filters unsupported APK actions from the workers panel when a worker declares supported tasks.
- Replaces the Python-first APK log report with a lightweight Java snapshot.
- Bumps Core Worker APK to `0.5.55` / versionCode `70`.

Validation:
- python3 -m py_compile utility/commands/workers.py webserver.py
- javac syntax check of CoreLinuxRuntimeManager.java with minimal local stubs

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
