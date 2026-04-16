from __future__ import annotations

import io
import re
import time
from copy import deepcopy
from typing import Any

import discord
from discord.ext import commands

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover - runtime guard only
    Image = None
    ImageDraw = None
    ImageFont = None


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


def _font(size: int, *, bold: bool = True):
    if ImageFont is None:
        raise RuntimeError("Pillow não está disponível.")
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


def _attachment_uri(filename: str) -> str:
    return f"attachment://{filename}"


def _build_media_gallery(filename: str):
    ui = getattr(discord, "ui", None)
    media_gallery_cls = getattr(ui, "MediaGallery", None) if ui else None
    if media_gallery_cls is None:
        return None
    attachment_uri = _attachment_uri(filename)
    item_types = [
        getattr(discord, "MediaGalleryItem", None),
        getattr(ui, "MediaGalleryItem", None) if ui else None,
        getattr(discord, "UnfurledMediaItem", None),
        getattr(ui, "UnfurledMediaItem", None) if ui else None,
    ]
    item_types = [item_type for item_type in item_types if item_type is not None]
    for item_type in item_types:
        created_item = None
        for args, kwargs in (
            ((), {"media": attachment_uri}),
            ((), {"url": attachment_uri}),
            ((attachment_uri,), {}),
        ):
            try:
                created_item = item_type(*args, **kwargs)
                break
            except Exception:
                continue
        if created_item is None:
            continue
        for args, kwargs in (
            ((created_item,), {}),
            (([created_item],), {}),
            ((), {"items": [created_item]}),
        ):
            try:
                return media_gallery_cls(*args, **kwargs)
            except Exception:
                continue
    for args, kwargs in (
        ((attachment_uri,), {}),
        (([attachment_uri],), {}),
        ((), {"items": [attachment_uri]}),
    ):
        try:
            return media_gallery_cls(*args, **kwargs)
        except Exception:
            continue
    return None


class _ColorFieldEditModal(discord.ui.Modal):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int, field_name: str):
        labels = {"title": "Título", "subtitle": "Descrição", "footer": "Footer"}
        super().__init__(title=f"Editar {labels.get(field_name, field_name)} {_block_title(block_index)}")
        self.view_ref = view
        self.block_index = int(block_index)
        self.field_name = field_name
        cfg = self.view_ref.cog._get_message_block_config(self.view_ref.guild_id, self.block_index)
        current = str(cfg.get(field_name) or "")
        self.input = discord.ui.TextInput(
            label=labels.get(field_name, field_name),
            default=current,
            required=False,
            style=discord.TextStyle.paragraph if field_name != "title" else discord.TextStyle.short,
            max_length=300,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction):
        block_cfg = self.view_ref.cog._get_message_block_config(self.view_ref.guild_id, self.block_index)
        block_cfg[self.field_name] = str(self.input.value or "").strip()
        await self.view_ref.cog._update_message_block_config(
            self.view_ref.guild_id,
            self.block_index,
            title=str(block_cfg.get("title") or ""),
            subtitle=str(block_cfg.get("subtitle") or ""),
            footer=str(block_cfg.get("footer") or ""),
        )
        await self.view_ref.cog._refresh_public_panel_messages(self.view_ref.guild_id, block_indices=[self.block_index])
        await self.view_ref.refresh_editor_message(interaction)


