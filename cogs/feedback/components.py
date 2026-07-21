from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

import discord

from .constants import CATEGORY_OPTIONS, STATUS_IN_REVIEW, STATUS_OPEN


def _trim(value: Any, limit: int = 3900) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def safe_text(value: Any, *, limit: int = 3000) -> str:
    text = _trim(value, limit)
    text = discord.utils.escape_mentions(text)
    return discord.utils.escape_markdown(text, as_needed=False)


def quote_text(value: Any, *, limit: int = 3000) -> str:
    text = safe_text(value, limit=limit) or "Sem conteúdo textual."
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def display_name(value: Any, *, limit: int = 80) -> str:
    return safe_text(value, limit=limit) or "Usuário"


def protocol_of(feedback: dict[str, Any]) -> str:
    return str(feedback.get("protocol") or "FDB-??????")


def category_info(feedback_or_key: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(feedback_or_key, dict):
        key = str(feedback_or_key.get("category") or "help")
    else:
        key = str(feedback_or_key or "help")
    return CATEGORY_OPTIONS.get(key, CATEGORY_OPTIONS["help"])


def status_label(status: str) -> str:
    if status == STATUS_IN_REVIEW:
        return "Em análise"
    if status == STATUS_OPEN:
        return "Aguardando análise"
    if status == "resolving":
        return "Sendo encerrado"
    if status == "resolved":
        return "Resolvido"
    return "Aguardando análise"


def format_discord_time(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:f>"


def notice_view(
    title: str,
    lines: str | Iterable[str],
    *,
    ok: bool = True,
    accent_color: int | discord.Color | None = None,
) -> discord.ui.LayoutView:
    if isinstance(lines, str):
        body = lines.strip()
    else:
        body = "\n".join(str(line) for line in lines if str(line).strip()).strip()
    color = accent_color
    if color is None:
        color = discord.Color.green() if ok else discord.Color.red()
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            discord.ui.TextDisplay(_trim(f"# {title}\n{body}")),
            accent_color=color,
        )
    )
    return view


def build_feedback_created_dm(feedback: dict[str, Any]) -> discord.ui.LayoutView:
    info = category_info(feedback)
    protocol = protocol_of(feedback)
    guild_name = display_name(feedback.get("guild_name") or "Servidor", limit=100)
    body = (
        f"# {info['emoji']} Feedback enviado\n"
        "Sua solicitação foi registrada e já está disponível para análise.\n"
        "\n"
        f"**Protocolo**\n`{protocol}`\n"
        "\n"
        f"**Tipo**\n{info['label']}\n"
        "\n"
        f"**Servidor de origem**\n{guild_name}"
    )
    instructions = (
        "## Adicionar informações\n"
        "Envie uma mensagem nesta DM começando com `_`.\n"
        "\n"
        "**Exemplo**\n"
        "`_ O problema também acontece em outro comando.`\n"
        "\n"
        "As respostas do dono do bot chegarão por esta conversa. "
        "Use `_status` para ver o destino atual ou `_trocar` para selecionar outro atendimento aberto."
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            discord.ui.TextDisplay(_trim(body)),
            discord.ui.Separator(),
            discord.ui.TextDisplay(_trim(instructions)),
            accent_color=info["accent"],
        )
    )
    return view


def build_feedback_starter_text(feedback: dict[str, Any]) -> str:
    info = category_info(feedback)
    author_name = display_name(feedback.get("author_name") or "Usuário")
    author_id = int(feedback.get("author_id") or 0)
    guild_name = display_name(feedback.get("guild_name") or "Servidor", limit=100)
    guild_id = int(feedback.get("guild_id") or 0)
    return _trim(
        f"# {info['emoji']} Novo feedback\n"
        f"## `{protocol_of(feedback)}` · {info['label']}\n"
        "\n"
        f"**Autor**\n{author_name} · `{author_id}`\n"
        "\n"
        f"**Servidor de origem**\n{guild_name} · `{guild_id}`\n"
        "\n"
        "## Descrição\n"
        f"{quote_text(feedback.get('description'), limit=3000)}\n"
        "\n"
        f"**Criado em**\n{format_discord_time(feedback.get('created_at'))}"
    )


