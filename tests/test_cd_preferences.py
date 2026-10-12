from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from test_ttv_preferences import settings


DEFAULT_MELODY = "ttv-demo-v1"


def test_default_melody_read_does_not_create_a_user_or_write(settings):
    assert settings.get_user_tts(1, 2)["ttv_melody_id"] == ""
    assert settings.resolve_tts(1, 2)["ttv_melody_id"] == DEFAULT_MELODY
    assert settings.user_cache == {}
    settings.coll.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_saving_melody_preserves_complete_ttv_and_other_tts_settings(settings):
    original_tts = {
        "engine": "edge", "voice": "pt-BR-AntonioNeural", "language": "pt-br",
        "rate": "+25%", "pitch": "+35Hz", "android_voice": "AndroidVoice",
        "android_language": "ja-JP", "android_rate": "1.25", "android_pitch": "0.8",
        "ttv_voice_id": "hatsune-miku", "ttv_language": "en-US",
        "ttv_pitch_semitones": "-2.0", "teto_pitch_semitones": "-2.0",
        "ttv_speech_rate": 1.15, "advanced_nightcore_level": 2,
        "advanced_slowed_level": 1, "advanced_reverb_level": 3,
        "speaker_name": "Pessoa", "future_preference": "preserved",
    }
    settings.user_cache[(1, 2)] = {"tts": deepcopy(original_tts), "chips": 150}
    await settings.set_user_tts(1, 2, ttv_melody_id=DEFAULT_MELODY)
    saved = settings.coll.update_one.await_args.args[1]["$set"]
    assert saved["tts"] == {**original_tts, "ttv_melody_id": DEFAULT_MELODY}
    assert saved["chips"] == 150
    assert settings.get_user_tts(1, 2)["ttv_melody_id"] == DEFAULT_MELODY
    assert settings.resolve_tts(1, 2)["ttv_voice_id"] == "hatsune-miku"
    assert settings.resolve_tts(1, 2)["ttv_language"] == "en-US"
    assert settings.resolve_tts(1, 2)["ttv_speech_rate"] == 1.15


@pytest.mark.asyncio
async def test_melody_persists_through_database_cache_reload(settings):
    await settings.set_user_tts(1, 2, ttv_melody_id=DEFAULT_MELODY)
    stored = deepcopy(settings.coll.update_one.await_args.args[1]["$set"])

    async def cursor():
        yield deepcopy(stored)

    settings.coll.find = lambda *_args: cursor()
    settings.user_cache.clear()
    settings._resolved_tts_cache[(1, 2)] = {"ttv_melody_id": "stale"}
    await settings.load_cache()
    assert settings.get_user_tts(1, 2)["ttv_melody_id"] == DEFAULT_MELODY
    assert settings.resolve_tts(1, 2)["ttv_melody_id"] == DEFAULT_MELODY


@pytest.mark.asyncio
async def test_melody_save_invalidates_only_target_user_guild_cache(settings):
    settings.user_cache[(1, 2)] = {"tts": {"ttv_melody_id": "old-unavailable"}}
    settings.user_cache[(1, 3)] = {"tts": {"ttv_melody_id": "other-user"}}
    settings.user_cache[(2, 2)] = {"tts": {"ttv_melody_id": "other-guild"}}
    for key in settings.user_cache:
        settings.resolve_tts(*key)
    before_users = deepcopy(settings.user_cache)
    other_user_resolved = settings._resolved_tts_cache[(1, 3)]
    other_guild_resolved = settings._resolved_tts_cache[(2, 2)]
    await settings.set_user_tts(1, 2, ttv_melody_id=DEFAULT_MELODY)
    assert (1, 2) not in settings._resolved_tts_cache
    assert settings._resolved_tts_cache[(1, 3)] is other_user_resolved
    assert settings._resolved_tts_cache[(2, 2)] is other_guild_resolved
    assert settings.user_cache[(1, 3)] == before_users[(1, 3)]
    assert settings.user_cache[(2, 2)] == before_users[(2, 2)]
    assert settings.resolve_tts(1, 2)["ttv_melody_id"] == DEFAULT_MELODY
    assert settings.coll.update_one.await_args.args[0] == {
        "type": "user", "guild_id": 1, "user_id": 2,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["unknown-melody", "../../melody.mid", "https://example.com/melody.mid", True, 42])
