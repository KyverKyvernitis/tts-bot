from __future__ import annotations

import asyncio
import contextlib
import logging

import discord
from discord.ext import commands

from utility.technical_commands import can_use_technical_command
from utility.status.data import StatusCollector
from utility.status.views import COLLECTION_TIMEOUT_SECONDS, StatusView


logger = logging.getLogger(__name__)


def _notice_view(message: str, *, failed: bool = False) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(message),
        accent_color=discord.Color.orange() if failed else discord.Color.blurple(),
    ))
    return view


class StatusCommandMixin:
    def _status_collector_instance(self) -> StatusCollector:
        collector = getattr(self, "_status_collector", None)
        if collector is None:
            collector = StatusCollector(self.bot)
            self._status_collector = collector
        return collector

    def _track_status_view(self, view: StatusView) -> None:
        views = getattr(self, "_status_views", None)
        if views is None:
            views = set()
            self._status_views = views
        views.add(view)

    def _forget_status_view(self, view: StatusView) -> None:
        views = getattr(self, "_status_views", None)
        if views is not None:
            views.discard(view)

    def _stop_status_views(self) -> None:
        self._status_unloaded = True
        for view in tuple(getattr(self, "_status_views", ())):
            view.deactivate()
            try:
                asyncio.get_running_loop().create_task(view.close())
            except RuntimeError:
                pass

    @commands.command(name="status", hidden=True)
    async def status(self, ctx: commands.Context, *, section: str = "servidores") -> None:
        if not await can_use_technical_command(self.bot, ctx.author, ctx.guild):
            await ctx.send("Este comando técnico é exclusivo do dono do bot na guilda configurada.", allowed_mentions=discord.AllowedMentions.none())
            return
        section = section.strip().casefold()
        if section not in {"servidores", "tts"}:
            prefix = discord.utils.escape_mentions(str(getattr(ctx, "clean_prefix", ctx.prefix) or "_"))
            await ctx.send(f"Use `{prefix}status servidores` ou `{prefix}status tts`.", allowed_mentions=discord.AllowedMentions.none())
            return
        response = await ctx.send(view=_notice_view("## 📊 Coletando status…"), allowed_mentions=discord.AllowedMentions.none())
        collector = self._status_collector_instance()
        view = None
        try:
            snapshot = await asyncio.wait_for(collector.collect(), timeout=COLLECTION_TIMEOUT_SECONDS)
            if getattr(self, "_status_unloaded", False):
                await response.edit(view=_notice_view("## 📊 Painel encerrado\nAbra o comando status novamente."), allowed_mentions=discord.AllowedMentions.none())
                return
            view = StatusView(
                owner_id=ctx.author.id, snapshot=snapshot, collector=collector,
                tab="tts" if section == "tts" else "servers",
                prefix=str(getattr(ctx, "clean_prefix", ctx.prefix) or "_"),
                on_close=self._forget_status_view,
            )
            view.message = response
            self._track_status_view(view)
            await response.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
            if view.closed:
                await view.close()
        except Exception:
            if view is not None:
                view.deactivate()
            logger.exception("[utility/status] falha ao abrir painel")
            with contextlib.suppress(Exception):
                await response.edit(view=_notice_view("## ⚠️ Status indisponível\nTente novamente em alguns instantes.", failed=True), allowed_mentions=discord.AllowedMentions.none())
