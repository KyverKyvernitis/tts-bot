from __future__ import annotations

import re
import shlex
from pathlib import Path


BLOCKED_INTERACTIVE = {
    "nano", "vim", "vi", "nvim", "emacs", "micro",
    "top", "htop", "btop", "iotop", "iftop", "nmtui",
    "less", "more", "most", "man", "info",
    "tmux", "screen", "watch",
    "ssh", "sftp", "ftp", "telnet",
    "passwd", "su",
}

SESSION_ALLOWED = {"bash", "sh", "python", "python3", "node"}


def parse_head(command: str) -> tuple[str, list[str]]:
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        parts = command.strip().split()
    head = Path(parts[0]).name.lower() if parts else ""
    return head, parts


def is_non_emulable_interactive(command: str) -> tuple[bool, str]:
    head, parts = parse_head(command)
    if not head:
        return False, ""

    # Clientes SQL são úteis em modo comando direto, mas o shell interativo
    # depende de terminal real e fica ruim de controlar pelo Discord.
    if head in {"mysql", "mariadb"}:
        if "-e" not in parts and "--execute" not in parts:
            return True, "cliente SQL interativo sem -e/--execute"
        return False, ""
    if head == "psql":
        if "-c" not in parts and "--command" not in parts:
            return True, "psql interativo sem -c/--command"
        return False, ""
    if head == "sqlite3":
        # `sqlite3 arquivo.db 'select ...'` é não-interativo; só `sqlite3`
        # ou `sqlite3 arquivo.db` abre shell próprio.
        if len(parts) <= 2:
            return True, "sqlite3 interativo"
        return False, ""

    if head == "sudo" and any(p == "-S" or p.startswith("-S") for p in parts[1:]):
        return True, "sudo -S exige senha/stdin sensível"

    if head in BLOCKED_INTERACTIVE:
        return True, "precisa de TTY/tela cheia/cursor e não dá para emular direito no Discord"
    return False, ""


def is_destructive(command: str) -> tuple[bool, str]:
    lower = re.sub(r"\s+", " ", command.strip().lower())
    checks: list[tuple[str, str]] = [
        (r"(^|[;&|]\s*)rm\s+", "remove arquivos"),
        (r"(^|[;&|]\s*)dd\s+", "pode sobrescrever disco/arquivo"),
        (r"(^|[;&|]\s*)mkfs(\.|\s|$)", "formata filesystem"),
        (r"(^|[;&|]\s*)(shutdown|reboot|poweroff|halt)\b", "desliga/reinicia a VPS"),
        (r"(^|[;&|]\s*)init\s+[06]\b", "desliga/reinicia a VPS"),
        (r"(^|[;&|]\s*)systemctl\s+(restart|stop|disable|mask|kill|poweroff|reboot|halt)\b", "altera serviço/systemd"),
        (r"(^|[;&|]\s*)service\s+\S+\s+(restart|stop)\b", "altera serviço"),
        (r"(^|[;&|]\s*)kill\s+(-9\s+)?\d+", "encerra processo"),
        (r"(^|[;&|]\s*)(pkill|killall)\b", "encerra processos"),
        (r"(^|[;&|]\s*)git\s+reset\s+--hard\b", "descarta alterações do git"),
        (r"(^|[;&|]\s*)git\s+clean\s+[^;&|]*-[^;&|]*[df]", "apaga arquivos não rastreados"),
        (r"(^|[;&|]\s*)docker\s+(rm|rmi)\b", "remove containers/imagens"),
        (r"(^|[;&|]\s*)docker\s+system\s+prune\b", "limpa dados do Docker"),
        (r"(^|[;&|]\s*)truncate\s+[^;&|]*(--size=0|-s\s*0)\b", "zera arquivo"),
        (r"(^|[;&|]\s*)(chmod|chown)\s+-R\b", "altera permissões/dono recursivamente"),
        (r">\s*/(etc|usr|bin|sbin|lib|boot|var)/", "redireciona escrita em pasta sensível"),
    ]
    for pattern, reason in checks:
        if re.search(pattern, lower):
            return True, reason
    return False, ""


def normalize_session_command(command: str) -> str:
    head, parts = parse_head(command)
    if not parts:
        return command
    if head in {"bash", "sh"} and "-i" not in parts:
        return command + " -i"
    if head in {"python", "python3"} and "-i" not in parts:
        return command + " -i -u"
    if head == "node" and "-i" not in parts:
        return command + " -i"
    return command
