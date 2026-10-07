"""Painel pessoal dos efeitos DSP avançados do TTS Edge/gTTS."""
from __future__ import annotations

import discord

from .visoes_base import VisaoBaseTTS


_EFFECT_KEYS = {
    "nightcore": "advanced_nightcore_level",
    "slowed": "advanced_slowed_level",
    "reverb": "advanced_reverb_level",
}


def _level(value: object) -> int:
    try:
        return max(0, min(3, int(value or 0)))
    except (TypeError, ValueError):
        return 0


def _level_text(level: int) -> str:
    return "Desligado" if level <= 0 else f"Nível {level}"


class VisaoEfeitosAvancadosTTS(VisaoBaseTTS):
    """Configura DSP por usuário sem tocar no estado/mixer da música."""

    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, resolved: dict | None = None):
        super().__init__(cog, owner_id, guild_id, timeout=180)
        self.panel_kind = "advanced"
        self.resolved = dict(resolved or {})
        self._rebuild_items()

    @property
    def engine(self) -> str:
        return str(self.resolved.get("engine") or "gtts").strip().lower().replace("-", "_")

    def _levels(self) -> tuple[int, int, int]:
        return (
            _level(self.resolved.get("advanced_nightcore_level")),
            _level(self.resolved.get("advanced_slowed_level")),
            _level(self.resolved.get("advanced_reverb_level")),
        )

    def build_embed(self) -> discord.Embed:
        night, slowed, reverb = self._levels()
        engine_label = "Edge" if self.engine == "edge" else "gTTS" if self.engine == "gtts" else self.engine
        eligible = self.engine in {"edge", "gtts"}
        status = (
            f"**Engine atual:** `{engine_label}`\n"
            f"**Nightcore:** {_level_text(night)}\n"
            f"**Slowed:** {_level_text(slowed)}\n"
            f"**Reverb:** {_level_text(reverb)}"
        )
        note = (
            "Nightcore e Slowed são exclusivos entre si. Reverb pode ser combinado com qualquer um. "
            "Os efeitos são aplicados somente ao TTS, antes do mixer; efeitos e ducking da música não são alterados."
        )
        if not eligible:
            note += "\n\nA configuração fica salva, mas só é aplicada quando a fala usa **Edge** ou **gTTS**."
        embed = self.cog._make_embed("TTS avançado", status, ok=True)
        embed.add_field(name="Como funciona", value=note, inline=False)
        return embed

    def _make_effect_button(self, effect: str, label: str, emoji: str, level: int, row: int) -> discord.ui.Button:
        button = discord.ui.Button(
            label=f"{label} · {_level_text(level)}",
            emoji=emoji,
            style=discord.ButtonStyle.primary if level else discord.ButtonStyle.secondary,
            row=row,
        )

        async def callback(interaction: discord.Interaction) -> None:
            await self._cycle_effect(interaction, effect)

        button.callback = callback
        return button

    def _rebuild_items(self) -> None:
        self.clear_items()
        night, slowed, reverb = self._levels()
        self.add_item(self._make_effect_button("nightcore", "Nightcore", "⚡", night, 0))
        self.add_item(self._make_effect_button("slowed", "Slowed", "🐌", slowed, 1))
        self.add_item(self._make_effect_button("reverb", "Reverb", "🌊", reverb, 2))
        reset = discord.ui.Button(label="Desativar efeitos", emoji="⏹️", style=discord.ButtonStyle.danger, row=3)
        reset.callback = self._disable_all
        self.add_item(reset)

    async def _reload(self) -> None:
        db = self.cog._get_db()
        if db is None:
            raise RuntimeError("settings db unavailable")
        self.resolved = dict(await self.cog._maybe_await(db.resolve_tts(self.guild_id, self.owner_id)) or {})
        self._rebuild_items()

    async def _cycle_effect(self, interaction: discord.Interaction, effect: str) -> None:
        key = _EFFECT_KEYS[effect]
        current = _level(self.resolved.get(key))
        next_level = (current + 1) % 4
        updates: dict[str, int] = {key: next_level}
        if effect == "nightcore" and next_level:
            updates["advanced_slowed_level"] = 0
        elif effect == "slowed" and next_level:
            updates["advanced_nightcore_level"] = 0
        await self.cog._set_user_tts_and_refresh(self.guild_id, self.owner_id, **updates)
        await self._reload()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _disable_all(self, interaction: discord.Interaction) -> None:
        await self.cog._set_user_tts_and_refresh(
            self.guild_id,
            self.owner_id,
            advanced_nightcore_level=0,
            advanced_slowed_level=0,
            advanced_reverb_level=0,
        )
        await self._reload()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def send_for_message(self, message: discord.Message) -> discord.Message:
        sent = await message.channel.send(embed=self.build_embed(), view=self)
        self.message = sent
        return sent

    async def send(self, interaction: discord.Interaction) -> None:
        if interaction.response.is_done():
            sent = await interaction.followup.send(embed=self.build_embed(), view=self, ephemeral=True, wait=True)
        else:
            await interaction.response.send_message(embed=self.build_embed(), view=self, ephemeral=True)
            sent = await interaction.original_response()
        self.message = sent
