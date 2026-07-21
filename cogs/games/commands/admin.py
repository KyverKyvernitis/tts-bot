import logging

import discord
from discord import app_commands


log = logging.getLogger(__name__)


def _label(text: str, component: discord.ui.Item, description: str | None = None) -> discord.ui.Label:
    return discord.ui.Label(
        text=str(text)[:45],
        description=(str(description)[:100] if description else None),
        component=component,
    )


class _EconomyStaffModal(discord.ui.Modal, title="Cargo da staff"):
    def __init__(self, cog: "GincanaCommandMixin", *, guild: discord.Guild, opener_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = int(guild.id)
        self.opener_id = int(opener_id)

        current = cog._get_staff_role(guild)
        role_kwargs = {
            "custom_id": "games_economy_staff_role",
            "placeholder": "Selecione o cargo da staff",
            "min_values": 0,
            "max_values": 1,
            "required": False,
        }
        if current is not None:
            role_kwargs["default_values"] = [current]
        self.role_select = discord.ui.RoleSelect(**role_kwargs)
        self.clear_checkbox = discord.ui.Checkbox(
            custom_id="games_economy_staff_clear",
            default=False,
        )

        self.add_item(_label("Cargo", self.role_select, "Pode abrir os painéis administrativos da Games."))
        self.add_item(_label("Remover cargo atual", self.clear_checkbox, "Deixa apenas as permissões nativas e o dono do bot."))

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.cog._economy_validate_interaction(interaction, opener_id=self.opener_id):
            return
        guild = interaction.guild
        if guild is None or int(guild.id) != self.guild_id:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Servidor inválido", ["Abra o painel novamente."], ok=False),
                ephemeral=True,
            )
            return

        selected = list(self.role_select.values or [])
        clear = bool(self.clear_checkbox.value)
        if clear:
            await self.cog.db.set_gincana_staff_role_id(guild.id, 0)
            notice = "Cargo da staff removido."
        else:
            selected_role = selected[0] if selected else None
            role_id = int(getattr(selected_role, "id", 0) or 0)
            role = guild.get_role(role_id) if role_id else None
            if role is None:
                await interaction.response.send_message(
                    view=self.cog._make_v2_notice("Cargo não selecionado", ["Selecione um cargo ou marque a opção de remover."], ok=False),
                    ephemeral=True,
                )
                return
            if role.is_default():
                await interaction.response.send_message(
                    view=self.cog._make_v2_notice("Cargo inválido", ["O cargo @everyone não pode ser usado como staff."], ok=False),
                    ephemeral=True,
                )
                return
            await self.cog.db.set_gincana_staff_role_id(guild.id, role.id)
            notice = "Cargo da staff atualizado."

        await interaction.response.edit_message(
            view=self.cog._make_economy_panel_view(guild, self.opener_id, notice=notice)
        )


