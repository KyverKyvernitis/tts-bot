"""CD menu: approved notices, option metadata and app emoji reuse."""
from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from PIL import Image

from cogs.tts.interface.application_emojis import ApplicationSelectEmojis, emoji_name, square_emoji
from cogs.tts.interface.painel_canto import CDPanelView
from cogs.tts.mensagens.preparacao import _reply_ttv_error
from cogs.tts.failure_notices import _public_notice


def test_cd_panel_approved_title_details_and_no_select_descriptions():
    cog = SimpleNamespace(_get_db=lambda: None)
    view = CDPanelView(cog, 1, 2)
    children = view.to_components()[0]["components"]
    header = children[0]["content"]
    assert header == ("## <a:Vinyl_Music_loading:1559374199133315122> Menu de CDs\n"
                      "-# Escolha seu CD, o bot vai ler e no modo de canto vai cantar no ritmo dele.")
    contents = [component.get("content", "") for component in children]
    assert all("Em uso:" not in text and "Sua escolha neste servidor" not in text for text in contents)
    assert any("4 notas · 4 s" in text for text in contents)
    select = next(component for component in children if component["type"] == 1 and component["components"][0]["type"] == 3)
    assert all("description" not in option for option in select["components"][0]["options"])


def test_canto_notices_are_exact():
    exact = "Não consegui cantar isso pois essa voz está desativada, ou só por um erro msm sla"
    assert all(_public_notice(code) == exact for code in (
        "ttv_singing_failed", "ttv_singing_timeout", "ttv_singing_bank_missing"
    ))

    async def run():
        message = SimpleNamespace(reply=AsyncMock())
        await _reply_ttv_error(message, "No canto, use ~ depois de uma sílaba para continuar sua vogal.")
        assert message.reply.await_args.args[0] == (
            "-# No modo de canto, use ~ depois de uma sílaba para continuar sua vogal, com espaçamento"
        )
        assert message.reply.await_args.kwargs["allowed_mentions"].to_dict()["parse"] == []
    asyncio.run(run())


def test_app_emojis_are_named_stably_and_compressed():
    first = emoji_name("osakacd", "world-is-mine")
    assert first == emoji_name("osakacd", "world-is-mine")
    assert first != emoji_name("osakacd", "senbonzakura")
    assert 2 <= len(first) <= 32
    stream = io.BytesIO()
    Image.new("RGB", (640, 360)).save(stream, "JPEG")
    image = square_emoji(stream.getvalue())
    with Image.open(io.BytesIO(image)) as picture:
        assert picture.size == (128, 128)
        assert picture.format == "JPEG"


def test_app_emojis_provision_once_reuse_across_panels_and_restart():
    async def run():
        stream = io.BytesIO()
        Image.new("RGB", (640, 360)).save(stream, "JPEG")
        source = stream.getvalue()
        created = []
        remote = []
        async def fetch():
            return list(remote)
        async def create(*, name, image):
            created.append(name)
            obj = SimpleNamespace(name=name, id=100000000000000000 + len(created))
            remote.append(obj)
            return obj
        bot = SimpleNamespace(fetch_application_emojis=AsyncMock(side_effect=fetch),
                              create_application_emoji=AsyncMock(side_effect=create))
        manager = ApplicationSelectEmojis("osakacd", lambda item: source, lambda: ["world-is-mine", "melt"])
        manager.schedule(bot)
        await manager._task
        assert len(created) == 2
        emoji = manager.emoji("melt")
        assert isinstance(emoji, discord.PartialEmoji)
        assert emoji.id > 0
        # A second menu uses IDs already resolved, and never uploads again.
        manager.schedule(bot)
        assert len(created) == 2
        restarted = ApplicationSelectEmojis("osakacd", lambda item: source, lambda: ["world-is-mine", "melt"])
        restarted.schedule(bot)
        await restarted._task
        assert len(created) == 2
        assert restarted.emoji("world-is-mine") is not None
    asyncio.run(run())


def test_app_emojis_without_cover_or_permission_fail_open():
    async def run():
        bot = SimpleNamespace(fetch_application_emojis=AsyncMock(return_value=[]),
                              create_application_emoji=AsyncMock())
        manager = ApplicationSelectEmojis("osakacd", lambda item: None, lambda: ["world-is-mine"])
        manager.schedule(bot)
        await manager._task
        bot.create_application_emoji.assert_not_awaited()
        assert manager.emoji("world-is-mine") is None
    asyncio.run(run())


