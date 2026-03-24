import discord

from config import OFF_COLOR, ON_COLOR

from ..constants import _FOCUS_WORD_RE


class GincanaFocusMixin:
    def _format_focus_list(self, guild: discord.Guild) -> str:
        focus_map = self.db.get_gincana_focus_map(guild.id)
        if not focus_map:
            return "Nenhum membro está focado no momento."

        lines = []
        for uid in sorted(focus_map):
            member = guild.get_member(uid)
            label = member.mention if member else f"<@{uid}>"
            lines.append(f"• {label}")
        return "\n".join(lines)

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
        errored_ids = errored_ids or []

        def format_count(label: str, count: int, *, ok: bool = True, extra: str | None = None) -> str:
            icon = "✅" if ok else "⚠️"
            user_label = "usuário" if count == 1 else "usuários"
            base = f"{icon} **{count} {user_label}** {label}"
            if extra:
                base += f"\n{extra}"
            return base

        lines: list[str] = []
        if reset:
            title = "🧠 Modo foco resetado"
            description = "A lista de membros focados foi limpa com sucesso."
            color = discord.Color(OFF_COLOR)
            lines.append(description)
        else:
            title = "🧠 Modo foco atualizado"
            color = discord.Color(ON_COLOR)
            if added_ids:
                lines.append(format_count("adicionados à lista com sucesso.", len(added_ids)))
            if removed_ids:
                lines.append(format_count("removidos da lista com sucesso.", len(removed_ids)))
            if errored_ids:
                mention_list = ", ".join(guild.get_member(uid).mention if guild.get_member(uid) else f"<@{uid}>" for uid in errored_ids)
                lines.append(format_count(
                    "não puderam ser adicionados.",
                    len(errored_ids),
                    ok=False,
                    extra=f"Não posso entrar na própria lista de foco: {mention_list}",
                ))
            if not lines:
                lines.append("Nenhuma alteração foi feita na lista de foco.")

        focus_list = self._format_focus_list(guild)
        embed = discord.Embed(title=title, description="\n".join(lines), color=color)
        embed.add_field(name="📋 Lista atual", value=focus_list, inline=False)
        embed.set_footer(text="Nenhum membro focado no momento" if focus_list == "Nenhum membro está focado no momento." else "Lista de foco atualizada")
        await message.channel.send(embed=embed)

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