class _EconomyChannelModal(discord.ui.Modal, title="Canal dos jogos"):
    def __init__(self, cog: "GincanaCommandMixin", *, guild: discord.Guild, opener_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = int(guild.id)
        self.opener_id = int(opener_id)

        current = cog._get_gincana_channel(guild)
        channel_kwargs = {
            "custom_id": "games_economy_channel",
            "placeholder": "Selecione o canal exclusivo",
            "channel_types": [
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.voice,
                discord.ChannelType.stage_voice,
            ],
            "min_values": 0,
            "max_values": 1,
            "required": False,
        }
        if current is not None:
            channel_kwargs["default_values"] = [current]
        self.channel_select = discord.ui.ChannelSelect(**channel_kwargs)
        self.clear_checkbox = discord.ui.Checkbox(
            custom_id="games_economy_channel_clear",
            default=False,
        )

        self.add_item(_label("Canal", self.channel_select, "Comandos e triggers da Games começam somente aqui."))
        self.add_item(_label("Usar todos os canais", self.clear_checkbox, "Remove o canal exclusivo."))

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.cog._economy_validate_interaction(interaction, opener_id=self.opener_id):
            return
        guild = interaction.guild
        if guild is None or int(guild.id) != self.guild_id:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Servidor inválido", ["Abra o painel novamente."], ok=False),
                ephemeral=True,
            )
            return

        selected = list(self.channel_select.values or [])
        clear = bool(self.clear_checkbox.value)
        if clear:
            await self.cog.db.set_gincana_channel_id(guild.id, 0)
            notice = "Todos os canais foram liberados."
        else:
            channel = selected[0] if selected else None
            channel_id = int(getattr(channel, "id", 0) or 0)
            if channel_id <= 0:
                await interaction.response.send_message(
                    view=self.cog._make_v2_notice("Canal não selecionado", ["Selecione um canal ou marque a opção de liberar todos."], ok=False),
                    ephemeral=True,
                )
                return
            await self.cog.db.set_gincana_channel_id(guild.id, channel_id)
            notice = "Canal dos jogos atualizado."

        await interaction.response.edit_message(
            view=self.cog._make_economy_panel_view(guild, self.opener_id, notice=notice)
        )


class _EconomyModeModal(discord.ui.Modal, title="Forma de uso"):
    def __init__(self, cog: "GincanaCommandMixin", *, guild: discord.Guild, opener_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = int(guild.id)
        self.opener_id = int(opener_id)

        current = cog._gincana_input_mode(guild.id)
        self.mode_group = discord.ui.RadioGroup(
            custom_id="games_economy_input_mode",
            required=True,
        )
        self.mode_group.add_option(
            label="Triggers",
            value="triggers",
            description="Use palavras como roleta, daily e race.",
            default=current == "triggers",
        )
        self.mode_group.add_option(
            label="Comandos",
            value="commands",
            description="Use o prefixo do bot, como _roleta e _daily.",
            default=current == "commands",
        )
        self.add_item(_label("Entrada", self.mode_group, "Apenas uma forma fica ativa por vez."))

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.cog._economy_validate_interaction(interaction, opener_id=self.opener_id):
            return
        guild = interaction.guild
        if guild is None or int(guild.id) != self.guild_id:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Servidor inválido", ["Abra o painel novamente."], ok=False),
                ephemeral=True,
            )
            return

        mode = str(self.mode_group.value or "triggers").strip().casefold()
        if mode not in {"triggers", "commands"}:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Modo inválido", ["Selecione triggers ou comandos."], ok=False),
                ephemeral=True,
            )
            return
        await self.cog.db.set_gincana_input_mode(guild.id, mode)
        notice = "Agora o servidor usa comandos." if mode == "commands" else "Agora o servidor usa triggers."
        await interaction.response.edit_message(
            view=self.cog._make_economy_panel_view(guild, self.opener_id, notice=notice)
        )


