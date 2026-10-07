"""Painel Components V2 dos efeitos DSP avançados do TTS Edge/gTTS."""
from __future__ import annotations

import contextlib
import time

import discord

from .visoes_base import (
    DURACAO_DESPACHO_PAINEL_TTS,
    DURACAO_EXPIRACAO_PAINEL_TTS,
    mensagem_painel_expirado,
)



def _level(value: object) -> int:
    try:
        return max(0, min(3, int(value or 0)))
    except (TypeError, ValueError):
        return 0


def _engine_name(value: object) -> str:
    engine = str(value or "gtts").strip().lower().replace("-", "_")
    aliases = {
        "edge_tts": "edge",
        "microsoft": "edge",
        "microsoft_edge": "edge",
        "google": "gtts",
        "google_tts": "gtts",
    }
    return aliases.get(engine, engine)


def _engine_label(engine: str) -> str:
    if engine == "edge":
        return "Edge"
    if engine == "gtts":
        return "gTTS"
    return engine


def _effect_detail(effect: str, level: int) -> str:
    if level <= 0:
        return "Desligado"
    if effect == "nightcore":
        return (
            "Nível 1 · Leve · +10%",
            "Nível 2 · Médio · +20%",
            "Nível 3 · Forte · +30%",
        )[level - 1]
    if effect == "slowed":
        return (
            "Nível 1 · Leve · −8%",
            "Nível 2 · Médio · −16%",
            "Nível 3 · Forte · −24%",
        )[level - 1]
    return (
        "Nível 1 · Leve",
        "Nível 2 · Médio",
        "Nível 3 · Forte",
    )[level - 1]


def _radio_group(effect: str, current: int) -> discord.ui.RadioGroup:
    group = discord.ui.RadioGroup(custom_id=f"tts_advanced_{effect}", required=True)
    group.add_option(
        label="Desligado",
        value="0",
        description="Não aplica este efeito ao TTS.",
        default=current == 0,
    )

    if effect == "nightcore":
        descriptions = (
            "Velocidade e tom +10%.",
            "Velocidade e tom +20%.",
            "Velocidade e tom +30%.",
        )
    elif effect == "slowed":
        descriptions = (
            "Velocidade e tom −8%.",
            "Velocidade e tom −16%.",
            "Velocidade e tom −24%.",
        )
    else:
        descriptions = (
            "Ambiência curta e discreta.",
            "Ambiência média e perceptível.",
            "Ambiência forte com cauda controlada.",
        )

    for level, description in enumerate(descriptions, start=1):
        strength = ("Leve", "Médio", "Forte")[level - 1]
        group.add_option(
            label=f"Nível {level} · {strength}",
            value=str(level),
            description=description,
            default=current == level,
        )
    return group


