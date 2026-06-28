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
from typing import Any, Awaitable, Callable, Optional

import discord

from . import constants as C
from .profiles import ChatbotProfile, ProfileStore
from .persona import PersonaModalConfig, PersonaOptions

log = logging.getLogger(__name__)


# Placeholders do modal — Discord limita a 100 chars cada.
# O aviso completo sobre prompt injection fica no HARD_SYSTEM_PREAMBLE (vai
# pro modelo de IA), aqui só cabe um exemplo curto pra guiar a staff.
_SYSTEM_PROMPT_PLACEHOLDER = (
    "Ex: Você é a Lua, calma e poética. Fala em frases curtas."
)

_AVATAR_URL_PLACEHOLDER = (
    "https://... (link direto PNG/JPG/WEBP)"
)

_NAME_PLACEHOLDER = (
    "Ex: Lua, Assistente, Toguro"
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


def _modal_label(text: str, component: discord.ui.Item, description: str | None = None):
    """Cria Label para componentes modernos em modal.

    discord.py 2.7 usa keyword-only (`text=`, `component=`). O helper deixa a
    construção em um ponto só e facilita fallback se a assinatura mudar.
    """
    return discord.ui.Label(
        text=text[:45],
        description=(description[:100] if description else None),
        component=component,
    )


class PersonaConfigModal(discord.ui.Modal, title="Criar persona"):
    """Modal moderno de `/chatbot persona`.

    Usa componentes suportados em modais pelo discord.py 2.7+: UserSelect,
    ChannelSelect, RadioGroup e CheckboxGroup, cada um dentro de Label.
    """

    def __init__(
        self,
        *,
        requester_id: int,
        on_submit_config: Callable[[discord.Interaction, PersonaModalConfig], Awaitable[None]],
    ):
        super().__init__(timeout=600.0)
        self._requester_id = int(requester_id)
        self._on_submit_config = on_submit_config

        self.user_select = discord.ui.UserSelect(
            custom_id="chatbot_persona_user",
            placeholder="Escolha o usuário base",
            min_values=1,
            max_values=1,
            required=True,
        )
        self.channel_select = discord.ui.ChannelSelect(
            custom_id="chatbot_persona_channel",
            placeholder="Canal de onde as mensagens serão lidas",
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
            ],
            min_values=1,
            max_values=1,
            required=True,
        )
        self.action_group = discord.ui.RadioGroup(
            custom_id="chatbot_persona_action",
            required=True,
        )
        self.action_group.add_option(
            label="Criar ou atualizar",
            value="upsert",
            description="Gera uma persona nova ou substitui a existente.",
            default=True,
        )
        self.action_group.add_option(
            label="Remover persona",
            value="remove",
            description="Apaga a persona vinculada a esse usuário.",
        )

        self.depth_group = discord.ui.RadioGroup(
            custom_id="chatbot_persona_depth",
            required=True,
        )
        self.depth_group.add_option(
            label="Leve — até 30 mensagens",
            value="30",
            description="Mais rápido, menos fiel ao estilo.",
        )
        self.depth_group.add_option(
            label="Normal — até 80 mensagens",
            value="80",
            description="Melhor base para copiar o jeito de escrever.",
            default=True,
        )

        self.options_group = discord.ui.CheckboxGroup(
            custom_id="chatbot_persona_options",
            required=False,
            min_values=0,
            max_values=5,
        )
        self.options_group.add_option(
            label="Ignorar links",
            value="ignore_links",
            description="Não usa mensagens que são só link.",
            default=True,
        )
        self.options_group.add_option(
            label="Ignorar comandos",
            value="ignore_commands",
            description="Remove mensagens com prefixo de comando.",
            default=True,
        )
        self.options_group.add_option(
            label="Ignorar mensagens curtas",
            value="ignore_short",
            description="Evita amostras sem estilo suficiente.",
            default=True,
        )
        self.options_group.add_option(
            label="Não copiar frases exatas",
            value="avoid_exact_copy",
            description="Extrai estilo, não frases literais.",
            default=True,
        )
        self.options_group.add_option(
            label="Ativar depois de criar",
            value="activate",
            description="Deixa essa persona como profile ativo.",
            default=True,
        )

        self.add_item(_modal_label(
            "Usuário base",
            self.user_select,
            "Nick e avatar serão resolvidos desse usuário.",
        ))
        self.add_item(_modal_label(
            "Canal de leitura",
            self.channel_select,
            "Lê até 80 mensagens públicas desse canal.",
        ))
        self.add_item(_modal_label(
            "Ação",
            self.action_group,
            "Criar/atualizar ou remover a persona.",
        ))
        self.add_item(_modal_label(
            "Profundidade",
            self.depth_group,
            "Quantidade máxima de mensagens usadas.",
        ))
        self.add_item(_modal_label(
            "Filtros",
            self.options_group,
            "Limpeza aplicada antes de mandar à IA.",
        ))

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user is None or int(interaction.user.id) != self._requester_id:
            await interaction.response.send_message(
                "Este modal não é pra você.", ephemeral=True
            )
            return

        try:
            selected_user = self.user_select.values[0]
            selected_channel = self.channel_select.values[0]
            target_user_id = int(getattr(selected_user, "id"))
            channel_id = int(getattr(selected_channel, "id"))
            sample_limit = int(self.depth_group.value or C.PERSONA_MAX_MESSAGES)
            selected_options = set(self.options_group.values or [])
            config = PersonaModalConfig(
                action=str(self.action_group.value or "upsert"),
                target_user_id=target_user_id,
                channel_id=channel_id,
                sample_limit=sample_limit,
                options=PersonaOptions(
                    ignore_links="ignore_links" in selected_options,
                    ignore_commands="ignore_commands" in selected_options,
                    ignore_short="ignore_short" in selected_options,
                    avoid_exact_copy=("avoid_exact_copy" in selected_options),
                    activate_after_create=("activate" in selected_options),
                ),
            )
        except Exception:
            log.exception("chatbot: falha ao ler modal de persona")
            await interaction.response.send_message(
                "❌ Não consegui ler a configuração do modal.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._on_submit_config(interaction, config)


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
        label="Temperatura (0.0-1.5)",
        # Placeholder explicativo. Vai aparecer quando o campo estiver vazio
        # — por isso NÃO setamos `default`, senão o placeholder nunca aparece.
        # Limite: 100 chars.
        placeholder="Baixo=preciso e repetitivo. Alto=criativo. Padrão 0.8",
        required=False,
        max_length=4,
    )
    history_size_input = discord.ui.TextInput(
        label="Memória: mensagens (1-40)",
        placeholder="Quantas mensagens anteriores o bot lembra. Padrão 20",
        required=False,
        max_length=3,
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
                f"Apague algum com `/chatbot profile apagar` antes de criar outro.",
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
            f"`ID: {profile.profile_id}`\n"
            f"Use `/chatbot profile ativar` para ativá-lo como chatbot do servidor.\n\n"
            f"⚠️ **Sobre o system prompt**: o texto que você escreveu é "
            f"interpretado pela IA como instruções. Qualquer pessoa do server "
            f"vai conversar com esse profile. Se escrever algo malicioso, "
            f"a IA vai seguir. Cuidado ao editar.\n"
            f"-# Limite: {C.MAX_PROFILES_PER_GUILD} profiles por servidor."
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
            label="Temperatura (0=preciso, 1.5=criativo)",
            default=f"{profile.temperature:.2f}",
            placeholder="Padrão 0.8",
            required=False,
            max_length=4,
        )
        self.history_size_input = discord.ui.TextInput(
            label="Memória: mensagens (1-40)",
            default=str(profile.history_size),
            placeholder="Quantas mensagens o bot lembra. Padrão 20",
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

        # Aviso sobre system prompt só se realmente mudou (evita poluir
        # resposta quando a staff só trocou temp/memória).
        prompt_changed = (
            (updated.system_prompt or "").strip() != (self._profile.system_prompt or "").strip()
        )
        msg = f"✅ Profile **{discord.utils.escape_markdown(updated.name)}** atualizado."
        if prompt_changed:
            msg += (
                "\n\n⚠️ **Sobre o system prompt**: o texto que você escreveu é "
                "interpretado pela IA como instruções. Qualquer pessoa do "
                "server vai conversar com esse profile. Se escrever algo "
                "malicioso, a IA vai seguir."
            )
        await interaction.response.send_message(msg, ephemeral=True)


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


class MasterEditModal(discord.ui.Modal, title="Editar prompt mestre global"):
    """Modal de edição do master prompt (prompt supremo global).

    Uma só TextInput de paragraph mode — caber até 4000 chars. Ao submit,
    persiste via MasterStore e confirma.
    """

    # Single field. Declarado no __init__ pra ter default dinâmico.

    def __init__(
        self,
        *,
        master_store,  # .master.MasterStore — não tipado pra evitar import circular
        current_content: str = "",
    ):
        super().__init__(timeout=900.0)
        self._store = master_store

        self.prompt_input = discord.ui.TextInput(
            label="Prompt mestre (regras globais)",
            default=current_content,
            placeholder="Regras que valem pra TODOS os profiles em TODOS os servers",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=C.MAX_MASTER_PROMPT_LENGTH,
        )
        self.add_item(self.prompt_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            cfg = await self._store.update_prompt(
                str(self.prompt_input.value),
                updated_by=interaction.user.id,
            )
        except Exception:
            log.exception("chatbot: falha ao salvar master prompt")
            await interaction.response.send_message(
                "❌ Falha ao salvar. Tenta de novo.", ephemeral=True
            )
            return

        char_count = len(cfg.prompt)
        await interaction.response.send_message(
            f"✅ Prompt mestre atualizado ({char_count} chars). "
            f"Vai valer imediatamente na próxima mensagem processada "
            f"(cache invalidado).",
            ephemeral=True,
        )
