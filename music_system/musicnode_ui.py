from __future__ import annotations

import contextlib
from typing import Any

import discord


_ALLOWED_MODES = {"off", "shadow", "lavalink", "auto"}


def _escape(value: object, *, limit: int = 220, empty: str = "—") -> str:
    text = str(value or "").strip()
    if not text:
        return empty
    text = discord.utils.escape_markdown(text)
    if len(text) > limit:
        text = text[: max(0, limit - 3)].rstrip() + "..."
    return text


def _parse_bool_text(value: str, default: bool = False) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on", "sim", "s", "ssl", "https"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "não", "nao", "http"}:
        return False
    raise ValueError("Secure/SSL precisa ser true/false, sim/não ou on/off.")


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


def _status_icon(health: Any) -> str:
    if getattr(health, "available", False):
        return "🟢"
    if getattr(health, "configured", False):
        return "🟡"
    return "🔴"


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


class MusicNodeConfigModal(discord.ui.Modal):
    def __init__(self, panel: "MusicNodePanelView") -> None:
        super().__init__(title="Configurar node Lavalink", timeout=300)
        self.panel = panel
        summary = panel.router.lavalink_config_summary()
        self.node_name = discord.ui.TextInput(
            label="Nome do node",
            default=str(summary.get("node_name") or "main")[:80],
            placeholder="main",
            max_length=80,
            required=True,
        )
        self.host = discord.ui.TextInput(
            label="Host ou IP",
            default="",
            placeholder="exemplo.lavalink.host ou 1.2.3.4",
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
            placeholder="deixe vazio para manter a senha atual",
            max_length=300,
            required=False,
        )
        self.secure = discord.ui.TextInput(
            label="Secure/SSL",
            default="true" if summary.get("secure") else "false",
            placeholder="true/false",
            max_length=8,
            required=True,
        )
        for item in (self.node_name, self.host, self.port, self.password, self.secure):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            port = _parse_port(str(self.port.value))
            secure = _parse_bool_text(str(self.secure.value), False)
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
        await interaction.followup.send(
            "`✅` Node Lavalink salvo. O modo seguro padrão continua em `shadow` quando estava desligado; o player real ainda é local neste patch.",
            ephemeral=True,
        )