def test_cd_notes_rendered_below_count_in_real_midi_order():
    from cogs.tts.melodies import format_score_notes, get_melody, list_melodies

    default = get_melody()
    assert format_score_notes(default) == "C4 · E4 · G4 · E4"
    for melody in list_melodies():
        score = __import__("json").loads(melody.score_json)
        note_names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
        expected = " · ".join(
            f"{note_names[entry['midi_note'] % 12]}{entry['midi_note'] // 12 - 1}"
            for entry in score["notes"]
        )
        assert format_score_notes(melody) == expected
        assert len(score["notes"]) == melody.note_count

    cog = SimpleNamespace(_get_db=lambda: None)
    view = CDPanelView(cog, 1, 2)
    content = "\n".join(c["content"] for c in view.to_components()[0]["components"] if "content" in c)
    assert "4 notas · 4 s\nC4 · E4 · G4 · E4" in content


def test_cd_button_states_and_custom_animated_emojis():
    cog = SimpleNamespace(_get_db=lambda: None)
    view = CDPanelView(cog, 1, 2)
    components = list(view.to_components()[0]["components"])
    buttons = next(c["components"] for c in components if c["type"] == 1 and c["components"][0]["type"] == 2)
    apply = next(b for b in buttons if b["custom_id"] == "ttv:cd:apply")
    assert apply["label"] == "CD já em uso"
    assert apply["disabled"] is True
    assert str(apply["emoji"]["id"]) == "1559374199133315122"
    assert apply["emoji"]["animated"] is True

    view.active_melody_id = "world-is-mine"
    view._settings_available = True
    view._rebuild()
    components = list(view.to_components()[0]["components"])
    buttons = next(c["components"] for c in components if c["type"] == 1 and c["components"][0]["type"] == 2)
    apply = next(b for b in buttons if b["custom_id"] == "ttv:cd:apply")
    assert apply["label"] == "Usar esse CD"
    assert apply["disabled"] is False
    assert str(apply["emoji"]["id"]) == "1551635281738858670"
    assert apply["emoji"]["animated"] is True


def test_avatar_app_emoji_cached_and_rotates_when_avatar_changes():
    from cogs.tts.interface.painel_canto import _avatar_asset_id, _avatar_png

    async def run():
        stream = io.BytesIO()
        Image.new("RGBA", (256, 256), (40, 80, 255, 255)).save(stream, "PNG")
        image = stream.getvalue()
        avatar = SimpleNamespace(key="avatar-a", read=AsyncMock(return_value=image))
        bot = SimpleNamespace(user=SimpleNamespace(display_avatar=avatar))
        entries = []
        async def fetch():
            return list(entries)
        async def create(*, name, image):
            with Image.open(io.BytesIO(image)) as im:
                assert im.format == "JPEG" and im.size == (128, 128)
            entry = SimpleNamespace(name=name, id=100000000000000000 + len(entries))
            entries.append(entry)
            return entry
        bot.fetch_application_emojis = AsyncMock(side_effect=fetch)
        bot.create_application_emoji = AsyncMock(side_effect=create)
        manager = ApplicationSelectEmojis(
            "osakacd", lambda key: None, lambda: (),
            dynamic_ids=lambda b: (_avatar_asset_id(b),), remote_assets=_avatar_png,
        )
        manager.schedule(bot)
        await manager._task
        assert len(entries) == 1
        assert manager.emoji(_avatar_asset_id(bot)).id == entries[0].id
        manager.schedule(bot)
        assert len(entries) == 1
        avatar.key = "avatar-b"
        manager.schedule(bot)
        await manager._task
        assert len(entries) == 2
        assert manager.emoji(_avatar_asset_id(bot)).id == entries[1].id
        assert avatar.read.await_count == 2

        fresh = ApplicationSelectEmojis(
            "osakacd", lambda key: None, lambda: (),
            dynamic_ids=lambda b: (_avatar_asset_id(b),), remote_assets=_avatar_png,
        )
        fresh.schedule(bot)
        await fresh._task
        assert len(entries) == 2
        assert fresh.emoji(_avatar_asset_id(bot)).id == entries[1].id
    asyncio.run(run())
