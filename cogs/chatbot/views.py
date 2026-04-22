"""Modais e views do chatbot.

Dois modais principais:
- `ProfileCreateModal`: cria profile novo, todos os campos começam vazios.
- `ProfileEditModal`: edita profile existente, campos pré-preenchidos.

Por limitação do Discord (5 TextInputs por modal, 45 chars no label), os
campos são: Nome, URL do Avatar, Prompt do Sistema, Temperatura, Tamanho
da Memória. O aviso sobre prompt injection aparece como placeholder e no
próprio textarea pra que a staff veja antes de submeter.

Também exporta uma View simples pra confirmação de ações destrutivas
(apagar profile, reset de memória server-wide).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

import discord

from . import constants as C
from .profiles import ChatbotProfile, ProfileStore

log = logging.getLogger(__name__)


# O aviso sobre system prompt é longo e não cabe no label (45 chars). Vai
# no PLACEHOLDER que o Discord mostra dentro do textarea quando vazio.
_SYSTEM_PROMPT_PLACEHOLDER = (
    "⚠️ Atenção: qualquer texto aqui é executado pelo modelo. "
    "Pessoas no server vão conversar com este profile — cuidado com "
    "instruções maliciosas. Ex: 'Você é a Lua, calma e poética. "
    "Fala em frases curtas. Adora astronomia.'"
)

_AVATAR_URL_PLACEHOLDER = (
    "https://... (link direto de imagem — PNG/JPG/WEBP)"
)

_NAME_PLACEHOLDER = (
    "Nome que aparece no chat (ex: Lua, Bot Assistente, Toguro)"
)


def _parse_float_safe(text: str, default: float, lo: float, hi: float) -> float:
    """Parse defensivo de float. Aceita vírgula decimal (pt-BR)."""
    if not text:
        return default
    try:
        val = float(text.strip().replace(",", "."))
    except (ValueError, TypeError):
        return default
    return max(lo, min(hi, val))


def _parse_int_safe(text: str, default: int, lo: int, hi: int) -> int:
    if not text:
        return default
    try:
        val = int(text.strip())
    except (ValueError, TypeError):
        return default
    return max(lo, min(hi, val))


class ProfileCreateModal(discord.ui.Modal, title="Criar profile do chatbot"):
    """Modal de criação. Ao submeter, salva no Mongo e avisa na interação."""

    name_input = discord.ui.TextInput(
        label="Nome do profile",
        placeholder=_NAME_PLACEHOLDER,
        required=True,
        min_length=1,
        max_length=C.MAX_NAME_LENGTH,
    )
    avatar_input = discord.ui.TextInput(
        label="URL do avatar (imagem)",
        placeholder=_AVATAR_URL_PLACEHOLDER,
        required=False,
        max_length=C.MAX_AVATAR_URL_LENGTH,
    )
    system_prompt_input = discord.ui.TextInput(
        label="Personalidade / System prompt",
        placeholder=_SYSTEM_PROMPT_PLACEHOLDER,
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=C.MAX_SYSTEM_EXTRA_LENGTH,
    )
    temperature_input = discord.ui.TextInput(
        label="Temperatura (0.0-1.5, padrão 0.8)",
        placeholder="0.8",
        required=False,
        max_length=4,
        default=str(C.DEFAULT_TEMPERATURE),
    )
    history_size_input = discord.ui.TextInput(
        label="Memória: mensagens (1-40, padrão 20)",
        placeholder="20",
        required=False,
        max_length=3,
        default=str(C.DEFAULT_HISTORY_SIZE),
    )

    def __init__(
        self, *,
        store: ProfileStore,
        guild_limit: int = C.MAX_PROFILES_PER_GUILD,
        on_complete: Optional[Callable] = None,
    ):
        super().__init__(timeout=600.0)
        self._store = store
        self._guild_limit = int(guild_limit)
        self._on_complete = on_complete

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando só funciona dentro de um servidor.", ephemeral=True
            )
            return

        # Checa limite aqui (não antes do modal) pra não estourar o timeout
        # de 3s da interaction em servidores com Mongo mais lento.
        try:
            count = await self._store.count_profiles(guild.id)
        except Exception:
            log.exception("chatbot: falha ao contar profiles")
            await interaction.response.send_message(
                "❌ Erro ao acessar banco. Tenta de novo em alguns segundos.",
                ephemeral=True,
            )
            return
        if count >= self._guild_limit:
            await interaction.response.send_message(
                f"❌ Limite de {self._guild_limit} profiles atingido. "
                f"Apague algum com `/chatbot apagar` antes de criar outro.",
                ephemeral=True,
            )
            return

        temp = _parse_float_safe(
            str(self.temperature_input.value), C.DEFAULT_TEMPERATURE,
            C.MIN_TEMPERATURE, C.MAX_TEMPERATURE,
        )
        hist = _parse_int_safe(
            str(self.history_size_input.value), C.DEFAULT_HISTORY_SIZE,
            1, C.MAX_HISTORY_SIZE,
        )

        try:
            profile = await self._store.create_profile(
                guild_id=guild.id,
                name=str(self.name_input.value),
                created_by=interaction.user.id,
                system_prompt=str(self.system_prompt_input.value),
                avatar_url=str(self.avatar_input.value or ""),
                temperature=temp,
                history_size=hist,
            )
        except Exception:
            log.exception("chatbot: falha ao criar profile")
            await interaction.response.send_message(
                "❌ Falha ao criar profile. Tenta de novo.", ephemeral=True
            )
            return

        msg = (
            f"✅ Profile **{discord.utils.escape_markdown(profile.name)}** criado!\n"
            f"ID: `{profile.profile_id}`\n"
            f"Use `/chatbot ativar` para selecioná-lo como profile ativo do servidor.\n"
            f"-# Profiles existentes podem ser editados, mas o limite é "
            f"{C.MAX_PROFILES_PER_GUILD} por servidor."
        )
        await interaction.response.send_message(msg, ephemeral=True)

        if self._on_complete is not None:
            try:
                await self._on_complete(profile)
            except Exception:
                log.exception("chatbot: on_complete do modal falhou")


class ProfileEditModal(discord.ui.Modal, title="Editar profile do chatbot"):
    """Modal de edição. Campos pré-preenchidos com o profile atual."""

    # TextInputs são declarados no __init__ (valores dinâmicos).
    # Não declarar no nível da classe como o Create — os defaults dependem
    # do profile que está sendo editado.

    def __init__(self, *, store: ProfileStore, profile: ChatbotProfile):
        super().__init__(timeout=600.0)
        self._store = store
        self._profile = profile

        self.name_input = discord.ui.TextInput(
            label="Nome do profile",
            default=profile.name,
            required=True,
            min_length=1,
            max_length=C.MAX_NAME_LENGTH,
        )
        self.avatar_input = discord.ui.TextInput(
            label="URL do avatar (imagem)",
            default=profile.avatar_url or "",
            placeholder=_AVATAR_URL_PLACEHOLDER,
            required=False,
            max_length=C.MAX_AVATAR_URL_LENGTH,
        )
        self.system_prompt_input = discord.ui.TextInput(
            label="Personalidade / System prompt",
            default=profile.system_prompt or "",
            placeholder=_SYSTEM_PROMPT_PLACEHOLDER,
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=C.MAX_SYSTEM_EXTRA_LENGTH,
        )
        self.temperature_input = discord.ui.TextInput(
            label="Temperatura (0.0-1.5)",
            default=f"{profile.temperature:.2f}",
            required=False,
            max_length=4,
        )
        self.history_size_input = discord.ui.TextInput(
            label="Memória: mensagens (1-40)",
            default=str(profile.history_size),
            required=False,
            max_length=3,
        )

        # Adiciona na ordem de exibição
        self.add_item(self.name_input)
        self.add_item(self.avatar_input)
        self.add_item(self.system_prompt_input)
        self.add_item(self.temperature_input)
        self.add_item(self.history_size_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando só funciona dentro de um servidor.", ephemeral=True
            )
            return

        temp = _parse_float_safe(
            str(self.temperature_input.value), self._profile.temperature,
            C.MIN_TEMPERATURE, C.MAX_TEMPERATURE,
        )
        hist = _parse_int_safe(
            str(self.history_size_input.value), self._profile.history_size,
            1, C.MAX_HISTORY_SIZE,
        )

        try:
            updated = await self._store.update_profile(
                guild.id, self._profile.profile_id,
                name=str(self.name_input.value),
                avatar_url=str(self.avatar_input.value or ""),
                system_prompt=str(self.system_prompt_input.value),
                temperature=temp,
                history_size=hist,
            )
        except Exception:
            log.exception("chatbot: falha ao editar profile")
            await interaction.response.send_message(
                "❌ Falha ao editar. Tenta de novo.", ephemeral=True
            )
            return

        if updated is None:
            await interaction.response.send_message(
                "❌ Profile não encontrado (foi apagado?).", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ Profile **{discord.utils.escape_markdown(updated.name)}** atualizado.",
            ephemeral=True,
        )


class ConfirmView(discord.ui.View):
    """View simples com botões Confirmar / Cancelar, retorna resultado via callback.

    Uso:
        view = ConfirmView(requester_id=interaction.user.id, prompt="Apagar X?")
        await interaction.response.send_message(view.prompt, view=view, ephemeral=True)
        await view.wait()
        if view.result is True:
            # usuário confirmou
    """

    def __init__(self, *, requester_id: int, prompt: str, confirm_label: str = "Confirmar"):
        super().__init__(timeout=60.0)
        self._requester_id = int(requester_id)
        self.prompt = prompt
        self.result: Optional[bool] = None

        confirm_btn = discord.ui.Button(
            style=discord.ButtonStyle.danger, label=confirm_label,
        )
        cancel_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary, label="Cancelar",
        )
        confirm_btn.callback = self._on_confirm
        cancel_btn.callback = self._on_cancel
        self.add_item(confirm_btn)
        self.add_item(cancel_btn)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user is None or interaction.user.id != self._requester_id:
            await interaction.response.send_message(
                "Este botão não é pra você.", ephemeral=True
            )
            return False
        return True

    async def _on_confirm(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.result = True
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        self.stop()

    async def _on_cancel(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.result = False
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        self.stop()

    async def on_timeout(self):
        if self.result is None:
            self.result = False
