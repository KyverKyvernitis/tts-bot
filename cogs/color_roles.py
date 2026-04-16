from __future__ import annotations

import asyncio
import io
import re
import time
from copy import deepcopy
from typing import Any

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont


COLOR_PANEL_TIMEOUT = 600.0
COLOR_COMMAND_COOLDOWN = 20.0
COLOR_BLOCK_SIZE = 10
COLOR_BLOCK_COUNT = 3
COLOR_PANEL_VARIABLES = [
    "{membro}",
    "{membro_nome}",
    "{membro_id}",
    "{numero}",
    "{cor_nome}",
    "{cor_adicionada}",
    "{cor_removida}",
    "{cargo}",
    "{cargo_nome}",
    "{servidor}",
]

DEFAULT_SLOTS: list[dict[str, Any]] = [
    {"number": 1, "name": "Vermelho escuro", "text_hex": "#b11212", "role_hex": "#8b0000"},
    {"number": 2, "name": "Amarelo escuro", "text_hex": "#c9a31a", "role_hex": "#b8860b"},
    {"number": 3, "name": "Verde escuro", "text_hex": "#0b5d30", "role_hex": "#006400"},
    {"number": 4, "name": "Azul escuro", "text_hex": "#1737d8", "role_hex": "#00008b"},
    {"number": 5, "name": "Rosa escuro", "text_hex": "#d61ea6", "role_hex": "#c71585"},
    {"number": 6, "name": "Roxo escuro", "text_hex": "#9a0ec7", "role_hex": "#800080"},
    {"number": 7, "name": "Laranja escuro", "text_hex": "#d98900", "role_hex": "#ff8c00"},
    {"number": 8, "name": "Bege escuro", "text_hex": "#b96d43", "role_hex": "#a0522d"},
    {"number": 9, "name": "Ciano escuro", "text_hex": "#008f98", "role_hex": "#008b8b"},
    {"number": 10, "name": "Preto escuro", "text_hex": "#4a4a4a", "role_hex": "#1f1f1f"},
    {"number": 11, "name": "Vermelho", "text_hex": "#ff1b1b", "role_hex": "#ff0000"},
    {"number": 12, "name": "Amarelo", "text_hex": "#ffec1a", "role_hex": "#ffd700"},
    {"number": 13, "name": "Verde", "text_hex": "#11b611", "role_hex": "#00ff00"},
    {"number": 14, "name": "Azul", "text_hex": "#0e2fff", "role_hex": "#1e90ff"},
    {"number": 15, "name": "Rosa", "text_hex": "#ff62c3", "role_hex": "#ff69b4"},
    {"number": 16, "name": "Roxo", "text_hex": "#c020ff", "role_hex": "#9370db"},
    {"number": 17, "name": "Laranja", "text_hex": "#ffad13", "role_hex": "#ffa500"},
    {"number": 18, "name": "Bege", "text_hex": "#d6b694", "role_hex": "#f5deb3"},
    {"number": 19, "name": "Ciano", "text_hex": "#00ecff", "role_hex": "#00ffff"},
    {"number": 20, "name": "Cinza", "text_hex": "#8f8f8f", "role_hex": "#808080"},
    {"number": 21, "name": "Vermelho claro", "text_hex": "#ff8b8b", "role_hex": "#ff7f7f"},
    {"number": 22, "name": "Amarelo claro", "text_hex": "#fff38f", "role_hex": "#fff68f"},
    {"number": 23, "name": "Verde claro", "text_hex": "#9cff9c", "role_hex": "#90ee90"},
    {"number": 24, "name": "Azul claro", "text_hex": "#a6c7ff", "role_hex": "#87cefa"},
    {"number": 25, "name": "Rosa claro", "text_hex": "#ffb6d9", "role_hex": "#ffb6c1"},
    {"number": 26, "name": "Roxo claro", "text_hex": "#d6a5ff", "role_hex": "#d8bfd8"},
    {"number": 27, "name": "Laranja claro", "text_hex": "#ffd199", "role_hex": "#ffcc99"},
    {"number": 28, "name": "Bege claro", "text_hex": "#ffe8d0", "role_hex": "#f5f5dc"},
    {"number": 29, "name": "Ciano claro", "text_hex": "#d6ffff", "role_hex": "#e0ffff"},
    {"number": 30, "name": "Branco", "text_hex": "#ffffff", "role_hex": "#ffffff"},
]

