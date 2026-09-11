from __future__ import annotations

import re
import traceback

import discord

import config
from ..prefix import validate_prefix_values
from .componentes import (
    adicionar_entrada_texto_modal,
    adicionar_item_rotulo_modal,
    criar_entrada_texto_modal,
    criar_grupo_checkbox_modal,
    criar_seletor_cargo_modal,
    primeiro_cargo_selecionado,
    rotulo_modal_disponivel,
    valor_item,
    valores_selecionados,
)
from .operacoes_painel import salvar_atualizacoes_modal_tts


class ModalPrefixosServidor(discord.ui.Modal, title="Prefixos do servidor"):
    bot_prefix = discord.ui.TextInput(label="Prefixo do bot", placeholder="Ex.: _", required=True, max_length=8)
    atts_prefix = discord.ui.TextInput(label="Prefixo do ATTS", placeholder="Ex.: %", required=True, max_length=8)
    teto_prefix = discord.ui.TextInput(label="Prefixo da Kasane Teto", placeholder="Ex.: '", required=True, max_length=8)
    gtts_prefix = discord.ui.TextInput(label="Prefixo do gTTS", placeholder="Ex.: .", required=True, max_length=8)
    edge_prefix = discord.ui.TextInput(label="Prefixo do Edge", placeholder="Ex.: ,", required=True, max_length=8)

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        try:
            db = cog._get_db()
            defaults = db.get_guild_tts_defaults(guild_id) if db is not None and guild_id else {}
        except Exception:
            defaults = {}
        bot_prefix = str((defaults or {}).get("bot_prefix") or getattr(config, "PREFIX", "_") or "_")[:8]
        atts_prefix = str((defaults or {}).get("atts_prefix") or getattr(config, "TTS_ATTS_PREFIX", "%") or "%")[:8]
        teto_prefix = str((defaults or {}).get("teto_prefix") or getattr(config, "TTS_TETO_PREFIX", "'") or "'")[:8]
        edge_prefix = str((defaults or {}).get("edge_prefix") or getattr(config, "EDGE_TTS_PREFIX", ",") or ",")[:8]
        gtts_prefix = str((defaults or {}).get("gtts_prefix") or (defaults or {}).get("tts_prefix") or getattr(config, "TTS_PREFIX", ".") or ".")[:8]
        # Guilds antigas podiam herdar o mesmo `tts_prefix` para gTTS e Edge.
        # O modal já abre com um valor migrável, sem exigir que a staff descubra
        # manualmente por que uma configuração histórica ficou inválida.
        occupied = {bot_prefix, atts_prefix, teto_prefix, edge_prefix}
        if not gtts_prefix or gtts_prefix in occupied:
            gtts_prefix = next((candidate for candidate in (".", "!", ";", "~", "?") if candidate not in occupied), ".")
        self.bot_prefix.default = bot_prefix
        self.atts_prefix.default = atts_prefix
        self.teto_prefix.default = teto_prefix
        self.gtts_prefix.default = gtts_prefix
        self.edge_prefix.default = edge_prefix

    async def on_submit(self, interaction: discord.Interaction):
        values = {
            "bot_prefix": valor_item(self.bot_prefix)[:8],
            "atts_prefix": valor_item(self.atts_prefix)[:8],
            "teto_prefix": valor_item(self.teto_prefix)[:8],
            "gtts_prefix": valor_item(self.gtts_prefix)[:8],
            "edge_prefix": valor_item(self.edge_prefix)[:8],
        }
        valid, validation_error = validate_prefix_values(**values)
        if not valid:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Prefixo inválido", validation_error, ok=False),
                ephemeral=True,
            )
            return
        updates = dict(values)
        updates["tts_prefix"] = values["gtts_prefix"]
        parts = [
            f"bot: {values['bot_prefix']}",
            f"ATTS: {values['atts_prefix']}",
            f"Teto: {values['teto_prefix']}",
            f"gTTS: {values['gtts_prefix']}",
            f"Edge: {values['edge_prefix']}",
        ]
        await salvar_atualizacoes_modal_tts(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=True,
            updates=updates,
            success_title="Prefixos atualizados",
            success_description="Salvo: " + ", ".join(parts) + ".",
        )


