"""Components V2 para os efeitos DSP pessoais do TTS Edge/gTTS."""
from __future__ import annotations

import contextlib

import discord

from .visoes_layout import VisaoLayoutBaseTTS


_ELIGIBLE_ENGINES = frozenset(("edge", "gtts"))
_LEVEL_LABELS = ("Desligado", "Nível 1 · Leve", "Nível 2 · Médio", "Nível 3 · Forte")
_SUMMARY = {
    "nightcore": ("Desligado", "Nível 1 · Leve · +10%", "Nível 2 · Médio · +20%", "Nível 3 · Forte · +30%"),
    "slowed": ("Desligado", "Nível 1 · Leve · −8%", "Nível 2 · Médio · −16%", "Nível 3 · Forte · −24%"),
    "reverb": ("Desligado", "Nível 1 · Leve", "Nível 2 · Médio", "Nível 3 · Forte"),
}
_RADIO_DESCRIPTIONS = {
    "nightcore": (None, "Velocidade e tom +10%.", "Velocidade e tom +20%.", "Velocidade e tom +30%."),
    "slowed": (None, "Velocidade e tom −8%.", "Velocidade e tom −16%.", "Velocidade e tom −24%."),
}


def _level(value: object) -> int:
    try:
        return max(0, min(3, int(value or 0)))
    except (TypeError, ValueError):
        return 0


async def _silent_defer(interaction: discord.Interaction) -> None:
    with contextlib.suppress(Exception):
        if not interaction.response.is_done():
            await interaction.response.defer()


def _resolve_exclusive_levels(
    original_nightcore: int,
    original_slowed: int,
    nightcore: int,
    slowed: int,
) -> tuple[int, int]:
    """Resolve conflito sem mensagem: o efeito alterado prevalece; empate = Slowed."""
    nightcore = _level(nightcore)
    slowed = _level(slowed)
    if not (nightcore and slowed):
        return nightcore, slowed

    nightcore_changed = nightcore != original_nightcore
    slowed_changed = slowed != original_slowed
    if nightcore_changed and not slowed_changed:
        return nightcore, 0
    return 0, slowed


def _radio_group(effect: str, current: int) -> discord.ui.RadioGroup:
    group = discord.ui.RadioGroup(custom_id=f"tts_advanced_{effect}", required=True)
    descriptions = _RADIO_DESCRIPTIONS.get(effect)
    for level, label in enumerate(_LEVEL_LABELS):
        kwargs = {
            "label": label,
            "value": str(level),
            "default": current == level,
        }
        if descriptions and descriptions[level]:
            kwargs["description"] = descriptions[level]
        group.add_option(**kwargs)
    return group


class ModalEfeitosAvancadosTTS(discord.ui.Modal, title="Editar efeitos do TTS"):
    def __init__(self, panel: "VisaoEfeitosAvancadosTTS"):
        super().__init__(timeout=300.0)
        self.panel = panel
        night, slowed, reverb = panel._levels()
        self.original_nightcore = night
        self.original_slowed = slowed

        self.nightcore = _radio_group("nightcore", night)
        self.slowed = _radio_group("slowed", slowed)
        self.reverb = _radio_group("reverb", reverb)

        for text, description, component in (
            ("Nightcore", "Aumenta velocidade e tom. Não pode ser combinado com Slowed.", self.nightcore),
            ("Slowed", "Reduz velocidade e tom. Não pode ser combinado com Nightcore.", self.slowed),
            ("Reverb", "Adiciona ambiência e pode ser combinado com qualquer um dos dois.", self.reverb),
        ):
            self.add_item(discord.ui.Label(text=text, description=description, component=component))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        panel = self.panel
        if panel._is_expired() or interaction.user.id != panel.owner_id:
            await _silent_defer(interaction)
            return

        night, slowed = _resolve_exclusive_levels(
            self.original_nightcore,
            self.original_slowed,
            _level(self.nightcore.value),
            _level(self.slowed.value),
        )
        reverb = _level(self.reverb.value)

        await panel.cog._set_user_tts_and_refresh(
            panel.guild_id,
            panel.owner_id,
            advanced_nightcore_level=night,
            advanced_slowed_level=slowed,
            advanced_reverb_level=reverb,
        )
        panel._apply_levels(night, slowed, reverb)
        await interaction.response.edit_message(view=panel)


class VisaoEfeitosAvancadosTTS(VisaoLayoutBaseTTS):
    """Configura DSP por usuário sem compartilhar estado com os efeitos da música."""

    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        resolved: dict | None = None,
    ):
        super().__init__(cog, owner_id, guild_id)
        self.resolved = dict(resolved or {})
        self._rebuild_items()

    @property
    def engine(self) -> str:
        return str(self.resolved.get("engine") or "gtts").strip().lower().replace("-", "_")

    def _engine_is_eligible(self) -> bool:
        return self.engine in _ELIGIBLE_ENGINES

    def _levels(self) -> tuple[int, int, int]:
        return (
            _level(self.resolved.get("advanced_nightcore_level")),
            _level(self.resolved.get("advanced_slowed_level")),
            _level(self.resolved.get("advanced_reverb_level")),
        )

    def _summary_text(self) -> str:
        night, slowed, reverb = self._levels()
        return (
            "# TTS avançado\n\n"
            f"⚡ **Nightcore**\n-# {_SUMMARY['nightcore'][night]}\n\n"
            f"🐌 **Slowed**\n-# {_SUMMARY['slowed'][slowed]}\n\n"
            f"🌊 **Reverb**\n-# {_SUMMARY['reverb'][reverb]}"
        )

    def _rebuild_items(self) -> None:
        self.clear_items()
        levels = self._levels()
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
            disabled=not any(levels),
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

    def _apply_levels(self, nightcore: int, slowed: int, reverb: int) -> None:
        self.resolved.update(
            advanced_nightcore_level=nightcore,
            advanced_slowed_level=slowed,
            advanced_reverb_level=reverb,
        )
        self._rebuild_items()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._is_expired() or interaction.user.id != self.owner_id:
            await _silent_defer(interaction)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        print(
            f"[tts_panel_error] user={getattr(interaction.user, 'id', None)} "
            f"guild={getattr(interaction.guild, 'id', None)} "
            f"item={getattr(item, 'custom_id', None) or getattr(item, 'label', None) or type(item).__name__} "
            f"error={error!r}"
        )
        await _silent_defer(interaction)

    async def _load_resolved(self) -> None:
        db = self.cog._get_db()
        if db is None:
            raise RuntimeError("settings db unavailable")
        self.resolved = dict(await self.cog._maybe_await(db.resolve_tts(self.guild_id, self.owner_id)) or {})

    async def _open_modal(self, interaction: discord.Interaction) -> None:
        await self._load_resolved()
        if not self._engine_is_eligible():
            await _silent_defer(interaction)
            return
        await interaction.response.send_modal(ModalEfeitosAvancadosTTS(self))

    async def _disable_all(self, interaction: discord.Interaction) -> None:
        await self.cog._set_user_tts_and_refresh(
            self.guild_id,
            self.owner_id,
            advanced_nightcore_level=0,
            advanced_slowed_level=0,
            advanced_reverb_level=0,
        )
        self._apply_levels(0, 0, 0)
        await interaction.response.edit_message(view=self)

    async def send_for_message(self, message: discord.Message) -> discord.Message:
        return await message.channel.send(view=self)
