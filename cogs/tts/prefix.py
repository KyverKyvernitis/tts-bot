from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .aliases import extract_prefixed_argument, get_prefixed_aliases, matches_prefixed_command


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

    if matches_prefixed_command(raw, bot_prefix, kind="help"):
        return PrefixControlCommand("help")
    if matches_prefixed_command(raw, bot_prefix, kind="clear"):
        return PrefixControlCommand("clear")
    if matches_prefixed_command(raw, bot_prefix, kind="leave"):
        return PrefixControlCommand("leave")
    if matches_prefixed_command(raw, bot_prefix, kind="join"):
        return PrefixControlCommand("join")
    if matches_prefixed_command(raw, bot_prefix, kind="reset"):
        return PrefixControlCommand("reset", extract_prefixed_argument(raw, bot_prefix, kind="reset"))
    if matches_prefixed_command(raw, bot_prefix, kind="set_lang"):
        return PrefixControlCommand("set_lang", extract_prefixed_argument(raw, bot_prefix, kind="set_lang"))
    if matches_prefixed_command(raw, bot_prefix, kind="panel_user"):
        return PrefixControlCommand("panel_user")
    if matches_prefixed_command(raw, bot_prefix, kind="panel_server"):
        return PrefixControlCommand("panel_server")
    if matches_prefixed_command(raw, bot_prefix, kind="panel_toggle"):
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
