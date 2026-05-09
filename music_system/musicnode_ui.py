from __future__ import annotations

import contextlib
from typing import Any

import discord


_ALLOWED_MODES = {"off", "shadow", "lavalink", "auto"}
_SEARCH_TYPES = {"auto", "youtube", "soundcloud", "raw"}


def _escape(value: object, *, limit: int = 220, empty: str = "—") -> str:
    text = str(value or "").strip()
    if not text:
        return empty
    text = discord.utils.escape_markdown(text)
    if len(text) > limit:
        text = text[: max(0, limit - 3)].rstrip() + "..."
    return text


def _modal_v2_available() -> bool:
    return all(hasattr(discord.ui, attr) for attr in ("Label", "Checkbox", "CheckboxGroup", "RadioGroup"))


def _flag_values(group) -> set[str]:
    values = getattr(group, "values", []) or []
    result: set[str] = set()
    for value in values:
        result.add(str(getattr(value, "value", value)))
    return result


def _parse_port(value: str) -> int:
    try:
        port = int(str(value or "").strip())
    except Exception as exc:
        raise ValueError("Porta precisa ser um número. Exemplo: 2333.") from exc
    if port < 1 or port > 65535:
        raise ValueError("Porta precisa ficar entre 1 e 65535.")
    return port


def _normalize_mode(value: str) -> str:
    raw = str(value or "off").strip().lower()
    aliases = {
        "desativado": "off",
        "desligado": "off",
        "off": "off",
        "0": "off",
        "shadow": "shadow",
        "teste": "shadow",
        "diagnostico": "shadow",
        "diagnóstico": "shadow",
        "lavalink": "lavalink",
        "real": "lavalink",
        "ativo": "lavalink",
        "auto": "auto",
        "automatico": "auto",
        "automático": "auto",
    }
    return aliases.get(raw, raw if raw in _ALLOWED_MODES else "off")


def _mode_label(mode: str) -> str:
    mode = _normalize_mode(mode)
    return {
        "off": "⛔ Desativado",
        "shadow": "👻 Shadow/teste",
        "lavalink": "🎶 Lavalink real",
        "auto": "🧰 Auto/fallback",
    }.get(mode, "⛔ Desativado")


def _mode_hint(mode: str) -> str:
    mode = _normalize_mode(mode)
    return {
        "off": "não consulta o node; tudo segue local",
        "shadow": "testa Lavalink sem trocar o player real",
        "lavalink": "preparado para ativação real em patch futuro",
        "auto": "preparado para fallback automático em patch futuro",
    }.get(mode, "não consulta o node")


def _status_icon(health: Any) -> str:
    if getattr(health, "available", False):
        return "🟢"
    if getattr(health, "configured", False):
        return "🟡"
    return "🔴"


def _available_label(health: Any) -> str:
    if getattr(health, "available", False):
        return "online"
    if getattr(health, "configured", False):
        return "configurado, mas sem resposta"
    return "não configurado"


def _accent_for(health: Any, summary: dict[str, Any]) -> discord.Color:
    if getattr(health, "available", False):
        return discord.Color.green()
    if summary.get("configured"):
        return discord.Color.gold()
    return discord.Color.red()


