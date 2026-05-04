import asyncio
import inspect
import os
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

import config
from .tts.aliases import format_prefixed_aliases, matches_prefixed_command
from .tts.utils.app_commands import fetch_root_command_ids_cached, slash_mention


HELP_EXPIRE_AFTER_SECONDS = 600.0
HELP_MAX_SESSIONS_PER_GUILD = 5
HELP_HEADER_EMOJI = "<a:livro:1500584783779070143>"
HEALTH_GUILDS_PER_PAGE = 15

HEALTH_COMMAND_GUILD_ID = 927002914449424404
HEALTH_COMMAND_GUILD = discord.Object(id=HEALTH_COMMAND_GUILD_ID)


from dataclasses import dataclass


@dataclass
class HelpPage:
    """Página do help. `body` é markdown que vai dentro de um TextDisplay; o
    Discord cuida da renderização. `title` aparece como heading com ##."""
    title: str
    body: str
    accent: discord.Color


class _HelpPageJumpModal(discord.ui.Modal, title="Ir para página"):
    """Modal acionado pelo botão central do paginator. O usuário digita o
    número da página e o paginator muda de página direto, sem ter que clicar
    várias vezes em prev/next."""

    page_input = discord.ui.TextInput(
        label="Número da página",
        placeholder="Ex: 5",
        required=True,
        max_length=2,
    )

    def __init__(self, view: "HelpPaginatorView"):
        super().__init__(timeout=60)
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.page_input.value or "").strip()
        try:
            target = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "Digite só o número da página.",
                ephemeral=True,
            )
            return
        total = len(self.view_ref.pages)
        if target < 1 or target > total:
            await interaction.response.send_message(
                f"Página inválida. Use 1 a {total}.",
                ephemeral=True,
            )
            return
        self.view_ref.page_index = target - 1
        self.view_ref._touch()
        self.view_ref._rebuild_layout()
        await interaction.response.edit_message(view=self.view_ref)


class HelpPaginatorView(discord.ui.LayoutView):
    """Paginator do help em Components V2. Usa Container + TextDisplay no lugar
    de Embed pra ter layout mais limpo. O botão central abre um modal de
    seleção rápida em vez de só mostrar o número."""

    def __init__(
        self,
        cog: "Utility",
        *,
        owner_id: int,
        pages: list[HelpPage],
        command_mention: str,
        prefix_hint: str,
        bot_avatar_url: str | None = None,
        timeout: float = 180,
    ):
        requested_timeout = max(1.0, float(timeout or HELP_EXPIRE_AFTER_SECONDS))
        super().__init__(timeout=requested_timeout)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.pages = pages
        self.command_mention = str(command_mention or "`/help`")
        self.prefix_hint = str(prefix_hint or "`help`")
        self.bot_avatar_url = bot_avatar_url
        self.page_index = 0
        self.message: discord.Message | None = None
        self.timeout_seconds = requested_timeout
        self.expires_at_monotonic = time.monotonic() + requested_timeout
        self._expired = False
        self._rebuild_layout()

    def _rebuild_layout(self) -> None:
        """Reconstrói os componentes V2 com a página atual. Chamado a cada
        navegação porque a estrutura inteira do Container muda."""
        # Limpa items existentes do LayoutView e reanexa.
        for item in list(self.children):
            self.remove_item(item)

        page = self.pages[self.page_index]
        total = max(1, len(self.pages))
        at_start = self.page_index <= 0
        at_end = self.page_index >= total - 1

        # Cabeçalho: linha fina de breadcrumb + título destacado. O índice da
        # página fica em cima ao invés de no fim, aí o user já sabe onde tá
        # antes mesmo de ler o corpo.
        header_text = (
            f"-# {HELP_HEADER_EMOJI} Página **{self.page_index + 1}** de **{total}**\n"
            f"## {page.title}"
        )

        # Rodapé curto com a dica do paginator. Vai abaixo do corpo, antes dos
        # botões — fica claro pra quê serve cada controle sem ter que clicar.
        footer_hint = (
            f"-# _    _⏪ Início · ◀ Voltar `{self.page_index + 1}/{total}` Pular "
            f"▶ · Fim ⏩"
        )

        # Linha de botões: ⏪ ◀ N/Total ▶ ⏩. O botão central abre o modal de
        # jump em vez de ser puramente decorativo.
        first = discord.ui.Button(
            emoji="⏪", style=discord.ButtonStyle.secondary, disabled=at_start,
        )
        first.callback = self._go_first

        prev_b = discord.ui.Button(
            emoji="◀️", style=discord.ButtonStyle.secondary, disabled=at_start,
        )
        prev_b.callback = self._go_prev

        jump = discord.ui.Button(
            label=f"{self.page_index + 1}/{total}",
            style=discord.ButtonStyle.primary,
        )
        jump.callback = self._open_jump_modal

        next_b = discord.ui.Button(
            emoji="▶️", style=discord.ButtonStyle.secondary, disabled=at_end,
        )
        next_b.callback = self._go_next

        last = discord.ui.Button(
            emoji="⏩", style=discord.ButtonStyle.secondary, disabled=at_end,
        )
        last.callback = self._go_last

        action_row = discord.ui.ActionRow(first, prev_b, jump, next_b, last)

        # Layout: cabeçalho · corpo · rodapé · botões. As Separators visuais
        # quebram a leitura sem precisar enfeitar o markdown do corpo.
        container = discord.ui.Container(
            discord.ui.TextDisplay(header_text),
            discord.ui.Separator(),
            discord.ui.TextDisplay(page.body),
            discord.ui.Separator(),
            discord.ui.TextDisplay(footer_hint),
            action_row,
            accent_color=page.accent,
        )
        self.add_item(container)

    def _touch(self) -> None:
        self.expires_at_monotonic = time.monotonic() + self.timeout_seconds

    def _is_expired(self) -> bool:
        return self._expired or time.monotonic() >= self.expires_at_monotonic

    def _rebuild_expired_layout(self) -> None:
        for item in list(self.children):
            self.remove_item(item)
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                "Essa central de ajuda expirou porque ficou aberta por tempo demais.\n\n"
                f"Use {self.command_mention} novamente — ou, se preferir, {self.prefix_hint}."
            ),
            accent_color=discord.Color.dark_grey(),
        )
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._is_expired():
            message = (
                "Essa central de ajuda já expirou porque ficou aberta por tempo demais.\n\n"
                f"Para abrir tudo de novo, use {self.command_mention} novamente"
                f" — ou, se preferir, {self.prefix_hint}."
            )
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except Exception:
                pass
            return False

        if int(getattr(getattr(interaction, "user", None), "id", 0) or 0) == self.owner_id:
            self._touch()
            return True
        message = "Só quem abriu esse help pode trocar de página."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            pass
        return False

    async def on_timeout(self) -> None:
        self._expired = True
        try:
            await self.cog._unregister_help_session(self)
        except Exception:
            pass
        self._rebuild_expired_layout()
        if self.message is not None:
            try:
                await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())
            except TypeError:
                try:
                    await self.message.edit(view=self)
                except Exception:
                    pass
            except Exception:
                pass

    async def _go_first(self, interaction: discord.Interaction):
        self.page_index = 0
        self._rebuild_layout()
        await interaction.response.edit_message(view=self)

    async def _go_prev(self, interaction: discord.Interaction):
        if self.page_index > 0:
            self.page_index -= 1
        self._rebuild_layout()
        await interaction.response.edit_message(view=self)

    async def _open_jump_modal(self, interaction: discord.Interaction):
        # Botão central: em vez de ser desabilitado, agora abre modal com
        # input pra digitar a página. Mais rápido que apertar prev/next várias
        # vezes pra chegar na página 8.
        await interaction.response.send_modal(_HelpPageJumpModal(self))

    async def _go_next(self, interaction: discord.Interaction):
        if self.page_index < len(self.pages) - 1:
            self.page_index += 1
        self._rebuild_layout()
        await interaction.response.edit_message(view=self)

    async def _go_last(self, interaction: discord.Interaction):
        self.page_index = max(0, len(self.pages) - 1)
        self._rebuild_layout()
        await interaction.response.edit_message(view=self)


