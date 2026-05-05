import discord

from config import OFF_COLOR, ON_COLOR

from ..constants import _FOCUS_WORD_RE


_FOCUS_EMOJI = "<:alvo:1501014211571220543>"


class GincanaFocusMixin:
    def _focus_mention(self, guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(int(user_id))
        return member.mention if member else f"<@{int(user_id)}>"

    def _format_focus_list(self, guild: discord.Guild) -> str:
        focus_map = self.db.get_gincana_focus_map(guild.id)
        if not focus_map:
            return "Ninguém por enquanto."

        lines = []
        callkeeper_ids = self._get_callkeeper_bot_ids()
        for uid in sorted(focus_map):
            if int(uid) in callkeeper_ids:
                continue
            lines.append(self._focus_mention(guild, int(uid)))
        return "\n".join(lines) if lines else "Ninguém por enquanto."

    def _format_focus_mentions(self, guild: discord.Guild, user_ids: list[int]) -> str:
        return ", ".join(self._focus_mention(guild, uid) for uid in user_ids)

    def _build_focus_feedback_view(
        self,
        guild: discord.Guild,
        *,
        added_ids: list[int],
        removed_ids: list[int],
        errored_ids: list[int],
        reset: bool,
    ) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)

        if reset:
            view.add_item(discord.ui.Container(
                discord.ui.TextDisplay(f"# {_FOCUS_EMOJI} Foco limpo"),
                discord.ui.TextDisplay("A lista de foco foi esvaziada."),
                accent_color=discord.Color(OFF_COLOR),
            ))
            return view

        lines: list[str] = []
        if added_ids:
            mentions = self._format_focus_mentions(guild, added_ids)
            verb = "entrou" if len(added_ids) == 1 else "entraram"
            lines.append(f"{mentions} {verb} no modo foco.")

        if removed_ids:
            mentions = self._format_focus_mentions(guild, removed_ids)
            verb = "saiu" if len(removed_ids) == 1 else "saíram"
            lines.append(f"{mentions} {verb} do modo foco.")

        if errored_ids:
            mentions = self._format_focus_mentions(guild, errored_ids)
            lines.append(f"Não posso entrar na própria lista de foco: {mentions}")

        if not lines:
            lines.append("Nada mudou por enquanto.")

        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# {_FOCUS_EMOJI} Foco atualizado"),
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"## Agora em foco\n{self._format_focus_list(guild)}"),
            accent_color=discord.Color(ON_COLOR),
        ))
        return view

    async def _send_focus_feedback(
        self,
        message: discord.Message,
        *,
        added_ids: list[int],
        removed_ids: list[int],
        errored_ids: list[int] | None = None,
        reset: bool = False,
    ):
        guild = message.guild
        assert guild is not None
        view = self._build_focus_feedback_view(
            guild,
            added_ids=added_ids,
            removed_ids=removed_ids,
            errored_ids=errored_ids or [],
            reset=reset,
        )
        await message.channel.send(view=view)

    async def _handle_focus_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False

        content = (message.content or "")
        if not _FOCUS_WORD_RE.search(content):
            return False

        if not isinstance(message.author, discord.Member):
            return True

        if not self._is_staff_member(message.author):
            return True

        mentions: list[discord.Member] = []
        seen: set[int] = set()

        raw_ids = list(getattr(message, "raw_mentions", []) or [])
        if raw_ids:
            for user_id in raw_ids:
                try:
                    uid = int(user_id)
                except (TypeError, ValueError):
                    continue
                if uid in seen:
                    continue
                member = guild.get_member(uid)
                if member is None:
                    try:
                        fetched = await guild.fetch_member(uid)
                    except Exception:
                        fetched = None
                    member = fetched if isinstance(fetched, discord.Member) else None
                if member is None:
                    continue
                mentions.append(member)
                seen.add(uid)
        else:
            for member in message.mentions:
                if member.id not in seen:
                    mentions.append(member)
                    seen.add(member.id)

        if not mentions:
            await self.db.clear_gincana_focus_users(guild.id)
            await self._send_focus_feedback(message, added_ids=[], removed_ids=[], errored_ids=[], reset=True)
            return True

        valid_user_ids: list[int] = []
        errored_ids: list[int] = []
        for member in mentions:
            if member.id == self.bot.user.id:
                errored_ids.append(member.id)
                continue
            if self._is_callkeeper_bot(member):
                continue
            valid_user_ids.append(member.id)

        added: list[int] = []
        removed: list[int] = []
        if valid_user_ids:
            added, removed, _ = await self.db.toggle_gincana_focus_users(
                guild.id,
                valid_user_ids,
            )

        await self._send_focus_feedback(
            message,
            added_ids=added,
            removed_ids=removed,
            errored_ids=errored_ids,
            reset=False,
        )
        return True
