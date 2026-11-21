from types import SimpleNamespace

from cogs.tts import first_audio, providers_rust


def test_segmentation_requires_guild_opt_in_and_a_supporting_extension(monkeypatch):
    monkeypatch.setenv("TTS_FIRST_AUDIO_GUILDS", "123, 456, *")
    monkeypatch.setenv("TTS_FIRST_AUDIO_SEGMENTS", "1")
    monkeypatch.delenv("TTS_NATIVE_EXISTING_VOICE_ENABLED", raising=False)
    monkeypatch.setenv("TTS_PROVIDERS_RUST", "1")
    item = SimpleNamespace(engine="edge", guild_id=123)

    monkeypatch.setattr(providers_rust, "native_module", lambda: SimpleNamespace())
    assert not first_audio.segments_for(item)
    monkeypatch.setattr(providers_rust, "native_module", lambda: SimpleNamespace(NATIVE_STARTUP_SEGMENTS_VERSION=1))
    assert first_audio.segments_for(item)
    assert not first_audio.enabled_for(123, "route")
    assert not first_audio.segments_for(SimpleNamespace(engine="edge", guild_id=999))
    assert not first_audio.segments_for(SimpleNamespace(engine="teto", guild_id=123))
