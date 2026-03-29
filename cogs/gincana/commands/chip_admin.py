import re
import discord

from ..constants import CHIPS_DEFAULT, CHIPS_INITIAL


BOT_OWNER_ID = 394316054433628160


def _is_owner(user_id: int) -> bool:
    return int(user_id) == BOT_OWNER_ID


class _AdminUserAdjustModal(discord.ui.Modal, title="Ajustar saldo"):
    target_input = discord.ui.TextInput(label="Usuário", placeholder="@usuário ou ID", required=True, max_length=64)
    chips_input = discord.ui.TextInput(label="Fichas normais", placeholder="Deixe vazio para não alterar", required=False, max_length=16)
    bonus_input = discord.ui.TextInput(label="Fichas bônus", placeholder="Deixe vazio para não alterar", required=False, max_length=16)

    def __init__(self, cog: "GincanaChipAdminMixin", opener_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.opener_id = int(opener_id)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.cog._chip_admin_validate_interaction(interaction, opener_id=self.opener_id, owner_only=False):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(view=self.cog._make_v2_notice("Servidor inválido", ["Use esse painel dentro de um servidor."], ok=False), ephemeral=True)
            return
        member = self.cog._chip_admin_parse_member(guild, str(self.target_input.value))
        if member is None:
            await interaction.response.send_message(view=self.cog._make_v2_notice("Usuário inválido", ["Informe uma menção ou um ID válido."], ok=False), ephemeral=True)
            return
        chips_raw = str(self.chips_input.value or '').strip()
        bonus_raw = str(self.bonus_input.value or '').strip()
        if not chips_raw and not bonus_raw:
            await interaction.response.send_message(view=self.cog._make_v2_notice("Nada para alterar", ["Preencha fichas normais, fichas bônus ou ambos."], ok=False), ephemeral=True)
            return
        try:
            chips_val = int(chips_raw) if chips_raw else None
            bonus_val = int(bonus_raw) if bonus_raw else None
        except Exception:
            await interaction.response.send_message(view=self.cog._make_v2_notice("Valor inválido", ["Use números inteiros nas fichas."], ok=False), ephemeral=True)
            return
        if chips_val is not None:
            await self.cog._set_user_chips_value(guild.id, member.id, chips_val, mark_activity=True)
        if bonus_val is not None:
            await self.cog.db.set_user_bonus_chips(guild.id, member.id, bonus_val)
            await self.cog._mark_chip_activity(guild.id, member.id)
        chips_now = self.cog.db.get_user_chips(guild.id, member.id, default=CHIPS_INITIAL)
        bonus_now = self.cog._get_user_bonus_chips(guild.id, member.id)
        lines = [f"{member.mention} agora tem {self.cog._chip_amount(chips_now)}"]
        if bonus_now > 0:
            lines[0] += f" • {self.cog._bonus_chip_amount(bonus_now)}"
        await interaction.response.send_message(view=self.cog._make_v2_notice("Saldo atualizado", lines, ok=True), ephemeral=True)


class _AdminUserResetModal(discord.ui.Modal, title="Resetar usuário"):
    target_input = discord.ui.TextInput(label="Usuário", placeholder="@usuário ou ID", required=True, max_length=64)

    def __init__(self, cog: "GincanaChipAdminMixin", opener_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.opener_id = int(opener_id)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.cog._chip_admin_validate_interaction(interaction, opener_id=self.opener_id, owner_only=True):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(view=self.cog._make_v2_notice("Servidor inválido", ["Use esse painel dentro de um servidor."], ok=False), ephemeral=True)
            return
        member = self.cog._chip_admin_parse_member(guild, str(self.target_input.value))
        if member is None:
            await interaction.response.send_message(view=self.cog._make_v2_notice("Usuário inválido", ["Informe uma menção ou um ID válido."], ok=False), ephemeral=True)
            return
        await self.cog._force_reset_chips(guild.id, member.id)
        lines = [f"{member.mention} voltou para {self.cog._chip_amount(CHIPS_DEFAULT)}."]
        await interaction.response.send_message(view=self.cog._make_v2_notice("Usuário resetado", lines, ok=True), ephemeral=True)


class _AdminServerResetModal(discord.ui.Modal, title="Resetar servidor"):
    confirm_input = discord.ui.TextInput(label="Confirmação", placeholder="Digite RESETAR", required=True, max_length=16)

    def __init__(self, cog: "GincanaChipAdminMixin", opener_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.opener_id = int(opener_id)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.cog._chip_admin_validate_interaction(interaction, opener_id=self.opener_id, owner_only=True):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(view=self.cog._make_v2_notice("Servidor inválido", ["Use esse painel dentro de um servidor."], ok=False), ephemeral=True)
            return
        if str(self.confirm_input.value or '').strip().upper() != 'RESETAR':
            await interaction.response.send_message(view=self.cog._make_v2_notice("Confirmação inválida", ["Digite RESETAR para continuar."], ok=False), ephemeral=True)
            return
        user_ids = self.cog._iter_active_chip_user_ids(guild.id)
        if not user_ids:
            await interaction.response.send_message(view=self.cog._make_v2_notice("Nada para resetar", ["Não há perfis com movimentação neste servidor."], ok=False), ephemeral=True)
            return
        total = 0
        for user_id in user_ids:
            await self.cog._force_full_reset_ficha_profile(guild.id, user_id, amount=CHIPS_DEFAULT)
            total += 1
        lines = [f"Perfis afetados: **{total}**", f"Novo saldo padrão: {self.cog._chip_amount(CHIPS_DEFAULT)}"]
        await interaction.response.send_message(view=self.cog._make_v2_notice("Servidor resetado", lines, ok=True), ephemeral=True)


class _ChipAdminPanelView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaChipAdminMixin", opener_id: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.opener_id = int(opener_id)
        owner = _is_owner(self.opener_id)
        adjust_button = discord.ui.Button(style=discord.ButtonStyle.secondary, label='Ajustar usuário', emoji='💰')
        adjust_button.callback = self._adjust_user
        reset_user_button = discord.ui.Button(style=discord.ButtonStyle.secondary, label='Resetar usuário', emoji='♻️', disabled=not owner)
        reset_user_button.callback = self._reset_user
        reset_server_button = discord.ui.Button(style=discord.ButtonStyle.danger, label='Resetar servidor', emoji='🧨', disabled=not owner)
        reset_server_button.callback = self._reset_server
        close_button = discord.ui.Button(style=discord.ButtonStyle.secondary, label='Fechar', emoji='✖️')
        close_button.callback = self._close_panel
        row = discord.ui.ActionRow(adjust_button, reset_user_button, reset_server_button, close_button)
        lines = [
            '# 🛠️ Painel de fichas',
            'Ajuste fichas normais e bônus em um só lugar.',
            'Staff pode ajustar saldo. Resetar usuário e servidor é exclusivo do dono do bot.',
            'Valores negativos são aceitos nas fichas normais.',
        ]
        self.add_item(discord.ui.Container(discord.ui.TextDisplay("\n".join(lines)), row, accent_color=discord.Color.blurple()))

    async def _adjust_user(self, interaction: discord.Interaction):
        if not await self.cog._chip_admin_validate_interaction(interaction, opener_id=self.opener_id, owner_only=False, send_response=False):
            return
        await interaction.response.send_modal(_AdminUserAdjustModal(self.cog, self.opener_id))

    async def _reset_user(self, interaction: discord.Interaction):
        if not await self.cog._chip_admin_validate_interaction(interaction, opener_id=self.opener_id, owner_only=True, send_response=False):
            return
        await interaction.response.send_modal(_AdminUserResetModal(self.cog, self.opener_id))

    async def _reset_server(self, interaction: discord.Interaction):
        if not await self.cog._chip_admin_validate_interaction(interaction, opener_id=self.opener_id, owner_only=True, send_response=False):
            return
        await interaction.response.send_modal(_AdminServerResetModal(self.cog, self.opener_id))

    async def _close_panel(self, interaction: discord.Interaction):
        if not await self.cog._chip_admin_validate_interaction(interaction, opener_id=self.opener_id, owner_only=False, send_response=False):
            return
        closed = self.cog._make_v2_notice('Painel fechado', ['Abra de novo pelo comando quando precisar.'], ok=True)
        await interaction.response.edit_message(view=closed)
        try:
            self.stop()
        except Exception:
            pass


class GincanaChipAdminMixin:
    def _chip_admin_parse_member(self, guild: discord.Guild, raw: str) -> discord.Member | None:
        text = str(raw or '').strip()
        if not text:
            return None
        match = re.search(r'(\d{15,22})', text)
        if not match:
            return None
        try:
            user_id = int(match.group(1))
        except Exception:
            return None
        return guild.get_member(user_id)

    async def _chip_admin_validate_interaction(self, interaction: discord.Interaction, *, opener_id: int, owner_only: bool, send_response: bool = True) -> bool:
        user = interaction.user
        if user is None or int(user.id) != int(opener_id):
            if send_response:
                await interaction.response.send_message(view=self._make_v2_notice('Sem permissão', ['Esse painel pertence a outra pessoa.'], ok=False), ephemeral=True)
            return False
        member = user if isinstance(user, discord.Member) else interaction.guild.get_member(user.id) if interaction.guild else None
        if member is None or not self._is_staff_member(member):
            if send_response:
                await interaction.response.send_message(view=self._make_v2_notice('Sem permissão', ['Esse painel é exclusivo da staff.'], ok=False), ephemeral=True)
            return False
        if owner_only and not _is_owner(member.id):
            if send_response:
                await interaction.response.send_message(view=self._make_v2_notice('Sem permissão', ['Essa ação é exclusiva do dono do bot.'], ok=False), ephemeral=True)
            return False
        return True

    def _make_chip_admin_panel_view(self, opener_id: int) -> discord.ui.LayoutView:
        return _ChipAdminPanelView(self, opener_id)
