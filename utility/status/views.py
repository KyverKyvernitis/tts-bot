from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Callable

import discord

from .data import StatusCollector, StatusSnapshot
from .render import TTS_SECTIONS, PanelPage, panel_pages, safe_text


logger = logging.getLogger(__name__)
STATUS_TIMEOUT_SECONDS = 300.0
REFRESH_COOLDOWN_SECONDS = 3.0
COLLECTION_TIMEOUT_SECONDS = 20.0


class _StatusButton(discord.ui.Button):
    def __init__(self, panel: "StatusView", *, label: str, action: str, value: Any = None,
                 disabled: bool = False, style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        super().__init__(label=label, style=style, disabled=disabled)
        self.panel = panel
        self.action = action
        self.value = value
        self.source_revision = panel.revision

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.navigate(interaction, self.action, value=self.value, source_revision=self.source_revision)


class _TTSSectionSelect(discord.ui.Select):
    def __init__(self, panel: "StatusView"):
        super().__init__(
            placeholder="Detalhes do TTS",
            options=[discord.SelectOption(label=label, value=key, default=key == panel.section) for key, label in TTS_SECTIONS.items()],
            disabled=panel.closed,
        )
        self.panel = panel
        self.source_revision = panel.revision

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.navigate(interaction, "section", value=self.values[0], source_revision=self.source_revision)


class StatusView(discord.ui.LayoutView):
    """Uma mensagem por sessão; abas e páginas leem apenas o snapshot."""

    def __init__(self, *, owner_id: int, snapshot: StatusSnapshot, collector: StatusCollector,
                 tab: str = "servers", prefix: str = "_", on_close: Callable[["StatusView"], None] | None = None,
                 timeout: float = STATUS_TIMEOUT_SECONDS):
        super().__init__(timeout=timeout)
        self.owner_id = int(owner_id)
        self.snapshot = snapshot
        self.collector = collector
        self.tab = "tts" if tab == "tts" else "servers"
        self.section = "summary"
        self.page = 0
        self.prefix = prefix
        self.revision = 0
        self.message: discord.Message | None = None
        self.closed = False
        self._notice = ""
        self._on_close = on_close
        self._lock = asyncio.Lock()
        self._last_refresh = float("-inf")
        self._page_cache: dict[tuple[str, str], tuple[PanelPage, ...]] = {}
        self._rebuild()

    def _pages(self) -> tuple[PanelPage, ...]:
        # TTS detail selection has no effect on the servers snapshot/pages.
        key = (self.tab, self.section if self.tab == "tts" else "summary")
        if key not in self._page_cache:
            self._page_cache[key] = panel_pages(self.snapshot, self.tab, self.section)
        return self._page_cache[key]

    def _rebuild(self) -> None:
        self.clear_items()
        pages = self._pages()
        self.page = min(max(0, self.page), len(pages) - 1)
        page = pages[self.page]
        children: list[discord.ui.Item[Any]] = []
        header = discord.ui.TextDisplay(page.header)
        if self.snapshot.thumbnail_url:
            children.append(discord.ui.Section(header, accessory=discord.ui.Thumbnail(self.snapshot.thumbnail_url, description="Avatar do bot")))
        else:
            children.append(header)
        children.append(discord.ui.ActionRow(
            _StatusButton(self, label="Servidores", action="tab", value="servers", disabled=self.closed or self.tab == "servers", style=discord.ButtonStyle.primary if self.tab == "servers" else discord.ButtonStyle.secondary),
            _StatusButton(self, label="TTS", action="tab", value="tts", disabled=self.closed or self.tab == "tts", style=discord.ButtonStyle.primary if self.tab == "tts" else discord.ButtonStyle.secondary),
        ))
        if self.tab == "tts":
            children.append(discord.ui.ActionRow(_TTSSectionSelect(self)))
        children.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        for index, card in enumerate(page.cards):
            if index:
                children.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            text = discord.ui.TextDisplay(card.text)
            if card.thumbnail_url:
                children.append(discord.ui.Section(text, accessory=discord.ui.Thumbnail(card.thumbnail_url, description="Ícone do servidor")))
            else:
                children.append(text)
        footer = page.footer
        if self.closed:
            footer += f"\nPainel encerrado · abra {safe_text(self.prefix + 'status', 80)} novamente."
        if self._notice:
            footer += "\n" + self._notice
        children.append(discord.ui.TextDisplay("-# " + footer.replace("\n", "\n-# ")))
        children.append(discord.ui.ActionRow(
            _StatusButton(self, label="Anterior", action="page", value=-1, disabled=self.closed or self.page == 0),
            _StatusButton(self, label="Próxima", action="page", value=1, disabled=self.closed or self.page >= len(pages) - 1),
            _StatusButton(self, label="Atualizar", action="refresh", disabled=self.closed),
        ))
        self.add_item(discord.ui.Container(*children, accent_color=page.accent))

    async def _private_notice(self, interaction: discord.Interaction, message: str) -> None:
        with contextlib.suppress(Exception):
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            else:
                await interaction.response.send_message(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.owner_id:
            await self._private_notice(interaction, "Só quem abriu este painel pode usar os controles.")
            return False
        if self.closed:
            await self._private_notice(interaction, "Este painel encerrou. Abra o comando status novamente.")
            return False
        return True

    async def navigate(self, interaction: discord.Interaction, action: str, *, value: Any = None,
                       source_revision: int) -> None:
        if not await self.interaction_check(interaction):
            return
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception:
            logger.debug("[utility/status] não foi possível reconhecer controle", exc_info=True)
            return
        async with self._lock:
            if self.closed or source_revision != self.revision:
                return
            previous = (self.snapshot, self.tab, self.section, self.page, self.revision, self._notice, self._page_cache)
            self._notice = ""
            if action == "tab" and value in {"servers", "tts"}:
                self.tab = value
                self.page = 0
            elif action == "section" and value in TTS_SECTIONS:
                self.section = value
                self.page = 0
            elif action == "page" and value in {-1, 1}:
                target = self.page + value
                if target < 0 or target >= len(self._pages()):
                    return
                self.page = target
            elif action == "refresh":
                now = time.monotonic()
                if now - self._last_refresh < REFRESH_COOLDOWN_SECONDS:
                    await self._private_notice(interaction, "Aguarde alguns segundos antes de atualizar novamente.")
                    return
                self._last_refresh = now
                try:
                    candidate = await asyncio.wait_for(self.collector.collect(), timeout=COLLECTION_TIMEOUT_SECONDS)
                    lost_source = any(getattr(self.snapshot, field) and not getattr(candidate, field) for field in ("servers_available", "stats_available", "tts_available"))
                    old_complete_stats = self.snapshot.stats_available and all(row.stats_available for row in self.snapshot.servers)
                    new_complete_stats = candidate.stats_available and all(row.stats_available for row in candidate.servers)
                    if lost_source or (old_complete_stats and not new_complete_stats) or (self.snapshot.storage.total_bytes is not None and candidate.storage.total_bytes is None):
                        raise RuntimeError("uma fonte do snapshot ficou indisponível")
                    if self.closed:
                        return
                    self.snapshot = candidate
                    self._page_cache = {}
                except Exception:
                    logger.exception("[utility/status] atualização indisponível; snapshot anterior preservado")
                    self._notice = "Não foi possível atualizar; os dados anteriores foram preservados."
            else:
                return
            self.revision += 1
            self._rebuild()
            try:
                edited = await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())
                if edited is not None:
                    self.message = edited
            except Exception:
                self.snapshot, self.tab, self.section, self.page, self.revision, self._notice, self._page_cache = previous
                self._rebuild()
                logger.exception("[utility/status] falha ao editar painel")
                await self._private_notice(interaction, "Não foi possível atualizar a mensagem. Tente novamente.")

    def deactivate(self) -> None:
        """Interrompe callbacks imediatamente, inclusive durante unload da cog."""
        if self.closed:
            return
        self.closed = True
        self.stop()
        if self._on_close is not None:
            self._on_close(self)
            self._on_close = None

    async def close(self) -> None:
        self.deactivate()
        async with self._lock:
            self._rebuild()
            if self.message is not None:
                with contextlib.suppress(Exception):
                    await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def on_timeout(self) -> None:
        await self.close()
