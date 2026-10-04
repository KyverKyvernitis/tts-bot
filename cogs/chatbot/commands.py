"""Comandos para configurar e usar o próprio bot como chatbot."""
from __future__ import annotations

import functools
import io
import logging
import math
import os
import re
from typing import Optional

import discord
from discord import app_commands

from . import constants as C
from .config import GuildChatbotConfig
from .media import channel_is_nsfw
from .views import ConfirmView, EditConfigView, EditMasterView

log = logging.getLogger(__name__)


async def _staff_check(interaction: discord.Interaction) -> bool:
    guild, member = interaction.guild, interaction.user
    if guild is None or not isinstance(member, discord.Member):
        return False
    member = guild.get_member(member.id) or member
    return bool(member.id == guild.owner_id or member.guild_permissions.manage_guild or member.guild_permissions.administrator)


async def _operator_check(interaction: discord.Interaction) -> bool:
    try:
        if await interaction.client.is_owner(interaction.user):
            return True
    except Exception:
        pass
    operators: set[int] = set()
    for raw in os.environ.get("CHATBOT_OPERATOR_IDS", "").split(","):
        try:
            operators.add(int(raw.strip()))
        except ValueError:
            continue
    return int(getattr(interaction.user, "id", 0) or 0) in operators


async def _send(interaction: discord.Interaction, content: str, **kwargs):
    kwargs.setdefault("ephemeral", True)
    kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
    if interaction.response.is_done():
        return await interaction.followup.send(content, **kwargs)
    return await interaction.response.send_message(content, **kwargs)


def _safe_slash(func):
    @functools.wraps(func)
    async def wrapper(self, interaction: discord.Interaction, *args, **kwargs):
        try:
            return await func(self, interaction, *args, **kwargs)
        except Exception:
            log.exception("chatbot: exceção em %s", func.__name__)
            try:
                await _send(interaction, "Erro interno no comando. Tente novamente em alguns segundos.")
            except Exception:
                pass
    return wrapper