class ModalEfeitosAvancadosTTS(discord.ui.Modal, title="Editar efeitos do TTS"):
    """Edita todos os efeitos em um único formulário nativo."""

    def __init__(self, panel: "VisaoEfeitosAvancadosTTS"):
        super().__init__(timeout=300.0)
        self.panel = panel
        night, slowed, reverb = panel._levels()

        self.nightcore = _radio_group("nightcore", night)
        self.slowed = _radio_group("slowed", slowed)
        self.reverb = _radio_group("reverb", reverb)

        self.add_item(
            discord.ui.Label(
                text="Nightcore",
                description="Aumenta velocidade e tom. Não pode ser combinado com Slowed.",
                component=self.nightcore,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Slowed",
                description="Reduz velocidade e tom. Não pode ser combinado com Nightcore.",
                component=self.slowed,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Reverb",
                description="Adiciona ambiência e pode ser combinado com qualquer um dos dois.",
                component=self.reverb,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.panel.owner_id:
            await interaction.response.send_message(
                "Esse painel pertence a quem abriu o comando.",
                ephemeral=True,
            )
            return

        await self.panel._reload()
        if not self.panel._engine_is_eligible():
            await interaction.response.send_message(
                "Sua engine atual não é Edge nem gTTS. Nenhuma configuração foi alterada.",
                ephemeral=True,
            )
            return

        try:
            night = _level(self.nightcore.value)
            slowed = _level(self.slowed.value)
            reverb = _level(self.reverb.value)
        except (TypeError, ValueError):
            await interaction.response.send_message(
                "Não consegui ler os níveis escolhidos. Abra o painel novamente.",
                ephemeral=True,
            )
            return

        if night and slowed:
            await interaction.response.send_message(
                "Nightcore e Slowed são exclusivos. Deixe um deles como **Desligado** e tente novamente.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.panel.cog._set_user_tts_and_refresh(
            self.panel.guild_id,
            self.panel.owner_id,
            advanced_nightcore_level=night,
            advanced_slowed_level=slowed,
            advanced_reverb_level=reverb,
        )
        await self.panel._reload()
        await self.panel._refresh_panel_message()
        await interaction.followup.send("Efeitos atualizados.", ephemeral=True)


class VisaoEfeitosAvancadosTTS(discord.ui.LayoutView):
    """Configura DSP por usuário sem tocar no estado/mixer da música."""

    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        resolved: dict | None = None,
    ):
        duracao_solicitada = max(1.0, float(DURACAO_EXPIRACAO_PAINEL_TTS))
        duracao_despacho = max(duracao_solicitada, DURACAO_DESPACHO_PAINEL_TTS)
        super().__init__(timeout=duracao_despacho)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.guild_id = int(guild_id)
        self.message: discord.Message | None = None
        self.panel_kind = "advanced"
        self.expires_at_monotonic = time.monotonic() + duracao_solicitada
        self.resolved = dict(resolved or {})
        self._rebuild_items()

    @property
    def engine(self) -> str:
        return _engine_name(self.resolved.get("engine"))

    def _engine_is_eligible(self) -> bool:
        return self.engine in {"edge", "gtts"}

    def _levels(self) -> tuple[int, int, int]:
        return (
            _level(self.resolved.get("advanced_nightcore_level")),
            _level(self.resolved.get("advanced_slowed_level")),
            _level(self.resolved.get("advanced_reverb_level")),
        )

    def _is_expired(self) -> bool:
        return time.monotonic() >= self.expires_at_monotonic

    def _summary_text(self) -> str:
        night, slowed, reverb = self._levels()
        return (
            "# TTS avançado\n"
            f"-# {_engine_label(self.engine)} · efeitos pessoais\n\n"
            f"⚡ **Nightcore**\n-# {_effect_detail('nightcore', night)}\n\n"
            f"🐌 **Slowed**\n-# {_effect_detail('slowed', slowed)}\n\n"
            f"🌊 **Reverb**\n-# {_effect_detail('reverb', reverb)}"
        )

    def _rebuild_items(self) -> None:
        self.clear_items()
        night, slowed, reverb = self._levels()
        has_effect = any((night, slowed, reverb))

        edit = discord.ui.Button(
            label="Editar efeitos",
            emoji="🎛️",
            style=discord.ButtonStyle.primary,
            custom_id=f"tts:advanced:edit:{self.guild_id}:{self.owner_id}",
        )
        reset = discord.ui.Button(
            label="Desativar tudo",
            emoji="🔄",
            style=discord.ButtonStyle.secondary,
            disabled=not has_effect,
            custom_id=f"tts:advanced:reset:{self.guild_id}:{self.owner_id}",
        )
        edit.callback = self._open_modal
        reset.callback = self._disable_all

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(self._summary_text()),
                discord.ui.Separator(),
                discord.ui.ActionRow(edit, reset),
            )
        )

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

        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Esse painel pertence a quem abriu o comando.",
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
        with contextlib.suppress(Exception):
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Essa interação falhou. Abra o `_advanced` novamente.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Essa interação falhou. Abra o `_advanced` novamente.",
                    ephemeral=True,
                )

    async def on_timeout(self) -> None:
        pass

    async def _reload(self) -> None:
        db = self.cog._get_db()
        if db is None:
            raise RuntimeError("settings db unavailable")
        self.resolved = dict(await self.cog._maybe_await(db.resolve_tts(self.guild_id, self.owner_id)) or {})
        self._rebuild_items()

    async def _ensure_current_engine_is_eligible(self, interaction: discord.Interaction) -> bool:
        await self._reload()
        if self._engine_is_eligible():
            return True
        message = "Sua engine atual não é Edge nem gTTS. A configuração salva foi preservada."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return False

    async def _open_modal(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_current_engine_is_eligible(interaction):
            return
        await interaction.response.send_modal(ModalEfeitosAvancadosTTS(self))

    async def _disable_all(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_current_engine_is_eligible(interaction):
            return
        await self.cog._set_user_tts_and_refresh(
            self.guild_id,
            self.owner_id,
            advanced_nightcore_level=0,
            advanced_slowed_level=0,
            advanced_reverb_level=0,
        )
        await self._reload()
        await interaction.response.edit_message(view=self)

    async def _refresh_panel_message(self) -> None:
        if self.message is None:
            return
        with contextlib.suppress(Exception):
            await self.message.edit(view=self)

    async def send_for_message(self, message: discord.Message) -> discord.Message:
        sent = await message.channel.send(view=self)
        self.message = sent
        return sent

    async def send(self, interaction: discord.Interaction) -> None:
        if interaction.response.is_done():
            sent = await interaction.followup.send(view=self, ephemeral=True, wait=True)
        else:
            await interaction.response.send_message(view=self, ephemeral=True)
            sent = await interaction.original_response()
        self.message = sent
