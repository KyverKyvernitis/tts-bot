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
    command: str | None
    pre_ack: str | None = None
    self_affecting: bool = False
    blocked_message: str | None = None


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

    # O bot principal não consegue se ligar de volta depois de parado, porque
    # precisa estar online para ler `_cmd start bot`. O restart continua útil:
    # responde antes e reinicia em background.
    if unit == SELF_SERVICE and action == "start":
        return ServiceShortcut(
            action=action,
            alias=alias,
            unit=unit,
            command=None,
            blocked_message=(
                "⚠️ Atalho inútil neste processo\n"
                "`_cmd start bot` só funcionaria se o bot já estivesse online. "
                "Para recuperar o bot parado, use o modo rescue dos CallKeepers ou a VPS."
            ),
        )
    if unit == SELF_SERVICE and action == "stop":
        return ServiceShortcut(
            action=action,
            alias=alias,
            unit=unit,
            command=None,
            blocked_message=(
                "⚠️ Parada do bot principal bloqueada\n"
                "Se o bot principal parar, ele não consegue ler `_cmd start bot`. "
                "Use `_cmd restart bot` quando quiser reiniciar."
            ),
        )

    if action == "status":
        command = f"sudo systemctl status {unit} --no-pager"
    elif action == "logs":
        command = f"sudo journalctl -u {unit} -n 200 --no-pager"
    else:
        command = f"sudo systemctl {action} {unit}"

    self_affecting = unit == SELF_SERVICE and action == "restart"
    pre_ack = None
    if self_affecting:
        pre_ack = f"🖥️ Reinício solicitado para `{unit}`."

    return ServiceShortcut(
        action=action,
        alias=alias,
        unit=unit,
        command=command,
        pre_ack=pre_ack,
        self_affecting=self_affecting,
    )