class ModalRegrasServidorTTS(discord.ui.Modal, title="Regras do TTS"):
    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None, *, force_text_fallback: bool = False):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.force_text_fallback = bool(force_text_fallback)
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        try:
            db = cog._get_db()
            defaults = db.get_guild_tts_defaults(guild_id) if db is not None and guild_id else {}
        except Exception:
            defaults = {}
        self.current_announce_author = bool((defaults or {}).get("announce_author"))
        role_id = int((defaults or {}).get("ignored_tts_role_id") or 0)
        self.current_ignored_role_id = role_id
        if "ignored_tts_role_enabled" in (defaults or {}):
            self.current_ignored_role_enabled = bool((defaults or {}).get("ignored_tts_role_enabled", False))
        else:
            # Migração suave: se já havia cargo salvo antes da flag, ele começa ativo.
            self.current_ignored_role_enabled = bool(role_id)
        self.current_ignored_role = str(role_id) if role_id else ""
        self.current_ignored_role_name = ""
        self.current_ignored_role_obj = None
        try:
            guild = getattr(panel_message, "guild", None)
            role = guild.get_role(role_id) if guild is not None and role_id else None
            self.current_ignored_role_obj = role
            self.current_ignored_role_name = str(getattr(role, "name", "") or "")
        except Exception:
            self.current_ignored_role_name = ""
            self.current_ignored_role_obj = None
        if self.force_text_fallback or not self._build_guided_modal():
            self._build_text_fallback()

    def _build_guided_modal(self) -> bool:
        if not rotulo_modal_disponivel():
            return False
        try:
            rules = criar_grupo_checkbox_modal(
                "tts_rules",
                options=[
                    (
                        "Autor antes da frase",
                        "announce_author",
                        "Fala o nome de quem mandou.",
                        self.current_announce_author,
                    ),
                    (
                        "Cargo ignorado",
                        "ignored_role_enabled",
                        "",
                        self.current_ignored_role_enabled,
                    ),
                ],
                min_values=0,
                max_values=2,
            )
            if rules is None:
                return False
            ok = adicionar_item_rotulo_modal(
                self,
                "rules",
                text="Regras",
                component=rules,
            )

            role_select = criar_seletor_cargo_modal(
                "ignored_role",
                placeholder="Escolha o cargo ignorado",
                required=False,
                default_role=self.current_ignored_role_obj,
            )
            if role_select is not None:
                ok = ok and adicionar_item_rotulo_modal(
                    self,
                    "ignored_role",
                    text="Cargo ignorado",
                    component=role_select,
                )
            else:
                role_input = criar_entrada_texto_modal(
                    label=None,
                    placeholder="ID/nome. Vazio mantém; off desliga sem apagar.",
                    current=self.current_ignored_role,
                    max_length=80,
                    required=False,
                )
                ok = ok and adicionar_item_rotulo_modal(
                    self,
                    "ignored_role",
                    text="Cargo ignorado",
                    component=role_input,
                )
            return bool(ok)
        except Exception as e:
            print(f"[tts_modal] regras guiadas falharam: {e!r}")
            traceback.print_exception(type(e), e, e.__traceback__)
            try:
                self.clear_items()
            except Exception:
                pass
            return False

    def _build_text_fallback(self) -> None:
        adicionar_entrada_texto_modal(
            self,
            "announce_author",
            label="Autor antes da frase",
            placeholder="sim para ligar; não para desligar",
            current="sim" if self.current_announce_author else "não",
            max_length=8,
        )
        adicionar_entrada_texto_modal(
            self,
            "ignored_role_enabled",
            label="Cargo ignorado ativo",
            placeholder="sim para ligar; não para desligar",
            current="sim" if self.current_ignored_role_enabled else "não",
            max_length=8,
            required=False,
        )
        adicionar_entrada_texto_modal(
            self,
            "ignored_role",
            label="Cargo ignorado",
            placeholder="ID/nome. Vazio mantém; off desliga sem apagar.",
            current=self.current_ignored_role,
            max_length=80,
            required=False,
        )

    def _find_role(self, guild: discord.Guild | None, raw: str) -> discord.Role | None:
        if guild is None:
            return None
        value = str(raw or "").strip()
        if not value:
            return None
        match = re.fullmatch(r"<@&(\d+)>", value)
        role_id = int(match.group(1)) if match else int(value) if value.isdigit() else 0
        if role_id:
            return guild.get_role(role_id)
        lowered = value.casefold()
        for role in getattr(guild, "roles", []) or []:
            if str(getattr(role, "name", "")).casefold() == lowered:
                return role
        return None

    async def on_submit(self, interaction: discord.Interaction):
        updates: dict[str, object] = {}
        parts: list[str] = []

        if hasattr(self, "rules"):
            values = set(valores_selecionados(getattr(self, "rules", None)))
            enabled = "announce_author" in values
            ignored_enabled = "ignored_role_enabled" in values
            if enabled != self.current_announce_author:
                updates["announce_author"] = enabled
                parts.append("autor antes da frase ligado" if enabled else "autor antes da frase desligado")
            if ignored_enabled != self.current_ignored_role_enabled:
                updates["ignored_tts_role_enabled"] = ignored_enabled
                parts.append("cargo ignorado ligado" if ignored_enabled else "cargo ignorado desligado")
        else:
            text = valor_item(getattr(self, "announce_author", None)).lower()
            if text:
                enabled = text in {"sim", "s", "yes", "y", "true", "1", "on", "ativo", "ativado", "ligado"}
                if enabled != self.current_announce_author:
                    updates["announce_author"] = enabled
                    parts.append("autor antes da frase ligado" if enabled else "autor antes da frase desligado")
            role_enabled_text = valor_item(getattr(self, "ignored_role_enabled", None)).lower()
            if role_enabled_text:
                ignored_enabled = role_enabled_text in {"sim", "s", "yes", "y", "true", "1", "on", "ativo", "ativado", "ligado"}
                if ignored_enabled != self.current_ignored_role_enabled:
                    updates["ignored_tts_role_enabled"] = ignored_enabled
                    parts.append("cargo ignorado ligado" if ignored_enabled else "cargo ignorado desligado")

        def marcar_cargo_ignorado_alterado(role: discord.Role) -> None:
            role_id = int(getattr(role, "id", 0) or 0)
            if not role_id:
                return
            role_text = getattr(role, "mention", None) or getattr(role, "name", None) or "cargo"
            if role_id != self.current_ignored_role_id:
                updates["ignored_tts_role_id"] = role_id
                parts.append(f"cargo ignorado {role_text}")
                # Trocar/definir cargo liga a regra automaticamente. Isso não
                # deve acontecer apenas por causa do cargo salvo vindo como
                # default_values no RoleSelect.
                updates["ignored_tts_role_enabled"] = True
                parts[:] = [part for part in parts if part != "cargo ignorado desligado"]
                if "cargo ignorado ligado" not in parts:
                    parts.append("cargo ignorado ligado")

        selected_role = primeiro_cargo_selecionado(getattr(self, "ignored_role", None))
        if selected_role is not None:
            marcar_cargo_ignorado_alterado(selected_role)
        else:
            raw_role = valor_item(getattr(self, "ignored_role", None))
            if raw_role:
                raw_lower = raw_role.strip().lower()
                if raw_lower in {"0", "nenhum", "remover", "remove", "off", "desativar", "desligar", "não", "nao"}:
                    if self.current_ignored_role_enabled:
                        updates["ignored_tts_role_enabled"] = False
                        parts.append("cargo ignorado desligado")
                else:
                    role = self._find_role(getattr(interaction, "guild", None), raw_role)
                    if role is None:
                        await interaction.response.send_message(
                            embed=self.cog._make_embed("Cargo não encontrado", "Use menção, ID ou nome exato do cargo.", ok=False),
                            ephemeral=True,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        return
                    marcar_cargo_ignorado_alterado(role)

        if updates.get("ignored_tts_role_enabled") is True and not int(updates.get("ignored_tts_role_id") or self.current_ignored_role_id or 0):
            await interaction.response.send_message(
                embed=self.cog._make_embed("Cargo obrigatório", "Escolha um cargo antes de ligar o cargo ignorado.", ok=False),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await salvar_atualizacoes_modal_tts(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=True,
            updates=updates,
            success_title="Regras atualizadas",
            success_description="\n".join(f"• {part}" for part in parts) if parts else "Nada mudou.",
        )
