"""Modal guiado de preferências pessoais do Piper no Termux."""
from __future__ import annotations

import math
import discord

from .componentes import (
    adicionar_item_rotulo_modal, adicionar_radio_modal,
    criar_seletor_modal, opcoes_com_valor_padrao, rotulo_modal_disponivel,
    valor_tts_atual, valor_unico_componente,
)
from .operacoes_painel import salvar_atualizacoes_modal_tts

# O Phone Worker da implantação atual só disponibiliza este modelo validado.
VOZES_PIPER = {"pt_BR-faber-medium": "Faber Medium (PT-BR)"}
VELOCIDADES = {"0.85", "1.0", "1.15"}
TONS = {"-2", "0", "2"}


def _numero(raw: object, minimum: float, maximum: float) -> str | None:
    try:
        value = float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        return None
    return f"{value:.2f}".rstrip("0").rstrip(".") if value else "0"


class ModalConfiguracaoPiper(discord.ui.Modal, title="Personalizar Piper"):
    def __init__(self, cog, panel_message, *, server=False, target_user_id=None, target_user_name=None):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = bool(server)
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        user_id = int(target_user_id or 0)
        guild_id = int(getattr(getattr(panel_message, "guild", None), "id", 0) or 0)
        self.voice_saved = str(valor_tts_atual(cog, guild_id, user_id, "piper_voice", "pt_BR-faber-medium", server=server) or "pt_BR-faber-medium")
        self.speed_saved = str(valor_tts_atual(cog, guild_id, user_id, "piper_speed", "1.0", server=server) or "1.0")
        self.pitch_saved = str(valor_tts_atual(cog, guild_id, user_id, "piper_pitch", "0", server=server) or "0")
        # Sem fallback textual: se a biblioteca Discord não suportar os
        # componentes guiados, o chamador informa que o modal está indisponível.
        if not rotulo_modal_disponivel():
            raise RuntimeError("Componentes de modal guiado indisponíveis")
        voice = criar_seletor_modal("piper_voice", placeholder="Voz instalada no Termux", options=opcoes_com_valor_padrao([
            discord.SelectOption(label=label, value=value)
            for value, label in VOZES_PIPER.items()
        ], self.voice_saved))
        if not adicionar_item_rotulo_modal(self, "voice", text="Voz Piper", component=voice):
            raise RuntimeError("Seletor de voz Piper indisponível")
        if not adicionar_radio_modal(self, "speed", text="Velocidade", description="", current=self.speed_saved if self.speed_saved in VELOCIDADES else "1.0", options=[
            ("Mais lenta", "0.85", "85%"), ("Normal", "1.0", "100%"),
            ("Mais rápida", "1.15", "115%"),
        ]):
            raise RuntimeError("Controle de velocidade Piper indisponível")
        if not adicionar_radio_modal(self, "pitch", text="Tom da voz", description="", current=self.pitch_saved if self.pitch_saved in TONS else "0", options=[
            ("Mais grave", "-2", "-2 semitons"), ("Normal", "0", "Sem ajuste"),
            ("Mais agudo", "2", "+2 semitons"),
        ]):
            raise RuntimeError("Controle de tom Piper indisponível")

    async def on_submit(self, interaction: discord.Interaction):
        voice = valor_unico_componente(self.voice, self.voice_saved)
        if voice not in VOZES_PIPER:
            await interaction.response.send_message("Voz Piper indisponível.", ephemeral=True)
            return
        speed = valor_unico_componente(self.speed, self.speed_saved)
        pitch = valor_unico_componente(self.pitch, self.pitch_saved)
        if speed not in VELOCIDADES or pitch not in TONS:
            await interaction.response.send_message("Seleção Piper inválida.", ephemeral=True)
            return
        speed = _numero(speed, 0.7, 1.5)
        pitch = _numero(pitch, -4, 4)
        updates = {}
        if voice != self.voice_saved:
            updates["piper_voice"] = voice
        if speed != (_numero(self.speed_saved, 0.7, 1.5) or "1"):
            updates["piper_speed"] = speed
        if pitch != (_numero(self.pitch_saved, -4, 4) or "0"):
            updates["piper_pitch"] = pitch
        await salvar_atualizacoes_modal_tts(
            self.cog, interaction, source_panel_message=self.panel_message,
            server=self.server, updates=updates, success_title="Piper atualizado",
            success_description=f"{VOZES_PIPER[voice]} · Velocidade {float(speed)*100:g}% · Tom {float(pitch):+g}st",
            target_user_id=self.target_user_id, target_user_name=self.target_user_name,
        )
