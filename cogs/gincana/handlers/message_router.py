import asyncio

import discord

from config import MUTE_TOGGLE_WORD, TRIGGER_WORD


class GincanaMessageRouterMixin:
    async def _safe_route_call(self, handler_name: str, message: discord.Message) -> bool:
        handler = getattr(self, handler_name, None)
        if handler is None:
            return False
        try:
            return bool(await handler(message))
        except Exception as e:
            print(f"[gincana][router] {handler_name} falhou: {e!r}")
            return False

    def _matches_exact_trigger(self, content: str | None, trigger: str) -> bool:
        if not trigger:
            return False
        return str(content or "").strip().casefold() == str(trigger).strip().casefold()

    async def _handle_text_profile_commands(self, message: discord.Message) -> bool:
        content = str(message.content or "").strip().casefold()
        if not content or content.startswith("_"):
            return False
        if content not in {"ficha", "fichas", "rank", "leaderboard", "daily", "bonus", "login", "gincanahelp", "helpgincana", "jogoshelp"}:
            return False
        if message.guild is None:
            return True
        if content in {"ficha", "fichas"}:
            await message.channel.send(embed=self._make_chip_balance_embed(message.author))
            return True
        if content in {"rank", "leaderboard"}:
            await message.channel.send(embed=self._make_chip_leaderboard_embed(message.guild, message.author))
            return True
        if content in {"gincanahelp", "helpgincana", "jogoshelp"}:
            await message.channel.send(embed=discord.Embed(title="🎲 Help da gincana", description=(
                "Jogos, fichas e atalhos da gincana em um lugar só.\n\n"
                f"{self._CHIP_EMOJI} **Economia**\n"
                "• `ficha` — mostra seu saldo e seus destaques\n"
                "• `daily` — resgata o bônus diário\n"
                "• `rank` — ranking semanal\n"
                "• `pay @usuário valor` — transfere fichas\n\n"
                "🎮 **Jogos**\n"
                "• `roleta` — aposta rápida com jackpot\n"
                "• `buckshot` — rodada de sobrevivência\n"
                "• `alvo` — disputa de mira\n"
                "• `corrida` — corrida de cavalos\n"
                "• `poker` — mesa de poker\n\n"
                "🕹️ **Como entra**\n"
                "• alguns jogos abrem um lobby com botão\n"
                "• `atirar` fecha o buckshot\n"
                "• use os botões dos lobbies para começar os jogos"
            ), color=discord.Color.blurple()))
            return True

        claimed, new_balance, bonus, streak = await self.db.claim_daily_bonus(message.guild.id, message.author.id)
        if not claimed:
            await message.channel.send(
                embed=self._make_embed(
                    "🎁 Daily já resgatado",
                    f"Você já pegou seu bônus de hoje. Streak atual: **{streak}**.",
                    ok=False,
                )
            )
            return True
        await self._grant_weekly_points(message.guild.id, message.author.id, max(3, bonus // 2))
        embed = discord.Embed(
            title="🎁 Bônus diário resgatado",
            description=(
                f"Você ganhou {self._chip_amount(bonus)}\n"
                f"Streak atual: **{streak}**\n"
                f"Novo saldo: {self._chip_amount(new_balance)}"
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text="Volte amanhã para manter a sequência")
        await message.channel.send(embed=embed)
        return True

    async def _handle_gincana_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return


        if await self._safe_route_call("_handle_payment_message", message):
            return

        if await self._safe_route_call("_handle_text_profile_commands", message):
            return

        if await self._safe_route_call("_handle_focus_trigger", message):
            return

        if await self._safe_route_call("_handle_role_toggle_trigger", message):
            return

        if await self._safe_route_call("_handle_dj_toggle_trigger", message):
            return

        if await self._safe_route_call("_handle_buckshot_trigger", message):
            return

        if await self._safe_route_call("_handle_target_trigger", message):
            return


        if await self._safe_route_call("_handle_corrida_trigger", message):
            return

        if await self._safe_route_call("_handle_poker_trigger", message):
            return

        if await self._safe_route_call("_handle_roleta_trigger", message):
            return

        if not self.db.gincana_enabled(message.guild.id):
            return

        if self._gincana_only_kick_members(message.guild.id) and not self._is_staff_member(message.author):
            return

        if not TRIGGER_WORD and not MUTE_TOGGLE_WORD:
            return

        author_voice = getattr(message.author, "voice", None)
        voice_channel = getattr(author_voice, "channel", None)
        if not isinstance(voice_channel, discord.VoiceChannel):
            return

        content = message.content or ""
        normalized_content = content.strip().casefold()
        targets = self._resolve_targets(message.guild, voice_channel)
        if not targets:
            return

        target_ids = {member.id for member in targets}
        author_is_target = message.author.id in target_ids
        author_is_focused_non_staff = self._is_focused_non_staff_member(message.author)
        did_trigger_action = False

        if TRIGGER_WORD and normalized_content == TRIGGER_WORD.casefold():
            did_trigger_action = True
            trigger_voice_channel = None
            for target in targets:
                target_channel = getattr(getattr(target, "voice", None), "channel", None)
                if isinstance(target_channel, discord.VoiceChannel):
                    trigger_voice_channel = target_channel
                    break

            if trigger_voice_channel is not None:
                try:
                    await self._play_pinto_sfx(message.guild, trigger_voice_channel)
                except Exception:
                    pass
                try:
                    await asyncio.sleep(0.20)
                except Exception:
                    pass

            for target in targets:
                if target.voice and target.voice.channel:
                    try:
                        await target.move_to(None, reason="gincana disconnect")
                    except Exception:
                        pass

        if MUTE_TOGGLE_WORD and normalized_content == MUTE_TOGGLE_WORD.casefold():
            if author_is_focused_non_staff:
                return
            did_trigger_action = True
            if author_is_target:
                return

            for target in targets:
                if target.voice and target.voice.channel:
                    try:
                        new_muted = not bool(target.voice.mute)
                        await target.edit(mute=new_muted, reason="gincana toggle mute")
                    except Exception:
                        pass

            await self._refresh_targets_suffix_nicknames(message.guild, targets)

        if did_trigger_action:
            await self._react_success_temporarily(message)