class _ColorTemplatesEditModal(discord.ui.Modal):
    def __init__(self, view: "_ColorUnifiedEditView"):
        super().__init__(title="Editar respostas do painel")
        self.view_ref = view
        cfg = self.view_ref.cog._get_templates(self.view_ref.guild_id)
        self.apply_input = discord.ui.TextInput(label="Aplicar cor", default=str(cfg.get("apply") or ""), style=discord.TextStyle.paragraph, max_length=200)
        self.remove_input = discord.ui.TextInput(label="Remover cor", default=str(cfg.get("remove") or ""), style=discord.TextStyle.paragraph, max_length=200)
        self.switch_input = discord.ui.TextInput(label="Trocar cor", default=str(cfg.get("switch") or ""), style=discord.TextStyle.paragraph, max_length=220)
        self.add_item(self.apply_input)
        self.add_item(self.remove_input)
        self.add_item(self.switch_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.view_ref.cog._update_templates(
            self.view_ref.guild_id,
            apply=str(self.apply_input.value or "").strip(),
            remove=str(self.remove_input.value or "").strip(),
            switch=str(self.switch_input.value or "").strip(),
        )
        await self.view_ref.refresh_editor_message(interaction)


class _ColorSlotEditModal(discord.ui.Modal):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int, slot_number: int):
        super().__init__(title=f"Editar slot {slot_number}")
        self.view_ref = view
        self.block_index = int(block_index)
        self.slot_number = int(slot_number)
        slot = self.view_ref.cog._get_slot_config(self.view_ref.guild_id, self.slot_number)
        self.name_input = discord.ui.TextInput(label="Nome da cor", default=str(slot.get("name") or ""), max_length=80)
        self.text_hex_input = discord.ui.TextInput(label="Hex do texto", default=str(slot.get("text_hex") or ""), max_length=7)
        self.role_name_input = discord.ui.TextInput(label="Nome do cargo do bot", default=str(slot.get("role_name") or slot.get("name") or ""), required=False, max_length=100)
        self.role_hex_input = discord.ui.TextInput(label="Hex do cargo", default=str(slot.get("role_hex") or slot.get("text_hex") or ""), max_length=7)
        self.add_item(self.name_input)
        self.add_item(self.text_hex_input)
        self.add_item(self.role_name_input)
        self.add_item(self.role_hex_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.view_ref.cog._update_slot_config(
            self.view_ref.guild_id,
            self.slot_number,
            name=str(self.name_input.value or "").strip() or f"Cor {self.slot_number}",
            text_hex=_clean_hex(str(self.text_hex_input.value or ""), "#ffffff"),
            role_name=str(self.role_name_input.value or "").strip() or f"Cor {self.slot_number}",
            role_hex=_clean_hex(str(self.role_hex_input.value or ""), "#ffffff"),
        )
        guild = interaction.guild
        if guild is not None:
            await self.view_ref.cog._ensure_slot_role(guild, self.slot_number)
        await self.view_ref.cog._refresh_public_panel_messages(self.view_ref.guild_id, block_indices=[self.block_index])
        await self.view_ref.refresh_editor_message(interaction)


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
        panel_children = []
        if lines:
            panel_children.append(discord.ui.TextDisplay("\n".join(lines)))
        media_gallery = _build_media_gallery(f"colors-{self.block_index}.png")
        if media_gallery is not None:
            panel_children.append(media_gallery)
        if panel_children:
            self.add_item(discord.ui.Container(*panel_children, accent_color=discord.Color.dark_gray()))
        start, end = _chunk_block(self.block_index)
        buttons = [_ColorPickerButton(self.cog, self.guild_id, slot) for slot in range(start, end + 1)]
        for idx in range(0, len(buttons), 5):
            self.add_item(discord.ui.ActionRow(*buttons[idx:idx + 5]))


class _EditFieldButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int, field_name: str, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.view_ref = view
        self.block_index = int(block_index)
        self.field_name = field_name

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        await interaction.response.send_modal(_ColorFieldEditModal(self.view_ref, self.block_index, self.field_name))


class _OpenBlockEditorButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int):
        style = discord.ButtonStyle.primary if view.active_block == block_index else discord.ButtonStyle.secondary
        super().__init__(label=f"Editar visual/cargos {_block_title(block_index)}", style=style)
        self.view_ref = view
        self.block_index = int(block_index)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        self.view_ref.active_block = self.block_index
        await self.view_ref.refresh_editor_message(interaction)


class _EditTemplatesButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView"):
        super().__init__(label="Editar respostas do painel", style=discord.ButtonStyle.secondary)
        self.view_ref = view

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        await interaction.response.send_modal(_ColorTemplatesEditModal(self.view_ref))


