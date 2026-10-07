"""Visões base e expiração dos painéis do TTS.

Concentra comportamento comum de autorização, expiração e envio de seletores
simples sem conhecer áudio, síntese, Worker, Termux ou APK.
"""
from __future__ import annotations

import time

import discord

import config


DURACAO_EXPIRACAO_PAINEL_TTS = 180.0
DURACAO_DESPACHO_PAINEL_TTS = 86400.0
EMOJI_PAINEL_TTS_EXPIRADO = "<:osaka:1539137127852539944>"


def dica_comando_painel_expirado(tipo_painel: str) -> str:
    prefixo = str(getattr(config, "BOT_PREFIX", getattr(config, "PREFIX", "_")) or "_")
    comando = {
        "launcher": "tts",
        "user": "tts",
        "server": "panel_server",
        "toggle": "toggle_panel",
    }.get(str(tipo_painel or "user"), "tts")
    return f"`{prefixo}{comando}`"


def mensagem_painel_expirado(tipo_painel: str) -> str:
    return (
        f"{EMOJI_PAINEL_TTS_EXPIRADO}| Essa interação expirou, você terá que usar o comando "
        f"{dica_comando_painel_expirado(tipo_painel)} novamente para usar esse botão"
    )


class VisaoBaseTTS(discord.ui.View):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        timeout: float = 180,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        duracao_solicitada = max(1.0, float(timeout or DURACAO_EXPIRACAO_PAINEL_TTS))
        duracao_despacho = max(duracao_solicitada, DURACAO_DESPACHO_PAINEL_TTS)
        super().__init__(timeout=duracao_despacho)
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.message: discord.Message | None = None
        self.panel_kind: str = "user"
        self.target_user_id: int | None = target_user_id
        self.target_user_name: str | None = target_user_name
        self.expires_at_monotonic = time.monotonic() + duracao_solicitada

    def _is_expired(self) -> bool:
        return time.monotonic() >= self.expires_at_monotonic

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._is_expired():
            try:
                message = await self.cog._build_expired_panel_message(self.guild_id, self.panel_kind)
            except Exception:
                message = mensagem_painel_expirado(self.panel_kind)
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return False

        if self.owner_id == 0:
            return True
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=self.cog._make_embed(
                    "Painel bloqueado",
                    "Só quem abriu esse painel pode usar esses botões e menus.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        print(
            f"[tts_panel_error] user={getattr(interaction.user, 'id', None)} "
            f"guild={getattr(interaction.guild, 'id', None)} "
            f"item={getattr(item, 'custom_id', None) or getattr(item, 'label', None) or type(item).__name__} "
            f"error={repr(error)}"
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    embed=self.cog._make_embed(
                        "Erro no painel",
                        "Essa interação falhou. Abra o painel novamente.",
                        ok=False,
                    ),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    embed=self.cog._make_embed(
                        "Erro no painel",
                        "Essa interação falhou. Abra o painel novamente.",
                        ok=False,
                    ),
                    ephemeral=True,
                )
        except Exception as e:
            print(f"[tts_panel_error] falha ao responder erro: {e!r}")

    async def on_timeout(self) -> None:
        pass


class VisaoSelecaoSimples(VisaoBaseTTS):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        title: str,
        description: str,
        select: discord.ui.Select,
        *,
        timeout: float = 180,
        source_panel_message: discord.Message | None = None,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        super().__init__(
            cog,
            owner_id,
            guild_id,
            timeout=timeout,
            target_user_id=target_user_id,
            target_user_name=target_user_name,
        )
        self.title = title
        self.description = description
        self.source_panel_message: discord.Message | None = source_panel_message
        try:
            select.guild_id = guild_id
            select.owner_id = owner_id
            select.target_user_id = target_user_id
            select.target_user_name = target_user_name
        except Exception:
            pass
        self.add_item(select)

    async def send(self, interaction: discord.Interaction):
        if self.source_panel_message is None:
            self.source_panel_message = getattr(interaction, "message", None)
        embed = self.cog._make_embed(self.title, self.description, ok=True)
        if interaction.response.is_done():
            msg = await interaction.followup.send(embed=embed, view=self, ephemeral=True, wait=True)
        else:
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)
            try:
                msg = await interaction.original_response()
            except Exception:
                msg = None
        self.message = msg
