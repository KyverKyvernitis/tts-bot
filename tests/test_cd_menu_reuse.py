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
