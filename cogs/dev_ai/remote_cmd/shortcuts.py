from __future__ import annotations

from dataclasses import dataclass


SERVICE_ALIASES: dict[str, str] = {
    "bot": "tts-bot.service",
    "tts": "tts-bot.service",
    "tts-bot": "tts-bot.service",
    "main": "tts-bot.service",
    "principal": "tts-bot.service",
    "callkeeper": "callkeeper.service",
    "call": "callkeeper.service",
    "keeper": "callkeeper.service",
    "activity": "sinuca-activity-server.service",
    "sinuca": "sinuca-activity-server.service",
    "sinuca-activity": "sinuca-activity-server.service",
}

SERVICE_ACTIONS = {"start", "stop", "restart", "status", "logs"}
SELF_SERVICE = "tts-bot.service"


@dataclass(slots=True)
class ServiceShortcut:
    action: str
    alias: str
    unit: str
    command: str
    pre_ack: str | None = None
    self_affecting: bool = False


def resolve_service_shortcut(raw_command: str) -> ServiceShortcut | None:
    parts = (raw_command or "").strip().split()
    if len(parts) != 2:
        return None

    action = parts[0].lower().strip()
    alias = parts[1].lower().strip()
    if action not in SERVICE_ACTIONS:
        return None

    unit = SERVICE_ALIASES.get(alias)
    if not unit:
        return None

    if action == "status":
        command = f"sudo systemctl status {unit} --no-pager"
    elif action == "logs":
        command = f"sudo journalctl -u {unit} -n 200 --no-pager"
    else:
        command = f"sudo systemctl {action} {unit}"

    self_affecting = unit == SELF_SERVICE and action in {"stop", "restart"}
    pre_ack = None
    if self_affecting:
        label = "reinício" if action == "restart" else "parada"
        pre_ack = f"🖥️ {label.capitalize()} solicitado para `{unit}`."

    return ServiceShortcut(
        action=action,
        alias=alias,
        unit=unit,
        command=command,
        pre_ack=pre_ack,
        self_affecting=self_affecting,
    )
