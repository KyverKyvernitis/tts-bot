from __future__ import annotations

import contextlib
import math
from typing import Optional

import discord

from .errors import MusicExtractionError
from .models import MusicTrack

PLAYER_BAR_URL = "https://cdn.discordapp.com/attachments/554468640942981147/1127294696025227367/rainbow_bar3.gif"
QUEUE_PAGE_SIZE = 8
MIN_DUCK_PERCENT = 5


def _bar(percent: float, *, size: int = 12) -> str:
    percent = max(0.0, min(1.0, float(percent)))
    filled = int(round(percent * size))
    return "▰" * filled + "▱" * max(0, size - filled)


def _escape(value: str, *, limit: int | None = None) -> str:
    value = discord.utils.escape_markdown((value or "").strip()) or "sem título"
    if limit and len(value) > limit:
        return value[: max(0, limit - 3)].rstrip() + "..."
    return value


def _track_link(track: MusicTrack, *, title_limit: int = 82) -> str:
    title = _escape(track.short_title or track.title, limit=title_limit)
    if track.display_url:
        return f"[`{title}`]({track.display_url})"
    return f"`{title}`"


def _queue_items(state) -> list[MusicTrack]:
    return list(getattr(state.queue, "_queue", []))


def _queue_duration_label(items: list[MusicTrack]) -> str:
    total = 0
    unknown = False
    for track in items:
        if track.is_live or track.duration is None:
            unknown = True
            continue
        total += max(0, int(track.duration))
    if not total and unknown:
        return "desconhecida"
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    label = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
    if unknown:
        label += "+"
    return label


def build_now_playing_embeds(state, track: MusicTrack) -> list[discord.Embed]:
    """Painel inspirado no MuseHeart, adaptado para discord.py/FFmpeg."""
    status = str(getattr(state, "current_status", "playing") or "playing")
    paused = bool(getattr(state, "paused", False)) or status == "paused"
    loading = status in {"resolving", "starting"}
    errored = status == "error"
    color = discord.Color.gold() if paused or loading else discord.Color.red() if errored else discord.Color.blurple()
    queue = _queue_items(state)
    volume_percent = int(round(float(getattr(state, "volume", 0.55)) * 100))
    duck_percent = int(round(float(getattr(state, "duck_volume", 0.15)) * 100))

    embed = discord.Embed(color=color)
    if loading:
        author_name = "Preparando áudio:"
        author_icon = "https://i.ibb.co/QXtk5VB/neon-circle.gif"
    elif paused:
        author_name = "Em Pausa:"
        author_icon = "https://cdn.discordapp.com/attachments/480195401543188483/896013933197013002/pause.png"
    elif errored:
        author_name = "Erro no player:"
        author_icon = "https://cdn.discordapp.com/emojis/1215703754471268414.png"
    else:
        author_name = "Tocando Agora:"
        author_icon = "https://i.ibb.co/QXtk5VB/neon-circle.gif"
    embed.set_author(name=author_name, icon_url=author_icon)

    duration_line = "> -# 🔴 **⠂** `Livestream`" if track.is_live else f"> -# ⏰ **⠂** `{track.duration_label}`"
    requester = track.requester_name or f"<@{track.requester_id}>"
    source = track.uploader or track.source or track.extractor or "fonte desconhecida"

    lines = [
        f"-# {_track_link(track)}",
        "",
    ]
    if loading:
        lines.append("> -# 🔄 **⠂** `Resolvendo stream de áudio...`")
    lines.extend([
        duration_line,
        f"> -# 👤 **⠂** {_escape(source, limit=64)}",
        f"> -# ✋ **⠂** {requester}",
        f"> -# 🔊 **⠂** `Volume: {volume_percent}%` `{_bar(min(float(getattr(state, 'volume', 0.55)), 1.0), size=10)}`",
        f"> -# 🎙️ **⠂** `TTS sobre música: reduz para {duck_percent}%`",
    ])

    loop_mode = getattr(state, "loop_mode", None)
    loop_label = getattr(loop_mode, "label", "desligado")
    if loop_label and loop_label != "desligado":
        loop_emoji = "🔂" if loop_label == "música atual" else "🔁"
        lines.append(f"> -# {loop_emoji} **⠂** `Repetição: {loop_label}`")

    if getattr(state, "shuffle", False):
        lines.append("> -# 🔀 **⠂** `Fila misturada`")

    if queue:
        lines.append(f"> -# 🎶 **⠂** `{len(queue)} música{'s' if len(queue) != 1 else ''} na fila`")

    history_count = len(list(getattr(state, "history", []) or []))
    if history_count:
        lines.append(f"> -# ↩️ **⠂** `{history_count} música{'s' if history_count != 1 else ''} no histórico`")

    embed.description = "\n".join(lines)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    embed.set_image(url=PLAYER_BAR_URL)
    embed.set_footer(text="Use os botões ou o menu abaixo para controlar o player.")

    embeds: list[discord.Embed] = []
    if queue:
        mini = discord.Embed(
            title=f"Músicas na fila: {len(queue)}",
            color=discord.Color.blurple(),
        )
        mini_lines = []
        for n, item in enumerate(queue[:3], start=1):
            mini_lines.append(f"-# `{n:02}) [{item.duration_label}]` {_track_link(item, title_limit=42)}")
        if len(queue) > 3:
            mini_lines.append(f"-# `+ {len(queue) - 3} restante(s)`")
        mini_lines.append(f"-# `⌛ Duração aproximada da fila: {_queue_duration_label(queue)}`")
        mini.description = "\n".join(mini_lines)
        mini.set_image(url=PLAYER_BAR_URL)
        embeds.append(mini)

    embeds.append(embed)
    return embeds


