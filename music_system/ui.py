from __future__ import annotations

import contextlib

import discord

from .models import MusicTrack


def _bar(percent: float, *, size: int = 12) -> str:
    percent = max(0.0, min(1.0, float(percent)))
    filled = int(round(percent * size))
    return "▰" * filled + "▱" * max(0, size - filled)


def build_now_playing_embed(state, track: MusicTrack) -> discord.Embed:
    title = track.short_title or "Música sem título"
    embed = discord.Embed(
        title="🎵 Tocando agora",
        description=f"**[{discord.utils.escape_markdown(title)}]({track.display_url})**" if track.display_url else f"**{discord.utils.escape_markdown(title)}**",
        color=discord.Color.blurple(),
    )
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    embed.add_field(name="Fonte", value=track.source or track.uploader or "desconhecida", inline=True)
    embed.add_field(name="Duração", value=track.duration_label, inline=True)
    embed.add_field(name="Pedido por", value=track.requester_name or f"<@{track.requester_id}>", inline=True)
    volume_percent = int(round(float(state.volume) * 100))
    duck_percent = int(round(float(state.duck_volume) * 100))
    duck_text = f"ativo `{duck_percent}%`" if state.duck_enabled else "desligado"
    embed.add_field(
        name="Estado",
        value=(
            f"Volume: `{volume_percent}%` {_bar(min(float(state.volume), 1.0))}\n"
            f"Fila: `{state.queue.qsize()}` música(s)\n"
            f"Loop: `{state.loop_mode.label}`\n"
            f"Shuffle: `{'ativo' if state.shuffle else 'desligado'}`\n"
            f"TTS duck: `{duck_text}`"
        ),
        inline=False,
    )
    return embed


