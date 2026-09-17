from __future__ import annotations

from discord.ext import commands


class FluxoControle:
    """Lógica dos comandos que controlam a sessão musical remota."""

    async def _run_pause(self, ctx: commands.Context) -> None:
        if not await self._ensure_music_action_voice(ctx):
            return
        if await self._send_music_agent_control(ctx, "pause", "`⏸️` Música pausada."):
            return
        ok = await self.router.pause(ctx.guild.id)
        if not ok:
            await self._reply(ctx, "Não há música tocando para pausar.")

    async def _run_resume(self, ctx: commands.Context) -> None:
        if not await self._ensure_music_action_voice(ctx):
            return
        if await self._send_music_agent_control(ctx, "resume", "`▶️` Música retomada."):
            return
        ok = await self.router.resume(ctx.guild.id)
        if not ok:
            await self._reply(ctx, "Não há música pausada.")

    async def _run_skip(self, ctx: commands.Context) -> None:
        if not await self._ensure_music_action_voice(ctx):
            return
        if await self._send_music_agent_control(ctx, "skip", "`⏭️` Pulando música."):
            return
        _ok, message = await self.router.request_skip(ctx.guild.id, ctx.author)
        await self._reply(ctx, message)

    async def _run_back(self, ctx: commands.Context) -> None:
        if not await self._ensure_music_action_voice(ctx):
            return
        ok = await self.router.previous(ctx.guild.id)
        await self._reply(ctx, "`⏮️` Voltando para a música anterior." if ok else "Não há música anterior no histórico.")

    async def _run_stop(self, ctx: commands.Context) -> None:
        if not await self._ensure_music_action_voice(ctx):
            return
        if await self._send_music_agent_control(ctx, "stop", "`⏹️` Player encerrado e desconectado."):
            return
        _ok, message = await self.router.request_stop(ctx.guild.id, ctx.author, disconnect=True)
        await self._reply(ctx, message)

    async def _run_volume(self, ctx: commands.Context, value: int | None = None) -> None:
        if not await self._ensure_music_action_voice(ctx):
            return
        state = self.router.get_state(ctx.guild.id)
        if value is None:
            await self._reply(ctx, f"`🔊` Volume atual: `{int(round(state.volume * 100))}%`.")
            return
        if not self.router.is_music_staff(ctx.author):
            await self._reply(ctx, "Apenas staff pode alterar o volume do player.")
            return
        volume = await self.router.set_volume(ctx.guild.id, value)
        await self._reply(ctx, f"`🔊` Volume da música ajustado para `{int(round(volume * 100))}%`.")

    async def _run_shuffle(self, ctx: commands.Context) -> None:
        if not await self._ensure_music_action_voice(ctx):
            return
        _ok, message = await self.router.request_shuffle(ctx.guild.id, ctx.author)
        await self._reply(ctx, message)

    async def _run_loop(self, ctx: commands.Context) -> None:
        if not await self._ensure_music_action_voice(ctx):
            return
        _ok, message = await self.router.request_loop(ctx.guild.id, ctx.author)
        await self._reply(ctx, message)