async def test_invalid_melody_rejects_entire_edit_before_database_or_cache(settings, invalid):
    settings.user_cache[(1, 2)] = {"tts": {"voice": "OldEdgeVoice"}}
    settings.resolve_tts(1, 2)
    before_users = deepcopy(settings.user_cache)
    before_resolved = deepcopy(settings._resolved_tts_cache)
    with pytest.raises(ValueError):
        await settings.set_user_tts(1, 2, voice="NewEdgeVoice", ttv_melody_id=invalid)
    assert settings.user_cache == before_users
    assert settings._resolved_tts_cache == before_resolved
    settings.coll.update_one.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["", " ttv-demo-v1 "])
async def test_melody_save_canonicalizes_default_identifier(settings, selection):
    await settings.set_user_tts(1, 2, ttv_melody_id=selection)
    assert settings.get_user_tts(1, 2)["ttv_melody_id"] == DEFAULT_MELODY
    assert settings.coll.update_one.await_args.args[1]["$set"]["tts"]["ttv_melody_id"] == DEFAULT_MELODY


@pytest.mark.parametrize("engine", ["edge", "gtts"])
def test_unknown_saved_melody_is_preserved_without_blocking_other_engines(settings, engine):
    settings.user_cache[(1, 2)] = {"tts": {
        "ttv_melody_id": "unavailable-song", "engine": engine,
    }}
    assert settings.get_user_tts(1, 2)["ttv_melody_id"] == "unavailable-song"
    resolved = settings.resolve_tts(1, 2)
    assert resolved["ttv_melody_id"] == "unavailable-song"
    assert resolved["engine"] == engine
    settings.coll.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_melody_save_leaves_prior_user_and_resolved_cache(settings):
    settings.user_cache[(1, 2)] = {"tts": {
        "ttv_melody_id": "old-song", "ttv_language": "en-US", "voice": "OldEdgeVoice",
    }}
    settings.resolve_tts(1, 2)
    before_users = deepcopy(settings.user_cache)
    before_resolved = deepcopy(settings._resolved_tts_cache)

    async def failed_update(*_args, **_kwargs):
        assert settings.user_cache == before_users
        assert settings._resolved_tts_cache == before_resolved
        raise RuntimeError("database unavailable")

    settings.coll.update_one.side_effect = failed_update
    with pytest.raises(RuntimeError, match="database unavailable"):
        await settings.set_user_tts(1, 2, ttv_melody_id=DEFAULT_MELODY)
    assert settings.user_cache == before_users
    assert settings._resolved_tts_cache == before_resolved
    assert settings.resolve_tts(1, 2)["ttv_melody_id"] == "old-song"


@pytest.mark.asyncio
async def test_failed_first_melody_save_does_not_create_user(settings):
    settings.coll.update_one.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        await settings.set_user_tts(1, 2, ttv_melody_id=DEFAULT_MELODY)
    assert settings.user_cache == {}
    assert settings.resolve_tts(1, 2)["ttv_melody_id"] == DEFAULT_MELODY


@pytest.mark.asyncio
async def test_general_tts_reset_also_clears_personal_melody(settings):
    await settings.set_user_tts(1, 2, ttv_melody_id=DEFAULT_MELODY)
    assert await settings.reset_user_tts(1, 2)
    assert settings.get_user_tts(1, 2)["ttv_melody_id"] == ""
    assert settings.resolve_tts(1, 2)["ttv_melody_id"] == DEFAULT_MELODY


@pytest.mark.asyncio
async def test_regular_ttv_edit_keeps_selected_melody(settings):
    await settings.set_user_tts(1, 2, ttv_melody_id=DEFAULT_MELODY)
    await settings.set_user_tts(1, 2, ttv_voice_id="hatsune-miku", ttv_language="en-US")
    assert settings.get_user_tts(1, 2)["ttv_melody_id"] == DEFAULT_MELODY


