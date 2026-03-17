from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PrefixControlCommand:
    kind: str
    argument: str = ""


@dataclass(frozen=True)
class PrefixRoutingConfig:
    bot_prefix: str
    gtts_prefix: str
    edge_prefix: str
    gcloud_prefix: str


def build_panel_aliases(bot_prefix: str) -> dict[str, set[str]]:
    return {
        "user": {
            f"{bot_prefix}panel",
            f"{bot_prefix}painel",
            f"{bot_prefix}p",
        },
        "server": {
            f"{bot_prefix}panel_server", f"{bot_prefix}panel-server", f"{bot_prefix}panelserver",
            f"{bot_prefix}server_panel", f"{bot_prefix}server-panel", f"{bot_prefix}serverpanel",
            f"{bot_prefix}painel_server", f"{bot_prefix}painel-server", f"{bot_prefix}painelserver",
            f"{bot_prefix}servidor_panel", f"{bot_prefix}servidor-panel", f"{bot_prefix}servidorpanel",
            f"{bot_prefix}sp",
        },
        "toggle": {
            f"{bot_prefix}panel_toggle", f"{bot_prefix}panel-toggle", f"{bot_prefix}paneltoggle",
            f"{bot_prefix}panel_toggles", f"{bot_prefix}panel-toggles", f"{bot_prefix}paneltoggles",
            f"{bot_prefix}toggle_panel", f"{bot_prefix}toggle-panel", f"{bot_prefix}togglepanel",
            f"{bot_prefix}toggles_panel", f"{bot_prefix}toggles-panel", f"{bot_prefix}togglespanel",
            f"{bot_prefix}tp",
        },
    }


def build_prefix_routing_config(guild_defaults: dict[str, Any] | None, *, bot_prefix_default: str, gcloud_prefix_default: str) -> PrefixRoutingConfig:
    defaults = guild_defaults or {}
    return PrefixRoutingConfig(
        bot_prefix=str(defaults.get("bot_prefix", bot_prefix_default) or bot_prefix_default),
        gtts_prefix=str(defaults.get("gtts_prefix", defaults.get("tts_prefix", ".")) or "."),
        edge_prefix=str(defaults.get("edge_prefix", ",") or ","),
        gcloud_prefix=str(defaults.get("gcloud_prefix", gcloud_prefix_default) or gcloud_prefix_default),
    )


def match_prefix_control_command(content: str, bot_prefix: str) -> PrefixControlCommand | None:
    raw = str(content or "").strip()
    if not raw:
        return None

    lowered = raw.lower()
    aliases = build_panel_aliases(bot_prefix)
    reset_command = f"{bot_prefix}reset"
    set_lang_command = f"{bot_prefix}set lang"

    if lowered == f"{bot_prefix}help":
        return PrefixControlCommand("help")
    if lowered == f"{bot_prefix}clear":
        return PrefixControlCommand("clear")
    if lowered == f"{bot_prefix}leave":
        return PrefixControlCommand("leave")
    if lowered == f"{bot_prefix}join":
        return PrefixControlCommand("join")
    if lowered == reset_command or lowered.startswith(reset_command + " "):
        return PrefixControlCommand("reset", raw[len(reset_command):].strip())
    if lowered == set_lang_command or lowered.startswith(set_lang_command + " "):
        return PrefixControlCommand("set_lang", raw[len(set_lang_command):].strip())
    if lowered in aliases["user"]:
        return PrefixControlCommand("panel_user")
    if lowered in aliases["server"]:
        return PrefixControlCommand("panel_server")
    if lowered in aliases["toggle"]:
        return PrefixControlCommand("panel_toggle")
    return None


def match_engine_prefix(content: str, *, edge_prefix: str, gtts_prefix: str, gcloud_prefix: str) -> tuple[str | None, str | None]:
    text = str(content or "")
    if text.startswith(edge_prefix):
        return "edge", edge_prefix
    if text.startswith(gtts_prefix):
        return "gtts", gtts_prefix
    if text.startswith(gcloud_prefix):
        return "gcloud", gcloud_prefix
    return None, None


async def dispatch_prefix_control_command(cog: Any, message: Any, command: PrefixControlCommand) -> bool:
    kind = command.kind

    if kind == "help":
        return True
    if kind == "clear":
        await cog._prefix_clear(message)
        return True
    if kind == "leave":
        await cog._prefix_leave(message)
        return True
    if kind == "join":
        await cog._prefix_join(message)
        return True
    if kind == "reset":
        await cog._prefix_reset_user(message, command.argument)
        return True
    if kind == "set_lang":
        await cog._prefix_set_lang(message, command.argument)
        return True
    if kind == "panel_user":
        await cog._send_prefix_panel(message, panel_type="user")
        return True
    if kind == "panel_server":
        await cog._send_prefix_panel(message, panel_type="server")
        return True
    if kind == "panel_toggle":
        await cog._send_prefix_panel(message, panel_type="toggle")
        return True
    return False