class _BlockSlotSelect(discord.ui.Select):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int):
        self.view_ref = view
        self.block_index = int(block_index)
        start, end = _chunk_block(block_index)
        options = []
        for slot_number in range(start, end + 1):
            slot = view.cog._get_slot_config(view.guild_id, slot_number)
            options.append(discord.SelectOption(label=f"{slot_number}. {slot.get('name')}", value=str(slot_number), default=view.selected_slots.get(block_index, start) == slot_number))
        super().__init__(placeholder="Escolha o slot desta faixa", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        self.view_ref.selected_slots[self.block_index] = int(self.values[0])
        await self.view_ref.refresh_editor_message(interaction)


class _BlockRoleSelect(discord.ui.RoleSelect):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int):
        super().__init__(placeholder="Vincular um cargo existente ao slot atual", min_values=1, max_values=1)
        self.view_ref = view
        self.block_index = int(block_index)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        selected_slot = self.view_ref.selected_slots.get(self.block_index, _chunk_block(self.block_index)[0])
        if not self.values:
            await interaction.response.send_message("Escolha um cargo para vincular ao slot.", ephemeral=True)
            return
        role = self.values[0]
        slot = self.view_ref.cog._get_slot_config(self.view_ref.guild_id, selected_slot)
        await self.view_ref.cog._update_slot_config(
            self.view_ref.guild_id,
            selected_slot,
            role_id=int(role.id),
            role_name=str(role.name),
            managed=False,
            name=str(slot.get("name") or f"Cor {selected_slot}"),
            text_hex=str(slot.get("text_hex") or "#ffffff"),
            role_hex=str(slot.get("role_hex") or "#ffffff"),
        )
        await self.view_ref.cog._refresh_public_panel_messages(self.view_ref.guild_id, block_indices=[self.block_index])
        await self.view_ref.refresh_editor_message(interaction)


class _AutoRoleButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int):
        super().__init__(label="Usar cargo automático do bot", style=discord.ButtonStyle.secondary)
        self.view_ref = view
        self.block_index = int(block_index)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Isso só funciona dentro de um servidor.", ephemeral=True)
            return
        selected_slot = self.view_ref.selected_slots.get(self.block_index, _chunk_block(self.block_index)[0])
        await self.view_ref.cog._update_slot_config(self.view_ref.guild_id, selected_slot, role_id=0, managed=True)
        await self.view_ref.cog._ensure_slot_role(guild, selected_slot)
        await self.view_ref.cog._refresh_public_panel_messages(self.view_ref.guild_id, block_indices=[self.block_index])
        await self.view_ref.refresh_editor_message(interaction)


class _EditSlotButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int):
        super().__init__(label="Editar slot atual", style=discord.ButtonStyle.secondary)
        self.view_ref = view
        self.block_index = int(block_index)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        selected_slot = self.view_ref.selected_slots.get(self.block_index, _chunk_block(self.block_index)[0])
        await interaction.response.send_modal(_ColorSlotEditModal(self.view_ref, self.block_index, selected_slot))