class _EconomyResetModal(discord.ui.Modal, title="Restaurar padrão"):
    def __init__(self, cog: "GincanaCommandMixin", *, guild_id: int, opener_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.opener_id = int(opener_id)
        self.confirm_checkbox = discord.ui.Checkbox(
            custom_id="games_economy_reset_confirm",
            default=False,
        )
        self.add_item(
            _label(
                "Confirmar restauração",
                self.confirm_checkbox,
                "Remove o cargo e o canal exclusivos e volta para triggers.",
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.cog._economy_validate_interaction(interaction, opener_id=self.opener_id):
            return
        guild = interaction.guild
        if guild is None or int(guild.id) != self.guild_id:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Servidor inválido", ["Abra o painel novamente."], ok=False),
                ephemeral=True,
            )
            return
        if not bool(self.confirm_checkbox.value):
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Nada alterado", ["Marque a confirmação para restaurar o padrão."], ok=False),
                ephemeral=True,
            )
            return

        await self.cog.db.set_gincana_staff_role_id(guild.id, 0)
        await self.cog.db.set_gincana_channel_id(guild.id, 0)
        await self.cog.db.set_gincana_input_mode(guild.id, "triggers")
        await interaction.response.edit_message(
            view=self.cog._make_economy_panel_view(guild, self.opener_id, notice="Configuração restaurada.")
        )


class _EconomyPanelView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaCommandMixin", *, guild: discord.Guild, opener_id: int, notice: str | None = None):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild_id = int(guild.id)
        self.opener_id = int(opener_id)

        staff_role = cog._get_staff_role(guild)
        channel = cog._get_gincana_channel(guild)
        mode = cog._gincana_input_mode(guild.id)

        staff_button = discord.ui.Button(label="Alterar", emoji="🛡️", style=discord.ButtonStyle.secondary)
        staff_button.callback = self._edit_staff
        channel_button = discord.ui.Button(label="Alterar", emoji="📍", style=discord.ButtonStyle.secondary)
        channel_button.callback = self._edit_channel
        mode_button = discord.ui.Button(label="Alterar", emoji="🔁", style=discord.ButtonStyle.secondary)
        mode_button.callback = self._edit_mode
        reset_button = discord.ui.Button(label="Restaurar padrão", emoji="↩️", style=discord.ButtonStyle.secondary)
        reset_button.callback = self._reset
        close_button = discord.ui.Button(label="Fechar", emoji="✖️", style=discord.ButtonStyle.secondary)
        close_button.callback = self._close

        staff_text = staff_role.mention if staff_role is not None else "Não definido"
        channel_text = getattr(channel, "mention", None) or "Todos os canais"
        mode_text = "Comandos" if mode == "commands" else "Triggers"
        mode_example = "`_roleta` · `_daily` · `_race`" if mode == "commands" else "`roleta` · `daily` · `race`"

        items: list[discord.ui.Item] = [
            discord.ui.TextDisplay("# ⚙️ Economia\nConfiguração da cog Games."),
        ]
        if notice:
            items.extend([
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                discord.ui.TextDisplay(f"{cog._EFFECT_EMOJI} {notice}"),
            ])
        items.extend([
            discord.ui.Separator(),
            discord.ui.Section(
                discord.ui.TextDisplay(f"## Cargo da staff\n{staff_text}"),
                accessory=staff_button,
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.Section(
                discord.ui.TextDisplay(f"## Canal dos jogos\n{channel_text}"),
                accessory=channel_button,
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.Section(
                discord.ui.TextDisplay(f"## Forma de uso\n**{mode_text}**\n{mode_example}"),
                accessory=mode_button,
            ),
            discord.ui.Separator(),
            discord.ui.ActionRow(reset_button, close_button),
        ])
        self.add_item(discord.ui.Container(*items, accent_color=discord.Color.blurple()))

    async def _validate(self, interaction: discord.Interaction) -> discord.Guild | None:
        if not await self.cog._economy_validate_interaction(interaction, opener_id=self.opener_id):
            return None
        guild = interaction.guild
        if guild is None or int(guild.id) != self.guild_id:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Servidor inválido", ["Abra o painel novamente."], ok=False),
                ephemeral=True,
            )
            return None
        return guild

    async def _edit_staff(self, interaction: discord.Interaction):
        guild = await self._validate(interaction)
        if guild is not None:
            await interaction.response.send_modal(_EconomyStaffModal(self.cog, guild=guild, opener_id=self.opener_id))

    async def _edit_channel(self, interaction: discord.Interaction):
        guild = await self._validate(interaction)
        if guild is not None:
            await interaction.response.send_modal(_EconomyChannelModal(self.cog, guild=guild, opener_id=self.opener_id))

    async def _edit_mode(self, interaction: discord.Interaction):
        guild = await self._validate(interaction)
        if guild is not None:
            await interaction.response.send_modal(_EconomyModeModal(self.cog, guild=guild, opener_id=self.opener_id))

    async def _reset(self, interaction: discord.Interaction):
        guild = await self._validate(interaction)
        if guild is not None:
            await interaction.response.send_modal(
                _EconomyResetModal(self.cog, guild_id=guild.id, opener_id=self.opener_id)
            )

    async def _close(self, interaction: discord.Interaction):
        if await self._validate(interaction) is None:
            return
        self.stop()
        await interaction.response.edit_message(
            view=self.cog._make_v2_notice("Economia", ["Painel fechado."], ok=True)
        )


class GincanaCommandMixin:
    async def _economy_is_bot_owner(self, user: discord.abc.User) -> bool:
        user_id = int(getattr(user, "id", 0) or 0)
        owner_id = int(getattr(self.bot, "owner_id", 0) or 0)
        owner_ids = {int(value) for value in (getattr(self.bot, "owner_ids", None) or set())}
        if user_id and (user_id == owner_id or user_id in owner_ids):
            return True
        try:
            return bool(await self.bot.is_owner(user))
        except Exception:
            return False

    async def _economy_user_allowed(self, guild: discord.Guild, user: discord.abc.User) -> bool:
        if await self._economy_is_bot_owner(user):
            return True
        if int(getattr(user, "id", 0) or 0) == int(guild.owner_id or 0):
            return True
        return isinstance(user, discord.Member) and self._is_staff_member(user)

    async def _economy_validate_interaction(self, interaction: discord.Interaction, *, opener_id: int) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != int(opener_id):
            await interaction.response.send_message(
                view=self._make_v2_notice("Painel reservado", ["Abra seu próprio `/economia`."], ok=False),
                ephemeral=True,
            )
            return False
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                view=self._make_v2_notice("Servidor inválido", ["Use esse painel dentro de um servidor."], ok=False),
                ephemeral=True,
            )
            return False
        if not await self._economy_user_allowed(guild, interaction.user):
            await interaction.response.send_message(
                view=self._make_v2_notice("Sem permissão", ["Você não pode alterar a economia deste servidor."], ok=False),
                ephemeral=True,
            )
            return False
        return True

    async def _normalize_economy_config(self, guild: discord.Guild):
        role_id = int(self.db.get_gincana_staff_role_id(guild.id) or 0)
        if role_id and guild.get_role(role_id) is None:
            await self.db.set_gincana_staff_role_id(guild.id, 0)

        channel_id = self._gincana_channel_id(guild.id)
        if channel_id and self._get_gincana_channel(guild) is None:
            await self.db.set_gincana_channel_id(guild.id, 0)

        raw_mode = self._gincana_input_mode(guild.id)
        if raw_mode not in {"triggers", "commands"}:
            await self.db.set_gincana_input_mode(guild.id, "triggers")

    def _make_economy_panel_view(self, guild: discord.Guild, opener_id: int, *, notice: str | None = None) -> discord.ui.LayoutView:
        return _EconomyPanelView(self, guild=guild, opener_id=opener_id, notice=notice)

    async def _run_gincana_command(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                view=self._make_v2_notice("Servidor inválido", ["Use `/economia` dentro de um servidor."], ok=False),
                ephemeral=True,
            )
            return
        if not await self._economy_user_allowed(interaction.guild, interaction.user):
            await interaction.response.send_message(
                view=self._make_v2_notice("Sem permissão", ["Você não pode alterar a economia deste servidor."], ok=False),
                ephemeral=True,
            )
            return

        await self._normalize_economy_config(interaction.guild)
        await interaction.response.send_message(
            view=self._make_economy_panel_view(interaction.guild, interaction.user.id),
            ephemeral=True,
        )

    async def _handle_gincana_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        log.error("games: erro no /economia: %r", error)
        view = self._make_v2_notice("Erro na economia", ["Não foi possível abrir o painel."], ok=False)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(view=view, ephemeral=True)
            else:
                await interaction.response.send_message(view=view, ephemeral=True)
        except Exception:
            pass
