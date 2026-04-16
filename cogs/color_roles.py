from __future__ import annotations

import io
import re
import time
from copy import deepcopy
from typing import Any, Awaitable, Callable

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
COLOR_MAX_MESSAGES = 5
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
    {"number": 10, "name": "Preto", "text_hex": "#000000", "role_hex": "#1f1f1f"},
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

_DEFAULT_MESSAGE = {"title": "", "subtitle": "", "footer": ""}

_DEFAULT_CONFIG: dict[str, Any] = {
    "channel_id": 0,
    "message_ids": [],
    "panel_count": 3,
    "messages": {str(index): dict(_DEFAULT_MESSAGE) for index in range(1, COLOR_MAX_MESSAGES + 1)},
    "templates": {
        "apply": "cor {cor_adicionada} aplicada.",
        "remove": "cor {cor_removida} removida.",
        "switch": "cor alterada: {cor_removida} → {cor_adicionada}.",
        "no_role": "Essa cor ainda não está configurada.",
        "hierarchy": "não consegui aplicar {cor_nome} por causa da hierarquia de cargos.",
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


def _default_slot_payload(slot_number: int) -> dict[str, Any]:
    default = next((item for item in DEFAULT_SLOTS if item["number"] == int(slot_number)), None)
    if default is None:
        return {
            "number": int(slot_number),
            "name": f"Cor {slot_number}",
            "text_hex": "#ffffff",
            "role_hex": "#ffffff",
            "role_id": 0,
            "role_name": f"Cor {slot_number}",
            "managed": False,
        }
    return {**default, "role_id": 0, "role_name": str(default["name"]), "managed": False}




_LEGACY_TEMPLATE_DEFAULTS: dict[str, tuple[str, ...]] = {
    "apply": ("{membro}, a cor {cor_adicionada} foi aplicada.",),
    "remove": ("{membro}, a cor {cor_removida} foi removida.",),
    "switch": ("{membro}, {cor_removida} foi removida e {cor_adicionada} foi aplicada.",),
    "hierarchy": ("Não consegui aplicar {cor_nome} por causa da hierarquia de cargos.",),
}


def _legacy_slot_payload(slot_number: int) -> dict[str, Any]:
    default = _default_slot_payload(slot_number)
    if int(slot_number) != 10:
        return default
    legacy = dict(default)
    legacy["name"] = "Preto escuro"
    legacy["text_hex"] = "#4a4a4a"
    legacy["role_hex"] = "#1f1f1f"
    legacy["role_name"] = "Preto escuro"
    return legacy


def _normalize_color_name(value: str | None) -> str:
    return str(value or "").strip().lower()


def _is_default_black_slot(slot_number: int, slot: dict[str, Any]) -> bool:
    if int(slot_number) != 10:
        return False
    default = _default_slot_payload(10)
    return (
        str(slot.get("name") or "") == str(default["name"])
        and _clean_hex(str(slot.get("text_hex") or ""), default["text_hex"]) == default["text_hex"]
    )


def _cleared_slot_payload(slot_number: int) -> dict[str, Any]:
    name = f"Cor {slot_number}"
    return {
        "number": int(slot_number),
        "name": name,
        "text_hex": "#ffffff",
        "role_hex": "#ffffff",
        "role_id": 0,
        "role_name": name,
        "managed": False,
    }


def _slot_payload_signature(slot: dict[str, Any], *, fallback_slot_number: int) -> dict[str, Any]:
    default = _default_slot_payload(fallback_slot_number)
    return {
        "name": str(slot.get("name") or default["name"]),
        "text_hex": _clean_hex(str(slot.get("text_hex") or ""), default["text_hex"]),
        "role_hex": _clean_hex(str(slot.get("role_hex") or ""), default["role_hex"]),
        "role_id": int(slot.get("role_id") or 0),
        "role_name": str(slot.get("role_name") or slot.get("name") or default["role_name"]),
        "managed": bool(slot.get("managed", False)),
    }


def _block_looks_like_default_source(slots: dict[str, Any], block_index: int, source_block_index: int) -> bool:
    target_start, target_end = _chunk_block(block_index)
    source_start, source_end = _chunk_block(source_block_index)
    if (target_end - target_start) != (source_end - source_start):
        return False
    for offset, slot_number in enumerate(range(target_start, target_end + 1)):
        current = dict(slots.get(str(slot_number), {}) or _default_slot_payload(slot_number))
        source_default = _default_slot_payload(source_start + offset)
        comparable_current = _slot_payload_signature(current, fallback_slot_number=slot_number)
        comparable_source = _slot_payload_signature(source_default, fallback_slot_number=source_start + offset)
        if comparable_current != comparable_source:
            return False
    return True


def _message_supports_slots(message_index: int) -> bool:
    return 1 <= int(message_index) <= COLOR_BLOCK_COUNT


def _message_label(message_index: int) -> str:
    if _message_supports_slots(message_index):
        return f"Mensagem {message_index} • faixa {_block_title(message_index)}"
    return f"Mensagem extra {message_index}"


def _compose_block_text(block_cfg: dict[str, Any]) -> str | None:
    lines: list[str] = []
    title = str(block_cfg.get("title") or "").strip()
    subtitle = str(block_cfg.get("subtitle") or "").strip()
    footer = str(block_cfg.get("footer") or "").strip()
    if title:
        lines.append(title)
    if subtitle:
        if lines:
            lines.append("")
        lines.append(subtitle)
    if footer:
        if lines:
            lines.append("")
        lines.append(footer)
    text = "\n".join(lines).strip()
    return text or None


class _ColorContentEditModal(discord.ui.Modal):
    def __init__(self, view: "_ColorUnifiedEditView", message_index: int):
        super().__init__(title=f"Editar conteúdo • {_message_label(message_index)}")
        self.view_ref = view
        self.message_index = int(message_index)
        cfg = self.view_ref.cog._get_message_block_config(self.view_ref.guild_id, self.message_index)
        self.title_input = discord.ui.TextInput(
            label="Título",
            default=str(cfg.get("title") or ""),
            required=False,
            style=discord.TextStyle.short,
            max_length=250,
        )
        self.subtitle_input = discord.ui.TextInput(
            label="Descrição",
            default=str(cfg.get("subtitle") or ""),
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=600,
        )
        self.footer_input = discord.ui.TextInput(
            label="Footer",
            default=str(cfg.get("footer") or ""),
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=300,
        )
        self.add_item(self.title_input)
        self.add_item(self.subtitle_input)
        self.add_item(self.footer_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.view_ref.cog._update_message_block_config(
            self.view_ref.guild_id,
            self.message_index,
            title=str(self.title_input.value or "").strip(),
            subtitle=str(self.subtitle_input.value or "").strip(),
            footer=str(self.footer_input.value or "").strip(),
        )
        await self.view_ref.cog._refresh_public_panel_messages(self.view_ref.guild_id, block_indices=[self.message_index])
        await self.view_ref.refresh_editor_message(interaction)


class _ColorTemplatesEditModal(discord.ui.Modal):
    def __init__(self, view: "_ColorUnifiedEditView"):
        super().__init__(title="Editar respostas do painel")
        self.view_ref = view
        cfg = self.view_ref.cog._get_templates(self.view_ref.guild_id)
        self.apply_input = discord.ui.TextInput(label="Quando aplica cor", default=str(cfg.get("apply") or ""), style=discord.TextStyle.paragraph, max_length=300)
        self.remove_input = discord.ui.TextInput(label="Quando remove cor", default=str(cfg.get("remove") or ""), style=discord.TextStyle.paragraph, max_length=300)
        self.switch_input = discord.ui.TextInput(label="Quando troca cor", default=str(cfg.get("switch") or ""), style=discord.TextStyle.paragraph, max_length=300)
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
        self.text_hex_input = discord.ui.TextInput(label="Hex do texto da imagem", default=str(slot.get("text_hex") or ""), max_length=7)
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


class _ColorRoleLinkModal(discord.ui.Modal):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int, slot_number: int):
        super().__init__(title=f"Vincular cargo • slot {slot_number}")
        self.view_ref = view
        self.block_index = int(block_index)
        self.slot_number = int(slot_number)
        slot = self.view_ref.cog._get_slot_config(self.view_ref.guild_id, self.slot_number)
        current_role_id = int(slot.get("role_id") or 0)
        default_value = f"<@&{current_role_id}>" if current_role_id else ""
        self.role_input = discord.ui.TextInput(
            label="Cargo existente (menção ou ID)",
            default=default_value,
            required=False,
            max_length=64,
            placeholder="Ex.: @Cargo ou 1234567890",
        )
        self.add_item(self.role_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Isso só funciona dentro de um servidor.", ephemeral=True)
            return
        raw = str(self.role_input.value or "").strip()
        if not raw:
            await interaction.response.send_message("Informe um cargo existente para vincular ao slot.", ephemeral=True)
            return
        match = re.search(r"(\d{6,})", raw)
        if not match:
            await interaction.response.send_message("Não consegui identificar o cargo informado.", ephemeral=True)
            return
        role = guild.get_role(int(match.group(1)))
        if role is None:
            await interaction.response.send_message("Não encontrei esse cargo neste servidor.", ephemeral=True)
            return
        slot = self.view_ref.cog._get_slot_config(self.view_ref.guild_id, self.slot_number)
        await self.view_ref.cog._update_slot_config(
            self.view_ref.guild_id,
            self.slot_number,
            role_id=int(role.id),
            role_name=str(role.name),
            managed=False,
            name=str(slot.get("name") or f"Cor {self.slot_number}"),
            text_hex=str(slot.get("text_hex") or "#ffffff"),
            role_hex=str(slot.get("role_hex") or "#ffffff"),
        )
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


class _ColorPublicPanelView(discord.ui.View):
    def __init__(self, cog: "ColorRolesCog", guild_id: int, block_index: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.block_index = int(block_index)
        if _message_supports_slots(block_index):
            start, end = _chunk_block(block_index)
            for slot_number in range(start, end + 1):
                self.add_item(_ColorPickerButton(self.cog, self.guild_id, slot_number))


class _ConfirmActionView(discord.ui.View):
    def __init__(self, owner_id: int, action: Callable[[], Awaitable[None]], success_text: str):
        super().__init__(timeout=90)
        self.owner_id = int(owner_id)
        self.action = action
        self.success_text = success_text

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.owner_id:
            await interaction.response.send_message("Só quem abriu o editor pode confirmar essa ação.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.action()
        await interaction.response.edit_message(content=self.success_text, view=None)
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Ação cancelada.", view=None)
        self.stop()


class _EditContentButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", message_index: int):
        super().__init__(label="Editar conteúdo", style=discord.ButtonStyle.secondary)
        self.view_ref = view
        self.message_index = int(message_index)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        await interaction.response.send_modal(_ColorContentEditModal(self.view_ref, self.message_index))


class _EditTemplatesButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView"):
        super().__init__(label="Editar respostas", style=discord.ButtonStyle.secondary)
        self.view_ref = view

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        await interaction.response.send_modal(_ColorTemplatesEditModal(self.view_ref))


class _AddMessageButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView"):
        super().__init__(label="Adicionar mensagem", style=discord.ButtonStyle.success)
        self.view_ref = view

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        if self.view_ref.cog._get_panel_count(self.view_ref.guild_id) >= COLOR_MAX_MESSAGES:
            await interaction.response.send_message("O painel já está no máximo de 5 mensagens.", ephemeral=True)
            return
        new_count = await self.view_ref.cog._add_extra_message_live(self.view_ref.guild_id)
        self.view_ref.active_block = int(new_count)
        await self.view_ref.refresh_editor_message(interaction)


class _RemoveMessageModal(discord.ui.Modal):
    def __init__(self, view: "_ColorUnifiedEditView"):
        super().__init__(title="Remover mensagem")
        self.view_ref = view
        self.number_input = discord.ui.TextInput(
            label="Número da mensagem",
            placeholder="Ex.: 4",
            required=True,
            max_length=2,
        )
        self.add_item(self.number_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.number_input.value or "").strip()
        if not raw.isdigit():
            await interaction.response.send_message("Informe um número válido de mensagem.", ephemeral=True)
            return
        message_index = int(raw)
        panel_count = self.view_ref.cog._get_panel_count(self.view_ref.guild_id)
        if not (COLOR_BLOCK_COUNT + 1 <= message_index <= panel_count):
            await interaction.response.send_message("Você só pode remover mensagens extras existentes.", ephemeral=True)
            return
        await self.view_ref.cog._remove_extra_message_live(self.view_ref.guild_id, message_index)
        self.view_ref.active_block = min(self.view_ref.active_block, self.view_ref.cog._get_panel_count(self.view_ref.guild_id))
        await self.view_ref.refresh_editor_message(interaction)


class _RemoveMessageButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView"):
        super().__init__(label="Remover mensagem", style=discord.ButtonStyle.secondary)
        self.view_ref = view

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        if self.view_ref.cog._get_panel_count(self.view_ref.guild_id) <= COLOR_BLOCK_COUNT:
            await interaction.response.send_message("Não há mensagem extra para remover.", ephemeral=True)
            return
        await interaction.response.send_modal(_RemoveMessageModal(self.view_ref))


class _MessageSelect(discord.ui.Select):
    def __init__(self, view: "_ColorUnifiedEditView"):
        self.view_ref = view
        options: list[discord.SelectOption] = []
        panel_count = view.cog._get_panel_count(view.guild_id)
        for message_index in range(1, panel_count + 1):
            description = f"Faixa {_block_title(message_index)}" if _message_supports_slots(message_index) else "Mensagem extra"
            options.append(
                discord.SelectOption(
                    label=_message_label(message_index)[:100],
                    value=str(message_index),
                    description=description[:100],
                    default=view.active_block == message_index,
                )
            )
        super().__init__(placeholder="Escolha a mensagem para editar", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        self.view_ref.active_block = int(self.values[0])
        await self.view_ref.refresh_editor_message(interaction)


class _MoveMessageButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", message_index: int, direction: int):
        label = "↑" if direction < 0 else "↓"
        super().__init__(label=label, style=discord.ButtonStyle.secondary, emoji="⬆️" if direction < 0 else "⬇️")
        self.view_ref = view
        self.message_index = int(message_index)
        self.direction = -1 if direction < 0 else 1
        self.disabled = not view.cog._can_move_message(view.guild_id, self.message_index, self.direction)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        if not self.view_ref.cog._can_move_message(self.view_ref.guild_id, self.message_index, self.direction):
            await interaction.response.send_message("Essa mensagem não pode ser movida nessa direção.", ephemeral=True)
            return
        await self.view_ref.cog._swap_messages(self.view_ref.guild_id, self.message_index, self.message_index + self.direction)
        self.view_ref.active_block = self.message_index + self.direction
        await self.view_ref.refresh_editor_message(interaction)
        await self.view_ref.cog._refresh_public_panel_messages(self.view_ref.guild_id)


class _ClearMessageButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", message_index: int):
        super().__init__(label="Limpar mensagem", style=discord.ButtonStyle.danger)
        self.view_ref = view
        self.message_index = int(message_index)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return

        async def action():
            await self.view_ref.cog._clear_message_text(self.view_ref.guild_id, self.message_index)
            await self.view_ref.cog._refresh_public_panel_messages(self.view_ref.guild_id, block_indices=[self.message_index])
            await self.view_ref.force_refresh_from_background()

        await interaction.response.send_message(
            "Confirmar limpeza do conteúdo desta mensagem?",
            ephemeral=True,
            view=_ConfirmActionView(self.view_ref.owner_id, action, "Conteúdo da mensagem limpo."),
        )


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
        super().__init__(label="Usar cargo automático", style=discord.ButtonStyle.secondary)
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


class _ChangeActiveMessageButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", direction: int):
        is_prev = direction < 0
        current = int(view.active_block)
        panel_count = view.cog._get_panel_count(view.guild_id)
        target = current - 1 if is_prev else current + 1
        super().__init__(label="Mensagem anterior" if is_prev else "Próxima mensagem", style=discord.ButtonStyle.secondary)
        self.view_ref = view
        self.target = target
        self.disabled = not (1 <= target <= panel_count)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        if self.disabled:
            await interaction.response.send_message("Não há outra mensagem nessa direção.", ephemeral=True)
            return
        self.view_ref.active_block = self.target
        await self.view_ref.refresh_editor_message(interaction)


class _ChangeActiveSlotButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int, direction: int):
        start, end = _chunk_block(block_index)
        current = int(view.selected_slots.get(block_index, start))
        target = current - 1 if direction < 0 else current + 1
        super().__init__(label="Slot anterior" if direction < 0 else "Próximo slot", style=discord.ButtonStyle.secondary)
        self.view_ref = view
        self.block_index = int(block_index)
        self.target = target
        self.disabled = not (start <= target <= end)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        if self.disabled:
            await interaction.response.send_message("Não há outro slot nessa direção.", ephemeral=True)
            return
        self.view_ref.selected_slots[self.block_index] = self.target
        await self.view_ref.refresh_editor_message(interaction)


class _LinkExistingRoleButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int):
        super().__init__(label="Vincular cargo", style=discord.ButtonStyle.secondary)
        self.view_ref = view
        self.block_index = int(block_index)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        selected_slot = self.view_ref.selected_slots.get(self.block_index, _chunk_block(self.block_index)[0])
        await interaction.response.send_modal(_ColorRoleLinkModal(self.view_ref, self.block_index, selected_slot))


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


class _SlotPresetButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int):
        super().__init__(label="Resetar preset da faixa", style=discord.ButtonStyle.danger)
        self.view_ref = view
        self.block_index = int(block_index)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return

        async def action():
            await self.view_ref.cog._reset_slot_block_to_preset(self.view_ref.guild_id, self.block_index)
            await self.view_ref.cog._refresh_public_panel_messages(self.view_ref.guild_id, block_indices=[self.block_index])
            await self.view_ref.force_refresh_from_background()

        await interaction.response.send_message(
            "Confirmar reset desta faixa para o preset? Isso também zera os vínculos de cargo dessa faixa.",
            ephemeral=True,
            view=_ConfirmActionView(self.view_ref.owner_id, action, "Faixa resetada para o preset."),
        )


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
        active = self.active_block
        if not _message_supports_slots(active):
            return []
        return [self.cog._make_block_image(self.guild_id, active, filename=f"colors-editor-{active}.png")]

    def editor_message_payload(self) -> dict[str, Any]:
        return {
            "view": self,
            "attachments": self._editor_preview_files(),
        }

    async def force_refresh_from_background(self):
        self._build_layout()
        if self.message is not None:
            await self.message.edit(**self.editor_message_payload())

    async def refresh_editor_message(self, interaction: discord.Interaction):
        self._build_layout()
        payload = self.editor_message_payload()
        if not interaction.response.is_done():
            await interaction.response.defer()
        target = interaction.message or self.message
        if target is not None:
            await target.edit(**payload)
            self.message = target

    def _header_lines(self) -> list[str]:
        return [
            "# 🎨 Editor do painel de cores",
            f"Mensagem ativa: {_message_label(self.active_block)}",
        ]

    def _block_lines(self, message_index: int) -> list[str]:
        cfg = self.cog._get_message_block_config(self.guild_id, message_index)
        title = str(cfg.get("title") or "").strip() or "(vazio)"
        subtitle = str(cfg.get("subtitle") or "").strip() or "(vazio)"
        footer = str(cfg.get("footer") or "").strip() or "(vazio)"
        lines = [
            f"**Título:** {title}",
            f"**Descrição:** {subtitle}",
            f"**Footer:** {footer}",
        ]
        if _message_supports_slots(message_index):
            lines.append(f"**Faixa:** {_block_title(message_index)}")
        else:
            lines.append("**Tipo:** mensagem extra")
        return lines

    def _slot_editor_lines(self, block_index: int) -> list[str]:
        selected_slot = self.selected_slots.get(block_index, _chunk_block(block_index)[0])
        slot = self.cog._get_slot_config(self.guild_id, selected_slot)
        role_id = int(slot.get("role_id") or 0)
        role_repr = f"<@&{role_id}>" if role_id else "Automático"
        managed_text = "sim" if bool(slot.get("managed", False) or role_id <= 0) else "não"
        return [
            f"## Faixa {_block_title(block_index)}",
            f"**Slot:** {selected_slot}",
            f"**Nome:** {slot.get('name')}",
            f"**Texto:** {slot.get('text_hex')}",
            f"**Cargo:** {role_repr}",
            f"**Cor do cargo:** {slot.get('role_hex')}",
            f"**Automático:** {managed_text}",
        ]

    def _build_layout(self):
        self.clear_items()
        panel_count = self.cog._get_panel_count(self.guild_id)
        top_controls: list[discord.ui.Item[Any]] = [
            _EditTemplatesButton(self),
            _AddMessageButton(self),
        ]
        if panel_count > COLOR_BLOCK_COUNT:
            top_controls.append(_RemoveMessageButton(self))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(self._header_lines())),
            discord.ui.ActionRow(_MessageSelect(self)),
            discord.ui.ActionRow(*top_controls),
            accent_color=discord.Colour.green(),
        ))

        active = self.active_block
        row_one = [_EditContentButton(self, active)]
        row_two: list[discord.ui.Item[Any]] = []
        if self.cog._message_text_changed_from_preset(self.guild_id, active):
            row_two.append(_ClearMessageButton(self, active))
        block_children: list[discord.ui.Item[Any]] = [discord.ui.TextDisplay("\n".join(self._block_lines(active)))]
        if _message_supports_slots(active):
            block_children.append(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        f"attachment://colors-editor-{active}.png",
                        description=f"Preview da faixa {_block_title(active)}",
                    )
                )
            )
        block_children.append(discord.ui.ActionRow(*row_one))
        if row_two:
            block_children.append(discord.ui.ActionRow(*row_two))
        self.add_item(discord.ui.Container(
            *block_children,
            accent_color=discord.Colour.green(),
        ))

        if _message_supports_slots(active):
            slot_rows: list[discord.ui.Item[Any]] = [
                discord.ui.TextDisplay("\n".join(self._slot_editor_lines(active))),
                discord.ui.ActionRow(
                    _ChangeActiveSlotButton(self, active, -1),
                    _ChangeActiveSlotButton(self, active, 1),
                    _LinkExistingRoleButton(self, active),
                    _AutoRoleButton(self, active),
                    _EditSlotButton(self, active),
                ),
            ]
            if self.cog._slot_block_changed_from_preset(self.guild_id, active):
                slot_rows.append(discord.ui.ActionRow(_SlotPresetButton(self, active)))
            self.add_item(discord.ui.Container(
                *slot_rows,
                accent_color=discord.Colour.blurple(),
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
        for gid in list(getattr(db, "guild_cache", {}).keys()):
            cfg = self._get_config(int(gid))
            message_ids = [int(mid) for mid in (cfg.get("message_ids") or []) if mid]
            for block_index, message_id in enumerate(message_ids, start=1):
                if not _message_supports_slots(block_index):
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

    def _sanitize_config(self, guild_id: int, config: dict[str, Any]) -> dict[str, Any]:
        base = _deepcopy_default_config()
        payload = deepcopy(config or {})
        base["channel_id"] = int(payload.get("channel_id") or 0)
        base["message_ids"] = [int(mid) for mid in (payload.get("message_ids") or []) if str(mid).isdigit()]
        raw_count = int(payload.get("panel_count") or COLOR_BLOCK_COUNT)
        base["panel_count"] = max(COLOR_BLOCK_COUNT, min(COLOR_MAX_MESSAGES, raw_count))
        raw_messages = payload.get("messages") or {}
        for key in [str(idx) for idx in range(1, COLOR_MAX_MESSAGES + 1)]:
            block = raw_messages.get(key) or {}
            base["messages"][key] = {
                "title": str(block.get("title") or ""),
                "subtitle": str(block.get("subtitle") or ""),
                "footer": str(block.get("footer") or ""),
            }
        raw_templates = payload.get("templates") or {}
        for key in list(base["templates"].keys()):
            raw_value = raw_templates.get(key)
            if raw_value is None:
                continue
            text = str(raw_value)
            if text in _LEGACY_TEMPLATE_DEFAULTS.get(key, ()):
                continue
            base["templates"][key] = text
        raw_slots = payload.get("slots") or {}
        for slot_number in range(1, 31):
            key = str(slot_number)
            default_slot = _default_slot_payload(slot_number)
            legacy_slot = _legacy_slot_payload(slot_number)
            merged = dict(default_slot)
            merged.update(dict(raw_slots.get(key) or {}))
            merged["number"] = int(slot_number)
            merged["role_id"] = int(merged.get("role_id") or 0)
            merged["managed"] = bool(merged.get("managed", False))
            merged["name"] = str(merged.get("name") or default_slot["name"])
            merged["role_name"] = str(merged.get("role_name") or merged["name"])
            merged["text_hex"] = _clean_hex(str(merged.get("text_hex") or ""), default_slot["text_hex"])
            merged["role_hex"] = _clean_hex(str(merged.get("role_hex") or ""), default_slot["role_hex"])
            comparable_current = {
                "name": str(merged.get("name") or ""),
                "text_hex": merged["text_hex"],
                "role_hex": merged["role_hex"],
                "role_id": int(merged.get("role_id") or 0),
                "role_name": str(merged.get("role_name") or ""),
                "managed": bool(merged.get("managed", False)),
            }
            comparable_legacy = {
                "name": str(legacy_slot["name"]),
                "text_hex": legacy_slot["text_hex"],
                "role_hex": legacy_slot["role_hex"],
                "role_id": 0,
                "role_name": str(legacy_slot["role_name"]),
                "managed": False,
            }
            if comparable_current == comparable_legacy:
                merged = dict(default_slot)
            elif slot_number == 10 and int(merged.get("role_id") or 0) >= 0:
                legacy_name = {"Preto escuro", "Preto"}
                if (
                    str(merged.get("name") or "") in legacy_name
                    and merged["text_hex"] in {"#4a4a4a", "#000000"}
                    and merged["role_hex"] in {"#1f1f1f", "#000000"}
                    and (bool(merged.get("managed", False)) or int(merged.get("role_id") or 0) == 0)
                ):
                    merged["name"] = default_slot["name"]
                    merged["text_hex"] = default_slot["text_hex"]
                    merged["role_hex"] = default_slot["role_hex"]
                    if str(merged.get("role_name") or "") in {"", "Preto escuro", "Preto"}:
                        merged["role_name"] = default_slot["role_name"]
            base["slots"][key] = merged
        if _block_looks_like_default_source(base["slots"], 1, 2) and _block_looks_like_default_source(base["slots"], 2, 1):
            repaired_slots = dict(base["slots"])
            first_defaults = [_default_slot_payload(number) for number in range(1, 11)]
            second_defaults = [_default_slot_payload(number) for number in range(11, 21)]
            for offset, slot_number in enumerate(range(1, 11)):
                repaired_slots[str(slot_number)] = dict(first_defaults[offset])
            for offset, slot_number in enumerate(range(11, 21)):
                repaired_slots[str(slot_number)] = dict(second_defaults[offset])
            base["slots"] = repaired_slots
        return base

    def _get_config(self, guild_id: int) -> dict[str, Any]:
        db = self.db
        if db is None or not hasattr(db, "get_color_roles_config"):
            return _deepcopy_default_config()
        return self._sanitize_config(guild_id, db.get_color_roles_config(int(guild_id)))

    async def _save_config(self, guild_id: int, config: dict[str, Any]):
        db = self.db
        if db is None or not hasattr(db, "set_color_roles_config"):
            return
        await db.set_color_roles_config(int(guild_id), self._sanitize_config(guild_id, config))

    def _panel_exists(self, guild_id: int) -> bool:
        cfg = self._get_config(guild_id)
        return bool(int(cfg.get("channel_id") or 0) and list(cfg.get("message_ids") or []))

    def _get_panel_count(self, guild_id: int) -> int:
        return int(self._get_config(guild_id).get("panel_count") or COLOR_BLOCK_COUNT)

    async def _set_panel_count(self, guild_id: int, count: int):
        cfg = self._get_config(guild_id)
        cfg["panel_count"] = max(COLOR_BLOCK_COUNT, min(COLOR_MAX_MESSAGES, int(count)))
        await self._save_config(guild_id, cfg)

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

    async def _reset_message_text_to_preset(self, guild_id: int, block_index: int):
        await self._update_message_block_config(guild_id, block_index, title="", subtitle="", footer="")

    async def _clear_message_text(self, guild_id: int, block_index: int):
        await self._update_message_block_config(guild_id, block_index, title="", subtitle="", footer="")

    def _message_text_changed_from_preset(self, guild_id: int, block_index: int) -> bool:
        block = self._get_message_block_config(guild_id, block_index)
        return any(str(block.get(field) or "").strip() for field in ("title", "subtitle", "footer"))

    def _get_slot_config(self, guild_id: int, slot_number: int) -> dict[str, Any]:
        cfg = self._get_config(guild_id)
        slot = dict((cfg.get("slots") or {}).get(str(slot_number), {}) or {})
        if not slot:
            slot = _default_slot_payload(slot_number)
        return slot

    async def _update_slot_config(self, guild_id: int, slot_number: int, **updates: Any):
        cfg = self._get_config(guild_id)
        slots = dict(cfg.get("slots") or {})
        slot = dict(slots.get(str(slot_number), {}) or _default_slot_payload(slot_number))
        slot.update(updates)
        slot["number"] = int(slot_number)
        if not slot.get("role_name"):
            slot["role_name"] = str(slot.get("name") or f"Cor {slot_number}")
        slots[str(slot_number)] = slot
        cfg["slots"] = slots
        await self._save_config(guild_id, cfg)

    def _slot_block_changed_from_preset(self, guild_id: int, block_index: int) -> bool:
        if not _message_supports_slots(block_index):
            return False
        start, end = _chunk_block(block_index)
        for slot_number in range(start, end + 1):
            current = self._get_slot_config(guild_id, slot_number)
            default = _default_slot_payload(slot_number)
            comparable_current = {
                "name": str(current.get("name") or ""),
                "text_hex": _clean_hex(str(current.get("text_hex") or ""), default["text_hex"]),
                "role_hex": _clean_hex(str(current.get("role_hex") or ""), default["role_hex"]),
                "role_id": int(current.get("role_id") or 0),
                "role_name": str(current.get("role_name") or ""),
                "managed": bool(current.get("managed", False)),
            }
            comparable_default = {
                "name": str(default["name"]),
                "text_hex": default["text_hex"],
                "role_hex": default["role_hex"],
                "role_id": 0,
                "role_name": str(default["role_name"]),
                "managed": False,
            }
            if comparable_current != comparable_default:
                return True
        return False

    async def _reset_slot_block_to_preset(self, guild_id: int, block_index: int):
        if not _message_supports_slots(block_index):
            return
        cfg = self._get_config(guild_id)
        slots = dict(cfg.get("slots") or {})
        start, end = _chunk_block(block_index)
        for slot_number in range(start, end + 1):
            slots[str(slot_number)] = dict(_default_slot_payload(slot_number))
        cfg["slots"] = slots
        await self._save_config(guild_id, cfg)

    async def _clear_slot_block(self, guild_id: int, block_index: int):
        if not _message_supports_slots(block_index):
            return
        cfg = self._get_config(guild_id)
        slots = dict(cfg.get("slots") or {})
        start, end = _chunk_block(block_index)
        for slot_number in range(start, end + 1):
            slots[str(slot_number)] = dict(_cleared_slot_payload(slot_number))
        cfg["slots"] = slots
        await self._save_config(guild_id, cfg)

    async def _add_extra_message(self, guild_id: int):
        count = self._get_panel_count(guild_id)
        if count >= COLOR_MAX_MESSAGES:
            return 0
        new_count = count + 1
        await self._set_panel_count(guild_id, new_count)
        return new_count

    async def _add_extra_message_live(self, guild_id: int) -> int:
        old_count = self._get_panel_count(guild_id)
        new_count = await self._add_extra_message(guild_id)
        if not new_count:
            return old_count
        cfg = self._get_config(guild_id)
        channel_id = int(cfg.get("channel_id") or 0)
        message_ids = [int(mid) for mid in (cfg.get("message_ids") or []) if mid]
        if channel_id and len(message_ids) == old_count:
            channel = self.bot.get_channel(channel_id)
            guild = self.bot.get_guild(guild_id)
            if channel is not None and guild is not None:
                kwargs = self._public_message_kwargs(guild_id, new_count)
                try:
                    message = await channel.send(**kwargs)
                    message_ids.append(int(message.id))
                    cfg["message_ids"] = message_ids
                    await self._save_config(guild_id, cfg)
                except Exception:
                    pass
        return self._get_panel_count(guild_id)

    async def _remove_extra_message(self, guild_id: int, message_index: int):
        count = self._get_panel_count(guild_id)
        if count <= COLOR_BLOCK_COUNT or not (COLOR_BLOCK_COUNT + 1 <= int(message_index) <= count):
            return False
        cfg = self._get_config(guild_id)
        messages = dict(cfg.get("messages") or {})
        for idx in range(int(message_index), count):
            messages[str(idx)] = dict(messages.get(str(idx + 1), _DEFAULT_MESSAGE))
        messages[str(count)] = dict(_DEFAULT_MESSAGE)
        cfg["messages"] = messages
        cfg["panel_count"] = count - 1
        await self._save_config(guild_id, cfg)
        return True

    async def _remove_extra_message_live(self, guild_id: int, message_index: int) -> bool:
        count = self._get_panel_count(guild_id)
        if count <= COLOR_BLOCK_COUNT or not (COLOR_BLOCK_COUNT + 1 <= int(message_index) <= count):
            return False
        cfg_before = self._get_config(guild_id)
        channel_id = int(cfg_before.get("channel_id") or 0)
        message_ids = [int(mid) for mid in (cfg_before.get("message_ids") or []) if mid]
        removed_message_id = message_ids[int(message_index) - 1] if len(message_ids) >= int(message_index) else 0
        ok = await self._remove_extra_message(guild_id, message_index)
        if not ok:
            return False
        cfg = self._get_config(guild_id)
        if removed_message_id and channel_id:
            channel = self.bot.get_channel(channel_id)
            if channel is not None:
                try:
                    target = await channel.fetch_message(removed_message_id)
                    await target.delete()
                except Exception:
                    pass
        if len(message_ids) >= int(message_index):
            del message_ids[int(message_index) - 1]
            cfg["message_ids"] = message_ids[: self._get_panel_count(guild_id)]
            await self._save_config(guild_id, cfg)
        await self._refresh_public_panel_messages(guild_id)
        return True

    def _can_move_message(self, guild_id: int, block_index: int, direction: int) -> bool:
        count = self._get_panel_count(guild_id)
        target = int(block_index) + int(direction)
        if _message_supports_slots(block_index):
            return 1 <= target <= min(COLOR_BLOCK_COUNT, count)
        if block_index > COLOR_BLOCK_COUNT:
            return COLOR_BLOCK_COUNT + 1 <= target <= count
        return False

    async def _swap_messages(self, guild_id: int, left: int, right: int):
        cfg = self._get_config(guild_id)
        messages = dict(cfg.get("messages") or {})
        messages[str(left)], messages[str(right)] = dict(messages.get(str(right), _DEFAULT_MESSAGE)), dict(messages.get(str(left), _DEFAULT_MESSAGE))
        cfg["messages"] = messages
        if _message_supports_slots(left) and _message_supports_slots(right):
            slots = dict(cfg.get("slots") or {})
            left_start, left_end = _chunk_block(left)
            right_start, right_end = _chunk_block(right)
            left_payloads = [dict(slots.get(str(number), _default_slot_payload(number))) for number in range(left_start, left_end + 1)]
            right_payloads = [dict(slots.get(str(number), _default_slot_payload(number))) for number in range(right_start, right_end + 1)]
            for offset, slot_number in enumerate(range(left_start, left_end + 1)):
                payload = dict(right_payloads[offset])
                payload["number"] = slot_number
                slots[str(slot_number)] = payload
            for offset, slot_number in enumerate(range(right_start, right_end + 1)):
                payload = dict(left_payloads[offset])
                payload["number"] = slot_number
                slots[str(slot_number)] = payload
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
        me = guild.me or (guild.get_member(self.bot.user.id) if self.bot.user else None)
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
            "cor_nome": _normalize_color_name(str(slot.get("name") or "")),
            "cor_adicionada": _normalize_color_name(str(added_name or slot.get("name") or "")),
            "cor_removida": _normalize_color_name(str(removed_name or "")),
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
        me = guild.me or (guild.get_member(self.bot.user.id) if self.bot.user else None)
        if me is None or target_role >= me.top_role:
            text = self._render_template(str((cfg.get("templates") or {}).get("hierarchy") or ""), member=member, slot=slot)
            await interaction.response.send_message(text or "não consegui aplicar essa cor por causa da hierarquia de cargos.", ephemeral=True)
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
                removed_name = _normalize_color_name(str(slot.get("name") or ""))
                await interaction.response.send_message(text or f"cor {removed_name} removida.", ephemeral=True)
                return
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Troca de cor pelo painel")
            await member.add_roles(target_role, reason="Cor escolhida pelo painel")
        except discord.Forbidden:
            text = self._render_template(str((cfg.get("templates") or {}).get("hierarchy") or ""), member=member, slot=slot)
            await interaction.response.send_message(text or "não consegui aplicar essa cor por causa da hierarquia de cargos.", ephemeral=True)
            return
        if current_slot:
            template = str((cfg.get("templates") or {}).get("switch") or "")
            text = self._render_template(template, member=member, slot=slot, added_name=str(slot.get("name") or ""), removed_name=str(current_slot.get("name") or ""))
        else:
            template = str((cfg.get("templates") or {}).get("apply") or "")
            text = self._render_template(template, member=member, slot=slot, added_name=str(slot.get("name") or ""))
        applied_name = _normalize_color_name(str(slot.get("name") or ""))
        await interaction.response.send_message(text or f"cor {applied_name} aplicada.", ephemeral=True)

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
        if not _message_supports_slots(block_index):
            raise ValueError("Somente as três primeiras mensagens possuem imagem de faixa.")
        start, end = _chunk_block(block_index)
        cfg = self._get_config(guild_id)
        slots = cfg.get("slots") or {}
        width, height = 900, 330
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        font = _font(34, bold=True)
        y_positions = [20, 85, 150, 215, 280]
        x_left, x_right = 18, 465
        shadow = (0, 0, 0, 180)
        for idx, slot_number in enumerate(range(start, end + 1)):
            slot = dict(slots.get(str(slot_number), {}) or {})
            name = str(slot.get("name") or f"Cor {slot_number}").strip()
            label = f"{slot_number}. {name}" if name else f"{slot_number}."
            hex_color = _clean_hex(str(slot.get("text_hex") or "#ffffff"), "#ffffff")
            x = x_left if idx % 2 == 0 else x_right
            y = y_positions[idx // 2]
            if _is_default_black_slot(slot_number, slot):
                try:
                    draw.text((x, y), label, font=font, fill=hex_color, stroke_width=3, stroke_fill="#8f8f8f")
                except TypeError:
                    draw.text((x + 2, y + 2), label, font=font, fill="#8f8f8f")
                    draw.text((x, y), label, font=font, fill=hex_color)
                continue
            draw.text((x + 2, y + 2), label, font=font, fill=shadow)
            draw.text((x, y), label, font=font, fill=hex_color)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return discord.File(buffer, filename=filename or f"colors-{block_index}.png")

    def _public_message_kwargs(self, guild_id: int, block_index: int) -> dict[str, Any]:
        block_cfg = self._get_message_block_config(guild_id, block_index)
        content = _compose_block_text(block_cfg)
        payload: dict[str, Any] = {"content": content or ("\u200b" if not _message_supports_slots(block_index) else None)}
        if _message_supports_slots(block_index):
            filename = f"colors-{block_index}.png"
            payload["file"] = self._make_block_image(guild_id, block_index, filename=filename)
            payload["view"] = _ColorPublicPanelView(self, guild_id, block_index)
        else:
            payload["view"] = None
        return payload

    async def _post_public_panel(self, channel: discord.abc.Messageable, guild: discord.Guild) -> list[int]:
        message_ids: list[int] = []
        panel_count = self._get_panel_count(guild.id)
        for block_index in range(1, panel_count + 1):
            kwargs = self._public_message_kwargs(guild.id, block_index)
            message = await channel.send(**kwargs)
            message_ids.append(int(message.id))
            if _message_supports_slots(block_index):
                key = (guild.id, block_index, int(message.id))
                try:
                    self.bot.add_view(kwargs["view"], message_id=int(message.id))
                except Exception:
                    pass
                self._public_views_registered.add(key)
        return message_ids

    async def _rebuild_public_panel(self, guild_id: int):
        cfg = self._get_config(guild_id)
        channel_id = int(cfg.get("channel_id") or 0)
        if not channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        guild = self.bot.get_guild(guild_id)
        if channel is None or guild is None:
            return
        await self._delete_existing_panel_messages(guild_id)
        message_ids = await self._post_public_panel(channel, guild)
        cfg["message_ids"] = message_ids
        await self._save_config(guild_id, cfg)

    async def _refresh_public_panel_messages(self, guild_id: int, *, block_indices: list[int] | None = None):
        cfg = self._get_config(guild_id)
        channel_id = int(cfg.get("channel_id") or 0)
        message_ids = [int(mid) for mid in (cfg.get("message_ids") or []) if mid]
        panel_count = self._get_panel_count(guild_id)
        if not channel_id or not message_ids:
            return
        if len(message_ids) != panel_count:
            await self._rebuild_public_panel(guild_id)
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return
        targets = block_indices or list(range(1, panel_count + 1))
        for block_index in targets:
            if block_index < 1 or block_index > len(message_ids):
                continue
            message_id = message_ids[block_index - 1]
            try:
                message = await channel.fetch_message(message_id)
            except Exception:
                continue
            try:
                if _message_supports_slots(block_index):
                    filename = f"colors-{block_index}.png"
                    file = self._make_block_image(guild_id, block_index, filename=filename)
                    view = _ColorPublicPanelView(self, guild_id, block_index)
                    await message.edit(content=_compose_block_text(self._get_message_block_config(guild_id, block_index)), embed=None, embeds=[], attachments=[file], view=view)
                    key = (guild_id, block_index, message_id)
                    try:
                        self.bot.add_view(view, message_id=message_id)
                    except Exception:
                        pass
                    self._public_views_registered.add(key)
                else:
                    await message.edit(content=_compose_block_text(self._get_message_block_config(guild_id, block_index)) or "\u200b", attachments=[], view=None)
            except Exception:
                pass

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
            await ctx.send("Só administradores podem convocar o painel de cores.")
            return
        remaining = await self._consume_color_command_cooldown(ctx.guild.id)
        if remaining > 0:
            await ctx.send(f"Espere {remaining:.0f}s para convocar o painel de cores de novo.")
            return
        await self._delete_existing_panel_messages(ctx.guild.id)
        message_ids = await self._post_public_panel(ctx.channel, ctx.guild)
        cfg = self._get_config(ctx.guild.id)
        cfg["channel_id"] = int(ctx.channel.id)
        cfg["message_ids"] = message_ids
        await self._save_config(ctx.guild.id, cfg)
        await ctx.send(f"Painel de cores publicado com {self._get_panel_count(ctx.guild.id)} mensagem(ns).")

    @commands.command(name="coloredit")
    @commands.guild_only()
    async def coloredit_command(self, ctx: commands.Context):
        if not self._is_admin(getattr(ctx, "author", None)):
            await ctx.send("Só administradores podem abrir o editor de cores.")
            return
        key = (ctx.guild.id, ctx.author.id)
        old_id = self._active_edit_messages.get(key)
        if old_id:
            try:
                old_msg = await ctx.channel.fetch_message(old_id)
                await old_msg.delete()
            except Exception:
                pass
        try:
            view = _ColorUnifiedEditView(self, guild_id=ctx.guild.id, owner_id=ctx.author.id)
            payload = view.editor_message_payload()
            msg = await ctx.channel.send(view=view, files=payload["attachments"])
        except Exception as e:
            await ctx.send(f"não consegui abrir o editor de cores: {e}")
            return
        view.message = msg
        self._active_edit_messages[key] = int(msg.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(ColorRolesCog(bot))
