from __future__ import annotations

import html
import io
from datetime import datetime, timezone
from typing import Any

import discord

from .constants import TRANSCRIPT_FETCH_LIMIT


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "sem data"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _message_author(message: discord.Message) -> str:
    author = getattr(message, "author", None)
    if author is None:
        return "desconhecido"
    name = str(getattr(author, "display_name", None) or getattr(author, "name", None) or author)
    user_id = int(getattr(author, "id", 0) or 0)
    return f"{name} ({user_id})" if user_id else name


async def build_transcript_file(channel: discord.TextChannel, *, ticket: dict[str, Any] | None = None) -> discord.File:
    messages = [message async for message in channel.history(limit=TRANSCRIPT_FETCH_LIMIT, oldest_first=True)]
    ticket = ticket or {}
    title = f"Transcript — #{channel.name}"
    generated_at = datetime.now(timezone.utc)

    rows: list[str] = []
    for msg in messages:
        author = html.escape(_message_author(msg))
        created = html.escape(_fmt_dt(getattr(msg, "created_at", None)))
        content = html.escape(str(getattr(msg, "content", "") or ""))
        if not content and getattr(msg, "embeds", None):
            content = "<em>mensagem com embed</em>"
        if not content and getattr(msg, "components", None):
            content = "<em>mensagem com componentes</em>"
        attachments = []
        for att in getattr(msg, "attachments", []) or []:
            filename = html.escape(str(getattr(att, "filename", "anexo") or "anexo"))
            url = html.escape(str(getattr(att, "url", "") or ""))
            if url:
                attachments.append(f'<a href="{url}">{filename}</a>')
            else:
                attachments.append(filename)
        if attachments:
            content += "<br><strong>Anexos:</strong> " + ", ".join(attachments)
        rows.append(
            "<article class='message'>"
            f"<header><strong>{author}</strong><span>{created}</span></header>"
            f"<p>{content or '<em>sem texto</em>'}</p>"
            "</article>"
        )

    ticket_id = int(ticket.get("ticket_id") or 0)
    kind = html.escape(str(ticket.get("label") or ticket.get("kind") or "ticket"))
    owner = html.escape(str(ticket.get("user_id") or ""))
    body = "\n".join(rows) or "<p><em>Nenhuma mensagem encontrada.</em></p>"
    doc = f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 24px; background: #111214; color: #f2f3f5; }}
main {{ max-width: 920px; margin: 0 auto; }}
.summary, .message {{ background: #1e1f22; border: 1px solid #313338; border-radius: 12px; padding: 14px 16px; margin: 12px 0; }}
.message header {{ display: flex; justify-content: space-between; gap: 12px; color: #dbdee1; }}
.message header span {{ color: #949ba4; font-size: 12px; white-space: nowrap; }}
.message p {{ white-space: pre-wrap; line-height: 1.45; }}
a {{ color: #00a8fc; }}
</style>
</head>
<body>
<main>
<h1>{html.escape(title)}</h1>
<section class="summary">
<p><strong>ID:</strong> {ticket_id or 'sem id'}</p>
<p><strong>Tipo:</strong> {kind}</p>
<p><strong>Dono:</strong> {owner}</p>
<p><strong>Gerado em:</strong> {html.escape(_fmt_dt(generated_at))}</p>
<p><strong>Mensagens exportadas:</strong> {len(messages)}</p>
</section>
{body}
</main>
</body>
</html>"""
    raw = doc.encode("utf-8", errors="replace")
    ticket_part = f"{ticket_id:04d}" if ticket_id else str(int(datetime.now(timezone.utc).timestamp()))
    return discord.File(io.BytesIO(raw), filename=f"ticket-{ticket_part}-transcript.html")