def build_queue_embed(state, page: int = 0) -> discord.Embed:
    items = list(getattr(state.queue, "_queue", []))
    page = max(0, int(page))
    per_page = 10
    max_page = max(0, (len(items) - 1) // per_page)
    page = min(page, max_page)
    start = page * per_page
    chunk = items[start : start + per_page]
    embed = discord.Embed(title="📜 Fila de música", color=discord.Color.blurple())
    if state.current:
        embed.add_field(name="Tocando agora", value=f"**{state.current.short_title}**", inline=False)
    if not chunk:
        embed.description = "A fila está vazia."
    else:
        lines = []
        for idx, track in enumerate(chunk, start=start + 1):
            requester = track.requester_name or f"<@{track.requester_id}>"
            lines.append(f"`{idx:02d}.` **{discord.utils.escape_markdown(track.short_title)}** • `{track.duration_label}` • {requester}")
        embed.description = "\n".join(lines)
    embed.set_footer(text=f"Página {page + 1}/{max_page + 1} • {len(items)} item(ns)")
    return embed


class VolumeModal(discord.ui.Modal):
    def __init__(self, router, guild_id: int, *, duck: bool = False) -> None:
        super().__init__(title="Volume do ducking" if duck else "Volume da música")
        self.router = router
        self.guild_id = int(guild_id)
        self.duck = duck
        self.value = discord.ui.TextInput(
            label="Volume em %",
            placeholder="Exemplo: 55" if not duck else "Exemplo: 15",
            min_length=1,
            max_length=3,
            required=True,
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            raw = str(self.value.value).strip().replace("%", "")
            value = int(raw)
        except Exception:
            await interaction.response.send_message("Envie apenas um número de 0 a 150.", ephemeral=True)
            return
        if self.duck:
            value = max(0, min(100, value))
            await self.router.set_duck_volume(self.guild_id, value)
            await interaction.response.send_message(f"🎙️ Volume da música durante TTS: `{value}%`.", ephemeral=True)
        else:
            value = max(0, min(150, value))
            await self.router.set_volume(self.guild_id, value)
            await interaction.response.send_message(f"🔊 Volume da música: `{value}%`.", ephemeral=True)


class QueueSelect(discord.ui.Select):
    def __init__(self, router, guild_id: int, page: int = 0) -> None:
        self.router = router
        self.guild_id = int(guild_id)
        self.page = max(0, int(page))
        state = router.get_state(guild_id)
        items = router.snapshot_queue(guild_id)
        start = self.page * 10
        options = []
        for idx, track in enumerate(items[start : start + 10], start=start + 1):
            options.append(
                discord.SelectOption(
                    label=f"{idx}. {track.short_title}"[:100],
                    description=f"Remover da fila • {track.duration_label}"[:100],
                    value=str(idx),
                    emoji="🗑️",
                )
            )
        if not options:
            options.append(discord.SelectOption(label="Fila vazia", value="none", emoji="📭"))
        super().__init__(placeholder="Remover música da fila", min_values=1, max_values=1, options=options, disabled=not items)

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "none":
            await interaction.response.send_message("A fila já está vazia.", ephemeral=True)
            return
        removed = await self.router.remove_at(self.guild_id, int(self.values[0]))
        if removed is None:
            await interaction.response.send_message("Essa posição não existe mais.", ephemeral=True)
            return
        await interaction.response.send_message(f"🗑️ Removido: **{removed.short_title}**", ephemeral=True)


class QueueView(discord.ui.View):
    def __init__(self, router, guild_id: int, page: int = 0) -> None:
        super().__init__(timeout=300)
        self.router = router
        self.guild_id = int(guild_id)
        self.page = max(0, int(page))
        self.add_item(QueueSelect(router, guild_id, self.page))

    async def _redraw(self, interaction: discord.Interaction) -> None:
        state = self.router.get_state(self.guild_id)
        await interaction.response.edit_message(embed=build_queue_embed(state, self.page), view=QueueView(self.router, self.guild_id, self.page))

    @discord.ui.button(label="Anterior", emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._redraw(interaction)

    @discord.ui.button(label="Próxima", emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self._redraw(interaction)

    @discord.ui.button(label="Limpar fila", emoji="🧹", style=discord.ButtonStyle.danger)
    async def clear_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.router.replace_queue(self.guild_id, [])
        await interaction.response.edit_message(embed=build_queue_embed(self.router.get_state(self.guild_id), 0), view=QueueView(self.router, self.guild_id, 0))


class SearchSelect(discord.ui.Select):
    def __init__(self, router, guild_id: int, voice_channel_id: int, text_channel_id: int, tracks: list[MusicTrack]) -> None:
        self.router = router
        self.guild_id = int(guild_id)
        self.voice_channel_id = int(voice_channel_id)
        self.text_channel_id = int(text_channel_id)
        self.tracks = tracks
        options = []
        for idx, track in enumerate(tracks[:10]):
            options.append(
                discord.SelectOption(
                    label=track.short_title[:100],
                    description=f"{track.uploader or track.source or 'resultado'} • {track.duration_label}"[:100],
                    value=str(idx),
                    emoji="🎵",
                )
            )
        super().__init__(placeholder="Escolha o resultado para adicionar à fila", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild não encontrada.", ephemeral=True)
            return
        idx = int(self.values[0])
        track = self.tracks[idx]
        voice_channel = guild.get_channel(self.voice_channel_id) or interaction.client.get_channel(self.voice_channel_id)
        text_channel = guild.get_channel(self.text_channel_id) or interaction.channel
        if voice_channel is None or text_channel is None:
            await interaction.response.send_message("Canal não encontrado.", ephemeral=True)
            return
        added, dropped = await self.router.enqueue(guild, voice_channel, text_channel, [track])
        msg = f"✅ Adicionado: **{track.short_title}**"
        if dropped:
            msg += "\n⚠️ A fila está cheia; alguns itens não entraram."
        await interaction.response.edit_message(content=msg, embed=None, view=None)


class SearchResultView(discord.ui.View):
    def __init__(self, router, guild_id: int, voice_channel_id: int, text_channel_id: int, tracks: list[MusicTrack]) -> None:
        super().__init__(timeout=120)
        self.add_item(SearchSelect(router, guild_id, voice_channel_id, text_channel_id, tracks))


class MusicPlayerView(discord.ui.View):
    def __init__(self, router, guild_id: int) -> None:
        super().__init__(timeout=None)
        self.router = router
        self.guild_id = int(guild_id)

    async def _ack(self, interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(label="Pausar/Retomar", emoji="⏯️", style=discord.ButtonStyle.primary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.router.get_state(self.guild_id)
        if state.paused:
            ok = await self.router.resume(self.guild_id)
            await self._ack(interaction, "▶️ Retomado." if ok else "Não havia música pausada.")
        else:
            ok = await self.router.pause(self.guild_id)
            await self._ack(interaction, "⏸️ Pausado." if ok else "Não havia música tocando.")

    @discord.ui.button(label="Pular", emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok = await self.router.skip(self.guild_id)
        await self._ack(interaction, "⏭️ Pulando música." if ok else "Não havia música para pular.")

    @discord.ui.button(label="Parar", emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.router.stop(self.guild_id, disconnect=True)
        await self._ack(interaction, "⏹️ Player parado e fila limpa.")

    @discord.ui.button(label="Fila", emoji="📜", style=discord.ButtonStyle.secondary)
    async def queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.router.get_state(self.guild_id)
        await interaction.response.send_message(embed=build_queue_embed(state, 0), view=QueueView(self.router, self.guild_id, 0), ephemeral=True)

    @discord.ui.button(label="Volume", emoji="🔊", style=discord.ButtonStyle.secondary)
    async def volume(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VolumeModal(self.router, self.guild_id, duck=False))

    @discord.ui.button(label="Loop", emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        mode = await self.router.cycle_loop(self.guild_id)
        await self._ack(interaction, f"🔁 Loop: `{mode.label}`.")

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        enabled = await self.router.toggle_shuffle(self.guild_id)
        await self._ack(interaction, f"🔀 Shuffle {'ativado' if enabled else 'desativado'}.")

    @discord.ui.button(label="TTS Duck", emoji="🎙️", style=discord.ButtonStyle.secondary)
    async def duck(self, interaction: discord.Interaction, button: discord.ui.Button):
        enabled = await self.router.toggle_duck(self.guild_id)
        await self._ack(interaction, f"🎙️ Ducking do TTS {'ativado' if enabled else 'desativado'}.")

    @discord.ui.button(label="Volume Duck", emoji="🔉", style=discord.ButtonStyle.secondary)
    async def duck_volume(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VolumeModal(self.router, self.guild_id, duck=True))