class _ColorUnifiedEditView(discord.ui.LayoutView):
    def __init__(self, cog: "ColorRolesCog", *, guild_id: int, owner_id: int):
        super().__init__(timeout=COLOR_PANEL_TIMEOUT)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.owner_id = int(owner_id)
        self.active_block = 1
        self.selected_slots = {1: 1, 2: 11, 3: 21}
        self.message: discord.Message | None = None
        self._build_layout()

    async def ensure_owner(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.owner_id:
            await interaction.response.send_message("Só quem abriu esse painel pode mexer nele.", ephemeral=True)
            return False
        return True

    def _editor_preview_files(self) -> list[discord.File]:
        return [
            self.cog._make_block_image(self.guild_id, block_index, filename=f"colors-editor-{block_index}.png")
            for block_index in range(1, COLOR_BLOCK_COUNT + 1)
        ]

    async def refresh_editor_message(self, interaction: discord.Interaction):
        self._build_layout()
        payload = {
            "view": self,
            "attachments": self._editor_preview_files(),
        }
        if not interaction.response.is_done():
            await interaction.response.defer()
        target = interaction.message or self.message
        if target is not None:
            await target.edit(**payload)
            self.message = target

    def _block_preview_lines(self, block_index: int) -> list[str]:
        cfg = self.cog._get_message_block_config(self.guild_id, block_index)
        start, end = _chunk_block(block_index)
        title = str(cfg.get("title") or "").strip() or "(vazio)"
        subtitle = str(cfg.get("subtitle") or "").strip() or "(vazio)"
        footer = str(cfg.get("footer") or "").strip() or "(vazio)"
        return [
            f"## Faixa {_block_title(block_index)}",
            f"**Título atual:** {title}",
            f"**Descrição atual:** {subtitle}",
            f"**Footer atual:** {footer}",
            f"**Slots desta faixa:** {start}–{end}",
            "A imagem abaixo é o preview real desta mensagem.",
        ]

    def _slot_editor_lines(self, block_index: int) -> list[str]:
        selected_slot = self.selected_slots.get(block_index, _chunk_block(block_index)[0])
        slot = self.cog._get_slot_config(self.guild_id, selected_slot)
        role_id = int(slot.get("role_id") or 0)
        role_repr = f"<@&{role_id}>" if role_id else "Cargo criado e atualizado pelo bot"
        managed_text = "sim" if bool(slot.get("managed", False) or role_id <= 0) else "não"
        return [
            f"## Editor da faixa {_block_title(block_index)}",
            f"**Slot selecionado:** {selected_slot}",
            f"**Nome exibido na imagem:** {slot.get('name')}",
            f"**Cor do texto na imagem:** {slot.get('text_hex')}",
            f"**Cargo vinculado:** {role_repr}",
            f"**Cor do cargo:** {slot.get('role_hex')}",
            f"**Gerenciado pelo bot:** {managed_text}",
            "Escolha um slot para editar o visual da faixa, vincular um cargo existente ou deixar o bot cuidar do cargo automaticamente.",
        ]

    def _build_layout(self):
        self.clear_items()
        cfg = self.cog._get_config(self.guild_id)
        panel_ready = bool(int(cfg.get("channel_id") or 0) and list(cfg.get("message_ids") or []))
        panel_status = "Painel oficial encontrado: as mudanças já refletem ao vivo nas mensagens públicas." if panel_ready else "Painel oficial ainda não foi publicado: você já pode configurar tudo aqui e depois usar `_color` para postar as 3 mensagens."
        header_lines = [
            "# 🎨 Editor unificado do painel de cores",
            panel_status,
            "Cada faixa abaixo mostra o preview real da imagem e os campos de texto da mensagem pública.",
            "Use o bloco verde no final para editar o visual e os cargos da faixa selecionada.",
            "",
            "**Variáveis aceitas nas respostas**",
            "• " + " • ".join(COLOR_PANEL_VARIABLES[:5]),
            "• " + " • ".join(COLOR_PANEL_VARIABLES[5:]),
        ]
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(header_lines)),
            discord.ui.ActionRow(_EditTemplatesButton(self)),
            accent_color=discord.Color.green(),
        ))
        for block_index in range(1, 4):
            preview_children = [discord.ui.TextDisplay("\n".join(self._block_preview_lines(block_index)))]
            preview_gallery = _build_media_gallery(f"colors-editor-{block_index}.png")
            if preview_gallery is not None:
                preview_children.append(preview_gallery)
            preview_children.append(discord.ui.ActionRow(
                _EditFieldButton(self, block_index, "title", "Editar título"),
                _EditFieldButton(self, block_index, "subtitle", "Editar descrição"),
                _EditFieldButton(self, block_index, "footer", "Editar footer"),
                _OpenBlockEditorButton(self, block_index),
            ))
            preview_container = discord.ui.Container(
                *preview_children,
                accent_color=discord.Color.green() if self.active_block == block_index else discord.Color.dark_green(),
            )
            self.add_item(preview_container)
        active_block = self.active_block
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(self._slot_editor_lines(active_block))),
            discord.ui.ActionRow(_BlockSlotSelect(self, active_block)),
            discord.ui.ActionRow(_BlockRoleSelect(self, active_block)),
            discord.ui.ActionRow(_AutoRoleButton(self, active_block), _EditSlotButton(self, active_block)),
            accent_color=discord.Color.brand_green(),
        ))


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
        if db is None or not hasattr(db, "guild_cache"):
            return
        for gid, doc in list(getattr(db, "guild_cache", {}).items()):
            cfg = self._get_config(int(gid))
            message_ids = [int(mid) for mid in (cfg.get("message_ids") or []) if mid]
            for block_index, message_id in enumerate(message_ids, start=1):
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
        return dict(self._get_config(guild_id).get("templates") or {})

    async def _update_templates(self, guild_id: int, **kwargs: str):
        cfg = self._get_config(guild_id)
        templates = dict(cfg.get("templates") or {})
        for key, value in kwargs.items():
            templates[key] = str(value or "")
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
        slot = dict((cfg.get("slots") or {}).get(str(slot_number), {}) or {})
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

    async def _ensure_slot_role(self, guild: discord.Guild, slot_number: int) -> discord.Role | None:
        slot = self._get_slot_config(guild.id, slot_number)
        role_id = int(slot.get("role_id") or 0)
        existing = guild.get_role(role_id) if role_id else None
        desired_name = str(slot.get("role_name") or slot.get("name") or f"Cor {slot_number}")
        desired_colour = discord.Colour.from_str(_clean_hex(str(slot.get("role_hex") or ""), "#ffffff"))
        if existing and not bool(slot.get("managed")):
            return existing
        me = guild.me or guild.get_member(self.bot.user.id) if self.bot.user else None
        if me is None or not me.guild_permissions.manage_roles:
            return existing
        try:
            if existing is None:
                existing = await guild.create_role(name=desired_name, colour=desired_colour, reason="Criando cargo da paleta de cores")
            else:
                await existing.edit(name=desired_name, colour=desired_colour, reason="Atualizando cargo da paleta de cores")
            await self._update_slot_config(guild.id, slot_number, role_id=int(existing.id), role_name=existing.name, managed=True, role_hex=_clean_hex(str(slot.get("role_hex") or ""), "#ffffff"))
            return existing
        except Exception:
            return existing

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
        result = []
        for slot in (cfg.get("slots") or {}).values():
            try:
                rid = int(slot.get("role_id") or 0)
            except Exception:
                rid = 0
            if rid:
                result.append(rid)
        return result

    def _member_current_color_slot(self, guild: discord.Guild, member: discord.Member) -> tuple[int, dict[str, Any] | None]:
        cfg = self._get_config(guild.id)
        role_ids = {role.id for role in member.roles}
        for slot_num_str, slot in (cfg.get("slots") or {}).items():
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
        target_role = guild.get_role(role_id) if role_id else None
        if target_role is None:
            target_role = await self._ensure_slot_role(guild, slot_number)
            role_id = int(target_role.id) if target_role else 0
        if role_id <= 0 or target_role is None:
            await interaction.response.send_message(str((cfg.get("templates") or {}).get("no_role") or "Essa cor ainda não está configurada."), ephemeral=True)
            return
        me = guild.me or guild.get_member(self.bot.user.id) if self.bot.user else None
        if me is None or target_role >= me.top_role:
            text = self._render_template(str((cfg.get("templates") or {}).get("hierarchy") or ""), member=member, slot=slot)
            await interaction.response.send_message(text or "Não consegui aplicar essa cor por causa da hierarquia de cargos.", ephemeral=True)
            return
        current_slot_number, current_slot = self._member_current_color_slot(guild, member)
        roles_to_remove = [guild.get_role(rid) for rid in self._all_color_role_ids(guild.id)]
        roles_to_remove = [role for role in roles_to_remove if role and role in member.roles]
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

    def _make_block_image(self, guild_id: int, block_index: int, *, filename: str | None = None) -> discord.File:
        if Image is None or ImageDraw is None:
            raise RuntimeError("Pillow não está disponível para gerar as imagens do painel de cores.")
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
        return discord.File(buffer, filename=filename or f"colors-{block_index}.png")

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

    async def _refresh_public_panel_messages(self, guild_id: int, *, block_indices: list[int] | None = None):
        cfg = self._get_config(guild_id)
        channel_id = int(cfg.get("channel_id") or 0)
        message_ids = [int(mid) for mid in (cfg.get("message_ids") or []) if mid]
        if not channel_id or not message_ids:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return
        targets = block_indices or list(range(1, min(len(message_ids), COLOR_BLOCK_COUNT) + 1))
        for block_index in targets:
            if block_index < 1 or block_index > len(message_ids):
                continue
            message_id = message_ids[block_index - 1]
            try:
                message = await channel.fetch_message(message_id)
            except Exception:
                continue
            file = self._make_block_image(guild_id, block_index)
            view = _ColorPublicPanelView(self, guild_id, block_index)
            try:
                await message.edit(content=None, attachments=[file], view=view)
            except Exception:
                pass
            key = (guild_id, block_index, message_id)
            try:
                self.bot.add_view(view, message_id=message_id)
            except Exception:
                pass
            self._public_views_registered.add(key)

    def _is_admin(self, member: discord.Member | None) -> bool:
        return bool(member and member.guild_permissions.administrator)

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
        view = _ColorUnifiedEditView(self, guild_id=ctx.guild.id, owner_id=ctx.author.id)
        msg = await ctx.reply(view=view, files=view._editor_preview_files(), mention_author=False)
        view.message = msg
        self._active_edit_messages[key] = int(msg.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(ColorRolesCog(bot))
