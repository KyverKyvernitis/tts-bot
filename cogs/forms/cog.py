"""FormsCog — sistema de formulário com botão persistente.

==============================================================================
TRIGGERS (mensagem inteira após strip().lower(), staff-only)
==============================================================================

- form / formulário / formulario
    - Setup incompleto (canais não configurados): em qualquer canal abre o
      wizard de setup (ChannelSelect x2 + Confirmar).
    - Setup completo: só funciona dentro do canal de formulário. Apaga a
      palavra do staff, apaga a mensagem do form anterior, posta uma nova.

- c
    - Setup incompleto: ignorado.
    - Setup completo: funciona no canal de form OU canal de respostas.
      Apaga a sessão 'c' anterior (independente de quem disparou), apaga
      a palavra 'c' atual, posta o painel de customização.

==============================================================================
PERSISTÊNCIA
==============================================================================

O botão do form (FormView) é persistente — sobrevive reboots porque:
1. Tem custom_id estável: `forms:submit:{guild_id}`.
2. É registrado via bot.add_view(view, message_id=...) em cog_load + on_ready.
3. Active message_id fica salvo em forms.active_message_id no DB.

==============================================================================
SESSÃO 'c' E O BOTÃO "APAGAR"
==============================================================================

Cada `c` apaga a sessão `c` anterior (mensagem trigger + painel) **antes**
de criar a nova sessão. Isso é independente de quem disparou — staff A pode
apagar painel aberto pelo staff B.

O botão "Apagar" e o on_timeout do painel chamam o mesmo método
(_purge_previous_c_session), então o comportamento é simétrico.

==============================================================================
PERMISSÕES E FALLBACK DE DELETE
==============================================================================

Pra apagar mensagens (palavra do trigger, form antigo, painel antigo) o bot
precisa de Manage Messages no canal. Quando falta:
1. Tenta delete normal.
2. Em Forbidden, manda DM pro autor da mensagem alertando.
3. Se DM falhar, posta aviso no canal com auto-delete em 30s.
"""
from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from .constants import (
    DEFAULT_APPROVAL,
    DEFAULT_MODAL,
    DEFAULT_PANEL,
    DEFAULT_RESPONSE,
    TRIGGER_WORD_CUSTOMIZE,
    TRIGGER_WORDS_FORM,
)
from .modals import FormSubmissionModal
from .views import CustomizationPanelView, FormView, ResponseReviewView, SetupView


log = logging.getLogger(__name__)


def _is_staff_member(member: discord.Member) -> bool:
    """Critério de staff: kick_members OR manage_guild OR administrator.

    Espelha o helper de gincana (kick_members) mas inclui manage_guild
    pra alcançar staff de admin/mod sem permissões de kick. Forms não
    define um staff_role próprio no DB então não dá pra reusar 1:1
    o helper que depende de gincana_staff_role_id.
    """
    perms = getattr(member, "guild_permissions", None)
    if perms is None:
        return False
    return bool(perms.kick_members or perms.manage_guild or perms.administrator)


class FormsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Lock por guild pra serializar processamento de triggers — evita
        # corrida com 2 staffs digitando "form" ao mesmo tempo.
        self._guild_locks: dict[int, asyncio.Lock] = {}
        # Registro de quais (guild_id, message_id) já tiveram view persistente
        # registrada, pra evitar registrar 2x em on_ready/cog_load reentrant.
        self._registered_persistent_views: set[tuple[int, int]] = set()

    @property
    def db(self):
        return getattr(self.bot, "settings_db", None)

    # ===== Lifecycle =====

    async def cog_load(self):
        await self._restore_persistent_form_views()

    @commands.Cog.listener()
    async def on_ready(self):
        await self._restore_persistent_form_views()

    async def _restore_persistent_form_views(self):
        """Itera todas as guilds conhecidas e re-registra a FormView ativa.

        Idempotente — já evita re-registro via _registered_persistent_views.
        Erros individuais são logados e não bloqueiam outras guilds.
        """
        db = self.db
        if db is None:
            return
        guild_ids: set[int] = set()
        if hasattr(db, "guild_cache"):
            guild_ids.update(int(gid) for gid in db.guild_cache.keys() if gid)
        for guild in getattr(self.bot, "guilds", []):
            gid = int(getattr(guild, "id", 0) or 0)
            if gid:
                guild_ids.add(gid)

        for gid in sorted(guild_ids):
            cfg = self._get_config(gid)
            mid = int(cfg.get("active_message_id") or 0)
            if not mid:
                continue
            key = (gid, mid)
            if key in self._registered_persistent_views:
                continue
            try:
                view = FormView(self, gid)
                self.bot.add_view(view, message_id=mid)
                self._registered_persistent_views.add(key)
            except Exception as e:
                log.warning("[forms] falha ao registrar view persistente gid=%s mid=%s: %r", gid, mid, e)

    # ===== Config helpers =====

    def _get_config(self, guild_id: int) -> dict[str, Any]:
        db = self.db
        if db is None or not hasattr(db, "get_forms_config"):
            return self._normalize_config(self._default_config())
        try:
            return self._normalize_config(db.get_forms_config(int(guild_id)))
        except Exception as e:
            log.warning("[forms] erro ao ler config gid=%s: %r", guild_id, e)
            return self._normalize_config(self._default_config())

    @staticmethod
    def _normalize_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
        """Completa chaves novas e corrige defaults legados sem mexer em textos realmente customizados."""
        base = FormsCog._default_config()
        if not isinstance(cfg, dict):
            return base

        for key, value in base.items():
            if key not in cfg:
                cfg[key] = deepcopy(value)

        for section in ("panel", "modal", "response", "approval"):
            current = cfg.get(section)
            if not isinstance(current, dict):
                current = {}
            merged = deepcopy(base.get(section) or {})
            merged.update(current)
            cfg[section] = merged

        modal = cfg.get("modal") or {}
        if str(modal.get("field2_label") or "").strip().lower() == "idade":
            modal["field2_label"] = DEFAULT_MODAL["field2_label"]
            if str(modal.get("field2_placeholder") or "").strip() in {"", "17"}:
                modal["field2_placeholder"] = DEFAULT_MODAL["field2_placeholder"]
        if str(modal.get("field3_label") or "").strip().lower() == "motivo":
            modal["field3_label"] = DEFAULT_MODAL["field3_label"]
        cfg["modal"] = modal
        return cfg

    async def _save_config(self, guild_id: int, cfg: dict[str, Any]):
        db = self.db
        if db is None or not hasattr(db, "set_forms_config"):
            log.warning("[forms] settings_db sem set_forms_config — config NÃO foi salva")
            return
        try:
            await db.set_forms_config(int(guild_id), cfg)
        except Exception:
            log.exception("[forms] falha ao salvar config gid=%s", guild_id)

    @staticmethod
    def _default_config() -> dict[str, Any]:
        return {
            "form_channel_id": 0,
            "responses_channel_id": 0,
            "active_message_id": 0,
            "active_c_trigger": {"channel_id": 0, "message_id": 0},
            "active_c_panel": {"channel_id": 0, "message_id": 0},
            "panel": deepcopy(DEFAULT_PANEL),
            "modal": deepcopy(DEFAULT_MODAL),
            "response": deepcopy(DEFAULT_RESPONSE),
            "approval": deepcopy(DEFAULT_APPROVAL),
        }

    # ===== Permission helper =====

    def _is_staff(self, member: discord.Member) -> bool:
        return _is_staff_member(member)

    # ===== Delete with fallback =====

    async def _delete_with_fallback(
        self,
        message: discord.Message,
        *,
        actor: discord.Member | None = None,
    ):
        """Apaga mensagem; em Forbidden, DM o actor; senão posta no canal.

        actor: usuário que vai receber a DM se a deleção falhar. Se None,
        cai pra message.author quando ele é Member.
        """
        try:
            await message.delete()
            return
        except discord.NotFound:
            return
        except (discord.Forbidden, discord.HTTPException):
            pass

        if actor is None and isinstance(message.author, discord.Member):
            actor = message.author

        channel_mention = getattr(message.channel, "mention", "este canal")
        warning = (
            f"⚠️ Não consegui apagar uma mensagem em {channel_mention}. "
            f"Eu preciso da permissão `Gerenciar Mensagens` lá pra que o "
            f"sistema de formulário funcione direito."
        )

        if actor is not None:
            try:
                await actor.send(warning)
                return
            except (discord.Forbidden, discord.HTTPException):
                pass

        try:
            await message.channel.send(warning, delete_after=30)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ===== on_message dispatch =====

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Filtros baratos antes de qualquer DB call
        if message.author.bot or message.guild is None or not message.content:
            return
        if not isinstance(message.author, discord.Member):
            return

        content_norm = message.content.strip().lower()
        is_form_trigger = content_norm in TRIGGER_WORDS_FORM
        is_c_trigger = content_norm == TRIGGER_WORD_CUSTOMIZE
        if not (is_form_trigger or is_c_trigger):
            return

        if not self._is_staff(message.author):
            return  # ignora silenciosamente — não-staff não dispara

        guild_id = int(message.guild.id)
        cfg = self._get_config(guild_id)
        form_ch_id = int(cfg.get("form_channel_id") or 0)
        resp_ch_id = int(cfg.get("responses_channel_id") or 0)
        setup_complete = bool(form_ch_id and resp_ch_id)

        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            try:
                if is_form_trigger:
                    if not setup_complete:
                        await self._start_setup_flow(message)
                    elif message.channel.id == form_ch_id:
                        await self._refresh_form_message(message)
                    # senão: trigger 'form' fora do canal configurado, ignora
                elif is_c_trigger:
                    if not setup_complete:
                        return  # 'c' sem setup não faz nada
                    if message.channel.id not in (form_ch_id, resp_ch_id):
                        return
                    await self._open_customization_panel(message)
            except Exception:
                log.exception(
                    "[forms] erro processando trigger gid=%s ch=%s msg=%s",
                    guild_id, message.channel.id, message.id,
                )

    # ===== Setup flow =====

    async def _start_setup_flow(self, message: discord.Message):
        """Posta o SetupView no canal onde o staff disparou. A palavra é apagada."""
        await self._delete_with_fallback(message, actor=message.author)

        view = SetupView(
            self,
            guild_id=int(message.guild.id),
            staff_id=int(message.author.id),
        )
        try:
            sent = await message.channel.send(view=view)
            view.message = sent
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("[forms] falha ao postar setup view gid=%s: %r", message.guild.id, e)

    async def _finalize_setup(
        self,
        interaction: discord.Interaction,
        *,
        setup_view: SetupView,
        form_channel_id: int,
        resp_channel_id: int,
    ):
        """Salva config + posta form no canal escolhido + atualiza msg de setup."""
        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        cfg["form_channel_id"] = int(form_channel_id)
        cfg["responses_channel_id"] = int(resp_channel_id)
        await self._save_config(guild_id, cfg)

        form_channel = self.bot.get_channel(int(form_channel_id))
        posted_ok = False
        if isinstance(form_channel, discord.TextChannel):
            posted_ok = await self._post_form_message(guild_id, form_channel)

        confirmation = self._build_setup_confirmation_view(
            form_channel_id, resp_channel_id, posted_ok
        )
        if setup_view.message is not None:
            try:
                await setup_view.message.edit(view=confirmation)
                return
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass
        # Fallback: msg original sumiu, manda como followup ephemeral
        try:
            await interaction.followup.send(view=confirmation, ephemeral=True)
        except discord.HTTPException:
            pass

    def _build_setup_confirmation_view(
        self,
        form_channel_id: int,
        resp_channel_id: int,
        posted_ok: bool,
    ) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        status_line = (
            f"✅ Form postado em <#{form_channel_id}>."
            if posted_ok
            else (
                f"⚠️ Configuração salva, mas falhei ao postar o form em "
                f"<#{form_channel_id}>. Use `/form_repostar` ou digite `form` lá."
            )
        )
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# ✅ Configuração salva"),
                discord.ui.TextDisplay(
                    f"**Canal de formulário:** <#{form_channel_id}>\n"
                    f"**Canal de respostas:** <#{resp_channel_id}>"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(status_line),
                accent_color=discord.Color.green(),
            )
        )
        return view

    # ===== Form refresh (trigger 'form/formulário' no canal de form) =====

    async def _refresh_form_message(self, message: discord.Message):
        """Apaga palavra, apaga form antigo, posta novo."""
        guild_id = int(message.guild.id)
        cfg = self._get_config(guild_id)

        await self._delete_with_fallback(message, actor=message.author)

        old_mid = int(cfg.get("active_message_id") or 0)
        if old_mid:
            try:
                old_msg = await message.channel.fetch_message(old_mid)
                await old_msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await self._post_form_message(guild_id, message.channel)

    async def _post_form_message(
        self,
        guild_id: int,
        channel: discord.abc.Messageable,
    ) -> bool:
        """Posta a FormView no canal e atualiza active_message_id no DB.

        Retorna True se posted com sucesso. Em caso de erro, loga e retorna
        False sem alterar DB (mantém o active_message_id antigo, que pode
        estar válido ainda).
        """
        try:
            view = FormView(self, guild_id)
            sent = await channel.send(view=view)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("[forms] falha ao postar form gid=%s: %r", guild_id, e)
            return False

        cfg = self._get_config(guild_id)
        cfg["active_message_id"] = int(sent.id)
        await self._save_config(guild_id, cfg)

        try:
            self.bot.add_view(view, message_id=int(sent.id))
            self._registered_persistent_views.add((guild_id, int(sent.id)))
        except Exception as e:
            log.warning("[forms] falha ao registrar view persistente gid=%s mid=%s: %r",
                        guild_id, sent.id, e)

        return True

    # ===== Submission handling =====

    async def _handle_submit_click(self, interaction: discord.Interaction, guild_id: int):
        """Callback do botão do form: abre o modal de submissão."""
        try:
            await interaction.response.send_modal(FormSubmissionModal(self, guild_id))
        except discord.HTTPException as e:
            log.warning("[forms] falha ao abrir modal gid=%s: %r", guild_id, e)

    async def _handle_submission(
        self,
        interaction: discord.Interaction,
        *,
        field_values: dict[str, str] | None = None,
        age_pronoun: str | None = None,
        description: str | None = None,
    ):
        """Recebe o submit do FormSubmissionModal e posta no canal de respostas."""
        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        resp_ch_id = int(cfg.get("responses_channel_id") or 0)

        if field_values is None:
            # Compatibilidade com chamadas antigas: campo 2/3 recebem os nomes antigos.
            field_values = {
                "field1": getattr(interaction.user, "display_name", str(interaction.user)),
                "field2": str(age_pronoun or "").strip(),
                "field3": str(description or "").strip(),
            }
        else:
            field_values = {
                "field1": str(field_values.get("field1") or "").strip(),
                "field2": str(field_values.get("field2") or "").strip(),
                "field3": str(field_values.get("field3") or "").strip(),
            }

        if not resp_ch_id:
            await self._safe_send_ephemeral(
                interaction,
                "❌ Configuração inválida — peça pra um staff reconfigurar.",
            )
            return

        resp_channel = self.bot.get_channel(resp_ch_id)
        if not isinstance(resp_channel, discord.TextChannel):
            await self._safe_send_ephemeral(
                interaction,
                "❌ Canal de respostas indisponível — peça pra um staff reconfigurar.",
            )
            return

        response_view = self._build_response_view(
            guild_id, interaction.user, field_values
        )

        try:
            await resp_channel.send(view=response_view)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("[forms] falha ao postar resposta gid=%s: %r", guild_id, e)
            await self._safe_send_ephemeral(
                interaction,
                "❌ Não consegui postar no canal de respostas (talvez sem permissão). "
                "Avisa um staff.",
            )
            return

        await self._safe_send_ephemeral(interaction, "✅ Formulário enviado!")

    @staticmethod
    async def _safe_send_ephemeral(interaction: discord.Interaction, content: str):
        """Manda mensagem ephemeral sem levantar se interaction já foi respondida."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True)
            else:
                await interaction.response.send_message(content, ephemeral=True)
        except discord.HTTPException:
            pass

    def _build_response_view(
        self,
        guild_id: int,
        user,
        field_values: dict[str, str],
    ) -> discord.ui.LayoutView:
        """Constrói a mensagem Components V2 com campos separados."""
        return ResponseReviewView(
            self,
            guild_id=int(guild_id),
            applicant_id=int(getattr(user, "id", 0) or 0),
            field_values=field_values,
        )

    def _build_template_ctx(
        self,
        guild_id: int,
        user_id: int,
        field_values: dict[str, str] | None,
        *,
        sample: bool = False,
    ) -> dict[str, str]:
        """Contexto de placeholders para resposta/DMs."""
        guild = self.bot.get_guild(int(guild_id))
        member = guild.get_member(int(user_id)) if guild is not None and user_id else None
        user_mention = f"<@{int(user_id)}>" if user_id else "<@123456789>"
        display_name = getattr(member, "display_name", "Leonardo" if sample else str(user_id or "Usuário"))
        guild_name = getattr(guild, "name", "Servidor" if sample else "este servidor")
        values = dict(field_values or {})
        field1 = str(values.get("field1") or ("Leonardo" if sample else ""))
        field2 = str(values.get("field2") or ("17, ele" if sample else ""))
        field3 = str(values.get("field3") or ("Não sei" if sample else ""))
        return {
            "user": user_mention,
            "membro": user_mention,
            "user_id": str(user_id or "123456789"),
            "user_name": display_name,
            "nome_usuario": display_name,
            "guild": guild_name,
            "servidor": guild_name,
            "field1": field1,
            "field2": field2,
            "field3": field3,
            "nome": field1,
            "idade": field2,
            "motivo": field3,
            "pronome": field2,
            # aliases antigos
            "idade_pronome": field2,
            "descricao": field3,
        }

    @staticmethod
    def _safe_format(template: str, ctx: dict) -> str:
        """format_map com fallback: placeholders desconhecidos ficam intactos."""

        class _SafeDict(dict):
            def __missing__(self, key):
                return "{" + key + "}"

        try:
            return str(template or "").format_map(_SafeDict(ctx))
        except Exception:
            return str(template or "")

    # ===== Customization panel (trigger 'c') =====

    async def _open_customization_panel(self, message: discord.Message):
        """Apaga sessão `c` anterior e posta painel novo; a palavra atual fica até apagar."""
        guild_id = int(message.guild.id)

        # Limpeza global: independente de quem disparou a sessão anterior.
        # Isso apaga o `c` anterior e o painel anterior, mas não apaga o `c` atual.
        await self._purge_previous_c_session(guild_id)

        view = CustomizationPanelView(
            self, guild_id=guild_id, staff_id=int(message.author.id)
        )
        try:
            panel_msg = await message.channel.send(view=view)
            view.message = panel_msg
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("[forms] falha ao postar customization panel gid=%s: %r", guild_id, e)
            return

        cfg = self._get_config(guild_id)
        cfg["active_c_trigger"] = {
            "channel_id": int(message.channel.id),
            "message_id": int(message.id),
        }
        cfg["active_c_panel"] = {
            "channel_id": int(message.channel.id),
            "message_id": int(panel_msg.id),
        }
        await self._save_config(guild_id, cfg)

    async def _purge_previous_c_session(self, guild_id: int):
        """Apaga mensagem trigger e painel da sessão 'c' atual + zera o DB."""
        cfg = self._get_config(guild_id)
        for key in ("active_c_trigger", "active_c_panel"):
            entry = cfg.get(key) or {}
            ch_id = int(entry.get("channel_id") or 0)
            msg_id = int(entry.get("message_id") or 0)
            if not (ch_id and msg_id):
                continue
            ch = self.bot.get_channel(ch_id)
            if ch is None:
                continue
            try:
                msg = await ch.fetch_message(msg_id)
                await msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        cfg["active_c_trigger"] = {"channel_id": 0, "message_id": 0}
        cfg["active_c_panel"] = {"channel_id": 0, "message_id": 0}
        await self._save_config(guild_id, cfg)

    # ===== Update config (chamado pelos modais de edição) =====

    async def _update_panel_config(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        description: str,
        button_label: str,
        button_emoji: str = "",
        media_url: str = "",
    ):
        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        old = cfg.get("panel") or {}
        cfg["panel"] = {
            "title": title,
            "description": description,
            "button_label": button_label,
            "button_emoji": button_emoji,
            "button_style": str(old.get("button_style") or DEFAULT_PANEL.get("button_style") or "primary"),
            "media_url": media_url,
        }
        await self._save_config(guild_id, cfg)
        await self._rerender_active_form(guild_id)
        await self._rerender_customization_panel(guild_id, int(getattr(interaction.user, "id", 0) or 0))
        await self._safe_send_ephemeral(interaction, "✅ Painel atualizado.")

    async def _update_modal_config(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        field1_label: str,
        field1_placeholder: str,
        field2_label: str,
        field2_placeholder: str,
        field3_label: str,
        field3_placeholder: str,
    ):
        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        old = cfg.get("modal") or {}
        cfg["modal"] = {
            "title": title,
            "field1_label": field1_label,
            "field1_placeholder": field1_placeholder,
            "field1_required": bool(old.get("field1_required", True)),
            "field2_label": field2_label,
            "field2_placeholder": field2_placeholder,
            "field2_required": bool(old.get("field2_required", True)),
            "field3_label": field3_label,
            "field3_placeholder": field3_placeholder,
            "field3_required": bool(old.get("field3_required", True)),
        }
        await self._save_config(guild_id, cfg)
        await self._rerender_customization_panel(guild_id, int(getattr(interaction.user, "id", 0) or 0))
        await self._safe_send_ephemeral(interaction, "✅ Campos do modal atualizados.")

    async def _update_response_config(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        intro: str,
        footer: str,
        media_url: str,
    ):
        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        cfg["response"] = {
            "title": title,
            "intro": intro,
            "footer": footer,
            "media_url": media_url,
        }
        await self._save_config(guild_id, cfg)
        await self._rerender_customization_panel(guild_id, int(getattr(interaction.user, "id", 0) or 0))
        await self._safe_send_ephemeral(interaction, "✅ Resposta atualizada.")

    async def _toggle_approval(self, interaction: discord.Interaction):
        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        approval = dict(cfg.get("approval") or DEFAULT_APPROVAL)
        approval["enabled"] = not bool(approval.get("enabled", False))
        cfg["approval"] = approval
        await self._save_config(guild_id, cfg)
        await self._rerender_customization_panel(guild_id, int(getattr(interaction.user, "id", 0) or 0), interaction=interaction)

    async def _set_button_style(self, interaction: discord.Interaction, *, target: str, style_name: str):
        guild_id = int(interaction.guild_id or 0)
        style_name = str(style_name or "primary").strip().lower()
        if style_name not in {"primary", "secondary", "success", "danger"}:
            style_name = "primary"

        cfg = self._get_config(guild_id)
        target = str(target or "").strip().lower()
        if target == "panel":
            panel = dict(cfg.get("panel") or DEFAULT_PANEL)
            panel["button_style"] = style_name
            cfg["panel"] = panel
            await self._save_config(guild_id, cfg)
            await self._rerender_active_form(guild_id)
        elif target in {"approve", "reject"}:
            approval = dict(cfg.get("approval") or DEFAULT_APPROVAL)
            approval[f"{target}_style"] = style_name
            cfg["approval"] = approval
            await self._save_config(guild_id, cfg)
        else:
            await self._safe_send_ephemeral(interaction, "❌ Botão inválido.")
            return

        await self._rerender_customization_panel(
            guild_id,
            int(getattr(interaction.user, "id", 0) or 0),
            interaction=interaction,
        )

    async def _set_approval_role(self, interaction: discord.Interaction, role_id: int):
        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        approval = dict(cfg.get("approval") or DEFAULT_APPROVAL)
        approval["role_id"] = int(role_id)
        cfg["approval"] = approval
        await self._save_config(guild_id, cfg)
        await self._rerender_customization_panel(guild_id, int(getattr(interaction.user, "id", 0) or 0), interaction=interaction)

    async def _update_approval_options(
        self,
        interaction: discord.Interaction,
        *,
        enabled: bool,
        role_id: int,
        panel_style: str,
        approve_style: str,
        reject_style: str,
    ):
        guild_id = int(interaction.guild_id or 0)

        def clean_style(value: str, fallback: str) -> str:
            value = str(value or fallback).strip().lower()
            return value if value in {"primary", "secondary", "success", "danger"} else fallback

        cfg = self._get_config(guild_id)
        panel = dict(cfg.get("panel") or DEFAULT_PANEL)
        approval = dict(cfg.get("approval") or DEFAULT_APPROVAL)

        panel["button_style"] = clean_style(panel_style, "primary")
        approval["enabled"] = bool(enabled)
        approval["role_id"] = max(0, int(role_id or 0))
        approval["approve_style"] = clean_style(approve_style, "success")
        approval["reject_style"] = clean_style(reject_style, "danger")

        cfg["panel"] = panel
        cfg["approval"] = approval
        await self._save_config(guild_id, cfg)
        await self._rerender_active_form(guild_id)
        await self._rerender_customization_panel(guild_id, int(getattr(interaction.user, "id", 0) or 0))
        await self._safe_send_ephemeral(interaction, "✅ Opções atualizadas.")

    async def _update_approval_config(
        self,
        interaction: discord.Interaction,
        *,
        approve_label: str,
        approve_emoji: str,
        reject_label: str,
        reject_emoji: str,
        approve_dm: str,
        reject_dm: str,
    ):
        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        old = dict(cfg.get("approval") or DEFAULT_APPROVAL)
        old.update({
            "approve_label": approve_label,
            "approve_emoji": approve_emoji,
            "reject_label": reject_label,
            "reject_emoji": reject_emoji,
            "approve_dm": approve_dm,
            "reject_dm": reject_dm,
        })
        cfg["approval"] = old
        await self._save_config(guild_id, cfg)
        await self._rerender_customization_panel(guild_id, int(getattr(interaction.user, "id", 0) or 0))
        await self._safe_send_ephemeral(interaction, "✅ Aprovação atualizada.")

    async def _rerender_customization_panel(
        self,
        guild_id: int,
        staff_id: int,
        *,
        interaction: discord.Interaction | None = None,
    ):
        """Atualiza o painel `c` ativo para refletir as configs atuais."""
        if interaction is not None and not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True)
            except discord.HTTPException:
                pass
        cfg = self._get_config(guild_id)
        entry = cfg.get("active_c_panel") or {}
        ch_id = int(entry.get("channel_id") or 0)
        msg_id = int(entry.get("message_id") or 0)
        if not (ch_id and msg_id):
            if interaction is not None:
                await self._safe_send_ephemeral(interaction, "✅ Atualizado.")
            return
        channel = self.bot.get_channel(ch_id)
        if channel is None:
            if interaction is not None:
                await self._safe_send_ephemeral(interaction, "✅ Atualizado.")
            return
        try:
            msg = await channel.fetch_message(msg_id)
            new_view = CustomizationPanelView(self, guild_id=guild_id, staff_id=staff_id)
            await msg.edit(view=new_view)
            new_view.message = msg
            if interaction is not None:
                try:
                    await interaction.followup.send("✅ Atualizado.", ephemeral=True)
                except discord.HTTPException:
                    pass
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            if interaction is not None:
                await self._safe_send_ephemeral(interaction, "✅ Atualizado, mas não consegui redesenhar o painel `c`.")

    async def _handle_review_action(self, interaction: discord.Interaction, view: ResponseReviewView, *, approved: bool):
        if not isinstance(interaction.user, discord.Member) or not self._is_staff(interaction.user):
            await self._safe_send_ephemeral(interaction, "❌ Só staff pode revisar formulários.")
            return

        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass

        guild_id = int(interaction.guild_id or view.guild_id)
        cfg = self._get_config(guild_id)
        approval = cfg.get("approval") or DEFAULT_APPROVAL
        applicant = None
        if interaction.guild is not None:
            applicant = interaction.guild.get_member(int(view.applicant_id))
            if applicant is None:
                try:
                    applicant = await interaction.guild.fetch_member(int(view.applicant_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    applicant = None

        role_warning = ""
        if approved:
            role_id = int(approval.get("role_id") or 0)
            if role_id and isinstance(applicant, discord.Member):
                role = interaction.guild.get_role(role_id) if interaction.guild is not None else None
                if role is None:
                    role_warning = "\n⚠️ Cargo configurado não encontrado."
                else:
                    try:
                        await applicant.add_roles(role, reason=f"Formulário aprovado por {interaction.user}")
                    except (discord.Forbidden, discord.HTTPException):
                        role_warning = "\n⚠️ Não consegui dar o cargo. Verifique minha hierarquia/permissões."

        dm_ok = await self._send_review_dm(
            guild_id,
            int(view.applicant_id),
            view.field_values,
            approved=approved,
        )
        dm_warning = "" if dm_ok else "\n⚠️ Não consegui enviar DM ao membro."

        new_status = "approved" if approved else "rejected"
        new_view = ResponseReviewView(
            self,
            guild_id=guild_id,
            applicant_id=int(view.applicant_id),
            field_values=view.field_values,
            status=new_status,
            reviewer_mention=getattr(interaction.user, "mention", ""),
        )
        try:
            if interaction.message is not None:
                await interaction.message.edit(view=new_view)
        except (discord.Forbidden, discord.HTTPException):
            pass

        label = "aprovado" if approved else "rejeitado"
        try:
            await interaction.followup.send(f"✅ Formulário {label}.{role_warning}{dm_warning}", ephemeral=True)
        except discord.HTTPException:
            pass

    async def _send_review_dm(
        self,
        guild_id: int,
        applicant_id: int,
        field_values: dict[str, str],
        *,
        approved: bool,
    ) -> bool:
        guild = self.bot.get_guild(int(guild_id))
        user = None
        if guild is not None:
            user = guild.get_member(int(applicant_id))
        if user is None:
            try:
                user = await self.bot.fetch_user(int(applicant_id))
            except (discord.NotFound, discord.HTTPException):
                return False
        cfg = self._get_config(guild_id)
        approval = cfg.get("approval") or DEFAULT_APPROVAL
        tpl_raw = approval.get("approve_dm") if approved else approval.get("reject_dm")
        tpl = str(tpl_raw or "")
        if not tpl:
            tpl = DEFAULT_APPROVAL["approve_dm" if approved else "reject_dm"]
        ctx = self._build_template_ctx(guild_id, applicant_id, field_values)
        body = self._safe_format(tpl, ctx)
        title = "✅ Verificação aprovada" if approved else "❌ Verificação rejeitada"
        dm_view = discord.ui.LayoutView(timeout=None)
        dm_view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"# {title}"),
                discord.ui.TextDisplay(body),
                accent_color=discord.Color.green() if approved else discord.Color.red(),
            )
        )
        try:
            await user.send(view=dm_view)
            return True
        except (discord.Forbidden, discord.HTTPException):
            try:
                await user.send(body)
                return True
            except (discord.Forbidden, discord.HTTPException):
                return False

    async def _rerender_active_form(self, guild_id: int):
        """Edita a mensagem ativa do form pra refletir novos textos.

        Best-effort: se a mensagem foi apagada (NotFound) ou bot perdeu
        permissão (Forbidden), silenciosamente ignora — próximo trigger
        'form' vai criar uma nova com a config atualizada.
        """
        cfg = self._get_config(guild_id)
        form_ch_id = int(cfg.get("form_channel_id") or 0)
        active_mid = int(cfg.get("active_message_id") or 0)
        if not (form_ch_id and active_mid):
            return
        channel = self.bot.get_channel(form_ch_id)
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(active_mid)
            new_view = FormView(self, guild_id)
            await msg.edit(view=new_view)
            try:
                self.bot.add_view(new_view, message_id=active_mid)
            except Exception:
                pass
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    # ===== Slash commands =====

    @app_commands.command(
        name="form_config",
        description="[Staff] Configura os canais do sistema de formulário",
    )
    @app_commands.describe(
        canal_form="Canal onde o botão de formulário fica visível",
        canal_respostas="Canal pra onde as submissões vão",
    )
    async def slash_form_config(
        self,
        interaction: discord.Interaction,
        canal_form: discord.TextChannel,
        canal_respostas: discord.TextChannel,
    ):
        if not isinstance(interaction.user, discord.Member) or not self._is_staff(interaction.user):
            await self._safe_send_ephemeral(interaction, "❌ Só staff pode usar.")
            return

        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        cfg["form_channel_id"] = int(canal_form.id)
        cfg["responses_channel_id"] = int(canal_respostas.id)
        await self._save_config(guild_id, cfg)

        posted_ok = await self._post_form_message(guild_id, canal_form)
        msg = (
            f"✅ Configurado.\n**Canal de formulário:** {canal_form.mention}\n"
            f"**Canal de respostas:** {canal_respostas.mention}\n"
        )
        msg += "Form já postado no canal." if posted_ok else (
            "⚠️ Falha ao postar o form. Use `/form_repostar` ou trigger no canal."
        )
        await self._safe_send_ephemeral(interaction, msg)

    @app_commands.command(
        name="form_status",
        description="[Staff] Mostra a configuração atual do formulário",
    )
    async def slash_form_status(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not self._is_staff(interaction.user):
            await self._safe_send_ephemeral(interaction, "❌ Só staff pode usar.")
            return

        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        form_ch_id = int(cfg.get("form_channel_id") or 0)
        resp_ch_id = int(cfg.get("responses_channel_id") or 0)
        active_mid = int(cfg.get("active_message_id") or 0)

        lines = [
            f"**Canal de formulário:** {f'<#{form_ch_id}>' if form_ch_id else '_não configurado_'}",
            f"**Canal de respostas:** {f'<#{resp_ch_id}>' if resp_ch_id else '_não configurado_'}",
            f"**Mensagem ativa do form:** {f'`{active_mid}`' if active_mid else '_nenhuma_'}",
        ]
        await self._safe_send_ephemeral(interaction, "\n".join(lines))

    @app_commands.command(
        name="form_reset",
        description="[Staff] Limpa toda configuração do formulário",
    )
    async def slash_form_reset(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not self._is_staff(interaction.user):
            await self._safe_send_ephemeral(interaction, "❌ Só staff pode usar.")
            return

        guild_id = int(interaction.guild_id or 0)
        cfg = self._default_config()
        await self._save_config(guild_id, cfg)
        await self._safe_send_ephemeral(
            interaction,
            "✅ Configuração resetada. Mensagens já postadas continuam visíveis "
            "mas viram fantasmas (botões não respondem mais até repostagem).",
        )

    @app_commands.command(
        name="form_repostar",
        description="[Staff] Reposta a mensagem do formulário no canal configurado",
    )
    async def slash_form_repostar(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not self._is_staff(interaction.user):
            await self._safe_send_ephemeral(interaction, "❌ Só staff pode usar.")
            return

        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        form_ch_id = int(cfg.get("form_channel_id") or 0)
        if not form_ch_id:
            await self._safe_send_ephemeral(
                interaction,
                "❌ Canais não configurados. Use `/form_config` primeiro.",
            )
            return

        channel = self.bot.get_channel(form_ch_id)
        if not isinstance(channel, discord.TextChannel):
            await self._safe_send_ephemeral(interaction, "❌ Canal de form não acessível.")
            return

        old_mid = int(cfg.get("active_message_id") or 0)
        if old_mid:
            try:
                old_msg = await channel.fetch_message(old_mid)
                await old_msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        ok = await self._post_form_message(guild_id, channel)
        await self._safe_send_ephemeral(
            interaction,
            "✅ Form repostado." if ok else "❌ Falha ao repostar.",
        )

    @app_commands.command(
        name="form_customizar",
        description="[Staff] Abre o painel de customização do formulário",
    )
    async def slash_form_customizar(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not self._is_staff(interaction.user):
            await self._safe_send_ephemeral(interaction, "❌ Só staff pode usar.")
            return

        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        if not (int(cfg.get("form_channel_id") or 0) and int(cfg.get("responses_channel_id") or 0)):
            await self._safe_send_ephemeral(
                interaction,
                "❌ Configure os canais primeiro (`/form_config` ou trigger `form` em algum canal).",
            )
            return

        # Limpa sessão 'c' anterior pra consistência com o trigger por palavra
        await self._purge_previous_c_session(guild_id)

        view = CustomizationPanelView(
            self, guild_id=guild_id, staff_id=int(interaction.user.id)
        )
        try:
            await interaction.response.send_message(view=view)
            sent = await interaction.original_response()
            view.message = sent
        except discord.HTTPException as e:
            log.warning("[forms] falha em /form_customizar gid=%s: %r", guild_id, e)
            return

        # Registra como sessão 'c' (sem trigger msg, só painel) — assim o
        # próximo 'c' ou botão Apagar fecha esse painel também.
        cfg = self._get_config(guild_id)
        cfg["active_c_trigger"] = {"channel_id": 0, "message_id": 0}
        cfg["active_c_panel"] = {
            "channel_id": int(sent.channel.id),
            "message_id": int(sent.id),
        }
        await self._save_config(guild_id, cfg)


async def setup(bot: commands.Bot):
    await bot.add_cog(FormsCog(bot))