def build_additional_message_view(
    feedback: dict[str, Any], content: str, *, attachment_count: int = 0
) -> discord.ui.LayoutView:
    attachment_line = ""
    if attachment_count:
        attachment_line = (
            f"\n\n**Anexos**\n{attachment_count} arquivo(s) encaminhado(s)."
        )
    body = (
        f"## Informação adicional · `{protocol_of(feedback)}`\n"
        "Enviada pelo autor do feedback.\n"
        "\n"
        f"{quote_text(content, limit=2600)}"
        f"{attachment_line}"
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            discord.ui.TextDisplay(_trim(body)),
            accent_color=category_info(feedback)["accent"],
        )
    )
    return view


def build_additional_confirmation(feedback: dict[str, Any]) -> discord.ui.LayoutView:
    return notice_view(
        "Informação adicionada",
        [
            f"Sua mensagem foi enviada para `{protocol_of(feedback)}`.",
            f"**Destino ativo:** {display_name(feedback.get('guild_name') or 'Servidor', limit=100)}",
        ],
        ok=True,
        accent_color=category_info(feedback)["accent"],
    )


def build_owner_update_dm(
    feedback: dict[str, Any], content: str, *, attachment_count: int = 0
) -> discord.ui.LayoutView:
    info = category_info(feedback)
    attachment_line = ""
    if attachment_count:
        attachment_line = f"\n\n**Anexos**\n{attachment_count} arquivo(s) enviado(s)."
    body = (
        f"# {info['emoji']} Atualização do feedback\n"
        f"## `{protocol_of(feedback)}` · {info['label']}\n"
        "\n"
        "O dono do bot enviou uma nova mensagem:\n"
        "\n"
        f"{quote_text(content, limit=2600)}"
        f"{attachment_line}"
    )
    instructions = "Para responder, envie uma mensagem nesta DM começando com `_`."
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            discord.ui.TextDisplay(_trim(body)),
            discord.ui.Separator(),
            discord.ui.TextDisplay(instructions),
            accent_color=info["accent"],
        )
    )
    return view


def build_resolved_dm(feedback: dict[str, Any]) -> discord.ui.LayoutView:
    return notice_view(
        "Feedback resolvido",
        [
            f"O atendimento `{protocol_of(feedback)}` foi marcado como resolvido.",
            "O tópico foi encerrado e não receberá novas informações.",
            "Obrigado por ajudar a melhorar o bot.",
        ],
        ok=True,
        accent_color=discord.Color.green(),
    )


def build_active_status_view(
    feedback: dict[str, Any] | None, *, open_count: int = 0
) -> discord.ui.LayoutView:
    if feedback is None:
        return notice_view(
            "Nenhum feedback ativo",
            "Use `/feedback` em um servidor para iniciar um atendimento.",
            ok=False,
        )
    info = category_info(feedback)
    lines = [
        f"**Protocolo**\n`{protocol_of(feedback)}`",
        f"**Servidor**\n{display_name(feedback.get('guild_name') or 'Servidor', limit=100)}",
        f"**Tipo**\n{info['label']}",
        f"**Status**\n{status_label(str(feedback.get('status') or STATUS_OPEN))}",
    ]
    if open_count > 1:
        lines.append(
            f"Você possui {open_count} atendimentos abertos. Use `_trocar` para mudar o destino."
        )
    return notice_view("Destino ativo", lines, ok=True, accent_color=info["accent"])


def build_delivery_failure_view(feedback: dict[str, Any]) -> discord.ui.LayoutView:
    return notice_view(
        "Falha na entrega",
        [
            f"Não foi possível enviar a atualização de `{protocol_of(feedback)}` para a DM do autor.",
            "As mensagens diretas dele podem estar desativadas.",
        ],
        ok=False,
    )
