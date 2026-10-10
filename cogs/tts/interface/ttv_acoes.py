"""Confirmação privada, amostra e restauração dos ajustes pessoais de TTV."""
from __future__ import annotations

import asyncio
import io

import discord

from ..ttv import resolve_preferences, voice_label
from .operacoes_painel import DESCRICAO_LANCADOR_TTS


def resumo_ttv(settings: dict) -> str:
    voice = voice_label(settings.get("ttv_voice_id"))
    pitch = float(settings.get("ttv_pitch_semitones", 0))
    tone = "Original" if pitch == 0 else f"{pitch:+g} semitons".replace(".", ",")
    rate = float(settings.get("ttv_speech_rate", 1.0))
    speed = "Normal" if rate == 1 else f"{rate * 100:g}%".replace(".", ",")
    return f"Voz: **{voice}** · Tom: **{tone}** · Velocidade: **{speed}**"


async def _responder(interaction: discord.Interaction, *, content: str, view=None, file=None) -> None:
    kwargs = {"content": content, "ephemeral": True, "allowed_mentions": discord.AllowedMentions.none()}
    if view is not None:
        kwargs["view"] = view
    if file is not None:
        kwargs["file"] = file
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


async def autorizar_ttv(interaction: discord.Interaction, *, owner_id: int, guild_id: int, target_user_id: int) -> bool:
    guild = getattr(interaction, "guild", None)
    actor = getattr(interaction, "user", None)
    if guild is None or (guild_id and int(guild.id) != int(guild_id)):
        await _responder(interaction, content="Esse ajuste só pode ser usado no servidor do painel.")
        return False
    actor_id = int(getattr(actor, "id", 0) or 0)
    if not actor_id or (owner_id and actor_id != int(owner_id)):
        await _responder(interaction, content="Esse formulário pertence a outro usuário. Abra seu painel de TTS.")
        return False
    if target_user_id and int(target_user_id) != actor_id and not getattr(getattr(actor, "guild_permissions", None), "kick_members", False):
        await _responder(interaction, content="Você precisa da permissão `Expulsar Membros` para configurar a voz de outro usuário.")
        return False
    return True


async def _atualizar_painel(cog, interaction, panel_message, message_id, user_id, user_name) -> None:
    if panel_message is None:
        return
    try:
        states = getattr(cog, "_public_panel_states", {})
        state = states.get(message_id or 0, {})
        if state.get("panel_kind") == "launcher":
            kwargs = {"owner_id": int(state.get("owner_id", 0) or 0), "timeout": 300}
            if state.get("target_user_id"):
                kwargs.update(target_user_id=int(state["target_user_id"]), target_user_name=state.get("target_user_name"))
            view = cog._build_public_tts_launcher_view(interaction.guild.id, **kwargs)
            embed = cog._make_embed("TTS", DESCRICAO_LANCADOR_TTS, ok=True)
        else:
            embed = await cog._build_settings_embed(interaction.guild.id, user_id, server=False, panel_kind="user", target_user_name=user_name, viewer_user_id=interaction.user.id)
            view = cog._build_panel_view(int(state.get("owner_id", interaction.user.id) or 0), interaction.guild.id, server=False, target_user_id=user_id, target_user_name=user_name)
        view.message = panel_message
        await cog._edit_panel_message_payload(panel_message, embed=embed, view=view)
    except Exception as error:
        # Os ajustes estão salvos; uma mensagem removida não deve desfazer a gravação.
        print(f"[ttv_panel] falha ao atualizar resumo: {error!r}")