_DEFAULT_CONFIG: dict[str, Any] = {
    "channel_id": 0,
    "message_ids": [],
    "messages": {
        "1": {"title": "", "subtitle": "", "footer": ""},
        "2": {"title": "", "subtitle": "", "footer": ""},
        "3": {"title": "", "subtitle": "", "footer": ""},
    },
    "templates": {
        "apply": "{membro}, a cor {cor_adicionada} foi aplicada.",
        "remove": "{membro}, a cor {cor_removida} foi removida.",
        "switch": "{membro}, {cor_removida} foi removida e {cor_adicionada} foi aplicada.",
        "no_role": "Essa cor ainda não está configurada.",
        "hierarchy": "Não consegui aplicar {cor_nome} por causa da hierarquia de cargos.",
        "missing_panel": "Esse painel de cores não é mais o oficial deste servidor.",
    },
    "slots": {str(item["number"]): {**item, "role_id": 0, "role_name": item["name"], "managed": False} for item in DEFAULT_SLOTS},
}


def _deepcopy_default_config() -> dict[str, Any]:
    return deepcopy(_DEFAULT_CONFIG)


def _clean_hex(value: str | None, fallback: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    if not raw.startswith("#"):
        raw = f"#{raw}"
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
        return fallback
    return raw.lower()


def _font(size: int, *, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _chunk_block(block_index: int) -> tuple[int, int]:
    start = (block_index - 1) * COLOR_BLOCK_SIZE + 1
    end = start + COLOR_BLOCK_SIZE - 1
    return start, end


def _block_title(block_index: int) -> str:
    start, end = _chunk_block(block_index)
    return f"{start}–{end}"


class _ColorMessageEditModal(discord.ui.Modal):
    def __init__(self, view: "_ColorMessagePanelView", block_index: int):
        super().__init__(title=f"Editar mensagem {_block_title(block_index)}")
        self.view_ref = view
        self.block_index = int(block_index)
        cfg = self.view_ref.cog._get_message_block_config(self.view_ref.guild_id, self.block_index)
        self.title_input = discord.ui.TextInput(label="Título", default=str(cfg.get("title") or ""), required=False, max_length=120)
        self.subtitle_input = discord.ui.TextInput(label="Subtítulo", default=str(cfg.get("subtitle") or ""), required=False, max_length=220)
        self.footer_input = discord.ui.TextInput(label="Rodapé", default=str(cfg.get("footer") or ""), required=False, style=discord.TextStyle.paragraph, max_length=250)
        self.add_item(self.title_input)
        self.add_item(self.subtitle_input)
        self.add_item(self.footer_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.view_ref.cog._update_message_block_config(
            self.view_ref.guild_id,
            self.block_index,
            title=str(self.title_input.value or "").strip(),
            subtitle=str(self.subtitle_input.value or "").strip(),
            footer=str(self.footer_input.value or "").strip(),
        )
        self.view_ref._build_layout()
        await interaction.response.edit_message(view=self.view_ref)


class _ColorTemplatesEditModal(discord.ui.Modal):
    def __init__(self, view: "_ColorMessagePanelView"):
        super().__init__(title="Editar respostas do painel")
        self.view_ref = view
        cfg = self.view_ref.cog._get_templates(self.view_ref.guild_id)
        self.apply_input = discord.ui.TextInput(label="Aplicar cor", default=str(cfg.get("apply") or ""), style=discord.TextStyle.paragraph, max_length=200)
        self.remove_input = discord.ui.TextInput(label="Remover cor", default=str(cfg.get("remove") or ""), style=discord.TextStyle.paragraph, max_length=200)
        self.switch_input = discord.ui.TextInput(label="Trocar cor", default=str(cfg.get("switch") or ""), style=discord.TextStyle.paragraph, max_length=220)
        self.no_role_input = discord.ui.TextInput(label="Cor não configurada", default=str(cfg.get("no_role") or ""), required=False, max_length=160)
        self.add_item(self.apply_input)
        self.add_item(self.remove_input)
        self.add_item(self.switch_input)
        self.add_item(self.no_role_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.view_ref.cog._update_templates(
            self.view_ref.guild_id,
            apply=str(self.apply_input.value or "").strip(),
            remove=str(self.remove_input.value or "").strip(),
            switch=str(self.switch_input.value or "").strip(),
            no_role=str(self.no_role_input.value or "").strip(),
        )
        self.view_ref._build_layout()
        await interaction.response.edit_message(view=self.view_ref)


class _ColorSlotEditModal(discord.ui.Modal):
    def __init__(self, view: "_ColorComponentPanelView", slot_number: int):
        self.view_ref = view
        self.slot_number = int(slot_number)
        slot = self.view_ref.cog._get_slot_config(self.view_ref.guild_id, self.slot_number)
        super().__init__(title=f"Editar cor {self.slot_number}")
        self.name_input = discord.ui.TextInput(label="Texto da cor", default=str(slot.get("name") or ""), max_length=80)
        self.text_hex_input = discord.ui.TextInput(label="Hex do texto", default=str(slot.get("text_hex") or ""), max_length=7)
        self.role_id_input = discord.ui.TextInput(label="ID do cargo existente (opcional)", default=(str(slot.get("role_id") or "") if int(slot.get("role_id") or 0) else ""), required=False, max_length=30)
        self.role_name_input = discord.ui.TextInput(label="Nome do cargo / novo cargo", default=str(slot.get("role_name") or slot.get("name") or ""), required=False, max_length=100)
        self.role_hex_input = discord.ui.TextInput(label="Hex do cargo", default=str(slot.get("role_hex") or slot.get("text_hex") or ""), max_length=7)
        self.add_item(self.name_input)
        self.add_item(self.text_hex_input)
        self.add_item(self.role_id_input)
        self.add_item(self.role_name_input)
        self.add_item(self.role_hex_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Esse painel só funciona dentro de um servidor.", ephemeral=True)
            return
        role_id_raw = str(self.role_id_input.value or "").strip()
        role_id = int(role_id_raw) if role_id_raw.isdigit() else 0
        role_name = str(self.role_name_input.value or "").strip() or str(self.name_input.value or "").strip() or f"Cor {self.slot_number}"
        text_hex = _clean_hex(str(self.text_hex_input.value or ""), str(self.view_ref.cog._get_slot_config(self.view_ref.guild_id, self.slot_number).get("text_hex") or "#ffffff"))
        role_hex = _clean_hex(str(self.role_hex_input.value or ""), text_hex)
        role_obj: discord.Role | None = guild.get_role(role_id) if role_id else None
        managed = False
        if role_obj is None:
            existing_id = int(self.view_ref.cog._get_slot_config(self.view_ref.guild_id, self.slot_number).get("role_id") or 0)
            existing_role = guild.get_role(existing_id) if existing_id else None
            try:
                colour = discord.Colour(int(role_hex.replace("#", ""), 16))
            except Exception:
                colour = discord.Colour.default()
            if existing_role is not None and self.view_ref.cog._get_slot_config(self.view_ref.guild_id, self.slot_number).get("managed"):
                try:
                    await existing_role.edit(name=role_name, colour=colour, reason="Atualização do painel de cores")
                    role_obj = existing_role
                except Exception:
                    role_obj = existing_role
                managed = True
            else:
                try:
                    role_obj = await guild.create_role(name=role_name, colour=colour, reason="Novo cargo do painel de cores")
                    managed = True
                except Exception:
                    role_obj = None
        else:
            try:
                colour = discord.Colour(int(role_hex.replace("#", ""), 16))
                await role_obj.edit(name=role_name, colour=colour, reason="Vinculado ao painel de cores")
            except Exception:
                pass
        if role_obj is None:
            await interaction.response.send_message("Não consegui preparar o cargo dessa cor. Verifique a hierarquia e tente de novo.", ephemeral=True)
            return
        await self.view_ref.cog._update_slot_config(
            self.view_ref.guild_id,
            self.slot_number,
            name=str(self.name_input.value or "").strip(),
            text_hex=text_hex,
            role_hex=role_hex,
            role_id=int(role_obj.id),
            role_name=role_name,
            managed=managed,
        )
        self.view_ref._build_layout()
        await interaction.response.edit_message(view=self.view_ref)


class _ColorPickerButton(discord.ui.Button):
    def __init__(self, cog: "ColorRolesCog", guild_id: int, slot_number: int):
        super().__init__(label=str(slot_number), style=discord.ButtonStyle.secondary, custom_id=f"color:pick:{guild_id}:{slot_number}")
        self.cog = cog
        self.guild_id = int(guild_id)
        self.slot_number = int(slot_number)

    async def callback(self, interaction: discord.Interaction):
        await self.cog._handle_public_pick(interaction, self.slot_number)


class _ColorPublicPanelView(discord.ui.LayoutView):
    def __init__(self, cog: "ColorRolesCog", guild_id: int, block_index: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.block_index = int(block_index)
        self._build_layout()

    def _build_layout(self):
        self.clear_items()
        block_cfg = self.cog._get_message_block_config(self.guild_id, self.block_index)
        lines: list[str] = []
        title = str(block_cfg.get("title") or "").strip()
        subtitle = str(block_cfg.get("subtitle") or "").strip()
        footer = str(block_cfg.get("footer") or "").strip()
        if title:
            lines.append(f"# {title}")
        if subtitle:
            lines.append(subtitle)
        if footer:
            if lines:
                lines.append("")
            lines.append(footer)
        if lines:
            self.add_item(discord.ui.Container(discord.ui.TextDisplay("\n".join(lines)), accent_color=discord.Color.dark_gray()))
        start, end = _chunk_block(self.block_index)
        buttons: list[discord.ui.Button] = []
        for slot in range(start, end + 1):
            buttons.append(_ColorPickerButton(self.cog, self.guild_id, slot))
        for idx in range(0, len(buttons), 5):
            self.add_item(discord.ui.ActionRow(*buttons[idx:idx + 5]))


class _ColorEditRootView(discord.ui.LayoutView):
    def __init__(self, cog: "ColorRolesCog", *, guild_id: int, owner_id: int):
        super().__init__(timeout=COLOR_PANEL_TIMEOUT)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.owner_id = int(owner_id)
        self.message: discord.Message | None = None
        self._build_layout()

    def _build_layout(self):
        self.clear_items()
        edit_messages = discord.ui.Button(label="Edição de mensagem", style=discord.ButtonStyle.secondary)
        edit_messages.callback = self._open_messages
        edit_components = discord.ui.Button(label="Edição de componentes", style=discord.ButtonStyle.secondary)
        edit_components.callback = self._open_components
        lines = [
            "# 🎨 Editor do painel de cores",
            "Escolha uma área para configurar.",
            "",
            "**Mensagens:** títulos, subtítulos, rodapés e respostas com variáveis.",
            "**Componentes:** texto, cor e cargo entregue em cada botão.",
        ]
        self.add_item(discord.ui.Container(discord.ui.TextDisplay("\n".join(lines)), discord.ui.ActionRow(edit_messages, edit_components), accent_color=discord.Color.blurple()))

    async def _ensure_owner(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.owner_id:
            await interaction.response.send_message("Só quem abriu esse painel pode mexer nele.", ephemeral=True)
            return False
        return True

    async def _open_messages(self, interaction: discord.Interaction):
        if not await self._ensure_owner(interaction):
            return
        view = _ColorMessagePanelView(self.cog, guild_id=self.guild_id, owner_id=self.owner_id)
        view.message = interaction.message
        await interaction.response.edit_message(view=view)

    async def _open_components(self, interaction: discord.Interaction):
        if not await self._ensure_owner(interaction):
            return
        view = _ColorComponentPanelView(self.cog, guild_id=self.guild_id, owner_id=self.owner_id)
        view.message = interaction.message
        await interaction.response.edit_message(view=view)


class _ColorMessagePanelView(discord.ui.LayoutView):
    def __init__(self, cog: "ColorRolesCog", *, guild_id: int, owner_id: int):
        super().__init__(timeout=COLOR_PANEL_TIMEOUT)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.owner_id = int(owner_id)
        self.message: discord.Message | None = None
        self._build_layout()

    def _build_layout(self):
        self.clear_items()
        lines = [
            "# 📝 Edição de mensagem",
            "Use os botões abaixo para editar os textos das 3 mensagens e as respostas do painel.",
            "",
            "**Variáveis disponíveis**",
            "• " + " • ".join(COLOR_PANEL_VARIABLES[:5]),
            "• " + " • ".join(COLOR_PANEL_VARIABLES[5:]),
        ]
        buttons = []
        for block_index in range(1, 4):
            button = discord.ui.Button(label=_block_title(block_index), style=discord.ButtonStyle.secondary)
            async def open_block(interaction: discord.Interaction, *, idx=block_index):
                if int(getattr(interaction.user, "id", 0) or 0) != self.owner_id:
                    await interaction.response.send_message("Só quem abriu esse painel pode mexer nele.", ephemeral=True)
                    return
                await interaction.response.send_modal(_ColorMessageEditModal(self, idx))
            button.callback = open_block
            buttons.append(button)
        responses_btn = discord.ui.Button(label="Respostas", style=discord.ButtonStyle.secondary)
        async def open_templates(interaction: discord.Interaction):
            if int(getattr(interaction.user, "id", 0) or 0) != self.owner_id:
                await interaction.response.send_message("Só quem abriu esse painel pode mexer nele.", ephemeral=True)
                return
            await interaction.response.send_modal(_ColorTemplatesEditModal(self))
        responses_btn.callback = open_templates
        buttons.append(responses_btn)
        row1 = discord.ui.ActionRow(*buttons[:4])
        self.add_item(discord.ui.Container(discord.ui.TextDisplay("\n".join(lines)), row1, accent_color=discord.Color.blurple()))


class _ColorSlotSelect(discord.ui.Select):
    def __init__(self, view: "_ColorComponentPanelView"):
        self.parent_view = view
        options = []
        for slot_number in range(1, 31):
            slot = view.cog._get_slot_config(view.guild_id, slot_number)
            options.append(discord.SelectOption(label=f"{slot_number}. {slot.get('name')}", value=str(slot_number)))
        super().__init__(placeholder="Escolha um slot para editar", options=options, min_values=1, max_values=1, custom_id=f"color:edit-select:{view.guild_id}:{view.owner_id}")

    async def callback(self, interaction: discord.Interaction):
        if int(getattr(interaction.user, "id", 0) or 0) != self.parent_view.owner_id:
            await interaction.response.send_message("Só quem abriu esse painel pode mexer nele.", ephemeral=True)
            return
        try:
            self.parent_view.selected_slot = int(self.values[0])
        except Exception:
            self.parent_view.selected_slot = 1
        self.parent_view._build_layout()
        await interaction.response.edit_message(view=self.parent_view)


class _ColorComponentPanelView(discord.ui.LayoutView):
    def __init__(self, cog: "ColorRolesCog", *, guild_id: int, owner_id: int):
        super().__init__(timeout=COLOR_PANEL_TIMEOUT)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.owner_id = int(owner_id)
        self.message: discord.Message | None = None
        self.selected_slot = 1
        self._build_layout()

    def _build_layout(self):
        self.clear_items()
        slot = self.cog._get_slot_config(self.guild_id, self.selected_slot)
        role_id = int(slot.get("role_id") or 0)
        role_repr = f"<@&{role_id}>" if role_id else "Sem cargo"
        lines = [
            "# 🎛️ Edição de componentes",
            f"**Slot atual:** {self.selected_slot}",
            f"**Texto:** {slot.get('name')}",
            f"**Hex do texto:** {slot.get('text_hex')}",
            f"**Cargo:** {role_repr}",
            f"**Hex do cargo:** {slot.get('role_hex')}",
            "",
            "Deixe o ID do cargo vazio para o bot criar ou atualizar um cargo próprio dessa cor.",
        ]
        select_row = discord.ui.ActionRow(_ColorSlotSelect(self))
        edit_button = discord.ui.Button(label="Editar slot", style=discord.ButtonStyle.secondary)
        async def edit_slot(interaction: discord.Interaction):
            if int(getattr(interaction.user, "id", 0) or 0) != self.owner_id:
                await interaction.response.send_message("Só quem abriu esse painel pode mexer nele.", ephemeral=True)
                return
            await interaction.response.send_modal(_ColorSlotEditModal(self, self.selected_slot))
        edit_button.callback = edit_slot
        self.add_item(discord.ui.Container(discord.ui.TextDisplay("\n".join(lines)), select_row, discord.ui.ActionRow(edit_button), accent_color=discord.Color.blurple()))


class ColorRolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active_edit_messages: dict[tuple[int, int], int] = {}
        self._public_views_registered: set[tuple[int, int, int]] = set()
        self._color_panel_cd: dict[int, float] = {}

    @property
    def db(self):
        return getattr(self.bot, "settings_db", None)

    async def cog_load(self):
        await self._restore_public_panel_views()

    async def _restore_public_panel_views(self):
        db = self.db
        if db is None or not hasattr(db, "get_color_roles_config"):
            return
        guild_cache = getattr(db, "guild_cache", {}) or {}
        for gid in list(guild_cache.keys()):
            try:
                cfg = db.get_color_roles_config(int(gid))
            except Exception:
                continue
            message_ids = cfg.get("message_ids") or []
            if len(message_ids) != 3:
                continue
            for block_index, message_id in enumerate(message_ids, start=1):
                if not message_id:
                    continue
                key = (int(gid), block_index, int(message_id))
                if key in self._public_views_registered:
                    continue
                view = _ColorPublicPanelView(self, int(gid), block_index)
                try:
                    self.bot.add_view(view, message_id=int(message_id))
                    self._public_views_registered.add(key)
                except Exception:
                    pass

    def _get_config(self, guild_id: int) -> dict[str, Any]:
        db = self.db
        if db is None or not hasattr(db, "get_color_roles_config"):
            return _deepcopy_default_config()
        return db.get_color_roles_config(int(guild_id))

    async def _save_config(self, guild_id: int, config: dict[str, Any]):
        db = self.db
        if db is None or not hasattr(db, "set_color_roles_config"):
            return
        await db.set_color_roles_config(int(guild_id), config)

    def _get_templates(self, guild_id: int) -> dict[str, str]:
        cfg = self._get_config(guild_id)
        return dict(cfg.get("templates") or {})

    async def _update_templates(self, guild_id: int, **kwargs: str):
        cfg = self._get_config(guild_id)
        templates = dict(cfg.get("templates") or {})
        for key, value in kwargs.items():
            if value:
                templates[key] = value
        cfg["templates"] = templates
        await self._save_config(guild_id, cfg)

    def _get_message_block_config(self, guild_id: int, block_index: int) -> dict[str, str]:
        cfg = self._get_config(guild_id)
        return dict((cfg.get("messages") or {}).get(str(block_index), {}) or {})

    async def _update_message_block_config(self, guild_id: int, block_index: int, *, title: str, subtitle: str, footer: str):
        cfg = self._get_config(guild_id)
        messages = dict(cfg.get("messages") or {})
        messages[str(block_index)] = {"title": title, "subtitle": subtitle, "footer": footer}
        cfg["messages"] = messages
        await self._save_config(guild_id, cfg)

    def _get_slot_config(self, guild_id: int, slot_number: int) -> dict[str, Any]:
        cfg = self._get_config(guild_id)
        slots = cfg.get("slots") or {}
        slot = dict(slots.get(str(slot_number), {}) or {})
        if not slot:
            default = next((item for item in DEFAULT_SLOTS if item["number"] == int(slot_number)), None)
            if default:
                slot = {**default, "role_id": 0, "role_name": default["name"], "managed": False}
        return slot

    async def _update_slot_config(self, guild_id: int, slot_number: int, **updates: Any):
        cfg = self._get_config(guild_id)
        slots = dict(cfg.get("slots") or {})
        slot = dict(slots.get(str(slot_number), {}) or {})
        slot.update(updates)
        slot["number"] = int(slot_number)
        if not slot.get("role_name"):
            slot["role_name"] = str(slot.get("name") or f"Cor {slot_number}")
        slots[str(slot_number)] = slot
        cfg["slots"] = slots
        await self._save_config(guild_id, cfg)

    def _render_template(self, template: str, *, member: discord.Member, slot: dict[str, Any], added_name: str = "", removed_name: str = "") -> str:
        guild = member.guild
        role_id = int(slot.get("role_id") or 0)
        role = guild.get_role(role_id) if guild and role_id else None
        payload = {
            "membro": member.mention,
            "membro_nome": member.display_name,
            "membro_id": str(member.id),
            "numero": str(slot.get("number") or ""),
            "cor_nome": str(slot.get("name") or ""),
            "cor_adicionada": str(added_name or slot.get("name") or ""),
            "cor_removida": str(removed_name or ""),
            "cargo": role.mention if role else "",
            "cargo_nome": role.name if role else str(slot.get("role_name") or slot.get("name") or ""),
            "servidor": guild.name if guild else "",
        }
        text = str(template or "").strip()
        for key, value in payload.items():
            text = text.replace(f"{{{key}}}", str(value))
        return text

    def _all_color_role_ids(self, guild_id: int) -> list[int]:
        cfg = self._get_config(guild_id)
        role_ids = []
        for slot in (cfg.get("slots") or {}).values():
            try:
                rid = int(slot.get("role_id") or 0)
            except Exception:
                rid = 0
            if rid:
                role_ids.append(rid)
        return role_ids

    def _member_current_color_slot(self, guild: discord.Guild, member: discord.Member) -> tuple[int, dict[str, Any] | None]:
        cfg = self._get_config(guild.id)
        slots = cfg.get("slots") or {}
        role_ids = {role.id for role in member.roles}
        for slot_num_str, slot in slots.items():
            try:
                rid = int(slot.get("role_id") or 0)
                slot_num = int(slot_num_str)
            except Exception:
                continue
            if rid and rid in role_ids:
                return slot_num, dict(slot)
        return 0, None

    async def _handle_public_pick(self, interaction: discord.Interaction, slot_number: int):
        guild = interaction.guild
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if guild is None or member is None:
            await interaction.response.send_message("Esse painel só funciona dentro de um servidor.", ephemeral=True)
            return
        cfg = self._get_config(guild.id)
        message_ids = [int(mid) for mid in (cfg.get("message_ids") or []) if mid]
        if int(getattr(interaction.message, "id", 0) or 0) not in message_ids:
            await interaction.response.send_message(str((cfg.get("templates") or {}).get("missing_panel") or "Esse painel não é mais o oficial."), ephemeral=True)
            return
        slot = self._get_slot_config(guild.id, slot_number)
        role_id = int(slot.get("role_id") or 0)
        if role_id <= 0:
            await interaction.response.send_message(str((cfg.get("templates") or {}).get("no_role") or "Essa cor ainda não está configurada."), ephemeral=True)
            return
        target_role = guild.get_role(role_id)
        if target_role is None:
            await interaction.response.send_message("O cargo dessa cor não existe mais. Peça para a staff revisar o painel.", ephemeral=True)
            return
        me = guild.me or guild.get_member(self.bot.user.id) if self.bot.user else None
        if me is None or target_role >= me.top_role:
            text = self._render_template(str((cfg.get("templates") or {}).get("hierarchy") or ""), member=member, slot=slot)
            await interaction.response.send_message(text or "Não consegui aplicar essa cor por causa da hierarquia de cargos.", ephemeral=True)
            return
        current_slot_number, current_slot = self._member_current_color_slot(guild, member)
        roles_to_remove = []
        for rid in self._all_color_role_ids(guild.id):
            role_obj = guild.get_role(rid)
            if role_obj and role_obj in member.roles:
                roles_to_remove.append(role_obj)
        try:
            if current_slot_number == int(slot_number):
                if roles_to_remove:
                    await member.remove_roles(*roles_to_remove, reason="Remoção de cor pelo painel")
                template = str((cfg.get("templates") or {}).get("remove") or "")
                text = self._render_template(template, member=member, slot=slot, removed_name=str(slot.get("name") or ""))
                await interaction.response.send_message(text or "Sua cor foi removida.", ephemeral=True)
                return
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Troca de cor pelo painel")
            await member.add_roles(target_role, reason="Cor escolhida pelo painel")
        except discord.Forbidden:
            text = self._render_template(str((cfg.get("templates") or {}).get("hierarchy") or ""), member=member, slot=slot)
            await interaction.response.send_message(text or "Não consegui aplicar essa cor por causa da hierarquia de cargos.", ephemeral=True)
            return
        if current_slot:
            template = str((cfg.get("templates") or {}).get("switch") or "")
            text = self._render_template(template, member=member, slot=slot, added_name=str(slot.get("name") or ""), removed_name=str(current_slot.get("name") or ""))
        else:
            template = str((cfg.get("templates") or {}).get("apply") or "")
            text = self._render_template(template, member=member, slot=slot, added_name=str(slot.get("name") or ""))
        await interaction.response.send_message(text or f"A cor {slot.get('name')} foi aplicada.", ephemeral=True)

    async def _delete_existing_panel_messages(self, guild_id: int):
        cfg = self._get_config(guild_id)
        channel_id = int(cfg.get("channel_id") or 0)
        channel = self.bot.get_channel(channel_id) if channel_id else None
        message_ids = [int(mid) for mid in (cfg.get("message_ids") or []) if mid]
        for message_id in message_ids:
            if channel is None:
                break
            try:
                msg = await channel.fetch_message(message_id)
                await msg.delete()
            except Exception:
                pass

    def _make_block_image(self, guild_id: int, block_index: int) -> discord.File:
        start, end = _chunk_block(block_index)
        cfg = self._get_config(guild_id)
        slots = cfg.get("slots") or {}
        width, height = 900, 330
        image = Image.new("RGB", (width, height), color="#000000")
        draw = ImageDraw.Draw(image)
        font = _font(34, bold=True)
        y_positions = [20, 85, 150, 215, 280]
        x_left, x_right = 18, 465
        for idx, slot_number in enumerate(range(start, end + 1)):
            slot = dict(slots.get(str(slot_number), {}) or {})
            label = f"{slot_number}. {slot.get('name') or f'Cor {slot_number}'}"
            hex_color = _clean_hex(str(slot.get("text_hex") or "#ffffff"), "#ffffff")
            x = x_left if idx % 2 == 0 else x_right
            y = y_positions[idx // 2]
            draw.text((x, y), label, font=font, fill=hex_color)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return discord.File(buffer, filename=f"colors-{block_index}.png")

    async def _post_public_panel(self, channel: discord.abc.Messageable, guild: discord.Guild) -> list[int]:
        message_ids: list[int] = []
        for block_index in range(1, COLOR_BLOCK_COUNT + 1):
            view = _ColorPublicPanelView(self, guild.id, block_index)
            file = self._make_block_image(guild.id, block_index)
            message = await channel.send(file=file, view=view)
            message_ids.append(int(message.id))
            key = (guild.id, block_index, int(message.id))
            try:
                self.bot.add_view(view, message_id=int(message.id))
            except Exception:
                pass
            self._public_views_registered.add(key)
        return message_ids

    def _is_admin(self, member: discord.Member | None) -> bool:
        if member is None:
            return False
        return bool(member.guild_permissions.administrator)

    async def _consume_color_command_cooldown(self, guild_id: int) -> float:
        now = time.monotonic()
        last = float(self._color_panel_cd.get(guild_id, 0.0) or 0.0)
        if now - last < COLOR_COMMAND_COOLDOWN:
            return COLOR_COMMAND_COOLDOWN - (now - last)
        self._color_panel_cd[guild_id] = now
        return 0.0

    @commands.command(name="color")
    @commands.guild_only()
    async def color_command(self, ctx: commands.Context):
        if not self._is_admin(getattr(ctx, "author", None)):
            await ctx.reply("Só administradores podem convocar o painel de cores.", mention_author=False)
            return
        remaining = await self._consume_color_command_cooldown(ctx.guild.id)
        if remaining > 0:
            await ctx.reply(f"Espere {remaining:.0f}s para convocar o painel de cores de novo.", mention_author=False)
            return
        await self._delete_existing_panel_messages(ctx.guild.id)
        message_ids = await self._post_public_panel(ctx.channel, ctx.guild)
        cfg = self._get_config(ctx.guild.id)
        cfg["channel_id"] = int(ctx.channel.id)
        cfg["message_ids"] = message_ids
        await self._save_config(ctx.guild.id, cfg)
        await ctx.reply("Painel de cores atualizado neste canal.", mention_author=False)

    @commands.command(name="coloredit")
    @commands.guild_only()
    async def coloredit_command(self, ctx: commands.Context):
        if not self._is_admin(getattr(ctx, "author", None)):
            await ctx.reply("Só administradores podem abrir o editor de cores.", mention_author=False)
            return
        key = (ctx.guild.id, ctx.author.id)
        old_id = self._active_edit_messages.get(key)
        if old_id:
            try:
                old_msg = await ctx.channel.fetch_message(old_id)
                await old_msg.delete()
            except Exception:
                pass
        view = _ColorEditRootView(self, guild_id=ctx.guild.id, owner_id=ctx.author.id)
        msg = await ctx.reply(view=view, mention_author=False)
        view.message = msg
        self._active_edit_messages[key] = int(msg.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(ColorRolesCog(bot))
