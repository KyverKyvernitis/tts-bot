"""Configuração e confirmações do chatbot único."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Awaitable, Callable, Optional

import discord

from . import constants as C
from .config import GuildChatbotConfig

log = logging.getLogger(__name__)
AuthorizationCheck = Callable[[discord.Interaction], Awaitable[bool]]
ConfigCallback = Callable[[discord.Interaction, GuildChatbotConfig], Awaitable[None]]
_CHANNEL_TYPES = [
    discord.ChannelType.text, discord.ChannelType.news,
    discord.ChannelType.voice, discord.ChannelType.stage_voice,
    discord.ChannelType.forum,
]


async def _notice(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def _label(text: str, component: discord.ui.Item, description: str):
    return discord.ui.Label(text=text, component=component, description=description)


def _status_group(custom_id: str, enabled: bool) -> discord.ui.RadioGroup:
    group = discord.ui.RadioGroup(custom_id=custom_id, required=True)
    group.add_option(label="Ativado", value="on", default=enabled)
    group.add_option(label="Desativado", value="off", default=not enabled)
    return group


def _channel_select(custom_id: str, channels: tuple[int, ...]) -> discord.ui.ChannelSelect:
    return discord.ui.ChannelSelect(
        custom_id=custom_id, channel_types=_CHANNEL_TYPES,
        min_values=0, max_values=10, required=False,
        default_values=[
            discord.SelectDefaultValue(id=channel_id, type=discord.SelectDefaultValueType.channel)
            for channel_id in channels[:10]
        ],
    )


class ChatbotConfigModal(discord.ui.Modal, title="Configurar chatbot"):
    def __init__(self, *, requester_id: int, current_config: GuildChatbotConfig,
                 on_submit_config: ConfigCallback, check_authorized: AuthorizationCheck):
        super().__init__(timeout=600.0)
        self._requester_id = int(requester_id)
        self._config = current_config
        self._on_submit_config = on_submit_config
        self._check_authorized = check_authorized
        self.status_group = _status_group("chatbot_status", current_config.enabled)
        self.channel_select = _channel_select("chatbot_channels", current_config.channel_ids)
        self.spontaneous_group = _status_group("chatbot_spontaneous", current_config.spontaneous_enabled)
        self.spontaneous_channel_select = _channel_select(
            "chatbot_spontaneous_channels", current_config.spontaneous_channel_ids,
        )
        self.chance_input = discord.ui.TextInput(
            custom_id="chatbot_spontaneous_chance",
            default=str(current_config.spontaneous_chance_percent),
            min_length=1, max_length=2, required=True,
        )
        self.add_item(_label("Status do chatbot", self.status_group, "Responde quando mencionado ou por reply."))
        self.add_item(_label("Canais permitidos", self.channel_select, "Vazio permite todos os canais. Escolha até 10."))
        self.add_item(_label("Respostas espontâneas", self.spontaneous_group, "Permite participar sem menção nos canais escolhidos."))
        self.add_item(_label("Canais espontâneos", self.spontaneous_channel_select, "Escolha entre os canais permitidos. Obrigatório para participar."))
        self.add_item(_label("Chance espontânea (%)", self.chance_input, "Número inteiro de 1 a 20. Padrão: 5%."))

    async def on_submit(self, interaction: discord.Interaction):
        if int(getattr(interaction.user, "id", 0) or 0) != self._requester_id:
            await _notice(interaction, "Esta configuração não é para você.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if not await self._check_authorized(interaction):
                return
            config = replace(self._config,
                enabled=self.status_group.value == "on",
                channel_ids=tuple(int(channel.id) for channel in self.channel_select.values),
                spontaneous_enabled=self.spontaneous_group.value == "on",
                spontaneous_channel_ids=tuple(int(channel.id) for channel in self.spontaneous_channel_select.values),
                spontaneous_chance_percent=int(self.chance_input.value),
            )
            await self._on_submit_config(interaction, config)
        except ValueError as exc:
            await _notice(interaction, f"Não consegui salvar: {exc}")
        except Exception:
            log.exception("chatbot: falha ao salvar configuração")
            await _notice(interaction, "Falha ao salvar a configuração. Tente novamente.")


class _OpenModalView(discord.ui.View):
    def __init__(self, *, requester_id: int, check_authorized: AuthorizationCheck, label: str):
        super().__init__(timeout=300.0)
        self._requester_id = int(requester_id)
        self._check_authorized = check_authorized
        button = discord.ui.Button(style=discord.ButtonStyle.primary, label=label)
        button.callback = self._open
        self.add_item(button)

    def _make_modal(self) -> discord.ui.Modal:
        raise NotImplementedError

    async def _open(self, interaction: discord.Interaction):
        await self._open_with(interaction, self._make_modal)

    async def _open_with(self, interaction: discord.Interaction, factory):
        if int(getattr(interaction.user, "id", 0) or 0) != self._requester_id:
            await _notice(interaction, "Este botão não é para você.")
            return
        try:
            authorized = await asyncio.wait_for(self._check_authorized(interaction), timeout=2.0)
            if authorized:
                await interaction.response.send_modal(factory())
        except asyncio.TimeoutError:
            await _notice(interaction, "A configuração demorou para carregar. Use o comando novamente.")
        except Exception:
            log.exception("chatbot: falha ao abrir modal")
            await _notice(interaction, "Não consegui abrir a configuração. Tente novamente.")


class EditConfigView(_OpenModalView):
    def __init__(self, *, requester_id: int, current_config: GuildChatbotConfig,
                 on_submit_config: ConfigCallback, check_authorized: AuthorizationCheck,
                 on_submit_actions: Optional[ConfigCallback] = None,
                 on_submit_audio: Optional[ConfigCallback] = None,
                 on_submit_allowlists: Optional[ConfigCallback] = None,
                 on_submit_provider: Optional[ConfigCallback] = None):
        super().__init__(requester_id=requester_id, check_authorized=check_authorized, label="Editar configuração")
        self._config = current_config
        self._on_submit_config = on_submit_config
        self._on_submit_actions = on_submit_actions or on_submit_config
        self._on_submit_audio = on_submit_audio or on_submit_config
        self._on_submit_allowlists = on_submit_allowlists
        self._on_submit_provider = on_submit_provider
        button = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Configurar ações")
        button.callback = self._open_actions
        self.add_item(button)
        audio_button = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Configurar áudios")
        audio_button.callback = self._open_audio
        self.add_item(audio_button)
        if on_submit_allowlists is not None:
            allowlists_button = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Autorizar cargos e canais")
            allowlists_button.callback = self._open_allowlists
            self.add_item(allowlists_button)
        if on_submit_provider is not None:
            provider_button = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Provedor de conversa")
            provider_button.callback = self._open_provider
            self.add_item(provider_button)

    async def _open_allowlists(self, interaction: discord.Interaction):
        await self._open_with(interaction, lambda: ActionAllowlistModal(
            requester_id=self._requester_id, current_config=self._config,
            on_submit_config=self._on_submit_allowlists, check_authorized=self._check_authorized,
        ))

    async def _open_provider(self, interaction: discord.Interaction):
        await self._open_with(interaction, lambda: ProviderConfigModal(
            requester_id=self._requester_id, current_config=self._config,
            on_submit_config=self._on_submit_provider, check_authorized=self._check_authorized,
        ))

    async def _open_audio(self, interaction: discord.Interaction):
        await self._open_with(interaction, lambda: AudioReplyConfigModal(
            requester_id=self._requester_id, current_config=self._config,
            on_submit_config=self._on_submit_audio, check_authorized=self._check_authorized,
        ))

    async def _open_actions(self, interaction: discord.Interaction):
        await self._open_with(interaction, lambda: ActionConfigModal(
            requester_id=self._requester_id, current_config=self._config,
            on_submit_config=self._on_submit_actions, check_authorized=self._check_authorized,
        ))

    def _make_modal(self) -> ChatbotConfigModal:
        return ChatbotConfigModal(
            requester_id=self._requester_id, current_config=self._config,
            on_submit_config=self._on_submit_config, check_authorized=self._check_authorized,
        )


class ActionConfigModal(discord.ui.Modal, title="Ações do chatbot"):
    def __init__(self, *, requester_id: int, current_config: GuildChatbotConfig,
                 on_submit_config: ConfigCallback, check_authorized: AuthorizationCheck):
        super().__init__(timeout=600.0)
        self._requester_id = int(requester_id)
        self._config = current_config
        self._on_submit_config = on_submit_config
        self._check_authorized = check_authorized
        self.actions_group = _status_group("chatbot_actions", current_config.actions_enabled)
        self.audio_group = _status_group("chatbot_audio_actions", current_config.audio_actions_enabled)
        self.voice_group = _status_group("chatbot_voice_actions", current_config.voice_actions_enabled)
        self.moderation_group = _status_group("chatbot_moderation_actions", current_config.moderation_actions_enabled)
        self.staff_roles = discord.ui.RoleSelect(
            custom_id="chatbot_action_staff_roles", min_values=0, max_values=10, required=False,
            default_values=[discord.SelectDefaultValue(id=role_id, type=discord.SelectDefaultValueType.role)
                            for role_id in current_config.action_staff_role_ids[:10]],
        )
        self.add_item(_label("Ações da IA", self.actions_group, "Permite usar os recursos abaixo durante a conversa."))
        self.add_item(_label("Áudios", self.audio_group, "Envia o arquivo no chat e pode tocar o mesmo áudio na call, sem aprovação."))
        self.add_item(_label("Calls", self.voice_group, "Entrar, sair, mudar de call e falar são automáticos, sem aprovação da staff."))
        self.add_item(_label("Moderação", self.moderation_group, "Banir, expulsar e silenciar exigem staff, suas permissões e hierarquia."))
        self.add_item(_label("Cargos de staff", self.staff_roles, "Opcional. Ações administrativas exigem a permissão Discord correspondente."))

    async def on_submit(self, interaction: discord.Interaction):
        if int(getattr(interaction.user, "id", 0) or 0) != self._requester_id:
            await _notice(interaction, "Esta configuração não é para você.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if not await self._check_authorized(interaction):
                return
            config = replace(
                self._config,
                actions_enabled=self.actions_group.value == "on",
                audio_actions_enabled=self.audio_group.value == "on",
                voice_actions_enabled=self.voice_group.value == "on",
                moderation_actions_enabled=self.moderation_group.value == "on",
                action_staff_role_ids=tuple(int(role.id) for role in self.staff_roles.values),
            )
            await self._on_submit_config(interaction, config)
        except Exception:
            log.exception("chatbot: falha ao salvar ações")
            await _notice(interaction, "Não consegui salvar as ações. Tente novamente.")


class ActionAllowlistModal(discord.ui.Modal, title="Autorizar cargos e canais"):
    def __init__(self, *, requester_id: int, current_config: GuildChatbotConfig,
                 on_submit_config: ConfigCallback, check_authorized: AuthorizationCheck):
        super().__init__(timeout=600.0)
        self._requester_id = int(requester_id)
        self._config = current_config
        self._on_submit_config = on_submit_config
        self._check_authorized = check_authorized
        self.allowed_roles = discord.ui.RoleSelect(
            custom_id="chatbot_action_allowed_roles", min_values=0, max_values=10, required=False,
            default_values=[discord.SelectDefaultValue(id=role_id, type=discord.SelectDefaultValueType.role)
                            for role_id in current_config.action_allowed_role_ids[:10]],
        )
        self.allowed_channels = _channel_select("chatbot_action_allowed_channels", current_config.action_allowed_channel_ids)
        self.add_item(_label("Cargos que posso adicionar ou remover", self.allowed_roles,
                             "Somente cargos sem privilégios. Vazio desativa alterações de cargos. A staff aprova cada ação."))
        self.add_item(_label("Canais que posso alterar ou limpar", self.allowed_channels,
                             "Vazio desativa alterações e limpeza. Esta lista não substitui as permissões da staff."))

    async def on_submit(self, interaction: discord.Interaction):
        if int(getattr(interaction.user, "id", 0) or 0) != self._requester_id:
            await _notice(interaction, "Esta configuração não é para você.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if not await self._check_authorized(interaction):
                return
            config = replace(self._config,
                action_allowed_role_ids=tuple(int(role.id) for role in self.allowed_roles.values),
                action_allowed_channel_ids=tuple(int(channel.id) for channel in self.allowed_channels.values),
            )
            await self._on_submit_config(interaction, config)
        except ValueError as exc:
            await _notice(interaction, f"Não consegui salvar: {exc}")
        except Exception:
            log.exception("chatbot: falha ao salvar cargos e canais autorizados")
            await _notice(interaction, "Não consegui salvar os cargos e canais. Tente novamente.")


class ProviderConfigModal(discord.ui.Modal, title="Provedor de conversa"):
    def __init__(self, *, requester_id: int, current_config: GuildChatbotConfig,
                 on_submit_config: ConfigCallback, check_authorized: AuthorizationCheck):
        super().__init__(timeout=600.0)
        self._requester_id = int(requester_id)
        self._config = current_config
        self._on_submit_config = on_submit_config
        self._check_authorized = check_authorized
        self.provider_group = discord.ui.RadioGroup(custom_id="chatbot_text_provider", required=True)
        preferred = current_config.text_provider_order[0] if current_config.text_provider_order else "groq"
        self.provider_group.add_option(label="Groq primeiro (padrão)", value="groq", default=preferred != "gemini")
        self.provider_group.add_option(label="Gemini primeiro", value="gemini", default=preferred == "gemini")
        self.add_item(_label("Primeiro provedor da conversa", self.provider_group,
                             "O outro provedor serve de fallback. Configure aqui, sem editar arquivos."))

    async def on_submit(self, interaction: discord.Interaction):
        if int(getattr(interaction.user, "id", 0) or 0) != self._requester_id:
            await _notice(interaction, "Esta configuração não é para você.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if not await self._check_authorized(interaction):
                return
            preferred = self.provider_group.value
            if preferred not in {"groq", "gemini"}:
                raise ValueError("Escolha Groq ou Gemini.")
            config = replace(self._config,
                text_provider_order=("groq", "gemini") if preferred == "groq" else ("gemini", "groq"),
            )
            await self._on_submit_config(interaction, config)
        except ValueError as exc:
            await _notice(interaction, f"Não consegui salvar: {exc}")
        except Exception:
            log.exception("chatbot: falha ao salvar provedor da conversa")
            await _notice(interaction, "Não consegui salvar o provedor. Tente novamente.")


class AudioReplyConfigModal(discord.ui.Modal, title="Áudios da conversa"):
    def __init__(self, *, requester_id: int, current_config: GuildChatbotConfig,
                 on_submit_config: ConfigCallback, check_authorized: AuthorizationCheck):
        super().__init__(timeout=600.0)
        self._requester_id = int(requester_id)
        self._config = current_config
        self._on_submit_config = on_submit_config
        self._check_authorized = check_authorized
        self.chance_input = discord.ui.TextInput(
            custom_id="chatbot_audio_reply_chance", default=str(current_config.audio_reply_chance_percent),
            min_length=1, max_length=3, required=True,
        )
        self.cooldown_input = discord.ui.TextInput(
            custom_id="chatbot_audio_reply_cooldown", default=str(current_config.audio_reply_cooldown_seconds),
            min_length=1, max_length=4, required=True,
        )
        self.add_item(_label("Respostas aleatórias em áudio (%)", self.chance_input,
                             "De 0 a 100. Padrão: 20%. Zero desativa o sorteio; pedidos de áudio continuam."))
        self.add_item(_label("Intervalo entre áudios aleatórios (s)", self.cooldown_input,
                             "Por canal, de 0 a 3600 segundos. Padrão: 60. Pedidos explícitos não esperam."))

    async def on_submit(self, interaction: discord.Interaction):
        if int(getattr(interaction.user, "id", 0) or 0) != self._requester_id:
            await _notice(interaction, "Esta configuração não é para você.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if not await self._check_authorized(interaction):
                return
            config = replace(self._config,
                audio_reply_chance_percent=int(self.chance_input.value),
                audio_reply_cooldown_seconds=int(self.cooldown_input.value),
            )
            await self._on_submit_config(interaction, config)
        except ValueError as exc:
            await _notice(interaction, f"Não consegui salvar: {exc}")
        except Exception:
            log.exception("chatbot: falha ao salvar áudios")
            await _notice(interaction, "Não consegui salvar os áudios. Tente novamente.")


class ConfirmView(discord.ui.View):
    def __init__(self, *, requester_id: int, prompt: str, confirm_label: str = "Confirmar"):
        super().__init__(timeout=60.0)
        self._requester_id = int(requester_id)
        self.prompt = prompt
        self.result: Optional[bool] = None
        self.confirmation_interaction: Optional[discord.Interaction] = None
        confirm_btn = discord.ui.Button(style=discord.ButtonStyle.danger, label=confirm_label)
        cancel_btn = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Cancelar")
        confirm_btn.callback = self._on_confirm
        cancel_btn.callback = self._on_cancel
        self.add_item(confirm_btn)
        self.add_item(cancel_btn)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self._requester_id:
            await _notice(interaction, "Este botão não é para você.")
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, result: bool):
        if not await self._guard(interaction):
            return
        self.result = result
        self.confirmation_interaction = interaction
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        self.stop()

    async def _on_confirm(self, interaction: discord.Interaction):
        await self._finish(interaction, True)

    async def _on_cancel(self, interaction: discord.Interaction):
        await self._finish(interaction, False)

    async def on_timeout(self):
        self.result = False


class MasterEditModal(discord.ui.Modal, title="Editar instruções globais do bot"):
    def __init__(self, *, master_store, current_content: str, requester_id: int,
                 check_authorized: AuthorizationCheck):
        super().__init__(timeout=900.0)
        self._store = master_store
        self._requester_id = int(requester_id)
        self._check_authorized = check_authorized
        self.prompt_input = discord.ui.TextInput(
            label="Instruções globais do bot", default=current_content,
            placeholder="Como o próprio bot deve conversar em todos os servidores",
            style=discord.TextStyle.paragraph, required=True,
            max_length=C.MAX_MASTER_PROMPT_LENGTH,
        )
        self.add_item(self.prompt_input)

    async def on_submit(self, interaction: discord.Interaction):
        if int(getattr(interaction.user, "id", 0) or 0) != self._requester_id:
            await _notice(interaction, "Este formulário não é para você.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if not await self._check_authorized(interaction):
                return
            config = await self._store.update_prompt(str(self.prompt_input.value), updated_by=interaction.user.id)
            await _notice(interaction, f"Instruções globais atualizadas ({len(config.prompt)} caracteres).")
        except Exception:
            log.exception("chatbot: falha ao salvar instruções globais")
            await _notice(interaction, "Falha ao salvar as instruções. Tente novamente.")


class EditMasterView(_OpenModalView):
    def __init__(self, *, master_store, current_content: str, requester_id: int,
                 check_authorized: AuthorizationCheck):
        super().__init__(requester_id=requester_id, check_authorized=check_authorized, label="Editar instruções")
        self._store = master_store
        self._content = current_content

    def _make_modal(self) -> MasterEditModal:
        return MasterEditModal(
            master_store=self._store, current_content=self._content,
            requester_id=self._requester_id, check_authorized=self._check_authorized,
        )