class _HealthPageJumpModal(discord.ui.Modal, title="Ir para página de guilds"):
    """Modal de salto rápido para a página de guilds do painel health."""

    page_input = discord.ui.TextInput(
        label="Número da página",
        placeholder="Ex: 2",
        required=True,
        max_length=3,
    )

    def __init__(self, view: "HealthDashboardView"):
        super().__init__(timeout=60)
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.page_input.value or "").strip()
        try:
            target = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "Digite só o número da página.",
                ephemeral=True,
            )
            return

        total = self.view_ref.total_guild_pages()
        if target < 1 or target > total:
            await interaction.response.send_message(
                f"Página inválida. Use 1 a {total}.",
                ephemeral=True,
            )
            return

        self.view_ref.guild_page_index = target - 1
        self.view_ref._touch()
        self.view_ref._rebuild_layout(refresh_snapshot=True)
        await interaction.response.edit_message(view=self.view_ref)


class HealthDashboardView(discord.ui.LayoutView):
    """Painel `/health` em Components V2.

    Mantém a paginação de guilds no mesmo estilo do help: início, voltar,
    pular por modal, avançar e fim. Os botões só aparecem quando existem 2+
    páginas; atualizar e fechar ficam separados para não poluir a navegação.
    """

    def __init__(
        self,
        cog: "Utility",
        *,
        owner_id: int,
        bot_avatar_url: str | None = None,
        timeout: float = HELP_EXPIRE_AFTER_SECONDS,
    ):
        requested_timeout = max(1.0, float(timeout or HELP_EXPIRE_AFTER_SECONDS))
        super().__init__(timeout=requested_timeout)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.bot_avatar_url = bot_avatar_url
        self.guild_page_index = 0
        self.message: discord.Message | None = None
        self.timeout_seconds = requested_timeout
        self.expires_at_monotonic = time.monotonic() + requested_timeout
        self._expired = False
        self._snapshot: dict[str, Any] = {}
        self._rebuild_layout(refresh_snapshot=True)

    def _touch(self) -> None:
        self.expires_at_monotonic = time.monotonic() + self.timeout_seconds

    def _is_expired(self) -> bool:
        return self._expired or time.monotonic() >= self.expires_at_monotonic

    def total_guild_pages(self) -> int:
        guilds = list(self._snapshot.get("guilds") or [])
        return max(1, (len(guilds) + HEALTH_GUILDS_PER_PAGE - 1) // HEALTH_GUILDS_PER_PAGE)

    def _status_accent(self) -> discord.Color:
        if bool(self._snapshot.get("healthy")):
            return discord.Color.green()
        if bool(self._snapshot.get("starting")):
            return discord.Color.gold()
        return discord.Color.red()

    def _status_title(self) -> tuple[str, str]:
        if bool(self._snapshot.get("starting")):
            return "⏳", "Inicializando"
        if bool(self._snapshot.get("healthy")):
            return "🟢", "Saudável"
        return "🔴", "Atenção necessária"

    def _metric_bool(self, value: Any, *, ok: str, bad: str) -> str:
        return f"🟢 {ok}" if bool(value) else f"🔴 {bad}"

    def _guild_member_count(self, guild: discord.Guild) -> int:
        try:
            count = getattr(guild, "member_count", None)
            if count is not None:
                return int(count)
        except Exception:
            pass
        try:
            return int(len(getattr(guild, "members", []) or []))
        except Exception:
            return 0

    def _format_int(self, value: Any) -> str:
        try:
            return f"{int(value):,}".replace(",", ".")
        except Exception:
            return "0"

    def _format_guild_name(self, guild: discord.Guild) -> str:
        name = str(getattr(guild, "name", "Servidor sem nome") or "Servidor sem nome")
        try:
            name = discord.utils.escape_markdown(name)
        except Exception:
            pass
        if len(name) > 48:
            name = name[:45].rstrip() + "..."
        return name

    def _engine_lines(self) -> str:
        engine_metrics = dict(self._snapshot.get("engine_metrics") or {})
        if not engine_metrics:
            return "Ainda não há métricas suficientes de engine para mostrar aqui."

        lines: list[str] = []
        for idx, (engine_name, data) in enumerate(sorted(engine_metrics.items()), start=1):
            if idx > 5:
                remaining = len(engine_metrics) - 5
                if remaining > 0:
                    lines.append(f"-# + {remaining} engine(s) ocultas para manter o painel limpo.")
                break
            data = dict(data or {})
            failures = int(data.get("synth_failures", 0) or 0)
            consecutive = int(data.get("consecutive_failures", 0) or 0)
            dot = "🟢" if failures == 0 and consecutive == 0 else ("🟡" if consecutive == 0 else "🔴")
            lines.append(
                f"{dot} **{str(engine_name).upper()}** · synths `{int(data.get('synth_count', 0) or 0)}` "
                f"· falhas `{failures}` · seguidas `{consecutive}` · média `{self.cog._format_ms(data.get('avg_synth_ms'))}`"
            )
        return "\n".join(lines)

    def _guild_lines(self) -> str:
        guilds = list(self._snapshot.get("guilds") or [])
        total_pages = self.total_guild_pages()
        self.guild_page_index = max(0, min(self.guild_page_index, total_pages - 1))
        start = self.guild_page_index * HEALTH_GUILDS_PER_PAGE
        current = guilds[start:start + HEALTH_GUILDS_PER_PAGE]

        if not current:
            return "Nenhuma guild carregada no cache do bot."

        lines: list[str] = []
        for offset, guild in enumerate(current, start=start + 1):
            members = self._guild_member_count(guild)
            name = self._format_guild_name(guild)
            lines.append(f"`{offset:02d}.` **{name}**\n    👥 `{self._format_int(members)}` membros")
        return "\n".join(lines)

    def _build_summary_text(self) -> str:
        status_emoji, status_label = self._status_title()
        updated_unix = int(discord.utils.utcnow().timestamp())
        latency = self._snapshot.get("latency_ms", "n/a")
        if isinstance(latency, float):
            latency = f"{latency:.2f}"

        return (
            f"-# 🩺 Health global · atualizado <t:{updated_unix}:R>\n"
            f"## {status_emoji} Saúde geral do bot\n"
            "Painel operacional em Components V2 para ver estabilidade, fila, cache, engines e guilds sem abrir logs.\n\n"
            f"**Estado geral:** {status_emoji} **{status_label}**\n"
            f"**Status bruto:** `{self._snapshot.get('status', 'unknown')}`\n"
            f"**Uptime:** `{self.cog._format_duration(self._snapshot.get('uptime_seconds'))}`\n"
            f"**Latência:** `{latency} ms`"
        )

    def _build_state_text(self) -> str:
        starting = bool(self._snapshot.get("starting"))
        discord_closed = bool(self._snapshot.get("discord_closed"))
        return (
            "### 🔌 Estado do bot\n"
            f"**Healthy:** {self._metric_bool(self._snapshot.get('healthy'), ok='saudável', bad='com problema')}\n"
            f"**Inicializando:** {'🟡 sim' if starting else '⚪ não'}\n"
            f"**Discord pronto:** {self._metric_bool(self._snapshot.get('discord_ready'), ok='pronto', bad='não pronto')}\n"
            f"**Conexão Discord:** {'🔴 fechada' if discord_closed else '🟢 ativa'}\n"
            f"**MongoDB:** {self._metric_bool(self._snapshot.get('mongo_ok'), ok='ok', bad='offline')}\n"
            f"**Voice clients:** `{len(getattr(self.cog.bot, 'voice_clients', []) or [])}`"
        )

    def _build_queue_cache_text(self) -> str:
        tts_metrics = dict(self._snapshot.get("tts_metrics") or {})
        cache_hits = int(tts_metrics.get("cache_hits", 0) or 0)
        cache_misses = int(tts_metrics.get("cache_misses", 0) or 0)
        cache_stores = int(tts_metrics.get("cache_stores", 0) or 0)
        total_cache_lookups = cache_hits + cache_misses
        cache_hit_rate = (cache_hits / total_cache_lookups * 100.0) if total_cache_lookups else 0.0
        return (
            "### 📦 Fila, cache e armazenamento\n"
            f"**Na fila agora:** `{int(tts_metrics.get('queued_items_current', 0) or 0)}`\n"
            f"**Enfileiradas / deduplicadas / descartadas:** "
            f"`{int(tts_metrics.get('queue_enqueued', 0) or 0)}` / "
            f"`{int(tts_metrics.get('queue_deduplicated', 0) or 0)}` / "
            f"`{int(tts_metrics.get('queue_dropped', 0) or 0)}`\n"
            f"**Espera média:** `{self.cog._format_ms(tts_metrics.get('avg_queue_wait_ms'))}`\n"
            f"**Despacho médio:** `{self.cog._format_ms(tts_metrics.get('avg_dispatch_ms'))}`\n"
            f"**Cache:** `{cache_hits}` hits · `{cache_misses}` misses · `{cache_stores}` stores · `{cache_hit_rate:.1f}%` hit rate\n"
            f"**tmp_audio:** `{self.cog._format_bytes_human(self._snapshot.get('total_tmp_bytes'))}` "
            f"· runtime/cache/cred `{self._snapshot.get('runtime_files', 0)}`/`{self._snapshot.get('cache_files', 0)}`/`{self._snapshot.get('cred_files', 0)}`"
        )

    def _build_guilds_text(self) -> str:
        guilds = list(self._snapshot.get("guilds") or [])
        total_pages = self.total_guild_pages()
        page_hint = f"\n**Página:** `{self.guild_page_index + 1}/{total_pages}`" if total_pages > 1 else ""
        return (
            "### 🏠 Guilds conectadas\n"
            f"**Total:** `{self._format_int(len(guilds))}` guilds\n"
            f"**Membros totais:** `{self._format_int(self._snapshot.get('total_members', 0))}`{page_hint}\n\n"
            f"{self._guild_lines()}"
        )

    def _rebuild_layout(self, *, refresh_snapshot: bool = False) -> None:
        for item in list(self.children):
            self.remove_item(item)

        if refresh_snapshot or not self._snapshot:
            self._snapshot = self.cog._collect_health_snapshot()

        total_pages = self.total_guild_pages()
        self.guild_page_index = max(0, min(self.guild_page_index, total_pages - 1))
        at_start = self.guild_page_index <= 0
        at_end = self.guild_page_index >= total_pages - 1

        header_item: Any
        if self.bot_avatar_url:
            header_item = discord.ui.Section(
                discord.ui.TextDisplay(self._build_summary_text()),
                accessory=discord.ui.Thumbnail(self.bot_avatar_url, description="Avatar do bot"),
            )
        else:
            header_item = discord.ui.TextDisplay(self._build_summary_text())

        children: list[Any] = [
            header_item,
            discord.ui.Separator(),
            discord.ui.TextDisplay(self._build_state_text()),
            discord.ui.Separator(),
            discord.ui.TextDisplay(self._build_queue_cache_text()),
            discord.ui.Separator(),
            discord.ui.TextDisplay("### ⚙️ Engines\n" + self._engine_lines()),
            discord.ui.Separator(),
            discord.ui.TextDisplay(self._build_guilds_text()),
        ]

        if total_pages > 1:
            first = discord.ui.Button(emoji="⏪", style=discord.ButtonStyle.secondary, disabled=at_start)
            first.callback = self._go_first

            prev_b = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.secondary, disabled=at_start)
            prev_b.callback = self._go_prev

            jump = discord.ui.Button(label=f"{self.guild_page_index + 1}/{total_pages}", style=discord.ButtonStyle.primary)
            jump.callback = self._open_jump_modal

            next_b = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.secondary, disabled=at_end)
            next_b.callback = self._go_next

            last = discord.ui.Button(emoji="⏩", style=discord.ButtonStyle.secondary, disabled=at_end)
            last.callback = self._go_last

            children.extend([
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"-# _    _⏪ Início · ◀ Voltar `{self.guild_page_index + 1}/{total_pages}` Pular ▶ · Fim ⏩"
                ),
                discord.ui.ActionRow(first, prev_b, jump, next_b, last),
            ])

        refresh = discord.ui.Button(label="Atualizar", emoji="🔄", style=discord.ButtonStyle.success)
        refresh.callback = self._refresh

        close = discord.ui.Button(label="Fechar", emoji="✖️", style=discord.ButtonStyle.danger)
        close.callback = self._close

        children.append(discord.ui.ActionRow(refresh, close))

        container = discord.ui.Container(*children, accent_color=self._status_accent())
        self.add_item(container)

    def _rebuild_expired_layout(self) -> None:
        for item in list(self.children):
            self.remove_item(item)
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                "### 🩺 Painel health expirado\n"
                "Esse painel ficou aberto por tempo demais e foi congelado para evitar botões antigos.\n\n"
                "Use `/health` novamente para abrir uma leitura nova."
            ),
            accent_color=discord.Color.dark_grey(),
        )
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._is_expired():
            message = "Esse painel health já expirou. Use `/health` novamente para abrir uma leitura nova."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except Exception:
                pass
            return False

        user_id = int(getattr(getattr(interaction, "user", None), "id", 0) or 0)
        if user_id == self.owner_id:
            self._touch()
            return True

        perms = getattr(getattr(interaction, "user", None), "guild_permissions", None)
        if bool(getattr(perms, "administrator", False)) or bool(getattr(perms, "manage_guild", False)):
            self._touch()
            return True

        message = "Só quem abriu esse health ou alguém com permissão de gerenciar servidor pode usar esses botões."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            pass
        return False

    async def on_timeout(self) -> None:
        self._expired = True
        self._rebuild_expired_layout()
        if self.message is not None:
            try:
                await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())
            except TypeError:
                try:
                    await self.message.edit(view=self)
                except Exception:
                    pass
            except Exception:
                pass

    async def _go_first(self, interaction: discord.Interaction):
        self.guild_page_index = 0
        self._touch()
        self._rebuild_layout(refresh_snapshot=True)
        await interaction.response.edit_message(view=self)

    async def _go_prev(self, interaction: discord.Interaction):
        if self.guild_page_index > 0:
            self.guild_page_index -= 1
        self._touch()
        self._rebuild_layout(refresh_snapshot=True)
        await interaction.response.edit_message(view=self)

    async def _open_jump_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_HealthPageJumpModal(self))

    async def _go_next(self, interaction: discord.Interaction):
        if self.guild_page_index < self.total_guild_pages() - 1:
            self.guild_page_index += 1
        self._touch()
        self._rebuild_layout(refresh_snapshot=True)
        await interaction.response.edit_message(view=self)

    async def _go_last(self, interaction: discord.Interaction):
        self.guild_page_index = max(0, self.total_guild_pages() - 1)
        self._touch()
        self._rebuild_layout(refresh_snapshot=True)
        await interaction.response.edit_message(view=self)

    async def _refresh(self, interaction: discord.Interaction):
        self._touch()
        self._rebuild_layout(refresh_snapshot=True)
        await interaction.response.edit_message(view=self)

    async def _close(self, interaction: discord.Interaction):
        self._expired = True
        self.stop()
        message = getattr(interaction, "message", None) or self.message
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception:
            pass
        if message is not None:
            try:
                await message.delete()
                return
            except Exception:
                pass
        self._rebuild_expired_layout()
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(view=self)
            else:
                await interaction.response.edit_message(view=self)
        except Exception:
            pass