def build_player_embeds(state) -> list[discord.Embed]:
    """Renderização central do painel fixo do player.

    Deve ser usada sempre que fila/estado/música mudar, inclusive quando não há
    música atual. Isso evita painel congelado com snapshot antigo.
    """
    current = getattr(state, "current", None)
    if current is not None:
        return build_now_playing_embeds(state, current)

    queue = _queue_items(state)
    volume_percent = int(round(float(getattr(state, "volume", 0.55)) * 100))
    duck_percent = int(round(float(getattr(state, "duck_volume", 0.15)) * 100))
    status = str(getattr(state, "current_status", "idle") or "idle")

    embed = discord.Embed(color=discord.Color.dark_grey() if not queue else discord.Color.blurple())
    if queue:
        embed.set_author(name="Fila pronta:", icon_url="https://i.ibb.co/QXtk5VB/neon-circle.gif")
        lines = [
            f"> -# 🎶 **⠂** `{len(queue)} música{'s' if len(queue) != 1 else ''} aguardando`",
            f"> -# ⌛ **⠂** `Duração aproximada: {_queue_duration_label(queue)}`",
            f"> -# 🔊 **⠂** `Volume: {volume_percent}%` `{_bar(min(float(getattr(state, 'volume', 0.55)), 1.0), size=10)}`",
            f"> -# 🎙️ **⠂** `TTS sobre música: reduz para {duck_percent}%`",
        ]
        for n, item in enumerate(queue[:5], start=1):
            lines.append(f"-# `{n:02}) [{item.duration_label}]` {_track_link(item, title_limit=48)}")
        if len(queue) > 5:
            lines.append(f"-# `+ {len(queue) - 5} restante(s)`")
        embed.description = "\n".join(lines)
        first = queue[0]
        if first.thumbnail:
            embed.set_thumbnail(url=first.thumbnail)
        embed.set_footer(text="A próxima música será preparada automaticamente.")
    else:
        embed.set_author(name="Player parado:", icon_url="https://cdn.discordapp.com/emojis/1215703754471268414.png")
        if status == "idle":
            embed.description = (
                "> -# `📭` Nenhuma música na fila.\n"
                f"> -# 🎙️ **⠂** `TTS sobre música sempre ativo: {duck_percent}%`"
            )
        else:
            embed.description = "> -# `📭` Fila vazia."
        embed.set_footer(text="Use _play <link ou pesquisa> para adicionar música.")

    embed.set_image(url=PLAYER_BAR_URL)
    return [embed]