class MusicNodeTestModal(discord.ui.Modal):
    def __init__(self, panel: "MusicNodePanelView") -> None:
        super().__init__(title="Testar busca no Lavalink", timeout=300)
        self.panel = panel
        self.query = discord.ui.TextInput(
            label="Busca ou link",
            placeholder="Cole um link ou pesquise uma música",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=True,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        query = str(self.query.value or "").strip()
        if not query:
            await interaction.response.send_message("`⚠️` Busca vazia.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.panel.router.test_lavalink_backend(
            query,
            requester_id=int(getattr(getattr(interaction, "user", None), "id", 0) or 0),
            requester_name=getattr(getattr(interaction, "user", None), "display_name", str(getattr(interaction, "user", ""))),
        )
        await interaction.followup.send(_format_test_result(result), ephemeral=True)


class MusicNodeModeModal(discord.ui.Modal):
    def __init__(self, panel: "MusicNodePanelView") -> None:
        super().__init__(title="Mudar modo Lavalink", timeout=300)
        self.panel = panel
        current = str(panel.router.lavalink_config_summary().get("mode") or "off")
        self.mode = discord.ui.TextInput(
            label="Modo",
            default=current,
            placeholder="off, shadow, lavalink ou auto",
            max_length=16,
            required=True,
        )
        self.add_item(self.mode)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        mode = _normalize_mode(str(self.mode.value or "off"))
        if mode not in _ALLOWED_MODES:
            await interaction.response.send_message("`⚠️` Use `off`, `shadow`, `lavalink` ou `auto`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.panel.router.set_lavalink_mode(mode)
        await self.panel.safe_refresh()
        await interaction.followup.send(
            f"`✅` Modo alterado para **{_mode_label(mode)}**. Neste patch o player real ainda continua local.",
            ephemeral=True,
        )


class MusicNodeClearModal(discord.ui.Modal):
    def __init__(self, panel: "MusicNodePanelView") -> None:
        super().__init__(title="Limpar configuração Lavalink", timeout=300)
        self.panel = panel
        self.confirm = discord.ui.TextInput(
            label="Digite LIMPAR para confirmar",
            placeholder="LIMPAR",
            max_length=10,
            required=True,
        )
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if str(self.confirm.value or "").strip().upper() != "LIMPAR":
            await interaction.response.send_message("`⚠️` Confirmação cancelada.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.panel.router.clear_lavalink_config()
        await self.panel.safe_refresh()
        await interaction.followup.send("`🧹` Configuração Lavalink limpa e modo voltou para `off`.", ephemeral=True)


class MusicNodePanelView(discord.ui.View):
    def __init__(self, router, bot, owner_id: int) -> None:
        super().__init__(timeout=600)
        self.router = router
        self.bot = bot
        self.owner_id = int(owner_id or 0)
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        user_id = int(getattr(getattr(interaction, "user", None), "id", 0) or 0)
        allowed = user_id == self.owner_id
        if not allowed:
            with contextlib.suppress(Exception):
                allowed = await self.bot.is_owner(interaction.user)  # type: ignore[arg-type]
        if not allowed:
            await interaction.response.send_message("Esse painel técnico só pode ser usado pelo dono que abriu o `_musicnode`.", ephemeral=True)
            return False
        return True

    async def build_embed(self) -> discord.Embed:
        statuses = await self.router.backend_status()
        runtime = self.router.backend_runtime_summary()
        summary = self.router.lavalink_config_summary()
        lavalink = statuses.get("lavalink")
        local = statuses.get("local")
        available = bool(getattr(lavalink, "available", False))
        configured = bool(summary.get("configured"))
        color = discord.Color.green() if available else discord.Color.gold() if configured else discord.Color.red()
        embed = discord.Embed(
            title="🔌 Central Lavalink",
            description=(
                "Configure e teste o node Lavalink sem mexer no `.env`.\n"
                "Neste patch, o **player real continua local** para não quebrar música/TTS."
            ),
            color=color,
        )
        mode = str(summary.get("mode") or runtime.get("lavalink_mode") or "off")
        embed.add_field(
            name="Estado",
            value=(
                f"• modo: **{_mode_label(mode)}**\n"
                f"• backend real: `{runtime.get('active_backend', 'local')}`\n"
                f"• fonte da config: `{_escape(summary.get('source'), limit=30)}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Node",
            value=(
                f"• configurado: `{'sim' if configured else 'não'}`\n"
                f"• nome: `{_escape(summary.get('node_name'), limit=60)}`\n"
                f"• host: `{'definido' if summary.get('host_defined') else 'não definido'}`\n"
                f"• porta: `{int(summary.get('port') or 2333)}`\n"
                f"• senha: `{'definida ••••••••' if summary.get('password_defined') else 'não definida'}`\n"
                f"• SSL: `{'sim' if summary.get('secure') else 'não'}`"
            ),
            inline=True,
        )
        health_lines = [
            f"• Lavalink: `{_status_icon(lavalink)} {'online' if available else 'offline/indisponível'}`",
        ]
        version = getattr(lavalink, "version", "") or ""
        if version:
            health_lines.append(f"• versão: `{_escape(version, limit=80)}`")
        latency = getattr(lavalink, "latency_ms", None)
        if latency is not None:
            health_lines.append(f"• latência: `{latency} ms`")
        players = getattr(lavalink, "players", None)
        playing = getattr(lavalink, "playing_players", None)
        if players is not None:
            health_lines.append(f"• players: `{players}` • tocando: `{playing if playing is not None else '?'}`")
        extra = getattr(lavalink, "extra", {}) or {}
        if "wavelink_installed" in extra:
            health_lines.append(f"• wavelink: `{'sim' if extra.get('wavelink_installed') else 'não'}`")
        message = getattr(lavalink, "message", "") or ""
        if message:
            health_lines.append(f"• detalhe: {_escape(message, limit=180)}")
        embed.add_field(name="Diagnóstico", value="\n".join(health_lines), inline=True)
        embed.add_field(
            name="Local preservado",
            value=(
                f"• {_escape(getattr(local, 'message', 'Player local ativo.'), limit=160)}\n"
                "• FFmpeg/yt-dlp continua sendo o playback real."
            ),
            inline=False,
        )
        embed.set_footer(text="Use os botões abaixo. A senha nunca é exibida no painel.")
        return embed

    async def safe_refresh(self) -> None:
        if self.message is None:
            return
        with contextlib.suppress(Exception):
            await self.message.edit(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="Configurar node", emoji="⚙️", style=discord.ButtonStyle.blurple, row=0)
    async def configure_node(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(MusicNodeConfigModal(self))

    @discord.ui.button(label="Testar busca", emoji="🧪", style=discord.ButtonStyle.green, row=0)
    async def test_node(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(MusicNodeTestModal(self))

    @discord.ui.button(label="Mudar modo", emoji="🔁", style=discord.ButtonStyle.gray, row=0)
    async def change_mode(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(MusicNodeModeModal(self))

    @discord.ui.button(label="Atualizar", emoji="📡", style=discord.ButtonStyle.gray, row=1)
    async def refresh_status(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await self.safe_refresh()

    @discord.ui.button(label="Limpar", emoji="🧹", style=discord.ButtonStyle.red, row=1)
    async def clear_config(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(MusicNodeClearModal(self))

    @discord.ui.button(label="Fechar", emoji="✖️", style=discord.ButtonStyle.gray, row=1)
    async def close_panel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        if interaction.message:
            with contextlib.suppress(Exception):
                await interaction.message.delete()

    async def on_timeout(self) -> None:
        for item in self.children:
            with contextlib.suppress(Exception):
                item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            with contextlib.suppress(Exception):
                await self.message.edit(view=self)