class Utility(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._app_command_id_cache: dict[object, tuple[float, dict[str, int]]] = {}
        self._help_sessions_by_user: dict[tuple[int, int], HelpPaginatorView] = {}
        self._help_sessions_by_message: dict[int, HelpPaginatorView] = {}
        self._help_session_lock = asyncio.Lock()

    def _get_db(self):
        return getattr(self.bot, "settings_db", None)

    async def _maybe_await(self, value: Any):
        if inspect.isawaitable(value):
            return await value
        return value


    def _help_guild_id(self, guild: discord.Guild | None) -> int:
        return int(getattr(guild, "id", 0) or 0)

    def _help_user_key(self, guild: discord.Guild | None, user_id: int) -> tuple[int, int]:
        return (self._help_guild_id(guild), int(user_id))

    async def _delete_message_safe(self, message: discord.Message | None) -> bool:
        if message is None:
            return True
        try:
            await message.delete()
            return True
        except Exception:
            return False

    def _unregister_help_session_now(self, view: HelpPaginatorView) -> None:
        for key, registered in list(self._help_sessions_by_user.items()):
            if registered is view:
                self._help_sessions_by_user.pop(key, None)
        message_id = int(getattr(getattr(view, "message", None), "id", 0) or 0)
        if message_id:
            self._help_sessions_by_message.pop(message_id, None)

    async def _unregister_help_session(self, view: HelpPaginatorView) -> None:
        async with self._help_session_lock:
            self._unregister_help_session_now(view)

    async def _close_help_session(self, view: HelpPaginatorView | None, *, delete_message: bool = True) -> None:
        if view is None:
            return
        self._unregister_help_session_now(view)
        try:
            view.stop()
        except Exception:
            pass
        if delete_message:
            deleted = await self._delete_message_safe(getattr(view, "message", None))
            if not deleted and getattr(view, "message", None) is not None:
                try:
                    view._expired = True
                    view._rebuild_expired_layout()
                    await view.message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
                except TypeError:
                    try:
                        await view.message.edit(view=view)
                    except Exception:
                        pass
                except Exception:
                    pass

    async def _help_message_missing(self, view: HelpPaginatorView) -> bool:
        message = getattr(view, "message", None)
        if message is None:
            return False

        flags = getattr(message, "flags", None)
        if bool(getattr(flags, "ephemeral", False)):
            return False

        channel = getattr(message, "channel", None)
        fetch_message = getattr(channel, "fetch_message", None)
        if not callable(fetch_message):
            return False

        try:
            await fetch_message(message.id)
            return False
        except discord.NotFound:
            return True
        except Exception:
            return False

    async def _cleanup_help_sessions(self, guild_id: int | None = None) -> None:
        for key, view in list(self._help_sessions_by_user.items()):
            if guild_id is not None and key[0] != int(guild_id):
                continue
            if view._is_expired() or view.is_finished() or await self._help_message_missing(view):
                self._unregister_help_session_now(view)

    def _cleanup_help_sessions_now(self) -> None:
        for view in list(self._help_sessions_by_user.values()):
            if view._is_expired() or view.is_finished():
                self._unregister_help_session_now(view)

    def _active_help_sessions_for_guild(self, guild_id: int) -> int:
        self._cleanup_help_sessions_now()
        count = 0
        for key, view in self._help_sessions_by_user.items():
            if key[0] == int(guild_id) and not view._is_expired() and not view.is_finished():
                count += 1
        return count

    async def _send_help_limit_message(
        self,
        *,
        responder: discord.abc.Messageable,
        interaction: discord.Interaction | None,
    ) -> None:
        message = (
            "Já existem 5 centrais de ajuda abertas neste servidor. "
            "Feche uma delas ou espere expirar."
        )
        if interaction is not None:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except Exception:
                pass
            return
        try:
            await responder.send(message, delete_after=12)
        except TypeError:
            try:
                await responder.send(message)
            except Exception:
                pass
        except Exception:
            pass

    async def _get_prefix_data(self, guild: discord.Guild | None) -> dict[str, str]:
        defaults = {
            "bot_prefix": str(getattr(config, "BOT_PREFIX", getattr(config, "PREFIX", "_")) or "_"),
            "gtts_prefix": ".",
            "edge_prefix": ",",
            "gcloud_prefix": str(getattr(config, "GOOGLE_CLOUD_TTS_PREFIX", "'") or "'"),
        }
        if guild is None:
            return defaults

        db = self._get_db()
        if db is None or not hasattr(db, "get_guild_tts_defaults"):
            return defaults

        try:
            guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(guild.id))
        except Exception:
            guild_defaults = {}

        guild_defaults = guild_defaults or {}
        defaults["bot_prefix"] = str(guild_defaults.get("bot_prefix", defaults["bot_prefix"]) or defaults["bot_prefix"])
        defaults["gtts_prefix"] = str(guild_defaults.get("gtts_prefix", guild_defaults.get("tts_prefix", defaults["gtts_prefix"])) or defaults["gtts_prefix"])
        defaults["edge_prefix"] = str(guild_defaults.get("edge_prefix", defaults["edge_prefix"]) or defaults["edge_prefix"])
        defaults["gcloud_prefix"] = str(guild_defaults.get("gcloud_prefix", defaults["gcloud_prefix"]) or defaults["gcloud_prefix"])
        return defaults

    async def _fetch_root_command_ids_cached(self, guild: discord.Guild | None) -> dict[str, int]:
        return await fetch_root_command_ids_cached(
            self.bot,
            self._app_command_id_cache,
            guild,
            ttl_seconds=600.0,
            include_global_fallback=True,
        )

    def _build_help_pages(self, *, guild: discord.Guild | None, prefixes: dict[str, str], root_ids: dict[str, int]) -> list[HelpPage]:
        bot_prefix = prefixes["bot_prefix"]
        gtts_prefix = prefixes["gtts_prefix"]
        edge_prefix = prefixes["edge_prefix"]
        gcloud_prefix = prefixes["gcloud_prefix"]

        # Slash mentions: cada um vira `</nome:ID>` no Discord (clicável). Quando
        # o root não está no cache (comando guild-only fora da guild correta,
        # por exemplo), o helper retorna texto literal `/path`.
        help_slash = slash_mention(root_ids, root="help", path="help")
        ping_slash = slash_mention(root_ids, root="ping", path="ping")
        tts_menu_slash = slash_mention(root_ids, root="tts", path="tts menu")
        tts_status_slash = slash_mention(root_ids, root="tts", path="tts status")
        tts_user_slash = slash_mention(root_ids, root="tts", path="tts usuario")
        tts_server_menu_slash = slash_mention(root_ids, root="tts", path="tts server menu")
        chatbot_profile_slash = slash_mention(root_ids, root="chatbot", path="chatbot profile")
        chatbot_memoria_slash = slash_mention(root_ids, root="chatbot", path="chatbot memoria")
        imagem_slash = slash_mention(root_ids, root="imagem", path="imagem")
        chatbot_reset_slash = slash_mention(root_ids, root="reset", path="reset")
        economia_slash = slash_mention(root_ids, root="economia", path="economia")

        prefix_help = format_prefixed_aliases(bot_prefix, "help")
        prefix_panel = format_prefixed_aliases(bot_prefix, "panel_user")
        prefix_server_panel = format_prefixed_aliases(bot_prefix, "panel_server")
        prefix_join = format_prefixed_aliases(bot_prefix, "join")
        prefix_leave = format_prefixed_aliases(bot_prefix, "leave")
        prefix_clear = format_prefixed_aliases(bot_prefix, "clear")
        prefix_reset = format_prefixed_aliases(bot_prefix, "reset")
        prefix_set_lang = format_prefixed_aliases(bot_prefix, "set_lang")
        prefix_color = format_prefixed_aliases(bot_prefix, "color")
        prefix_coloredit = format_prefixed_aliases(bot_prefix, "coloredit")

        pages: list[HelpPage] = []

        # === Página 1 — Visão geral ===========================================
        nav = (
            "**1.** Visão geral e começo rápido\n"
            "**2.** Atalhos de fala\n"
            "**3.** Comandos do usuário\n"
            "**4.** Comandos do servidor\n"
            "**5.** Chatbot e imagens\n"
            "**6.** Cores\n"
            "**7.** Formulário\n"
            "**8.** Utilidades\n"
            "**9.** Economia\n"
            "**10.** Jogos"
        )
        prefixes_text = (
            f"**Bot:** `{bot_prefix}`  ·  **gTTS:** `{gtts_prefix}`  ·  "
            f"**Edge:** `{edge_prefix}`  ·  **gcloud:** `{gcloud_prefix}`"
        )
        quick_start = (
            f"› {help_slash} ou {prefix_help} — abre esta central\n"
            f"› {tts_menu_slash} ou {prefix_panel} — painel pessoal de TTS\n"
            f"› `{edge_prefix}oi cheguei na call` — fala usando Edge\n"
            f"› `{gtts_prefix}teste de voz` — fala usando gTTS"
        )
        pages.append(HelpPage(
            title="📚 Central de ajuda",
            body=(
                "Bem-vindo ao painel de ajuda do bot. Os comandos estão separados "
                "por categoria; use os botões abaixo pra navegar e o número no centro "
                "abre uma seleção rápida de página.\n\n"
                f"### Navegação\n{nav}\n\n"
                f"### Prefixos ativos neste servidor\n{prefixes_text}\n\n"
                f"### Começo rápido\n{quick_start}"
            ),
            accent=discord.Color.blurple(),
        ))

        # === Página 2 — Atalhos de fala =======================================
        pages.append(HelpPage(
            title="🔊 Atalhos de fala",
            body=(
                "Não são painéis: são **prefixos de fala**. Você manda a mensagem "
                "começando com o prefixo e o bot fala.\n\n"
                f"### gTTS — `{gtts_prefix}`\n"
                f"Exemplo: `{gtts_prefix}olá tudo bem`\n"
                "Voz padrão da web, leve, funciona em qualquer cenário.\n\n"
                f"### Edge — `{edge_prefix}`\n"
                f"Exemplo: `{edge_prefix}essa frase vai no edge`\n"
                "Vozes neurais da Microsoft. Mais natural, com várias opções por idioma.\n\n"
                f"### Google Cloud — `{gcloud_prefix}`\n"
                f"Exemplo: `{gcloud_prefix}essa fala usa o google cloud`\n"
                "Vozes premium do Google. Qualidade alta, requer config no servidor.\n\n"
                "Voz, idioma, velocidade e tom usados nesses prefixos seguem o "
                f"painel pessoal ({tts_menu_slash}) ou o painel do servidor."
            ),
            accent=discord.Color.purple(),
        ))

        # === Página 3 — Comandos do usuário ===================================
        pages.append(HelpPage(
            title="👤 Comandos do usuário",
            body=(
                "Painéis pessoais e atalhos voltados para cada membro.\n\n"
                f"### Painel pessoal\n"
                f"{tts_menu_slash} ou {prefix_panel}\n"
                "Abre o painel principal do seu TTS — botões e menus para mexer "
                "em voz, idioma, velocidade, tom e apelido falado sem precisar decorar comando.\n\n"
                f"### Status do TTS\n"
                f"{tts_status_slash}\n"
                "Vê o seu status, mostra o de outro usuário ou copia a config "
                "dele pra você. Ações: `self`, `show_other`, `copy_other`.\n\n"
                f"### Gerenciar outro usuário\n"
                f"{tts_user_slash}\n"
                "Abre o painel de outro usuário, troca o apelido falado dele "
                "ou reseta as configs. Ações: `panel`, `spoken_name`, `reset`."
            ),
            accent=discord.Color.green(),
        ))

        # === Página 4 — Comandos de servidor ==================================
        pages.append(HelpPage(
            title="🛡️ Comandos do servidor",
            body=(
                "Área de staff. Comandos marcados com 🔒 dependem da permissão "
                "**Expulsar membros**.\n\n"
                f"### 🔒 Painel do servidor\n"
                f"{tts_server_menu_slash} ou {prefix_server_panel}\n"
                "Define os padrões do servidor — prefixos, engine padrão, permissões e configs globais.\n\n"
                "### Conexão\n"
                f"Entrar na call: {prefix_join}\n"
                f"Sair da call: {prefix_leave}\n"
                f"Limpar fila: {prefix_clear}\n\n"
                "### Atalhos rápidos\n"
                f"🔒 Resetar um usuário: {prefix_reset} `@usuário`\n"
                f"Trocar idioma pessoal do gTTS: {prefix_set_lang} `pt`"
            ),
            accent=discord.Color.gold(),
        ))

        # === Página 5 — Chatbot e imagens =====================================
        pages.append(HelpPage(
            title="🤖 Chatbot e imagens",
            body=(
                "Profiles conversacionais, geração de imagem e memória do chatbot. "
                "Profiles exigem **Manage Server**.\n\n"
                f"### Profiles\n"
                f"{chatbot_profile_slash}\n"
                "Cria, lista, edita, apaga, ativa ou desativa profiles do chatbot no servidor. "
                "Ações: `Criar`, `Listar`, `Editar`, `Apagar`, `Ativar`, `Desativar`.\n"
                "Só um profile fica ativo por servidor. Outros profiles podem ser invocados "
                "temporariamente com `@nome` em uma mensagem.\n\n"
                f"### Geração de imagem\n"
                f"{imagem_slash} `prompt`\n"
                "Gera uma imagem a partir do texto. Quanto mais específico o prompt, melhor.\n\n"
                f"### Memória\n"
                f"Reset pessoal: {chatbot_reset_slash}\n"
                "Limpa só a sua conversa pessoal com o profile ativo.\n\n"
                f"Reset do servidor: {chatbot_memoria_slash}\n"
                "Apaga toda a memória do chatbot no servidor — pessoal de cada membro "
                "mais a coletiva, todos os profiles. Irreversível, exige Manage Server.\n\n"
                "### Como conversar\n"
                "Mencione (`@bot`) ou responda a uma mensagem dele para continuar a conversa. "
                "Use `@nome do profile` para invocar outro profile temporariamente sem trocar o ativo. "
                "Imagens e áudios anexados são entendidos pelo profile."
            ),
            accent=discord.Color.fuchsia(),
        ))

        # === Página 6 — Cores =================================================
        pages.append(HelpPage(
            title="🎨 Cores",
            body=(
                "Painel de cargos coloridos do servidor. Comandos exigem **Administrador**.\n\n"
                f"### Publicar o painel\n"
                f"{prefix_color}\n"
                "Posta o painel público de cores no canal atual. Os membros clicam na cor "
                "que quiserem e ganham o cargo correspondente. Substitui o painel anterior se já existir um.\n\n"
                f"### Editar as cores disponíveis\n"
                f"{prefix_coloredit}\n"
                "Abre um editor interativo para adicionar, remover ou trocar cores do painel — "
                "escolhe nome, hex e ícone de cada uma. As mudanças aplicam no painel publicado automaticamente.\n\n"
                "Cooldown curto entre publicações para evitar spam de painéis."
            ),
            accent=discord.Color.from_rgb(255, 105, 180),
        ))

        # === Página 7 — Formulário ============================================
        pages.append(HelpPage(
            title="📋 Formulário",
            body=(
                "Sistema para publicar uma inscrição no servidor. O membro clica em "
                "**Preencher**, responde os campos e a staff recebe a resposta no canal configurado.\n\n"
                "### Comandos\n"
                "`form`\n"
                "Publica ou reposta o formulário público no canal configurado. "
                "Se os canais ainda não estiverem configurados, inicia a configuração.\n\n"
                "`c`\n"
                "Abre o painel de configuração do formulário para a staff.\n\n"
                "### Pelo painel `c`\n"
                "• editar o texto do formulário público;\n"
                "• configurar os campos que o membro preenche;\n"
                "• editar a resposta enviada para a staff;\n"
                "• ativar ou desativar aprovação;\n"
                "• escolher o cargo entregue ao aprovar;\n"
                "• editar textos e DMs de aprovação/rejeição;\n"
                "• mudar cores dos botões e dos cards.\n\n"
                "### Fluxo\n"
                "1. A staff configura usando `c`.\n"
                "2. A staff publica usando `form`.\n"
                "3. O membro clica em **Preencher**.\n"
                "4. A resposta chega no canal da staff.\n"
                "5. A staff aprova ou rejeita."
            ),
            accent=discord.Color.from_rgb(88, 101, 242),
        ))

        # === Página 8 — Utilidades ============================================
        pages.append(HelpPage(
            title="🧰 Utilidades",
            body=(
                "Comandos gerais e diagnósticos rápidos.\n\n"
                f"### Ping\n"
                f"{ping_slash}\n"
                "Latência, uptime, uso de recursos e status geral do bot.\n\n"
                f"### Help\n"
                f"{help_slash} ou {prefix_help}\n"
                "Abre esta central de ajuda. O número no centro do paginator "
                "abre um modal para pular direto para qualquer página."
            ),
            accent=discord.Color.teal(),
        ))

        # === Página 9 — Economia ==============================================
        pages.append(HelpPage(
            title="🪙 Economia",
            body=(
                "Saldo, daily, recarga, pagamentos e ranking. Triggers como `ficha` ou `daily` "
                "funcionam digitando a palavra sozinha no chat, sem prefixo.\n\n"
                "### Atalhos do dia a dia\n"
                f"Saldo: `{bot_prefix}ficha` ou `ficha`\n"
                "Extrato: `extrato` — 10 últimas movimentações\n"
                f"Daily: `{bot_prefix}daily` ou `daily`\n"
                "Recarga: `recarga` — volta saldo para 100 quando está abaixo de 15\n"
                f"Ranking: `{bot_prefix}rank` ou `rank`\n"
                "Pagar: `pay @usuário valor`\n"
                "Mendigar: `mendigar valor` ou `mendigar valor @usuário`\n\n"
                "### Como funciona o saldo\n"
                "Fichas bônus saem antes das normais nas apostas, e ganhos quitam dívida primeiro "
                "antes de voltar para o saldo. O daily dá fichas normais, fichas bônus e libera "
                "os giros extras do dia.\n\n"
                "### Painel de staff\n"
                f"`{bot_prefix}painelficha` (alias: `fichapainel`, `adminficha`)\n"
                "Painel completo para ajustar saldo, resetar usuário ou resetar o servidor inteiro. "
                "Qualquer staff com permissão **Expulsar membros** pode usar todos os botões.\n\n"
                "### Configuração\n"
                f"{economia_slash}\n"
                "Ativa/desativa a economia, define cargo staff e gerencia roles que recebem features extras."
            ),
            accent=discord.Color.orange(),
        ))

        # === Página 10 — Jogos =================================================
        pages.append(HelpPage(
            title="🎮 Jogos",
            body=(
                "Triggers são palavras digitadas sozinhas no chat. Se faltar saldo, "
                "o jogo avisa antes de te jogar no vermelho.\n\n"
                "### Apostas rápidas\n"
                "`roleta` — aposta com jackpot\n"
                "`carta` ou `cartas` — saque rápido de cartas\n\n"
                "### Lobbies com botão para entrar\n"
                "`buckshot` — rodada de sobrevivência\n"
                "`alvo` — disputa de mira\n"
                "`corrida` — corrida de cavalos\n\n"
                "### Mesas\n"
                "`poker` — mesa de poker com entrada própria\n"
                "`truco @usuário` — desafio de truco 1v1\n"
                "`truco2` — truco em duplas 2v2\n\n"
                "### Roubo\n"
                "`roubar @usuário` — alias `rob @usuário`\n"
                "Tenta roubar parte do saldo do alvo. Pode falhar; tem janela com cooldown."
            ),
            accent=discord.Color.dark_magenta(),
        ))

        return pages


    async def _send_help_response(
        self,
        *,
        guild: discord.Guild | None,
        owner: discord.abc.User,
        responder: discord.abc.Messageable,
        interaction: discord.Interaction | None = None,
        ephemeral: bool = False,
        prefix_command_message: discord.Message | None = None,
    ):
        prefixes = await self._get_prefix_data(guild)
        root_ids = await self._fetch_root_command_ids_cached(guild)
        pages = self._build_help_pages(guild=guild, prefixes=prefixes, root_ids=root_ids)
        avatar_url = None
        if self.bot.user and self.bot.user.display_avatar:
            avatar_url = self.bot.user.display_avatar.url
        view = HelpPaginatorView(
            self,
            owner_id=owner.id,
            pages=pages,
            command_mention=slash_mention(root_ids, root="help", path="help"),
            prefix_hint=f"`{prefixes['bot_prefix']}help`",
            bot_avatar_url=avatar_url,
            timeout=HELP_EXPIRE_AFTER_SECONDS,
        )

        guild_id = self._help_guild_id(guild)
        user_key = self._help_user_key(guild, int(owner.id))

        async with self._help_session_lock:
            await self._cleanup_help_sessions(guild_id)

            previous = self._help_sessions_by_user.get(user_key)
            if previous is not None:
                await self._close_help_session(previous, delete_message=True)

            if self._active_help_sessions_for_guild(guild_id) >= HELP_MAX_SESSIONS_PER_GUILD:
                await self._delete_message_safe(prefix_command_message)
                await self._send_help_limit_message(responder=responder, interaction=interaction)
                return

            # Components V2: o conteúdo (heading + corpo + botões) vai TODO dentro
            # do LayoutView. Não passa `embed=`, não passa `content=` — só `view=`.
            if interaction is not None:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        view=view,
                        ephemeral=ephemeral,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    try:
                        view.message = await interaction.original_response()
                    except Exception:
                        pass
                else:
                    view.message = await interaction.followup.send(
                        view=view,
                        ephemeral=ephemeral,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            else:
                view.message = await responder.send(
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await self._delete_message_safe(prefix_command_message)

            self._help_sessions_by_user[user_key] = view
            message_id = int(getattr(getattr(view, "message", None), "id", 0) or 0)
            if message_id:
                self._help_sessions_by_message[message_id] = view

    def _format_bool_badge(self, value: bool, *, ok_label: str = "OK", bad_label: str = "Falha") -> str:
        return f"🟢 {ok_label}" if bool(value) else f"🔴 {bad_label}"

    def _format_duration(self, total_seconds: float | int | None) -> str:
        try:
            total = int(float(total_seconds or 0))
        except Exception:
            total = 0
        days, rem = divmod(max(0, total), 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if seconds or not parts:
            parts.append(f"{seconds}s")
        return " ".join(parts)

    def _format_ms(self, value: Any) -> str:
        try:
            return f"{float(value):.2f} ms"
        except Exception:
            return "n/a"

    def _format_bytes_human(self, value: int | float | None) -> str:
        try:
            size = float(value or 0)
        except Exception:
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB"]
        idx = 0
        while size >= 1024.0 and idx < len(units) - 1:
            size /= 1024.0
            idx += 1
        if idx == 0:
            return f"{int(size)} {units[idx]}"
        return f"{size:.2f} {units[idx]}"

    def _collect_health_snapshot(self) -> dict[str, Any]:
        """Coleta todos os dados usados pelo painel `/health` em Components V2.

        A coleta fica separada da view para o botão Atualizar conseguir refazer
        a leitura sem duplicar lógica de métricas, cache e guilds.
        """
        snapshot: dict[str, Any] = {}
        get_snapshot = getattr(self.bot, "get_health_snapshot", None)
        if callable(get_snapshot):
            try:
                snapshot = dict(get_snapshot() or {})
            except Exception:
                snapshot = {}

        tts_metrics = dict(snapshot.get("tts_metrics") or {})
        engine_metrics = dict(tts_metrics.get("engines") or {})

        tmp_root = os.path.join(os.getcwd(), "tmp_audio")
        runtime_dir = os.path.join(tmp_root, "runtime")
        cache_dir = os.path.join(tmp_root, "cache")
        credentials_dir = os.path.join(tmp_root, "credentials")

        def _dir_stats(path: str) -> tuple[int, int]:
            total_bytes = 0
            total_files = 0
            try:
                for entry in os.scandir(path):
                    if not entry.is_file():
                        continue
                    total_files += 1
                    try:
                        total_bytes += int(entry.stat().st_size)
                    except Exception:
                        pass
            except Exception:
                return 0, 0
            return total_files, total_bytes

        runtime_files, runtime_bytes = _dir_stats(runtime_dir)
        cache_files, cache_bytes = _dir_stats(cache_dir)
        cred_files, cred_bytes = _dir_stats(credentials_dir)
        total_tmp_bytes = runtime_bytes + cache_bytes + cred_bytes

        def _guild_sort_key(guild: discord.Guild) -> tuple[int, str]:
            try:
                members = int(getattr(guild, "member_count", 0) or len(getattr(guild, "members", []) or []) or 0)
            except Exception:
                members = 0
            name = str(getattr(guild, "name", "") or "").casefold()
            return (-members, name)

        guilds = sorted(list(getattr(self.bot, "guilds", []) or []), key=_guild_sort_key)
        total_members = 0
        for guild in guilds:
            try:
                count = getattr(guild, "member_count", None)
                if count is None:
                    count = len(getattr(guild, "members", []) or [])
                total_members += int(count or 0)
            except Exception:
                pass

        snapshot.update({
            "tts_metrics": tts_metrics,
            "engine_metrics": engine_metrics,
            "runtime_files": runtime_files,
            "cache_files": cache_files,
            "cred_files": cred_files,
            "total_tmp_bytes": total_tmp_bytes,
            "guilds": guilds,
            "guild_count": len(guilds),
            "total_members": total_members,
        })
        return snapshot


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.content:
            return

        prefixes = await self._get_prefix_data(message.guild)
        bot_prefix = prefixes["bot_prefix"]
        if not matches_prefixed_command(message.content, bot_prefix, kind="help"):
            return

        await self._send_help_response(
            guild=message.guild,
            owner=message.author,
            responder=message.channel,
            prefix_command_message=message,
        )

    @app_commands.command(name="help", description="Mostra a central de ajuda com todos os comandos principais do bot")
    async def help_command(self, interaction: discord.Interaction):
        await self._send_help_response(
            guild=interaction.guild,
            owner=interaction.user,
            responder=interaction.channel,
            interaction=interaction,
            ephemeral=True,
        )

    @app_commands.command(name="health", description="Mostra a saúde geral do bot, fila, cache, engines e guilds")
    @app_commands.guilds(HEALTH_COMMAND_GUILD)
    async def health(self, interaction: discord.Interaction):
        avatar_url = None
        if self.bot.user and self.bot.user.display_avatar:
            avatar_url = self.bot.user.display_avatar.url

        view = HealthDashboardView(
            self,
            owner_id=int(interaction.user.id),
            bot_avatar_url=avatar_url,
            timeout=HELP_EXPIRE_AFTER_SECONDS,
        )
        await interaction.response.send_message(
            view=view,
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            view.message = await interaction.original_response()
        except Exception:
            pass

    @app_commands.command(name="ping", description="Mostra o status atual e a latência do bot")
    async def ping(self, interaction: discord.Interaction):
        import os
        import time

        try:
            import psutil
        except Exception:
            psutil = None

        start = time.perf_counter()
        await interaction.response.defer(ephemeral=True)

        ws_ping = round(self.bot.latency * 1000)
        response_ping = round((time.perf_counter() - start) * 1000)

        now = discord.utils.utcnow()
        started_at = getattr(self.bot, "started_at", None)
        if started_at is None:
            uptime_text = "Desconhecido"
        else:
            delta = now - started_at
            total_seconds = int(delta.total_seconds())
            days, rem = divmod(total_seconds, 86400)
            hours, rem = divmod(rem, 3600)
            minutes, seconds = divmod(rem, 60)
            parts = []
            if days:
                parts.append(f"{days}d")
            if hours:
                parts.append(f"{hours}h")
            if minutes:
                parts.append(f"{minutes}m")
            if seconds or not parts:
                parts.append(f"{seconds}s")
            uptime_text = " ".join(parts)

        shard_id = getattr(interaction.guild, "shard_id", None)
        shard_text = str(shard_id) if shard_id is not None else "Único"
        db = getattr(self.bot, "settings_db", None)
        db_status = "🟢 Online" if db is not None else "🔴 Offline"

        memory_mb = 0.0
        cpu_percent = 0.0
        if psutil is not None:
            process = psutil.Process(os.getpid())
            memory_mb = process.memory_info().rss / 1024 / 1024
            cpu_percent = psutil.cpu_percent(interval=None)

        if ws_ping < 120:
            status_text, color = "🟢 Excelente", discord.Color.green()
        elif ws_ping < 250:
            status_text, color = "🟡 Boa", discord.Color.gold()
        elif ws_ping < 400:
            status_text, color = "🟠 Instável", discord.Color.orange()
        else:
            status_text, color = "🔴 Alta", discord.Color.red()

        embed = discord.Embed(title="🏓 Pong!", description="Status atual do bot em tempo real.", color=color)
        embed.add_field(name="Latência WebSocket", value=f"`{ws_ping}ms`", inline=True)
        embed.add_field(name="Resposta do comando", value=f"`{response_ping}ms`", inline=True)
        embed.add_field(name="Status geral", value=status_text, inline=True)
        embed.add_field(name="Uptime", value=f"`{uptime_text}`", inline=True)
        embed.add_field(name="Banco de dados", value=db_status, inline=True)
        embed.add_field(name="Shard", value=f"`{shard_text}`", inline=True)
        embed.add_field(name="Uso de memória", value=f"`{memory_mb:.2f} MB`", inline=True)
        embed.add_field(name="Uso de CPU", value=f"`{cpu_percent:.1f}%`", inline=True)
        embed.add_field(name="Servidores", value=f"`{len(self.bot.guilds)}`", inline=True)
        if self.bot.user and self.bot.user.display_avatar:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.set_footer(text="Atualizado no momento do comando")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
