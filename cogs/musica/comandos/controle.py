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


    async def _run_audio_effect(self, ctx: commands.Context, effect: str, level: str = "") -> None:
        if not await self._ensure_music_action_voice(ctx):
            return
        if not self.router.is_music_staff(ctx.author):
            await self._reply(ctx, "Apenas staff pode alterar os efeitos do player.")
            return
        state = self.router.get_state(ctx.guild.id)
        if state.current is None:
            await self._reply(ctx, "Não há música tocando agora.")
            return
        raw = str(level or "").strip()
        try:
            current_level = int(getattr(state, f"{effect}_level", 0) or 0)
        except (TypeError, ValueError):
            current_level = 0
        if current_level <= 0 and bool(getattr(state, effect, False)):
            current_level = 1
        current_level = max(0, min(3, current_level))
        if raw:
            try:
                target_level = int(raw)
            except ValueError:
                target_level = -1
            if target_level not in {1, 2, 3}:
                command = "reverb" if effect == "slowed_reverb" else effect
                await self._reply(ctx, f"Use `_{command}`, `_{command} 1`, `_{command} 2` ou `_{command} 3`.")
                return
        else:
            target_level = 0 if current_level > 0 else 1
        ok, message = await self.router.set_audio_effect(ctx.guild.id, effect, level=target_level)
        if ok:
            emoji = {"bassboost": "🔊", "nightcore": "🌙", "slowed_reverb": "🌧️"}[effect]
            await self._reply(ctx, f"`{emoji}` {message}")
        else:
            await self._reply(ctx, message)

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