def _format_test_result(result: Any) -> str:
    icon = "🟢" if getattr(result, "ok", False) else "🔴"
    lines = [
        f"{icon} **Teste Lavalink**",
        f"• query: `{_escape(getattr(result, 'query', ''), limit=220)}`",
        f"• resultado: `{'OK' if getattr(result, 'ok', False) else 'falhou'}`",
    ]
    latency = getattr(result, "latency_ms", None)
    if latency is not None:
        lines.append(f"• latência: `{latency} ms`")
    load_type = getattr(result, "load_type", "") or ""
    if load_type:
        lines.append(f"• loadType: `{_escape(load_type, limit=40)}`")
    lines.append(f"• tracks encontradas: `{int(getattr(result, 'tracks_found', 0) or 0)}`")
    playlist = getattr(result, "playlist_name", "") or ""
    if playlist:
        lines.append(f"• playlist: `{_escape(playlist, limit=120)}`")
    title = getattr(result, "first_title", "") or ""
    if title:
        author = getattr(result, "first_author", "") or ""
        source = getattr(result, "first_source", "") or ""
        extra = []
        if author:
            extra.append(_escape(author, limit=80))
        if source:
            extra.append(_escape(source, limit=40))
        tail = f" • {' • '.join(extra)}" if extra else ""
        lines.append(f"• primeira: **{_escape(title, limit=120)}**{tail}")
    message = getattr(result, "message", "") or ""
    if message:
        lines.append(f"• detalhe: {_escape(message, limit=300)}")
    return "\n".join(lines)


def _make_mode_radio_group(current: str):
    group = discord.ui.RadioGroup(required=True)
    current = _normalize_mode(current)
    for value, label, description in (
        ("off", "Desativado", "Não consulta o node Lavalink."),
        ("shadow", "Shadow/teste", "Testa Lavalink sem trocar o player atual."),
        ("lavalink", "Lavalink real", "Reservado para ativação real futura."),
        ("auto", "Auto/fallback", "Reservado para fallback futuro."),
    ):
        group.add_option(label=label, value=value, description=description, default=(value == current))
    return group


def _make_search_type_radio_group(current: str = "auto"):
    current = str(current or "auto").strip().lower()
    if current not in _SEARCH_TYPES:
        current = "auto"
    group = discord.ui.RadioGroup(required=True)
    for value, label, description in (
        ("auto", "Auto", "Link direto ou YouTube search para texto."),
        ("youtube", "YouTube", "Força ytsearch para texto."),
        ("soundcloud", "SoundCloud", "Força scsearch para texto."),
        ("raw", "Sem prefixo extra", "Não força YouTube/SoundCloud; o backend decide."),
    ):
        group.add_option(label=label, value=value, description=description, default=(value == current))
    return group


def _prepare_query_for_test(query: str, search_type: str) -> str:
    query = str(query or "").strip()
    search_type = str(search_type or "auto").strip().lower()
    if not query or "://" in query or search_type == "auto":
        return query
    lower = query.lower()
    if lower.startswith(("ytsearch:", "ytmsearch:", "scsearch:", "amsearch:", "dzsearch:", "spsearch:")):
        return query
    if search_type == "youtube":
        return f"ytsearch:{query}"
    if search_type == "soundcloud":
        return f"scsearch:{query}"
    return query