def build_now_playing_embed(state, track: MusicTrack) -> discord.Embed:
    return build_now_playing_embeds(state, track)[-1]


def build_queue_embed(state, page: int = 0, *, selected_position: int | None = None) -> discord.Embed:
    items = _queue_items(state)
    page = max(0, int(page))
    max_page = max(0, (len(items) - 1) // QUEUE_PAGE_SIZE)
    page = min(page, max_page)
    start = page * QUEUE_PAGE_SIZE
    chunk = items[start : start + QUEUE_PAGE_SIZE]
    embed = discord.Embed(
        title=f"Músicas da fila [Página: {page + 1} / {max_page + 1}]",
        color=discord.Color.blurple(),
    )

    lines: list[str] = []
    if state.current:
        lines.append(f"`▶️` **Tocando agora:** {_track_link(state.current, title_limit=55)}")
        lines.append("")

    if not chunk:
        lines.append("`📭` A fila está vazia.")
    else:
        for offset, track in enumerate(chunk, start=1):
            index = start + offset
            requester = track.requester_name or f"<@{track.requester_id}>"
            if selected_position == index:
                lines.append(f"`╔{'=' * 34}`")
                lines.append(f"`║` **{index}º)** {_track_link(track, title_limit=45)}")
                lines.append(f"`║ ⏲️` **{track.duration_label}** **|** `✋` {requester}")
                lines.append(f"`╚{'=' * 34}`")
            else:
                lines.append(f"`┌ {index})` {_track_link(track, title_limit=48)}")
                lines.append(f"`└ ⏲️ {track.duration_label}` **|** `✋` {requester}")

    if items:
        lines.append("")
        lines.append(f"-# `⌛ Duração aproximada: {_queue_duration_label(items)}`")

    embed.description = "\n".join(lines)
    if selected_position and 1 <= selected_position <= len(items):
        selected = items[selected_position - 1]
        if selected.thumbnail:
            embed.set_thumbnail(url=selected.thumbnail)
    embed.set_footer(text=f"{len(items)} item(ns) • selecione uma música para tocar, mover ou remover")
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
            await interaction.response.send_message("Envie apenas um número válido.", ephemeral=True)
            return
        if self.duck:
            value = max(MIN_DUCK_PERCENT, min(100, value))
            await self.router.set_duck_volume(self.guild_id, value)
            await interaction.response.send_message(
                f"🎙️ Ducking continua sempre ativo. Volume da música durante TTS: `{value}%`.",
                ephemeral=True,
            )
        else:
            value = max(0, min(150, value))
            await self.router.set_volume(self.guild_id, value)
            await interaction.response.send_message(f"🔊 Volume da música: `{value}%`.", ephemeral=True)


class SearchSelect(discord.ui.Select):
    def __init__(self, router, guild_id: int, voice_channel_id: int, text_channel_id: int, tracks: list[MusicTrack], requester_id: int | None = None) -> None:
        self.router = router
        self.guild_id = int(guild_id)
        self.voice_channel_id = int(voice_channel_id)
        self.text_channel_id = int(text_channel_id)
        self.tracks = tracks
        self.requester_id = int(requester_id or 0)
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
        if self.requester_id and interaction.user and interaction.user.id != self.requester_id:
            await interaction.response.send_message("Só quem abriu essa busca pode escolher o resultado.", ephemeral=True)
            return
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
        state = self.router.get_state(self.guild_id)
        position = state.queue_size() + (1 if state.current else 0)
        msg = f"`🎶` **Adicionada à fila:** {track.short_title} • `{track.duration_label}` • posição `{max(1, position)}`"
        if dropped:
            msg += "\n`⚠️` A fila está cheia; alguns itens não entraram."
        await interaction.response.edit_message(content=msg, embed=None, view=None)


class SearchResultView(discord.ui.View):
    def __init__(self, router, guild_id: int, voice_channel_id: int, text_channel_id: int, tracks: list[MusicTrack], requester_id: int | None = None) -> None:
        super().__init__(timeout=120)
        self.add_item(SearchSelect(router, guild_id, voice_channel_id, text_channel_id, tracks, requester_id))


class AddSongModal(discord.ui.Modal):
    def __init__(self, router, guild_id: int, *, voice_channel_id: int | None = None, text_channel_id: int | None = None) -> None:
        super().__init__(title="Adicionar música")
        self.router = router
        self.guild_id = int(guild_id)
        self.voice_channel_id = int(voice_channel_id or 0)
        self.text_channel_id = int(text_channel_id or 0)
        self.query = discord.ui.TextInput(
            label="Nome, link ou playlist",
            placeholder="Exemplo: Laufey From The Start",
            min_length=2,
            max_length=300,
            required=True,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild não encontrada.", ephemeral=True)
            return

        state = self.router.get_state(self.guild_id)
        voice_channel = None
        user_voice = getattr(getattr(interaction.user, "voice", None), "channel", None)
        if user_voice is not None:
            voice_channel = user_voice
        if voice_channel is None and (self.voice_channel_id or state.last_voice_channel_id):
            cid = self.voice_channel_id or state.last_voice_channel_id
            voice_channel = guild.get_channel(int(cid)) or interaction.client.get_channel(int(cid))
        text_channel = None
        if self.text_channel_id or state.last_text_channel_id:
            cid = self.text_channel_id or state.last_text_channel_id
            text_channel = guild.get_channel(int(cid)) or interaction.client.get_channel(int(cid))
        text_channel = text_channel or interaction.channel

        if voice_channel is None:
            await interaction.response.send_message("Entre em um canal de voz primeiro.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        query = str(self.query.value).strip()
        try:
            batch = await self.router.extractor.extract(
                query,
                requester_id=interaction.user.id,
                requester_name=getattr(interaction.user, "display_name", str(interaction.user)),
            )
        except MusicExtractionError as exc:
            await interaction.followup.send(f"`⚠️` {exc}", ephemeral=True)
            return
        except Exception as exc:
            await interaction.followup.send(f"`⚠️` Não consegui preparar essa música: `{exc}`", ephemeral=True)
            return

        if not batch.tracks:
            await interaction.followup.send("`📭` Não encontrei nada tocável.", ephemeral=True)
            return

        if not self.router.extractor.looks_like_url(query) and len(batch.tracks) > 1:
            embed = discord.Embed(
                title="🔎 Escolha a música",
                description="Selecione um dos resultados abaixo para adicionar à fila.",
                color=discord.Color.blurple(),
            )
            for idx, track in enumerate(batch.tracks[:5], start=1):
                embed.add_field(name=f"{idx}. {track.short_title}", value=f"{track.uploader or track.source or 'resultado'} • `{track.duration_label}`", inline=False)
            await interaction.followup.send(
                embed=embed,
                view=SearchResultView(self.router, guild.id, getattr(voice_channel, "id", 0), getattr(text_channel, "id", 0), batch.tracks[:5], interaction.user.id),
                ephemeral=True,
            )
            return

        added, dropped = await self.router.enqueue(guild, voice_channel, text_channel, batch.tracks)
        if batch.is_playlist:
            msg = f"`📑` **Playlist adicionada:** `{added}` música(s)"
            if batch.playlist_title:
                msg += f" de **{batch.playlist_title}**"
            if batch.truncated:
                msg += "\n`⚠️` Playlist limitada para economizar RAM."
        else:
            state = self.router.get_state(self.guild_id)
            position = state.queue_size() + (1 if state.current else 0)
            msg = f"`🎶` **Adicionada à fila:** {batch.tracks[0].short_title} • `{batch.tracks[0].duration_label}` • posição `{max(1, position)}`"
        if dropped:
            msg += f"\n`⚠️` `{dropped}` item(ns) não entraram porque a fila está cheia."
        await interaction.followup.send(msg, ephemeral=True)


class QueueSelect(discord.ui.Select):
    def __init__(self, router, guild_id: int, page: int = 0, selected_position: int | None = None) -> None:
        self.router = router
        self.guild_id = int(guild_id)
        self.page = max(0, int(page))
        self.selected_position = selected_position
        items = router.snapshot_queue(guild_id)
        start = self.page * QUEUE_PAGE_SIZE
        options = []
        for idx, track in enumerate(items[start : start + QUEUE_PAGE_SIZE], start=start + 1):
            options.append(
                discord.SelectOption(
                    label=f"{idx}. {track.short_title}"[:100],
                    description=f"{track.duration_label} • {track.uploader or track.source or 'fila'}"[:100],
                    value=str(idx),
                    emoji="🎵",
                    default=selected_position == idx,
                )
            )
        if not options:
            options.append(discord.SelectOption(label="Fila vazia", value="none", emoji="📭"))
        super().__init__(placeholder="Selecione uma música da página", min_values=1, max_values=1, options=options, disabled=not items, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "none":
            await interaction.response.send_message("A fila já está vazia.", ephemeral=True)
            return
        view = self.view
        if isinstance(view, QueueView):
            view.selected_position = int(self.values[0])
            await view._redraw(interaction)
            return
        await interaction.response.defer()


class MoveSelectedModal(discord.ui.Modal):
    def __init__(self, router, guild_id: int, from_pos: int) -> None:
        super().__init__(title="Mover música selecionada")
        self.router = router
        self.guild_id = int(guild_id)
        self.from_pos = int(from_pos)
        self.to_pos = discord.ui.TextInput(
            label="Nova posição da fila",
            placeholder="Exemplo: 1",
            min_length=1,
            max_length=4,
            required=True,
        )
        self.add_item(self.to_pos)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            to_pos = int(str(self.to_pos.value).strip())
        except Exception:
            await interaction.response.send_message("Use apenas número válido.", ephemeral=True)
            return
        ok = await self.router.move(self.guild_id, self.from_pos, to_pos)
        await interaction.response.send_message("`↪️` Música movida." if ok else "Não consegui mover: confira a posição.", ephemeral=True)


class QueueView(discord.ui.View):
    def __init__(self, router, guild_id: int, page: int = 0, *, owner_id: int | None = None, selected_position: int | None = None) -> None:
        super().__init__(timeout=300)
        self.router = router
        self.guild_id = int(guild_id)
        self.page = max(0, int(page))
        self.owner_id = int(owner_id or 0)
        self.selected_position = selected_position
        self._refresh_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id and interaction.user and interaction.user.id != self.owner_id:
            await interaction.response.send_message(f"Apenas <@{self.owner_id}> pode interagir nesse painel de fila.", ephemeral=True)
            return False
        return True

    def _max_page(self) -> int:
        items = self.router.snapshot_queue(self.guild_id)
        return max(0, (len(items) - 1) // QUEUE_PAGE_SIZE)

    def _refresh_components(self) -> None:
        self.clear_items()
        max_page = self._max_page()
        self.page = max(0, min(self.page, max_page))
        self.add_item(QueueSelect(self.router, self.guild_id, self.page, self.selected_position))

        first = discord.ui.Button(emoji="⏮️", style=discord.ButtonStyle.secondary, row=1, disabled=self.page <= 0)
        first.callback = self.first_page
        self.add_item(first)
        previous = discord.ui.Button(emoji="⬅️", style=discord.ButtonStyle.secondary, row=1, disabled=self.page <= 0)
        previous.callback = self.previous_page
        self.add_item(previous)
        next_button = discord.ui.Button(emoji="➡️", style=discord.ButtonStyle.secondary, row=1, disabled=self.page >= max_page)
        next_button.callback = self.next_page
        self.add_item(next_button)
        last = discord.ui.Button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=1, disabled=self.page >= max_page)
        last.callback = self.last_page
        self.add_item(last)
        close = discord.ui.Button(label="Fechar", emoji="❌", style=discord.ButtonStyle.secondary, row=1)
        close.callback = self.close_view
        self.add_item(close)

        disabled = not self.selected_position
        play = discord.ui.Button(label="Tocar", emoji="▶️", style=discord.ButtonStyle.primary, row=2, disabled=disabled)
        play.callback = self.play_selected
        self.add_item(play)
        move = discord.ui.Button(label="Mover", emoji="↪️", style=discord.ButtonStyle.secondary, row=2, disabled=disabled)
        move.callback = self.move_selected
        self.add_item(move)
        remove = discord.ui.Button(label="Remover", emoji="🗑️", style=discord.ButtonStyle.danger, row=2, disabled=disabled)
        remove.callback = self.remove_selected
        self.add_item(remove)
        reload_button = discord.ui.Button(label="Recarregar", emoji="🔄", style=discord.ButtonStyle.secondary, row=2)
        reload_button.callback = self.reload
        self.add_item(reload_button)
        clear = discord.ui.Button(label="Limpar fila", emoji="🧹", style=discord.ButtonStyle.danger, row=3)
        clear.callback = self.clear_queue
        self.add_item(clear)

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._refresh_components()
        embed = build_queue_embed(self.router.get_state(self.guild_id), self.page, selected_position=self.selected_position)
        await interaction.response.edit_message(embed=embed, view=self)

    async def first_page(self, interaction: discord.Interaction):
        self.page = 0
        await self._redraw(interaction)

    async def previous_page(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        await self._redraw(interaction)

    async def next_page(self, interaction: discord.Interaction):
        self.page = min(self._max_page(), self.page + 1)
        await self._redraw(interaction)

    async def last_page(self, interaction: discord.Interaction):
        self.page = self._max_page()
        await self._redraw(interaction)

    async def close_view(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Fila fechada.", embed=None, view=None)
        self.stop()

    async def play_selected(self, interaction: discord.Interaction):
        if not self.selected_position:
            await interaction.response.send_message("Selecione uma música primeiro.", ephemeral=True)
            return
        ok = await self.router.skip_to(self.guild_id, self.selected_position)
        await interaction.response.send_message("`▶️` Tocando a música selecionada." if ok else "Não consegui tocar essa posição.", ephemeral=True)

    async def move_selected(self, interaction: discord.Interaction):
        if not self.selected_position:
            await interaction.response.send_message("Selecione uma música primeiro.", ephemeral=True)
            return
        await interaction.response.send_modal(MoveSelectedModal(self.router, self.guild_id, self.selected_position))

    async def remove_selected(self, interaction: discord.Interaction):
        if not self.selected_position:
            await interaction.response.send_message("Selecione uma música primeiro.", ephemeral=True)
            return
        removed = await self.router.remove_at(self.guild_id, self.selected_position)
        self.selected_position = None
        self._refresh_components()
        if removed is None:
            await interaction.response.send_message("Essa posição não existe mais.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=build_queue_embed(self.router.get_state(self.guild_id), self.page), view=self)

    async def reload(self, interaction: discord.Interaction):
        await self._redraw(interaction)

    async def clear_queue(self, interaction: discord.Interaction):
        await self.router.replace_queue(self.guild_id, [])
        self.selected_position = None
        self.page = 0
        await self._redraw(interaction)


class PlayerOptionsSelect(discord.ui.Select):
    def __init__(self, router, guild_id: int) -> None:
        state = router.get_state(guild_id)
        volume_percent = int(round(float(getattr(state, "volume", 0.55)) * 100))
        duck_percent = int(round(float(getattr(state, "duck_volume", 0.15)) * 100))
        options = [
            discord.SelectOption(label="Adicionar música", emoji="🎶", value="add_song", description="Adicionar uma música ou playlist na fila."),
            discord.SelectOption(label=f"Volume: {volume_percent}%", emoji="🔊", value="volume", description="Ajustar volume da música."),
            discord.SelectOption(label="Misturar fila", emoji="🔀", value="shuffle", description="Misturar as músicas que estão na fila."),
            discord.SelectOption(label="Readicionar histórico", emoji="🎶", value="readd", description="Readicionar músicas tocadas de volta na fila."),
            discord.SelectOption(label="Repetição", emoji="🔁", value="loop", description="Alternar repetição da música/fila."),
            discord.SelectOption(label=f"Duck TTS: {duck_percent}%", emoji="🎙️", value="duck_volume", description="Ajustar volume da música enquanto o TTS fala."),
        ]
        super().__init__(placeholder="Mais opções:", min_values=1, max_values=1, options=options, row=1)
        self.router = router
        self.guild_id = int(guild_id)

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        state = self.router.get_state(self.guild_id)
        if value == "add_song":
            await interaction.response.send_modal(
                AddSongModal(
                    self.router,
                    self.guild_id,
                    voice_channel_id=state.last_voice_channel_id,
                    text_channel_id=state.last_text_channel_id,
                )
            )
            return
        if value == "volume":
            await interaction.response.send_modal(VolumeModal(self.router, self.guild_id, duck=False))
            return
        if value == "duck_volume":
            await interaction.response.send_modal(VolumeModal(self.router, self.guild_id, duck=True))
            return
        if value == "shuffle":
            enabled = await self.router.toggle_shuffle(self.guild_id)
            await interaction.response.send_message(f"`🔀` Shuffle {'ativado' if enabled else 'desativado'}.", ephemeral=True)
            return
        if value == "readd":
            added = await self.router.readd_history(self.guild_id)
            await interaction.response.send_message(
                f"`🎶` Readicionei `{added}` música(s) do histórico." if added else "O histórico está vazio.",
                ephemeral=True,
            )
            return
        if value == "loop":
            mode = await self.router.cycle_loop(self.guild_id)
            await interaction.response.send_message(f"`🔁` Repetição: `{mode.label}`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)


class MusicPlayerView(discord.ui.View):
    def __init__(self, router, guild_id: int) -> None:
        super().__init__(timeout=None)
        self.router = router
        self.guild_id = int(guild_id)
        self.add_item(PlayerOptionsSelect(router, guild_id))

    async def _ack(self, interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(label="Pausar/Retomar", emoji="⏯️", style=discord.ButtonStyle.primary, row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.router.get_state(self.guild_id)
        if state.paused:
            ok = await self.router.resume(self.guild_id)
            await self._ack(interaction, "`▶️` Música retomada." if ok else "Não havia música pausada.")
        else:
            ok = await self.router.pause(self.guild_id)
            await self._ack(interaction, "`⏸️` Música pausada." if ok else "Não havia música tocando.")

    @discord.ui.button(label="Voltar", emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok = await self.router.previous(self.guild_id)
        await self._ack(interaction, "`⏮️` Voltando para a música anterior." if ok else "Não há música anterior no histórico.")

    @discord.ui.button(label="Parar", emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.router.stop(self.guild_id, disconnect=True)
        await self._ack(interaction, "`⏹️` Player parado e fila limpa. Se o TTS estiver mantendo a call, o bot permanece conectado.")

    @discord.ui.button(label="Pular", emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok = await self.router.skip(self.guild_id)
        await self._ack(interaction, "`⏭️` Pulando música." if ok else "Não havia música para pular.")

    @discord.ui.button(label="Fila", emoji="📜", style=discord.ButtonStyle.secondary, row=0)
    async def queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.router.get_state(self.guild_id)
        await interaction.response.send_message(
            embed=build_queue_embed(state, 0),
            view=QueueView(self.router, self.guild_id, 0, owner_id=getattr(interaction.user, "id", None)),
            ephemeral=True,
        )
