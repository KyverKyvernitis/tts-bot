import discord
from discord import app_commands


class GincanaCommandMixin:
    async def _run_gincana_command(
        self,
        interaction: discord.Interaction,
        action: str,
        role_id: str | None = None,
    ):
        if await self._reject_if_not_allowed_guild(interaction):
            return

        guild = interaction.guild
        if guild is None:
            return
        if not isinstance(interaction.user, discord.Member) or not self._is_staff_member(interaction.user):
            embed = self._make_embed("Sem permissão", "Você precisa ter o cargo staff da gincana ou a permissão **Expulsar Membros** para usar esse comando.", ok=False)
            await interaction.response.send_message(embed=embed)
            return
        chosen = action

        if chosen == "toggle":
            current = self.db.gincana_enabled(guild.id)
            new_value = not current
            await self.db.set_gincana_enabled(guild.id, new_value)

            role_total = len(self.db.get_gincana_role_ids(guild.id))
            embed = self._make_embed(
                "Configuração da economia",
                f"Status: **{'Ativado' if new_value else 'Desativado'}**\n"
                f"Roles cadastradas: **{role_total}**\n"
                f"Modo só para staff: **{'Ativado' if self._gincana_only_kick_members(guild.id) else 'Desativado'}**",
                ok=new_value,
            )
            await interaction.response.send_message(embed=embed)
            return

        if chosen == "toggle_kick_only":
            current = self._gincana_only_kick_members(guild.id)
            new_value = not current
            await self._set_gincana_only_kick_members(guild.id, new_value)

            embed = self._make_embed(
                "Modo só para staff atualizado",
                f"Agora a economia está **{'limitada à staff' if new_value else 'liberada para qualquer membro da call disparar'}**.",
                ok=True,
            )
            await interaction.response.send_message(embed=embed)
            return

        if chosen == "list":
            role_ids = self.db.get_gincana_role_ids(guild.id)
            if not role_ids:
                embed = self._make_embed(
                    "Sem roles cadastradas",
                    f"Nenhuma role está cadastrada na economia no momento\n\n"
                    f"Status: **{'Ativado' if self.db.gincana_enabled(guild.id) else 'Desativado'}**\n"
                    f"Modo só para staff: **{'Ativado' if self._gincana_only_kick_members(guild.id) else 'Desativado'}**",
                    ok=False,
                )
                await interaction.response.send_message(embed=embed)
                return

            lines = []
            for rid in role_ids:
                role = guild.get_role(rid)
                lines.append(role.mention if role else f"`{rid}`")

            embed = self._make_embed(
                "Roles da economia",
                "\n".join(lines)
                + f"\n\nStatus: **{'Ativado' if self.db.gincana_enabled(guild.id) else 'Desativado'}**"
                + f"\nModo só para staff: **{'Ativado' if self._gincana_only_kick_members(guild.id) else 'Desativado'}**",
                ok=True,
            )
            await interaction.response.send_message(embed=embed)
            return

        if chosen == "set_staff_role":
            if not role_id:
                embed = self._make_embed("ID obrigatório", "Você precisa informar o **ID da role** que será usada como cargo staff.", ok=False)
                await interaction.response.send_message(embed=embed)
                return
            try:
                parsed_role_id = int(role_id.strip())
            except (TypeError, ValueError):
                embed = self._make_embed("ID inválido", "Envie um **ID de role válido**.", ok=False)
                await interaction.response.send_message(embed=embed)
                return
            role = guild.get_role(parsed_role_id)
            if role is None:
                embed = self._make_embed("Role não encontrada", f"Não encontrei nenhuma role com o ID `{parsed_role_id}` neste servidor.", ok=False)
                await interaction.response.send_message(embed=embed)
                return
            await self.db.set_gincana_staff_role_id(guild.id, parsed_role_id)
            embed = self._make_embed("Cargo staff atualizado", f"✅ {role.mention} agora é o cargo staff da gincana.\n\nMembros com esse cargo podem usar os recursos de staff mesmo sem **Expulsar Membros**.")
            await interaction.response.send_message(embed=embed)
            return

        if chosen == "clear_staff_role":
            current_staff = self._get_staff_role(guild)
            await self.db.set_gincana_staff_role_id(guild.id, 0)
            current_text = current_staff.mention if current_staff else "o cargo staff atual"
            embed = self._make_embed("Cargo staff removido", f"✅ Removi {current_text} da configuração de staff da gincana.")
            await interaction.response.send_message(embed=embed)
            return

        if not role_id:
            embed = self._make_embed(
                "ID obrigatório",
                "Você precisa informar o **ID da role** para essa ação",
                ok=False,
            )
            await interaction.response.send_message(embed=embed)
            return

        try:
            parsed_role_id = int(role_id.strip())
        except (TypeError, ValueError):
            embed = self._make_embed(
                "ID inválido",
                "Envie um **ID de role válido**",
                ok=False,
            )
            await interaction.response.send_message(embed=embed)
            return

        role = guild.get_role(parsed_role_id)

        if chosen == "add":
            if role is None:
                embed = self._make_embed(
                    "Role não encontrada",
                    f"Não encontrei nenhuma role com o ID `{parsed_role_id}` neste servidor",
                    ok=False,
                )
                await interaction.response.send_message(embed=embed)
                return

            added = await self.db.add_gincana_role_id(guild.id, parsed_role_id)
            if not added:
                embed = self._make_embed(
                    "Role já cadastrada",
                    f"A role {role.mention} já está cadastrada na economia",
                    ok=False,
                )
                await interaction.response.send_message(embed=embed)
                return

            total = len(self.db.get_gincana_role_ids(guild.id))
            embed = self._make_embed(
                "Role adicionada",
                f"✅ Role {role.mention} adicionada à economia\n\n"
                f"Agora há **{total}** role(s) cadastrada(s)\n"
                f"Status: **{'Ativado' if self.db.gincana_enabled(guild.id) else 'Desativado'}**",
            )
            await interaction.response.send_message(embed=embed)
            return

        if chosen == "remove":
            removed = await self.db.remove_gincana_role_id(guild.id, parsed_role_id)
            if not removed:
                embed = self._make_embed(
                    "Role não cadastrada",
                    f"A role com ID `{parsed_role_id}` não está cadastrada na economia",
                    ok=False,
                )
                await interaction.response.send_message(embed=embed)
                return

            role_text = role.mention if role else f"`{parsed_role_id}`"
            total = len(self.db.get_gincana_role_ids(guild.id))
            embed = self._make_embed(
                "Role removida",
                f"✅ Role {role_text} removida da economia\n\n"
                f"Roles restantes: **{total}**\n"
                f"Status: **{'Ativado' if self.db.gincana_enabled(guild.id) else 'Desativado'}**",
                ok=True,
            )
            await interaction.response.send_message(embed=embed)
            return

    async def _handle_gincana_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            embed = self._make_embed(
                "Sem permissão",
                "Você precisa ter o cargo staff ou a permissão **Expulsar Membros** para usar esse comando.",
                ok=False,
            )
        else:
            embed = self._make_embed(
                "Erro na economia",
                "Ocorreu um erro ao executar esse comando",
                ok=False,
            )

        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed)
            else:
                await interaction.response.send_message(embed=embed)
        except Exception:
            pass