class MusicNodeConfigModal(discord.ui.Modal):
    def __init__(self, panel: "MusicNodePanelView") -> None:
        super().__init__(title="Configurar conexão Lavalink", timeout=300)
        if not _modal_v2_available():
            raise RuntimeError("discord.py 2.7+ é necessário para os modals novos do `_musicnode`.")
        self.panel = panel
        summary = panel.router.lavalink_config_summary()
        host_default = str(summary.get("host_label") or "") if summary.get("host_defined") else ""
        self.node_name = discord.ui.TextInput(
            label="Nome do node",
            default=str(summary.get("node_name") or "main")[:80],
            placeholder="main",
            max_length=80,
            required=True,
        )
        self.host = discord.ui.TextInput(
            label="Host ou IP",
            default=host_default[:180],
            placeholder="lavalink.exemplo.com ou 1.2.3.4",
            max_length=180,
            required=True,
        )
        self.port = discord.ui.TextInput(
            label="Porta",
            default=str(summary.get("port") or 2333),
            placeholder="2333",
            max_length=5,
            required=True,
        )
        self.password = discord.ui.TextInput(
            label="Senha",
            default="",
            placeholder="nova senha ou vazio para manter a atual",
            max_length=300,
            required=False,
        )
        self.secure_checkbox = discord.ui.Checkbox(default=bool(summary.get("secure")))
        for item in (self.node_name, self.host, self.port, self.password):
            self.add_item(item)
        self.add_item(discord.ui.Label(
            text="Secure/SSL",
            description="Marque para usar https/wss. Deixe desmarcado para http/ws.",
            component=self.secure_checkbox,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            port = _parse_port(str(self.port.value))
            secure = bool(getattr(self.secure_checkbox, "value", False))
            host = str(self.host.value or "").strip().replace("http://", "").replace("https://", "").strip().strip("/")
            if not host:
                raise ValueError("Host não pode ficar vazio.")
            password_value = str(self.password.value or "").strip()
            summary = self.panel.router.lavalink_config_summary()
            if not password_value and not summary.get("password_defined"):
                raise ValueError("Senha não pode ficar vazia na primeira configuração.")
        except Exception as exc:
            await interaction.response.send_message(f"`⚠️` {discord.utils.escape_markdown(str(exc))}", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.panel.router.update_lavalink_node_config(
            node_name=str(self.node_name.value or "main").strip() or "main",
            host=host,
            port=port,
            password=password_value if password_value else None,
            secure=secure,
        )
        await self.panel.safe_refresh()
        options = self.panel.router.lavalink_config_summary().get("options", {}) or {}
        if bool(options.get("test_after_save")):
            statuses = await self.panel.router.backend_status()
            health = statuses.get("lavalink")
            await interaction.followup.send(
                "`✅` Node salvo e testado. "
                f"Resultado: `{_status_icon(health)} {_available_label(health)}`. "
                "O player real ainda continua local neste patch.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "`✅` Node Lavalink salvo. Se o modo estava desligado, ele foi colocado em `shadow` para teste seguro.\n"
            "O player real ainda continua local neste patch.",
            ephemeral=True,
        )


class MusicNodeTestModal(discord.ui.Modal):
    def __init__(self, panel: "MusicNodePanelView") -> None:
        super().__init__(title="Testar busca no Lavalink", timeout=300)
        if not _modal_v2_available():
            raise RuntimeError("discord.py 2.7+ é necessário para os modals novos do `_musicnode`.")
        self.panel = panel
        self.query = discord.ui.TextInput(
            label="Busca ou link",
            placeholder="Cole um link ou pesquise uma música",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=True,
        )
        self.search_type_group = _make_search_type_radio_group("auto")
        self.add_item(self.query)
        self.add_item(discord.ui.Label(
            text="Tipo de teste",
            description="Use Auto na maioria dos casos. SoundCloud força scsearch para texto.",
            component=self.search_type_group,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        query = str(self.query.value or "").strip()
        if not query:
            await interaction.response.send_message("`⚠️` Busca vazia.", ephemeral=True)
            return
        search_type = str(getattr(self.search_type_group, "value", "auto") or "auto")
        query = _prepare_query_for_test(query, search_type)
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.panel.router.test_lavalink_backend(
            query,
            requester_id=int(getattr(getattr(interaction, "user", None), "id", 0) or 0),
            requester_name=getattr(getattr(interaction, "user", None), "display_name", str(getattr(interaction, "user", ""))),
        )
        self.panel.last_test_result = result
        await self.panel.safe_refresh()
        await interaction.followup.send(_format_test_result(result), ephemeral=True)


class MusicNodeModeOptionsModal(discord.ui.Modal):
    def __init__(self, panel: "MusicNodePanelView") -> None:
        super().__init__(title="Modo e opções Lavalink", timeout=300)
        if not _modal_v2_available():
            raise RuntimeError("discord.py 2.7+ é necessário para os modals novos do `_musicnode`.")
        self.panel = panel
        summary = panel.router.lavalink_config_summary()
        current = str(summary.get("mode") or "off")
        options = summary.get("options", {}) or {}
        self.mode_group = _make_mode_radio_group(current)
        self.options_group = discord.ui.CheckboxGroup(required=False, min_values=0, max_values=2)
        self.options_group.add_option(
            label="Ocultar host no painel",
            value="hide_host_in_panel",
            description="Mostra só se o host está definido, sem revelar o endereço.",
            default=bool(options.get("hide_host_in_panel", True)),
        )
        self.options_group.add_option(
            label="Testar node ao salvar",
            value="test_after_save",
            description="Faz health check automaticamente após configurar conexão.",
            default=bool(options.get("test_after_save", False)),
        )
        self.add_item(discord.ui.Label(
            text="Modo de operação",
            description="Shadow é o modo seguro: testa Lavalink sem trocar o player local.",
            component=self.mode_group,
        ))
        self.add_item(discord.ui.Label(
            text="Preferências do painel",
            description="Opções visuais e de diagnóstico da central.",
            component=self.options_group,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        mode = _normalize_mode(str(getattr(self.mode_group, "value", "off") or "off"))
        flags = _flag_values(self.options_group)
        if mode not in _ALLOWED_MODES:
            await interaction.response.send_message("`⚠️` Escolha um modo válido.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.panel.router.set_lavalink_mode(mode)
        await self.panel.router.update_lavalink_panel_options(
            hide_host_in_panel="hide_host_in_panel" in flags,
            test_after_save="test_after_save" in flags,
        )
        await self.panel.safe_refresh()
        await interaction.followup.send(
            f"`✅` Modo alterado para **{_mode_label(mode)}**. {_mode_hint(mode).capitalize()}.\n"
            "Neste patch o player real ainda continua local.",
            ephemeral=True,
        )


class MusicNodeClearModal(discord.ui.Modal):
    def __init__(self, panel: "MusicNodePanelView") -> None:
        super().__init__(title="Limpar configuração Lavalink", timeout=300)
        if not _modal_v2_available():
            raise RuntimeError("discord.py 2.7+ é necessário para os modals novos do `_musicnode`.")
        self.panel = panel
        self.confirm_checkbox = discord.ui.Checkbox(default=False)
        self.add_item(discord.ui.Label(
            text="Confirmar limpeza",
            description="Marque para apagar host, porta, senha e voltar o modo para off.",
            component=self.confirm_checkbox,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not bool(getattr(self.confirm_checkbox, "value", False)):
            await interaction.response.send_message("`⚠️` Limpeza cancelada; a caixa de confirmação não foi marcada.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.panel.router.clear_lavalink_config()
        await self.panel.safe_refresh()
        await interaction.followup.send("`🧹` Configuração Lavalink limpa e modo voltou para `off`.", ephemeral=True)


class MusicNodePanelView(discord.ui.LayoutView):
    def __init__(self, router, bot, owner_id: int) -> None:
        super().__init__(timeout=600)
        self.router = router
        self.bot = bot
        self.owner_id = int(owner_id or 0)
        self.message: discord.Message | None = None
        self.last_test_result: Any | None = None
        self._statuses: dict[str, Any] = {}
        self._runtime: dict[str, Any] = {}
        self._summary: dict[str, Any] = {}
        self._expired = False
        self._build_loading_layout()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._expired:
            await interaction.response.send_message("Essa central Lavalink expirou. Use `_musicnode` novamente.", ephemeral=True)
            return False
        user_id = int(getattr(getattr(interaction, "user", None), "id", 0) or 0)
        allowed = user_id == self.owner_id
        if not allowed:
            with contextlib.suppress(Exception):
                allowed = await self.bot.is_owner(interaction.user)  # type: ignore[arg-type]
        if not allowed:
            await interaction.response.send_message("Esse painel técnico só pode ser usado pelo dono que abriu o `_musicnode`.", ephemeral=True)
            return False
        return True

    def _build_loading_layout(self) -> None:
        self.clear_items()
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                "# 🔌 Lavalink Node Manager\n"
                "Carregando estado do node e preparando a central técnica..."
            ),
            accent_color=discord.Color.blurple(),
        ))

    async def prepare(self) -> None:
        self._statuses = await self.router.backend_status()
        self._runtime = self.router.backend_runtime_summary()
        self._summary = self.router.lavalink_config_summary()
        self._rebuild_layout()

    def _node_lines(self, lavalink: Any) -> list[str]:
        summary = self._summary or {}
        options = summary.get("options", {}) or {}
        hide_host = bool(options.get("hide_host_in_panel", True))
        host_text = "definido • oculto" if summary.get("host_defined") else "não definido"
        if summary.get("host_defined") and not hide_host:
            host_text = _escape(summary.get("host_label"), limit=80)
        return [
            f"**Node:** `{_escape(summary.get('node_name'), limit=60)}`",
            f"**Host:** `{host_text}`",
            f"**Porta:** `{int(summary.get('port') or 2333)}`",
            f"**Senha:** `{'definida ••••••••' if summary.get('password_defined') else 'não definida'}`",
            f"**SSL/Secure:** `{'sim' if summary.get('secure') else 'não'}`",
            f"**Config:** `{_escape(summary.get('source'), limit=30)}`",
        ]

    def _diagnostic_lines(self, lavalink: Any) -> list[str]:
        lines = [f"{_status_icon(lavalink)} **Conexão:** `{_available_label(lavalink)}`"]
        version = getattr(lavalink, "version", "") or ""
        if version:
            lines.append(f"**Versão:** `{_escape(version, limit=80)}`")
        latency = getattr(lavalink, "latency_ms", None)
        if latency is not None:
            lines.append(f"**Latência:** `{latency} ms`")
        players = getattr(lavalink, "players", None)
        playing = getattr(lavalink, "playing_players", None)
        if players is not None:
            lines.append(f"**Players:** `{players}` • **tocando:** `{playing if playing is not None else '?'}`")
        extra = getattr(lavalink, "extra", {}) or {}
        if "wavelink_installed" in extra:
            lines.append(f"**Wavelink:** `{'instalado' if extra.get('wavelink_installed') else 'não instalado'}`")
        message = getattr(lavalink, "message", "") or ""
        if message:
            lines.append(f"-# {_escape(message, limit=260)}")
        return lines

    def _last_test_lines(self) -> list[str]:
        result = self.last_test_result
        if result is None:
            return [
                "## 🧪 Último teste",
                "Nenhuma busca testada nesta sessão.",
                "-# Use **Testar busca** para validar YouTube, SoundCloud ou link direto.",
            ]
        status = "OK" if getattr(result, "ok", False) else "falhou"
        source = getattr(result, "first_source", "") or "—"
        title = getattr(result, "first_title", "") or "—"
        latency = getattr(result, "latency_ms", None)
        return [
            "## 🧪 Último teste",
            f"**Resultado:** `{'🟢' if getattr(result, 'ok', False) else '🔴'} {status}`",
            f"**Tracks:** `{int(getattr(result, 'tracks_found', 0) or 0)}` • **Fonte:** `{_escape(source, limit=40)}`",
            f"**Primeira:** {_escape(title, limit=110)}",
            f"**Latência:** `{latency} ms`" if latency is not None else "**Latência:** `—`",
        ]

    def _make_control_row(self) -> discord.ui.ActionRow:
        config = discord.ui.Button(label="Configurar", emoji="⚙️", style=discord.ButtonStyle.primary)
        config.callback = self._open_config_modal
        mode = discord.ui.Button(label="Modo e opções", emoji="🎛️", style=discord.ButtonStyle.secondary)
        mode.callback = self._open_mode_modal
        test = discord.ui.Button(label="Testar busca", emoji="🧪", style=discord.ButtonStyle.success)
        test.callback = self._open_test_modal
        refresh = discord.ui.Button(label="Atualizar", emoji="📡", style=discord.ButtonStyle.secondary)
        refresh.callback = self._refresh_button
        return discord.ui.ActionRow(config, mode, test, refresh)

    def _make_danger_row(self) -> discord.ui.ActionRow:
        clear = discord.ui.Button(label="Limpar node", emoji="🧹", style=discord.ButtonStyle.danger)
        clear.callback = self._open_clear_modal
        close = discord.ui.Button(label="Fechar", emoji="✖️", style=discord.ButtonStyle.secondary)
        close.callback = self._close_panel
        return discord.ui.ActionRow(clear, close)

    def _rebuild_layout(self) -> None:
        self.clear_items()
        lavalink = (self._statuses or {}).get("lavalink")
        local = (self._statuses or {}).get("local")
        runtime = self._runtime or {}
        summary = self._summary or {}
        mode = str(summary.get("mode") or runtime.get("lavalink_mode") or "off")
        configured = bool(summary.get("configured"))
        mode_text = _mode_label(mode)
        backend_real = str(runtime.get("active_backend", "local") or "local")
        accent = _accent_for(lavalink, summary)

        header = [
            "# 🔌 Lavalink Node Manager",
            f"**Modo:** {mode_text} — {_mode_hint(mode)}",
            f"**Backend real:** `{backend_real}` • **Node configurado:** `{'sim' if configured else 'não'}`",
            "-# O player real segue local neste patch; esta central só configura e testa o node.",
        ]
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(header)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(self._node_lines(lavalink))),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(self._diagnostic_lines(lavalink))),
            self._make_control_row(),
            accent_color=accent,
        ))

        local_msg = getattr(local, "message", "Player local ativo.") if local is not None else "Player local preservado."
        safety_lines = [
            "## 🛡️ Segurança da migração",
            f"**Local:** {_escape(local_msg, limit=160)}",
            "**Fallback:** FFmpeg/yt-dlp continua intacto.",
            "**Senha:** nunca aparece no painel; fica salva só em `data/music/lavalink_config.json`.",
        ]
        options = summary.get("options", {}) or {}
        if bool(options.get("test_after_save")):
            safety_lines.append("**Teste ao salvar:** `ativado`")
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(safety_lines)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(self._last_test_lines())),
            self._make_danger_row(),
            accent_color=discord.Color.blurple(),
        ))

    async def safe_refresh(self) -> None:
        if self.message is None:
            return
        with contextlib.suppress(Exception):
            await self.prepare()
            await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def _send_modal_or_error(self, interaction: discord.Interaction, modal_factory) -> None:
        try:
            modal = modal_factory()
        except Exception as exc:
            await interaction.response.send_message(
                f"`⚠️` Não consegui abrir este modal: {discord.utils.escape_markdown(str(exc))}",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(modal)

    async def _open_config_modal(self, interaction: discord.Interaction) -> None:
        await self._send_modal_or_error(interaction, lambda: MusicNodeConfigModal(self))

    async def _open_test_modal(self, interaction: discord.Interaction) -> None:
        await self._send_modal_or_error(interaction, lambda: MusicNodeTestModal(self))

    async def _open_mode_modal(self, interaction: discord.Interaction) -> None:
        await self._send_modal_or_error(interaction, lambda: MusicNodeModeOptionsModal(self))

    async def _refresh_button(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.safe_refresh()

    async def _open_clear_modal(self, interaction: discord.Interaction) -> None:
        await self._send_modal_or_error(interaction, lambda: MusicNodeClearModal(self))

    async def _close_panel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if interaction.message:
            with contextlib.suppress(Exception):
                await interaction.message.delete()

    async def on_timeout(self) -> None:
        self._expired = True
        self.clear_items()
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                "# 🔌 Lavalink Node Manager\n"
                "Essa central expirou para evitar interação antiga. Use `_musicnode` novamente."
            ),
            accent_color=discord.Color.dark_grey(),
        ))
        if self.message is not None:
            with contextlib.suppress(Exception):
                await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())
