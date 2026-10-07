"""Painel pessoal dos efeitos DSP avançados do TTS Edge/gTTS."""
from __future__ import annotations

import contextlib

import discord

from .visoes_base import VisaoBaseTTS


_EFFECT_KEYS = {
    "nightcore": "advanced_nightcore_level",
    "slowed": "advanced_slowed_level",
    "reverb": "advanced_reverb_level",
}
_EFFECT_META = {
    "nightcore": ("Nightcore", "⚡"),
    "slowed": ("Slowed", "🐌"),
    "reverb": ("Reverb", "🌊"),
}


def _level(value: object) -> int:
    try:
        return max(0, min(3, int(value or 0)))
    except (TypeError, ValueError):
        return 0


def _level_text(level: int) -> str:
    return "Desligado" if level <= 0 else f"Nível {level}"


def _effect_detail(effect: str, level: int) -> str:
    if level <= 0:
        return "Desligado"
    if effect == "nightcore":
        return ("Leve · +10%", "Médio · +20%", "Forte · +30%")[level - 1]
    if effect == "slowed":
        return ("Leve · −8%", "Médio · −16%", "Forte · −24%")[level - 1]
    return ("Leve", "Médio", "Forte")[level - 1]


class ModalNivelEfeitoTTS(discord.ui.Modal):
    """Escolhe explicitamente o nível de um efeito sem ciclo de botões."""

    def __init__(self, panel: "VisaoEfeitosAvancadosTTS", effect: str):
        label, _emoji = _EFFECT_META[effect]
        super().__init__(title=f"Configurar {label}")
        self.panel = panel
        self.effect = effect
        current = _level(panel.resolved.get(_EFFECT_KEYS[effect]))
        self.level = discord.ui.TextInput(
            label="Nível do efeito",
            placeholder="0 desliga · 1 leve · 2 médio · 3 forte",
            default=str(current),
            required=True,
            min_length=1,
            max_length=1,
        )
        self.add_item(self.level)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.panel.owner_id:
            await interaction.response.send_message(
                embed=self.panel.cog._make_embed(
                    "Painel bloqueado",
                    "Só quem abriu esse painel pode alterar esses efeitos.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        try:
            level = int(str(self.level.value or "").strip())
        except (TypeError, ValueError):
            level = -1
        if level not in {0, 1, 2, 3}:
            await interaction.response.send_message(
                embed=self.panel.cog._make_embed(
                    "Nível inválido",
                    "Escolha `0`, `1`, `2` ou `3`. O nível `0` desliga o efeito.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        # A engine pode mudar enquanto o modal está aberto. Revalida antes de
        # persistir para que ATTS/Teto nunca recebam DSP por uma interação velha.
        await self.panel._reload()
        if not self.panel._engine_is_eligible():
            await interaction.response.send_message(
                embed=self.panel.cog._make_embed(
                    "TTS avançado indisponível",
                    "Sua engine atual não é Edge nem gTTS. Nenhuma configuração foi alterada.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        key = _EFFECT_KEYS[self.effect]
        updates: dict[str, int] = {key: level}
        if self.effect == "nightcore" and level:
            updates["advanced_slowed_level"] = 0
        elif self.effect == "slowed" and level:
            updates["advanced_nightcore_level"] = 0

        await interaction.response.defer(ephemeral=True)
        await self.panel.cog._set_user_tts_and_refresh(
            self.panel.guild_id,
            self.panel.owner_id,
            **updates,
        )
        await self.panel._reload()

        panel_message = self.panel.message
        if panel_message is not None:
            with contextlib.suppress(Exception):
                await panel_message.edit(embed=self.panel.build_embed(), view=self.panel)

        label, emoji = _EFFECT_META[self.effect]
        await interaction.followup.send(
            f"{emoji} **{label}:** {_level_text(level)}",
            ephemeral=True,
        )


class VisaoEfeitosAvancadosTTS(VisaoBaseTTS):
    """Configura DSP por usuário sem tocar no estado/mixer da música."""

    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, resolved: dict | None = None):
        super().__init__(cog, owner_id, guild_id, timeout=180)
        self.panel_kind = "advanced"
        self.resolved = dict(resolved or {})
        self._rebuild_items()

    @property
    def engine(self) -> str:
        engine = str(self.resolved.get("engine") or "gtts").strip().lower().replace("-", "_")
        aliases = {
            "edge_tts": "edge",
            "microsoft": "edge",
            "microsoft_edge": "edge",
            "google": "gtts",
            "google_tts": "gtts",
        }
        return aliases.get(engine, engine)

    def _engine_is_eligible(self) -> bool:
        return self.engine in {"edge", "gtts"}

    def _levels(self) -> tuple[int, int, int]:
        return (
            _level(self.resolved.get("advanced_nightcore_level")),
            _level(self.resolved.get("advanced_slowed_level")),
            _level(self.resolved.get("advanced_reverb_level")),
        )

    def build_embed(self) -> discord.Embed:
        night, slowed, reverb = self._levels()
        engine_label = "Edge" if self.engine == "edge" else "gTTS" if self.engine == "gtts" else self.engine
        embed = self.cog._make_embed(
            "TTS avançado",
            f"Engine atual: **{engine_label}**\nClique em um efeito para escolher o nível.",
            ok=True,
        )
        embed.add_field(name="⚡ Nightcore", value=_effect_detail("nightcore", night), inline=True)
        embed.add_field(name="🐌 Slowed", value=_effect_detail("slowed", slowed), inline=True)
        embed.add_field(name="🌊 Reverb", value=_effect_detail("reverb", reverb), inline=True)
        embed.set_footer(text="0 desliga · níveis 1–3 aumentam a intensidade · Nightcore e Slowed são exclusivos")
        return embed

    def _make_effect_button(self, effect: str, level: int) -> discord.ui.Button:
        label, emoji = _EFFECT_META[effect]
        button = discord.ui.Button(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.primary if level else discord.ButtonStyle.secondary,
            row=0,
        )

        async def callback(interaction: discord.Interaction) -> None:
            await self._open_effect_modal(interaction, effect)

        button.callback = callback
        return button

    def _rebuild_items(self) -> None:
        self.clear_items()
        night, slowed, reverb = self._levels()
        self.add_item(self._make_effect_button("nightcore", night))
        self.add_item(self._make_effect_button("slowed", slowed))
        self.add_item(self._make_effect_button("reverb", reverb))
        reset = discord.ui.Button(
            label="Desativar tudo",
            emoji="⏹️",
            style=discord.ButtonStyle.danger if any((night, slowed, reverb)) else discord.ButtonStyle.secondary,
            disabled=not any((night, slowed, reverb)),
            row=1,
        )
        reset.callback = self._disable_all
        self.add_item(reset)

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
        embed = self.cog._make_embed(
            "TTS avançado indisponível",
            "Sua engine atual não é Edge nem gTTS. A configuração salva foi preservada e não foi alterada.",
            ok=False,
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return False

    async def _open_effect_modal(self, interaction: discord.Interaction, effect: str) -> None:
        if not await self._ensure_current_engine_is_eligible(interaction):
            return
        await interaction.response.send_modal(ModalNivelEfeitoTTS(self, effect))

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