async def salvar_configuracao_ttv(
    cog, interaction: discord.Interaction, *, source_panel_message, settings: dict,
    target_user_id: int | None = None, target_user_name: str | None = None,
    owner_id: int = 0, guild_id: int = 0, success_title: str = "TTV atualizado",
) -> None:
    actor_id = int(getattr(getattr(interaction, "user", None), "id", 0) or 0)
    if not await autorizar_ttv(interaction, owner_id=owner_id, guild_id=guild_id, target_user_id=int(target_user_id or actor_id)):
        return
    db = cog._get_db()
    if db is None:
        await _responder(interaction, content="Não consegui acessar os ajustes agora. Tente novamente.")
        return
    settings = resolve_preferences(settings)
    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    user_id, user_name, _ = cog._resolve_panel_target_user(interaction, server=False, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
    if not await autorizar_ttv(interaction, owner_id=owner_id, guild_id=guild_id, target_user_id=int(user_id)):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    await cog._set_user_tts_and_refresh(interaction.guild.id, user_id, **settings)
    await _atualizar_painel(cog, interaction, panel_message, message_id, user_id, user_name)
    view = VisaoConfirmacaoTTV(cog, actor_id, interaction.guild.id, user_id, user_name, panel_message)
    await _responder(interaction, content=f"**{success_title}**\n{resumo_ttv(settings)}", view=view)


class VisaoConfirmacaoTTV(discord.ui.View):
    def __init__(self, cog, owner_id: int, guild_id: int, target_user_id: int, target_user_name: str | None, panel_message):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.guild_id = int(guild_id)
        self.target_user_id = int(target_user_id)
        self.target_user_name = target_user_name
        self.panel_message = panel_message
        self._preview_running = False
        preview = discord.ui.Button(label="Ouvir amostra", style=discord.ButtonStyle.primary, custom_id="ttv:preview")
        reset = discord.ui.Button(label="Restaurar padrões", style=discord.ButtonStyle.secondary, custom_id="ttv:reset")
        preview.callback = self.ouvir_amostra
        reset.callback = self.restaurar_padroes
        self.add_item(preview)
        self.add_item(reset)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await autorizar_ttv(interaction, owner_id=self.owner_id, guild_id=self.guild_id, target_user_id=self.target_user_id)

    def _settings(self) -> dict:
        db = self.cog._get_db()
        if db is None:
            raise RuntimeError("Os ajustes estão indisponíveis agora.")
        settings = db.resolve_tts(self.guild_id, self.target_user_id)
        return resolve_preferences(settings)

    async def ouvir_amostra(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        if self._preview_running:
            await _responder(interaction, content="A amostra anterior ainda está sendo gerada.")
            return
        self._preview_running = True
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            settings = self._settings()
            mp3 = await asyncio.wait_for(self.cog._ttv_preview_mp3(self.guild_id, self.target_user_id, settings=settings), timeout=130)
            if not isinstance(mp3, bytes) or not mp3:
                raise RuntimeError("O motor não devolveu a amostra de áudio.")
            await _responder(interaction, content=f"**Amostra de TTV**\n{resumo_ttv(settings)}", file=discord.File(io.BytesIO(mp3), filename="ttv-amostra.mp3"))
        except (RuntimeError, ValueError) as error:
            await _responder(interaction, content=f"Não consegui gerar a amostra. {error}")
        except asyncio.TimeoutError:
            await _responder(interaction, content="A amostra demorou demais. Tente novamente quando o dispositivo de síntese estiver disponível.")
        except Exception as error:
            print(f"[ttv_preview] geração falhou: {error!r}")
            await _responder(interaction, content="Não consegui gerar a amostra agora. Seus ajustes continuam salvos.")
        finally:
            self._preview_running = False

    async def restaurar_padroes(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        try:
            settings = self._settings()
            settings.update(ttv_pitch_semitones="+0.0", ttv_speech_rate=1.0)
            await salvar_configuracao_ttv(self.cog, interaction, source_panel_message=self.panel_message, settings=settings, target_user_id=self.target_user_id, target_user_name=self.target_user_name, owner_id=self.owner_id, guild_id=self.guild_id, success_title="Padrões de TTV restaurados")
        except (ValueError, RuntimeError) as error:
            await _responder(interaction, content=f"Não consegui restaurar os padrões. {error}")