class ChatbotCommandsMixin:
    chatbot = app_commands.Group(
        name="chatbot", description="Configuração do chatbot do servidor",
        default_permissions=discord.Permissions(manage_guild=True),
    )
    chatbot_admin = app_commands.Group(
        name="chatbotadmin", description="Administração global do chatbot",
    )

    def _require_ready(self, interaction: discord.Interaction) -> bool:
        return getattr(self, "_config", None) is not None and getattr(self, "_memory", None) is not None

    async def _config_staff_check(self, interaction: discord.Interaction) -> bool:
        if not await _staff_check(interaction):
            await _send(interaction, "Só a staff com Gerenciar Servidor pode configurar o chatbot.")
            return False
        if not self._require_ready(interaction):
            await _send(interaction, "O chatbot não está pronto. Tente novamente em alguns segundos.")
            return False
        return True

    @staticmethod
    def _format_config(config: GuildChatbotConfig, provider_status: str = "") -> str:
        channels = ", ".join(f"<#{channel}>" for channel in config.channel_ids) or "todos os canais"
        spontaneous_channels = ", ".join(f"<#{channel}>" for channel in config.spontaneous_channel_ids) or "nenhum"
        allowed_roles = ", ".join(f"<@&{role}>" for role in config.action_allowed_role_ids[:5]) or "nenhum (alterações de cargos desativadas)"
        allowed_channels = ", ".join(f"<#{channel}>" for channel in config.action_allowed_channel_ids[:5]) or "nenhum (alterações e limpeza desativadas)"
        if len(config.action_allowed_role_ids) > 5:
            allowed_roles += f" e mais {len(config.action_allowed_role_ids) - 5}"
        if len(config.action_allowed_channel_ids) > 5:
            allowed_channels += f" e mais {len(config.action_allowed_channel_ids) - 5}"
        # A preferência salva continua escolhendo apenas quem vem primeiro entre
        # Groq/Gemini. Reservas independentes aparecem depois quando habilitadas.
        provider_order = " → ".join(
            {"groq": "Groq", "gemini": "Gemini"}[provider]
            for provider in config.text_provider_order if provider in {"groq", "gemini"}
        )
        if C.MISTRAL_ENABLED:
            provider_order += " → Mistral (texto)"
        provider_order += " → Cloudflare (texto)"
        return (
            f"**Chatbot:** {'ativado' if config.enabled else 'desativado'}\n"
            f"**Canais permitidos:** {channels}\n"
            f"**Espontâneo:** {'ativado' if config.spontaneous_enabled else 'desativado'} "
            f"({config.spontaneous_chance_percent}%)\n"
            f"**Canais espontâneos:** {spontaneous_channels}\n\n"
            f"**Ações:** {'ativadas' if config.actions_enabled else 'desativadas'} "
            f"(áudio: {'sim' if config.audio_actions_enabled else 'não'}, "
            f"calls: {'sim' if config.voice_actions_enabled else 'não'}, "
            f"moderação: {'sim' if config.moderation_actions_enabled else 'não'})\n"
            f"**Cargos autorizados para alteração:** {allowed_roles}\n"
            f"**Canais autorizados para alteração/limpeza:** {allowed_channels}\n"
            f"**Provedores de conversa:** {provider_order}\n"
            f"{provider_status}\n"
            f"**Respostas em áudio:** {config.audio_reply_chance_percent}% "
            f"(intervalo por canal: {config.audio_reply_cooldown_seconds}s)\n"
            "Pedidos de áudio são diretos; na call atual, o mesmo áudio também é reproduzido.\n\n"
            "Para conversar, mencione o bot ou responda a uma mensagem do chatbot."
        )

    def _provider_status(self) -> str:
        """Estado de configuração e circuitos, sem consultar credenciais ou APIs."""
        router = getattr(self, "_router", None)
        diagnostics = getattr(router, "diagnostics", None)
        if not callable(diagnostics):
            return "**Disponibilidade:** diagnóstico ainda indisponível."
        try:
            data = diagnostics()
        except Exception:
            return "**Disponibilidade:** diagnóstico ainda indisponível."
        if not isinstance(data, dict):
            return "**Disponibilidade:** diagnóstico ainda indisponível."
        configured = data.get("configured", {})
        circuits = data.get("circuits", {})
        if not isinstance(configured, dict) or not isinstance(circuits, dict):
            return "**Disponibilidade:** diagnóstico ainda indisponível."

        lines = []
        effective_models = data.get("models", {})
        if not isinstance(effective_models, dict):
            effective_models = {}

        def wait_text(seconds):
            seconds = max(1, min(86400, math.ceil(seconds)))
            if seconds < 60:
                return f"{seconds}s"
            if seconds < 3600:
                return f"{math.ceil(seconds / 60)}min"
            hours, minutes = divmod(math.ceil(seconds / 60), 60)
            return f"{hours}h{minutes:02d}min"

        for provider, title, models in (("groq", "Groq", C.GROQ_MODELS),
                                         ("gemini", "Gemini", C.GEMINI_MODELS),
                                         ("mistral", "Mistral · Small 4", C.MISTRAL_MODELS),
                                         ("cloudflare", "Cloudflare · Qwen", C.CLOUDFLARE_MODELS)):
            if configured.get(provider) is not True:
                if provider == "cloudflare":
                    setup = data.get("cloudflare_setup", {})
                    if not isinstance(setup, dict):
                        setup = {}
                    if setup.get("enabled") is False:
                        lines.append(f"**{title}:** desativada; configure uma conta Workers Free e CHATBOT_CLOUDFLARE_ENABLED=true.")
                        continue
                    missing = []
                    if setup.get("account_id_configured") is not True:
                        missing.append("ID da conta")
                    if setup.get("api_token_configured") is not True:
                        missing.append("token da API")
                    lines.append(f"**{title}:** falta {' e '.join(missing) or 'configuração'}; reserva de texto.")
                elif provider == "mistral":
                    setup = data.get("mistral_setup", {})
                    if isinstance(setup, dict) and setup.get("enabled") is False:
                        lines.append(f"**{title}:** desativada; use CHATBOT_MISTRAL_ENABLED=true para ativar a reserva.")
                    else:
                        lines.append(f"**{title}:** falta MISTRAL_API_KEY; reserva de texto.")
                else:
                    lines.append(f"**{title}:** chave não configurada.")
                continue
            candidates = effective_models.get(provider)
            if isinstance(candidates, (tuple, list)):
                safe_models = tuple(dict.fromkeys(name for name in candidates
                    if isinstance(name, str) and (
                        name in C.CLOUDFLARE_MODELS if provider == "cloudflare"
                        else re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/-]{0,99}", name)
                    )))[:32]
                if safe_models:
                    models = safe_models
            states = [circuits.get(f"{provider}/{model}") for model in models]
            waiting = [state for state in states if isinstance(state, dict) and state.get("available") is False]
            if len(waiting) != len(models) or not models:
                lines.append(f"**{title}:** configurado; {'há modelos em espera' if waiting else 'sem bloqueio local'}.")
            else:
                retries = []
                for state in waiting:
                    if state.get("last_kind") in {"model", "auth"} or state.get("last_status") in {401, 403, 404}:
                        continue
                    delay = state.get("cooldown_seconds")
                    if isinstance(delay, (float, int)) and not isinstance(delay, bool) and math.isfinite(delay):
                        retries.append(delay)
                delay_text = f"; próxima tentativa em cerca de {wait_text(min(retries))}" if retries else ""
                if all(state.get("last_kind") == "auth" or state.get("last_status") == 401 for state in waiting):
                    status = "credencial recusada"
                elif all(state.get("last_kind") == "model" or state.get("last_status") == 404 for state in waiting):
                    status = "modelos indisponíveis na API"
                else:
                    status = "temporariamente indisponível"
                lines.append(f"**{title}:** {status}{delay_text}.")
            reasons = {"rate_limit": "limite de uso", "model": "modelo indisponível", "auth": "credencial recusada",
                       "invalid_response": "chamada de ferramenta ou resposta inválida", "timeout": "tempo esgotado",
                       "network": "falha de conexão", "server": "falha do serviço"}
            shown = 0
            for model, state in zip(models, states):
                if not isinstance(state, dict) or state.get("available") is not False:
                    continue
                if shown >= 2:
                    break
                reason = reasons.get(state.get("last_kind"), "aguardando nova tentativa")
                delay = state.get("cooldown_seconds")
                suffix = ""
                if state.get("last_kind") not in {"model", "auth"} and isinstance(delay, (float, int)) and not isinstance(delay, bool) and math.isfinite(delay):
                    suffix = f"; espera de cerca de {wait_text(delay)}"
                # Apenas IDs de modelos declarados/localmente validados são exibidos.
                if isinstance(model, str) and (
                    model in C.CLOUDFLARE_MODELS if provider == "cloudflare"
                    else re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/-]{0,99}", model)
                ):
                    lines.append(f"{model}: {reason}{suffix}.")
                    shown += 1

        cloudflare_setup = data.get("cloudflare_setup")
        if isinstance(cloudflare_setup, dict):
            budget = cloudflare_setup.get("budget")
            if isinstance(budget, dict):
                limit = budget.get("limit_neurons")
                measured = budget.get("measured_neurons")
                uncertain = budget.get("uncertain_reserved_neurons")
                if all(isinstance(value, (int, float)) and not isinstance(value, bool)
                       and math.isfinite(value) and value >= 0 for value in (limit, measured, uncertain)) and limit > 0:
                    used = min(float(limit), float(measured) + float(uncertain))
                    suffix = f" + {float(uncertain):.3f} reservados/incertos" if uncertain else ""
                    lines.append(
                        f"**Reserva local Cloudflare:** {float(measured):.3f}{suffix} / {float(limit):.0f} neurons "
                        f"({used / float(limit) * 100:.1f}% contabilizado neste processo)."
                    )

        # Somente números reportados: não mostramos prompt, respostas, IDs ou
        # credenciais, e cache/raciocínio são parcelas já contidas nos totais.
        turn = getattr(self, "_last_turn_usage", None)
        usage = turn.get("usage") if isinstance(turn, dict) else None
        if isinstance(usage, dict):
            def token_number(value):
                return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000_000

            values = [usage.get(key) for key in ("input_tokens", "output_tokens")]
            if all(token_number(value) for value in values):
                parts = [f"entrada {values[0]}", f"saída {values[1]}"]
                cached, thinking = usage.get("cached_tokens"), usage.get("reasoning_tokens")
                if token_number(cached):
                    parts.append(f"cache {cached} (incluído na entrada)")
                if token_number(thinking):
                    parts.append(f"raciocínio {thinking} (incluído na saída)")
                requests = turn.get("request_count")
                if token_number(requests):
                    parts.append(f"{requests} tentativas")
                if turn.get("usage_complete") is not True:
                    parts.append("contagem parcial")
                wasted = turn.get("wasted_usage")
                if isinstance(wasted, dict) and token_number(wasted.get("total_tokens")) and wasted["total_tokens"]:
                    ratio = turn.get("wasted_token_ratio")
                    ratio_text = f" ({float(ratio) * 100:.1f}%)" if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) else ""
                    parts.append(f"descartados {wasted['total_tokens']}{ratio_text}")
                lines.append("**Tokens do último turno medido:** " + ", ".join(parts) + ".")
                resources = turn.get("resources")
                if isinstance(resources, dict) and isinstance(resources.get("neurons"), (int, float)):
                    lines.append(f"**Cloudflare no último turno:** {resources['neurons']:.3f} neurons reportados.")
                providers = turn.get("providers")
                if isinstance(providers, dict):
                    provider_parts = []
                    labels = {"groq": "Groq", "gemini": "Gemini", "mistral": "Mistral", "cloudflare": "Cloudflare"}
                    for name in ("groq", "gemini", "mistral", "cloudflare"):
                        item = providers.get(name)
                        item_usage = item.get("usage") if isinstance(item, dict) else None
                        if isinstance(item_usage, dict) and token_number(item_usage.get("total_tokens")):
                            provider_parts.append(f"{labels[name]} {item_usage['total_tokens']}")
                    if provider_parts:
                        lines.append("**Uso por provedor:** " + ", ".join(provider_parts) + " tokens.")
                tools = turn.get("tools")
                if isinstance(tools, dict) and token_number(tools.get("reused_reads")) and tools.get("reused_reads"):
                    lines.append(
                        f"**Ferramentas no último turno:** {tools.get('executed', 0)} execuções; "
                        f"{tools['reused_reads']} leitura(s) repetida(s) reaproveitada(s)."
                    )
                savings = turn.get("local_savings_chars")
                if isinstance(savings, dict):
                    saved_parts = []
                    labels = {"tool_preface_history": "prévias privadas",
                              "closing_state": "estado de fechamento",
                              "tool_protocol_compaction": "protocolo de ferramentas"}
                    for key in ("tool_preface_history", "closing_state", "tool_protocol_compaction"):
                        value = savings.get(key)
                        if token_number(value) and value:
                            saved_parts.append(f"{labels[key]} {value}")
                    if saved_parts:
                        lines.append("**Caracteres locais não reenviados:** " + ", ".join(saved_parts) + ".")
                selection = turn.get("tool_selection")
                if isinstance(selection, dict):
                    catalog_chars = selection.get("catalog_schema_chars")
                    initial_chars = selection.get("initial_loaded_schema_chars")
                    current_chars = selection.get("current_loaded_schema_chars")
                    if token_number(catalog_chars) and catalog_chars > 0 and token_number(initial_chars):
                        saved = max(0, catalog_chars - initial_chars)
                        ratio = selection.get("initial_schema_reduction_ratio")
                        ratio_text = (f" ({float(ratio) * 100:.1f}% menor)"
                                      if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) else "")
                        details = [f"inicial {initial_chars}/{catalog_chars}, {saved} caracteres evitados{ratio_text}"]
                        if token_number(current_chars) and current_chars != initial_chars:
                            details.append(f"após lote {current_chars}")
                        extra = selection.get("speculative_pruned")
                        if token_number(extra) and extra:
                            details.append(f"{extra} especulativa(s) removida(s)")
                        lines.append("**Schemas de ferramentas:** " + "; ".join(details) + ".")
                delivery = turn.get("delivery")
                if isinstance(delivery, dict) and delivery.get("delivered") is True:
                    efficiency = []
                    rounds = delivery.get("model_rounds_per_response")
                    if token_number(rounds):
                        efficiency.append(f"{rounds} rodada(s) de modelo")
                    attempts = delivery.get("generation_attempts_per_response")
                    if token_number(attempts):
                        efficiency.append(f"{attempts} tentativa(s)")
                    total = delivery.get("total_tokens_per_response")
                    if token_number(total):
                        efficiency.append(f"{total} tokens/resposta")
                    neurons = delivery.get("neurons_per_response")
                    if isinstance(neurons, (int, float)) and not isinstance(neurons, bool) and math.isfinite(neurons):
                        efficiency.append(f"{float(neurons):.3f} neurons/resposta")
                    if efficiency:
                        lines.append("**Eficiência da resposta entregue:** " + ", ".join(efficiency) + ".")
                stages = turn.get("stages")
                if isinstance(stages, dict):
                    stage_labels = {"direct": "direta", "initial": "inicial",
                                    "tool_followup": "pós-ferramenta", "closing": "fechamento"}
                    stage_parts = []
                    for name in ("direct", "initial", "tool_followup", "closing"):
                        item = stages.get(name)
                        stage_usage = item.get("usage") if isinstance(item, dict) else None
                        if not isinstance(item, dict) or not token_number(item.get("calls")) or not item.get("calls"):
                            continue
                        part = f"{stage_labels[name]} {item['calls']}x"
                        if isinstance(stage_usage, dict) and token_number(stage_usage.get("total_tokens")):
                            part += f"/{stage_usage['total_tokens']} tok"
                        stage_parts.append(part)
                    if stage_parts:
                        lines.append("**Rodadas:** " + ", ".join(stage_parts) + ".")
                return "\n".join(lines)

        last = data.get("last_request")
        attempts = last.get("attempts") if isinstance(last, dict) else None
        if isinstance(attempts, list):
            for attempt in reversed(attempts):
                usage = attempt.get("usage") if isinstance(attempt, dict) else None
                if not isinstance(usage, dict):
                    continue
                values = [usage.get(key) for key in ("input_tokens", "output_tokens")]
                if all(isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000_000 for value in values):
                    lines.append(f"**Tokens da última tentativa medida:** entrada {values[0]}, saída {values[1]}.")
                    break
        return "\n".join(lines)

    async def _do_configurar(self, interaction: discord.Interaction):
        if not await self._config_staff_check(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        config = await self._config.get_config(interaction.guild.id)
        await _send(
            interaction, self._format_config(config, self._provider_status()),
            view=EditConfigView(
                requester_id=interaction.user.id, current_config=config,
                on_submit_config=self._handle_config_modal,
                on_submit_actions=self._handle_actions_modal,
                on_submit_audio=self._handle_audio_modal,
                on_submit_allowlists=self._handle_allowlists_modal,
                on_submit_provider=self._handle_provider_modal,
                check_authorized=self._config_staff_check,
            ),
        )

    async def _handle_config_modal(self, interaction: discord.Interaction, config: GuildChatbotConfig):
        if not await self._config_staff_check(interaction):
            return
        if interaction.guild.id != config.guild_id:
            await _send(interaction, "Esta configuração pertence a outro servidor.")
            return
        saved = await self._config.save_config(
            guild_id=interaction.guild.id, enabled=config.enabled,
            channel_ids=config.channel_ids,
            spontaneous_enabled=config.spontaneous_enabled,
            spontaneous_channel_ids=config.spontaneous_channel_ids,
            spontaneous_chance_percent=config.spontaneous_chance_percent,
            updated_by=interaction.user.id,
        )
        await _send(interaction, "Configuração salva.\n\n" + self._format_config(saved, self._provider_status()))

    async def _handle_actions_modal(self, interaction: discord.Interaction, config: GuildChatbotConfig):
        if not await self._config_staff_check(interaction):
            return
        if interaction.guild.id != config.guild_id:
            await _send(interaction, "Esta configuração pertence a outro servidor.")
            return
        saved = await self._config.save_action_config(
            guild_id=interaction.guild.id, actions_enabled=config.actions_enabled,
            audio_actions_enabled=config.audio_actions_enabled,
            voice_actions_enabled=config.voice_actions_enabled,
            moderation_actions_enabled=config.moderation_actions_enabled,
            action_staff_role_ids=config.action_staff_role_ids, updated_by=interaction.user.id,
        )
        await _send(interaction, "Ações salvas.\n\n" + self._format_config(saved, self._provider_status()))

    async def _handle_audio_modal(self, interaction: discord.Interaction, config: GuildChatbotConfig):
        if not await self._config_staff_check(interaction):
            return
        if interaction.guild.id != config.guild_id:
            await _send(interaction, "Esta configuração pertence a outro servidor.")
            return
        saved = await self._config.save_audio_config(
            guild_id=interaction.guild.id,
            audio_reply_chance_percent=config.audio_reply_chance_percent,
            audio_reply_cooldown_seconds=config.audio_reply_cooldown_seconds,
            updated_by=interaction.user.id,
        )
        await _send(interaction, "Áudios salvos.\n\n" + self._format_config(saved, self._provider_status()))

    async def _handle_allowlists_modal(self, interaction: discord.Interaction, config: GuildChatbotConfig):
        if not await self._config_staff_check(interaction):
            return
        guild = interaction.guild
        if guild.id != config.guild_id:
            await _send(interaction, "Esta configuração pertence a outro servidor.")
            return
        from .action_policy import is_safe_assignable_role

        try:
            roles = tuple(dict.fromkeys(config.action_allowed_role_ids))
            channels = tuple(dict.fromkeys(config.action_allowed_channel_ids))
            if len(roles) > 10 or len(channels) > 10:
                raise ValueError("Escolha até 10 cargos e 10 canais.")
            current = await self._config.get_config(guild.id, fresh=True)
            for role_id in roles:
                if not isinstance(role_id, int) or isinstance(role_id, bool) or role_id <= 0:
                    raise ValueError("Escolha cargos deste servidor.")
                role = guild.get_role(role_id)
                if role is None or role_id in current.action_staff_role_ids or not is_safe_assignable_role(guild, role, guild.me):
                    raise ValueError("Cargos de staff, gerenciados ou com privilégios não podem entrar nesta lista.")
            for channel_id in channels:
                if not isinstance(channel_id, int) or isinstance(channel_id, bool) or channel_id <= 0:
                    raise ValueError("Escolha canais deste servidor.")
                channel = guild.get_channel(channel_id)
                if channel is None or getattr(getattr(channel, "guild", None), "id", None) != guild.id:
                    raise ValueError("Escolha canais existentes neste servidor.")
            if not await self._config_staff_check(interaction):
                return
            saved = await self._config.save_action_allowlists(
                guild_id=guild.id, action_allowed_role_ids=roles,
                action_allowed_channel_ids=channels, updated_by=interaction.user.id,
            )
        except ValueError as exc:
            await _send(interaction, f"Não consegui salvar: {exc}")
            return
        await _send(interaction, "Cargos e canais autorizados salvos.\n\n" + self._format_config(saved, self._provider_status()))

    async def _handle_provider_modal(self, interaction: discord.Interaction, config: GuildChatbotConfig):
        if not await self._config_staff_check(interaction):
            return
        if interaction.guild.id != config.guild_id:
            await _send(interaction, "Esta configuração pertence a outro servidor.")
            return
        if config.text_provider_order not in (("groq", "gemini"), ("gemini", "groq")):
            await _send(interaction, "Escolha Groq ou Gemini como primeiro provedor.")
            return
        saved = await self._config.save_provider_config(
            guild_id=interaction.guild.id, text_provider_order=config.text_provider_order,
            updated_by=interaction.user.id,
        )
        await _send(interaction, "Provedor salvo.\n\n" + self._format_config(saved, self._provider_status()))

    async def _do_memoria_reset_server(self, interaction: discord.Interaction):
        if not await self._config_staff_check(interaction):
            return
        view = ConfirmView(
            requester_id=interaction.user.id,
            prompt="Apagar toda a memória pessoal e coletiva do chatbot neste servidor? As mensagens do Discord continuam no canal.",
            confirm_label="Apagar memória",
        )
        await _send(interaction, view.prompt, view=view)
        await view.wait()
        if view.result is not True:
            return
        confirmation = getattr(view, "confirmation_interaction", None) or interaction
        if not await self._config_staff_check(confirmation):
            return
        count = await self._memory.clear_all_guild_memory(interaction.guild.id)
        await _send(interaction, f"Memória do servidor apagada ({count} registros removidos).")

    async def _do_conhecimento(
        self, interaction: discord.Interaction, *, acao: str, titulo: str = "",
        conteudo: str = "", referencia: str = "", tags: str = "", publica: bool = False,
    ):
        """Publicação explícita da staff; documentos nunca concedem autoridade."""
        if not await self._config_staff_check(interaction):
            return
        store = getattr(self, "_knowledge", None)
        guild, channel = interaction.guild, interaction.channel
        if store is None:
            await _send(interaction, "A base de conhecimento ainda não está pronta.")
            return
        if guild is None or channel is None or getattr(getattr(channel, "guild", None), "id", None) != guild.id:
            await _send(interaction, "Use este comando em um canal deste servidor.")
            return
        if acao not in {"salvar", "listar", "remover"}:
            await _send(interaction, "Escolha salvar, listar ou remover.")
            return
        # Não aceitamos um canal/servidor arbitrário nos parâmetros. O escopo
        # vem da interação e o acesso é conferido novamente após ler a geração.
        await interaction.response.defer(ephemeral=True, thinking=True)
        from .memory import visibility_scope_for

        epoch = await self._memory.capture_epoch(guild.id, interaction.user.id)
        if not await self._config_staff_check(interaction):
            return
        effective_nsfw = channel_is_nsfw(channel) and C.nsfw_enabled_for_guild(guild.id)
        visibility = visibility_scope_for(
            channel.id, is_nsfw=effective_nsfw,
            is_private=isinstance(channel, discord.Thread) and channel.is_private(),
        )
        try:
            if acao == "salvar":
                if not titulo.strip() or not conteudo.strip():
                    raise ValueError("Informe título e conteúdo para salvar.")
                result = await store.publish(
                    guild.id, channel.id, visibility, epoch, title=titulo, content=conteudo,
                    tags=tags, guild_public=publica, ref=referencia,
                )
                location = "em todo o servidor" if publica else "somente neste canal"
                await _send(interaction, f"Conhecimento salvo {location}. Referência: `{result['ref']}`.")
            elif acao == "remover":
                if not referencia.strip():
                    raise ValueError("Informe a referência exibida ao listar.")
                removed = await store.forget(guild.id, channel.id, visibility, epoch, ref=referencia)
                await _send(interaction, "Conhecimento removido." if removed else "Referência não encontrada neste escopo.")
            else:
                entries = await store.list(guild.id, channel.id, visibility, epoch, limit=15)
                if not entries:
                    await _send(interaction, "Nenhum conhecimento publicado acessível neste canal.")
                    return
                lines = []
                for entry in entries:
                    # O store valida referência/título. A lista não despeja o
                    # conteúdo completo e nunca dispara menções do Discord.
                    scope = "servidor" if entry.get("scope") == "guild" else "canal"
                    title = discord.utils.escape_markdown(str(entry.get("title", ""))[:80])
                    lines.append(f"`{entry['ref']}` · {title} ({scope})")
                await _send(interaction, "**Conhecimento acessível neste canal**\n" + "\n".join(lines))
        except ValueError as exc:
            await _send(interaction, f"Não consegui salvar: {exc}")

    async def _master_check(self, interaction: discord.Interaction) -> Optional[object]:
        master = getattr(self, "_master", None)
        if master is None:
            await _send(interaction, "O chatbot não está pronto.")
            return None
        if not await _staff_check(interaction):
            await _send(interaction, "Só a staff com Gerenciar Servidor pode acessar as instruções globais.")
            return None
        config = await master.get()
        if interaction.guild.id != config.config_guild_id:
            await _send(
                interaction,
                f"As instruções globais só podem ser acessadas no servidor de configuração (`{config.config_guild_id}`). "
                "O dono ou operador do bot pode transferir essa autoridade com `/chatbotadmin master`.",
            )
            return None
        return config

    async def _check_master_authorized(self, interaction: discord.Interaction) -> bool:
        return await self._master_check(interaction) is not None

    async def _do_master_ver(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        config = await self._master_check(interaction)
        if config is None:
            return
        preview = config.prompt[:1700]
        if len(config.prompt) > 1700:
            preview += "\n… (prévia reduzida)"
        await _send(interaction, f"**Instruções globais do bot** ({len(config.prompt)} caracteres):\n```\n{preview}\n```")

    async def _do_master_editar(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        config = await self._master_check(interaction)
        if config is None:
            return
        await _send(
            interaction, "Edite as instruções usadas pelo bot em todos os servidores.",
            view=EditMasterView(
                master_store=self._master, current_content=config.prompt,
                requester_id=interaction.user.id,
                check_authorized=self._check_master_authorized,
            ),
        )

    async def _do_master_transferir(self, interaction: discord.Interaction, destino: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if getattr(self, "_master", None) is None:
            await _send(interaction, "O chatbot não está pronto.")
            return
        if not await _operator_check(interaction):
            await _send(interaction, "Só o dono ou um operador autorizado do bot pode transferir a configuração.")
            return
        try:
            target_id = int(destino.strip())
        except ValueError:
            target_id = 0
        target = self.bot.get_guild(target_id) if target_id > 0 else None
        if target is None:
            await _send(interaction, "Informe o ID de um servidor em que o bot esteja.")
            return
        current = await self._master.get()
        if target.id == current.config_guild_id:
            await _send(interaction, "Este já é o servidor de configuração.")
            return
        view = ConfirmView(
            requester_id=interaction.user.id,
            prompt=f"Transferir a edição das instruções globais para **{target.name}** (`{target.id}`)? O servidor anterior perderá esse acesso.",
            confirm_label="Transferir",
        )
        await _send(interaction, view.prompt, view=view)
        await view.wait()
        if view.result is not True:
            return
        confirmation = getattr(view, "confirmation_interaction", None) or interaction
        if not await _operator_check(confirmation):
            await _send(interaction, "Você não tem mais autorização para transferir a configuração.")
            return
        await self._master.set_config_guild(target.id, updated_by=interaction.user.id)
        await _send(interaction, f"Servidor de configuração alterado para **{target.name}** (`{target.id}`).")

    @chatbot.command(name="configurar", description="Ativar o chatbot, escolher canais e participação espontânea")
    @_safe_slash
    async def chatbot_configurar(self, interaction: discord.Interaction):
        await self._do_configurar(interaction)

    @chatbot.command(name="memoria", description="Apagar toda a memória do chatbot neste servidor")
    @_safe_slash
    async def chatbot_memoria(self, interaction: discord.Interaction):
        await self._do_memoria_reset_server(interaction)

    @chatbot.command(name="conhecimento", description="Staff: publicar, listar ou remover informações do chatbot")
    @app_commands.choices(acao=[
        app_commands.Choice(name="Salvar ou atualizar", value="salvar"),
        app_commands.Choice(name="Listar neste canal", value="listar"),
        app_commands.Choice(name="Remover", value="remover"),
    ])
    @app_commands.describe(
        titulo="Título de até 80 caracteres", conteudo="Informação de até 2.000 caracteres",
        referencia="Referência exibida ao listar, para atualizar ou remover",
        tags="Até 8 assuntos separados por vírgula",
        publica="Ao salvar: disponibilizar em todo o servidor (padrão: somente este canal)",
    )
    @_safe_slash
    async def chatbot_conhecimento(
        self, interaction: discord.Interaction, acao: app_commands.Choice[str],
        titulo: str = "", conteudo: str = "", referencia: str = "", tags: str = "", publica: bool = False,
    ):
        await self._do_conhecimento(
            interaction, acao=acao.value, titulo=titulo, conteudo=conteudo,
            referencia=referencia, tags=tags, publica=publica,
        )

    @chatbot_admin.command(name="master", description="Ver, editar ou transferir as instruções globais do bot")
    @app_commands.choices(acao=[
        app_commands.Choice(name="Ver instruções", value="ver"),
        app_commands.Choice(name="Editar instruções", value="editar"),
        app_commands.Choice(name="Transferir servidor de configuração", value="transferir"),
    ])
    @app_commands.describe(destino="ID do servidor de destino ao transferir")
    @_safe_slash
    async def chatbotadmin_master(self, interaction: discord.Interaction,
                                 acao: app_commands.Choice[str], destino: str = ""):
        if acao.value == "ver":
            await self._do_master_ver(interaction)
        elif acao.value == "editar":
            await self._do_master_editar(interaction)
        elif acao.value == "transferir":
            await self._do_master_transferir(interaction, destino)

    @chatbot_admin.command(name="reset_global", description="Apagar a memória do chatbot em todos os servidores")
    @_safe_slash
    async def chatbotadmin_reset_global(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await _operator_check(interaction):
            await _send(interaction, "Só o dono ou um operador autorizado do bot pode apagar a memória global.")
            return
        if not self._require_ready(interaction):
            await _send(interaction, "O chatbot não está pronto.")
            return
        view = ConfirmView(
            requester_id=interaction.user.id,
            prompt="Apagar toda a memória pessoal e coletiva do chatbot em TODOS os servidores? Esta ação não pode ser desfeita.",
            confirm_label="Apagar memória global",
        )
        await _send(interaction, view.prompt, view=view)
        await view.wait()
        if view.result is not True:
            return
        confirmation = getattr(view, "confirmation_interaction", None) or interaction
        if not await _operator_check(confirmation):
            await _send(interaction, "Você não tem mais autorização para apagar a memória global.")
            return
        count = await self._memory.clear_all_memory_everywhere()
        log.warning("chatbot: reset_global | requester=%s registros=%s", interaction.user.id, count)
        await _send(interaction, f"Memória global apagada ({count} registros removidos).")

    async def _run_image_command(self, interaction: discord.Interaction, *, prompt: str) -> None:
        from . import imagegen
        from .memory import visibility_scope_for

        guild, channel = interaction.guild, interaction.channel
        if guild is None or channel is None or self._image_service is None:
            return
        if not await self._can_respond(guild.id, channel.id, parent_id=getattr(channel, "parent_id", None)):
            await interaction.edit_original_response(content="O chatbot está desativado ou este canal não está permitido.")
            return
        epoch = await self._capture_turn_epoch(guild.id, interaction.user.id)
        effective_nsfw = channel_is_nsfw(channel) and C.nsfw_enabled_for_guild(guild.id)
        generated = await self._image_service.generate(
            prompt=prompt, channel_is_nsfw=effective_nsfw, slot_acquired=True,
        )
        if not generated.ok or generated.image is None:
            await interaction.edit_original_response(content=imagegen.build_image_failure_message(generated))
            return
        if not await self._can_respond(guild.id, channel.id, parent_id=getattr(channel, "parent_id", None)):
            await interaction.edit_original_response(content="O chatbot foi desativado ou este canal deixou de ser permitido.")
            return
        extension = imagegen.generated_image_extension(generated.image.mime_type)
        sent = await interaction.edit_original_response(
            content=f"🖼️ Imagem gerada para: *{self._neutralize_mentions(prompt[:200])}*",
            attachments=[discord.File(io.BytesIO(generated.image.data), filename=f"imagem.{extension}")],
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._remember_sent_message(guild_id=guild.id, channel_id=channel.id, message_id=sent.id)
        if epoch is not None and self._memory is not None:
            is_private = bool(isinstance(channel, discord.Thread) and channel.is_private())
            await self._persist_turn(
                guild_id=guild.id, channel_id=channel.id,
                visibility_scope=visibility_scope_for(channel.id, is_nsfw=effective_nsfw, is_private=is_private),
                epoch=epoch, user_id=interaction.user.id,
                user_name=str(getattr(interaction.user, "display_name", interaction.user.name)),
                user_message=prompt, assistant_message=f"[imagem gerada: {prompt[:500]}]",
                user_history_size=C.DEFAULT_HISTORY_SIZE,
            )

    @app_commands.command(name="imagem", description="Gera uma imagem a partir de uma descrição")
    @app_commands.describe(prompt="Descrição da imagem que você quer gerar")
    @_safe_slash
    async def imagem(self, interaction: discord.Interaction, prompt: str):
        prompt = prompt.strip()
        if not prompt:
            await _send(interaction, "Descreva a imagem que você quer gerar.")
            return
        if interaction.guild is None or interaction.channel is None:
            await _send(interaction, "Este comando só funciona em servidor.")
            return
        if not self._require_ready(interaction) or getattr(self, "_image_service", None) is None:
            await _send(interaction, "O chatbot não está pronto.")
            return
        if C.SAFE_MODE:
            await _send(interaction, "A geração de imagens está temporariamente em recuperação.")
            return
        if self._is_user_on_cooldown(interaction.guild.id, interaction.user.id):
            await _send(interaction, "Espere um pouco antes de pedir outra imagem.")
            return
        lease = await self._admission.try_admit("image", guild_id=interaction.guild.id, user_id=interaction.user.id)
        if lease is None:
            await _send(interaction, "A fila de imagens está cheia ou você já tem um pedido em andamento.")
            return
        try:
            await interaction.response.defer(thinking=True)
            async with lease:
                self._apply_user_cooldown(interaction.guild.id, interaction.user.id)
                await self._run_image_command(interaction, prompt=prompt[:1000])
        finally:
            await lease.release()

    @app_commands.command(name="reset", description="Apaga sua memória pessoal com o chatbot neste servidor")
    @_safe_slash
    async def reset(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await _send(interaction, "Este comando só funciona em servidor.")
            return
        if not self._require_ready(interaction):
            await _send(interaction, "O chatbot não está pronto.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        count = await self._memory.clear_user_history(interaction.guild.id, interaction.user.id)
        await _send(interaction, f"Sua memória pessoal foi apagada ({count} registros). As mensagens do Discord continuam no canal.")