@pytest.mark.asyncio
@pytest.mark.parametrize("first_edit", ["melody", "voice"])
async def test_concurrent_melody_and_voice_saves_preserve_both_acknowledged_edits(settings, first_edit):
    settings.user_cache[(1, 2)] = {"tts": {"voice": "OldEdgeVoice", "ttv_language": "pt-BR"}}
    first_started = asyncio.Event()
    first_acknowledged = asyncio.Event()
    persisted = []

    async def update(_query, update_doc, **_kwargs):
        if not first_started.is_set():
            first_started.set()
            await first_acknowledged.wait()
        persisted.append(deepcopy(update_doc["$set"]))

    settings.coll.update_one.side_effect = update
    melody_edit = {"ttv_melody_id": DEFAULT_MELODY}
    voice_edit = {"ttv_voice_id": "hatsune-miku", "ttv_language": "en-US"}
    first_values, second_values = (
        (melody_edit, voice_edit) if first_edit == "melody" else (voice_edit, melody_edit)
    )
    first = asyncio.create_task(settings.set_user_tts(1, 2, **first_values))
    await first_started.wait()
    second = asyncio.create_task(settings.set_user_tts(1, 2, **second_values))
    await asyncio.sleep(0)
    assert settings.coll.update_one.await_count == 1
    assert settings.user_cache[(1, 2)]["tts"] == {"voice": "OldEdgeVoice", "ttv_language": "pt-BR"}
    first_acknowledged.set()
    await asyncio.gather(first, second)
    expected = {
        "voice": "OldEdgeVoice", "ttv_language": "en-US",
        "ttv_voice_id": "hatsune-miku", "ttv_melody_id": DEFAULT_MELODY,
    }
    assert settings.user_cache[(1, 2)]["tts"] == persisted[-1]["tts"] == expected
    assert settings.resolve_tts(1, 2)["ttv_melody_id"] == DEFAULT_MELODY
    assert settings.resolve_tts(1, 2)["ttv_voice_id"] == "hatsune-miku"
    assert len(persisted) == 2
    assert not settings._user_tts_locks


@pytest.mark.asyncio
async def test_slow_melody_save_does_not_block_different_user(settings):
    first_started = asyncio.Event()
    first_acknowledged = asyncio.Event()

    async def update(query, _update_doc, **_kwargs):
        if query["user_id"] == 2:
            first_started.set()
            await first_acknowledged.wait()

    settings.coll.update_one.side_effect = update
    first = asyncio.create_task(settings.set_user_tts(1, 2, ttv_melody_id=DEFAULT_MELODY))
    await first_started.wait()
    try:
        await asyncio.wait_for(settings.set_user_tts(1, 3, ttv_melody_id=DEFAULT_MELODY), timeout=1)
        assert (1, 2) not in settings.user_cache
        assert settings.get_user_tts(1, 3)["ttv_melody_id"] == DEFAULT_MELODY
    finally:
        first_acknowledged.set()
        await first


@pytest.mark.asyncio
async def test_failed_first_save_releases_same_user_for_next_edit(settings):
    settings.user_cache[(1, 2)] = {"tts": {"ttv_melody_id": "old-song"}}
    first_started = asyncio.Event()
    first_finished = asyncio.Event()

    async def update(_query, update_doc, **_kwargs):
        if update_doc["$set"]["tts"].get("ttv_melody_id") == DEFAULT_MELODY:
            first_started.set()
            await first_finished.wait()
            raise RuntimeError("database unavailable")

    settings.coll.update_one.side_effect = update
    first = asyncio.create_task(settings.set_user_tts(1, 2, ttv_melody_id=DEFAULT_MELODY))
    await first_started.wait()
    second = asyncio.create_task(settings.set_user_tts(1, 2, ttv_language="en-US"))
    await asyncio.sleep(0)
    first_finished.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], RuntimeError)
    assert results[1] is None
    assert settings.user_cache[(1, 2)]["tts"] == {"ttv_melody_id": "old-song", "ttv_language": "en-US"}
    assert settings.resolve_tts(1, 2)["ttv_melody_id"] == "old-song"
    # The retained exception traceback may still reference its released lock.
    assert all(not lock.locked() for lock in settings._user_tts_locks.values())

