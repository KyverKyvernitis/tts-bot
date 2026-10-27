"""Personal CD shuffle, drawn once when a new singing payload is prepared."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import inspect
import logging
import random
import sys
import weakref

from .melodies import DEFAULT_MELODY_ID, get_melody, list_melodies
from osaka_cd.runtime import pin_snapshot

log = logging.getLogger(__name__)
_HISTORY_LIMIT = 2048


def available_random_melodies():
    """Prefer installed songs; the built-in melody remains a valid fallback."""
    with pin_snapshot():
        catalog = tuple(list_melodies())
    songs = tuple(melody for melody in catalog if melody.id != DEFAULT_MELODY_ID)
    return songs or catalog


def choose_synthesis_melody(cog, guild_id: int, user_id: int, preferences) -> str:
    """Freeze one choice in the caller's new payload, never in saved settings."""
    fixed = str(preferences.get("ttv_melody_id") or DEFAULT_MELODY_ID)
    if preferences.get("ttv_random_cd") is not True:
        return fixed
    pool = available_random_melodies()
    if not pool:
        raise ValueError("Nenhum CD está disponível agora. Tente novamente em instantes.")
    key = (int(guild_id), int(user_id))
    history = getattr(cog, "_random_cd_last", None)
    if not isinstance(history, OrderedDict):
        history = cog._random_cd_last = OrderedDict()
    previous = history.get(key, fixed)
    candidates = tuple(melody for melody in pool if melody.id != previous)
    selected = random.choice(candidates or pool).id
    history[key] = selected
    history.move_to_end(key)
    while len(history) > _HISTORY_LIMIT:
        history.popitem(last=False)
    return selected


async def _maybe_await(value):
    return await value if inspect.isawaitable(value) else value


async def _reply(message, embed):
    import discord
    sender = getattr(message, "reply", None)
    kwargs = {"embed": embed, "allowed_mentions": discord.AllowedMentions.none()}
    if callable(sender):
        kwargs["mention_author"] = False
    else:
        sender = message.channel.send
    await asyncio.wait_for(sender(**kwargs), timeout=10)


async def _refresh_open_panels(cog, guild_id, user_id):
    # An open CD panel must let its owner restore even the same fixed CD.
    # A closed panel has no module/view to import or refresh here.
    module = sys.modules.get("cogs.tts.interface.painel_canto")
    panels = [view for view in tuple(getattr(module, "_ACTIVE_PANELS", ()))
              if view.cog is cog and view.guild_id == guild_id and view.owner_id == user_id
              and not view._is_expired()]

    async def refresh(view):
        try:
            await asyncio.wait_for(view.refresh(force=True), timeout=6)
            await asyncio.wait_for(view._edit_panel(), timeout=8)
        except Exception:
            log.debug("[random_cd] open CD panel refresh deferred")
    if panels:
        await asyncio.gather(*(refresh(view) for view in panels))


async def toggle_random_cd(cog, message) -> bool:
    """Toggle the invoking person's persisted shuffle mode in this server."""
    import discord
    if not getattr(message, "guild", None):
        return False
    guild_id, user_id = message.guild.id, message.author.id
    locks = getattr(cog, "_random_cd_toggle_locks", None)
    if locks is None:
        locks = cog._random_cd_toggle_locks = weakref.WeakValueDictionary()
    key = (guild_id, user_id)
    lock = locks.get(key)
    if lock is None:
        lock = locks[key] = asyncio.Lock()
    async with lock:
        try:
            db = cog._get_db()
            if db is None:
                raise RuntimeError("settings_unavailable")
            settings = await _maybe_await(db.resolve_tts(guild_id, user_id))
            settings = settings if isinstance(settings, dict) else {}
            enabled = settings.get("ttv_random_cd") is not True
            pool = available_random_melodies() if enabled else ()
            if enabled and not pool:
                raise ValueError("catalog_unavailable")
            await asyncio.wait_for(_maybe_await(cog._set_user_tts_and_refresh(
                guild_id, user_id, ttv_random_cd=enabled)), timeout=12)
        except Exception as exc:
            log.warning("[random_cd] mode save failed type=%s", type(exc).__name__)
            await _reply(message, discord.Embed(
                title="💿 Ajuste indisponível",
                description="Não consegui confirmar essa mudança agora. Tente novamente em instantes.",
                color=0xED7D8A))
            return True
        history = getattr(cog, "_random_cd_last", None)
        if history is not None:
            history.pop(key, None)
        prefix = str(settings.get("bot_prefix") or "_")
        if enabled:
            embed = discord.Embed(
                title="🎲 CDs aleatórios ligados",
                description=f"Um CD novo a cada canto com `~`.\n-# Use `{prefix}rcd` novamente para desligar.",
                color=0x9B7BF5)
            label = "CD disponível" if len(pool) == 1 else "CDs disponíveis"
            embed.set_footer(text=f"{len(pool)} {label} · só para você")
        else:
            try:
                melody = get_melody(settings.get("ttv_melody_id"))
                name = discord.utils.escape_markdown(melody.name)
                description = f"**{name}** voltou a ser seu CD fixo."
            except (ValueError, TypeError):
                description = "O sorteio foi desligado."
            embed = discord.Embed(
                title="💿 CD fixo restaurado",
                description=f"{description}\n-# Escolha outro com `{prefix}cd`.",
                color=0x75C7D4)
            embed.set_footer(text="Só para você neste servidor")
        await _reply(message, embed)
    await _refresh_open_panels(cog, guild_id, user_id)
    return True
