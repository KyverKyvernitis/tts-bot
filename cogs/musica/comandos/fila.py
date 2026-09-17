from __future__ import annotations

import discord
from discord.ext import commands

from ..interface.componentes import QueueView, build_queue_embed


class FluxoFila:
    """Lógica de visualização e edição do espelho de fila da VPS."""

    async def _run_queue(self, ctx: commands.Context) -> None:
        if not await self._ensure_music_action_voice(ctx):
            return
        state = self.router.get_state(ctx.guild.id)
        await self._reply(
            ctx,
            embed=build_queue_embed(state, 0),
            view=QueueView(self.router, ctx.guild.id, 0, owner_id=ctx.author.id),
        )

    async def _run_now_playing(self, ctx: commands.Context) -> None:
        if not await self._ensure_music_action_voice(ctx):
            return
        state = self.router.get_state(ctx.guild.id)
        if state.current is None and state.queue.empty():
            await self._reply(ctx, "Nada tocando agora.")
            return
        state.last_text_channel_id = ctx.channel.id
        await self.router.update_panel(ctx.guild.id, create=True)

    async def _run_remove(self, ctx: commands.Context, position: int | None = None) -> None:
        if not await self._ensure_music_action_voice(ctx):
            return
        if position is None:
            await self._reply(ctx, "Use `_remove <posição>`.")
            return
        removed = await self.router.remove_at(ctx.guild.id, position)
        if removed is None:
            await self._reply(ctx, "Essa posição não existe no queue.")
            return
        await self._reply(ctx, f"`🗑️` Removido do queue: **{removed.short_title}**.")

    async def _run_move(self, ctx: commands.Context, from_pos: int | None = None, to_pos: int | None = None) -> None:
        if not await self._ensure_music_action_voice(ctx):
            return
        if from_pos is None or to_pos is None:
            await self._reply(ctx, "Use `_move <posição atual> <nova posição>`.")
            return
        ok = await self.router.move(ctx.guild.id, from_pos, to_pos)
        await self._reply(ctx, "`↪️` Posição atualizada." if ok else "Não consegui mover: confira as posições do queue.")

    async def _run_skipto(self, ctx: commands.Context, position: int | None = None) -> None:
        if not await self._ensure_music_action_voice(ctx):
            return
        if position is None:
            await self._reply(ctx, "Use `_skipto <posição>`.")
            return
        ok = await self.router.skip_to(ctx.guild.id, position)
        await self._reply(ctx, "`▶️` Tocando a posição escolhida." if ok else "Não encontrei essa posição no queue.")

    async def _run_readd(self, ctx: commands.Context) -> None:
        added = await self.router.readd_history(ctx.guild.id)
        await self._reply(ctx, f"`🎶` Readicionei `{added}` música(s) do histórico." if added else "O histórico está vazio.")

    async def _run_history(self, ctx: commands.Context) -> None:
        history = self.router.history_snapshot(ctx.guild.id)
        if not history:
            await self._reply(ctx, "Histórico vazio.")
            return
        lines = []
        for idx, track in enumerate(reversed(history[-10:]), start=1):
            lines.append(f"`{idx:02d}.` **{discord.utils.escape_markdown(track.short_title)}** • `{track.duration_label}`")
        embed = discord.Embed(title="↩️ Histórico de músicas", description="\n".join(lines), color=discord.Color.blurple())
        embed.set_footer(text="Use _readd para colocar o histórico de volta no queue.")
        await self._reply(ctx, embed=embed)

    async def _run_clearqueue(self, ctx: commands.Context) -> None:
        if not await self._ensure_music_action_voice(ctx):
            return
        await self.router.replace_queue(ctx.guild.id, [])
        await self._reply(ctx, "`🧹` Queue limpo.")
