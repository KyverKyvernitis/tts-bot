from __future__ import annotations

import aiohttp
import discord
from pathlib import Path
from typing import Any

from .safety import redact_secrets


class WebhookReporter:
    def __init__(self, session: aiohttp.ClientSession, webhook_url: str):
        self.session = session
        self.webhook_url = (webhook_url or "").strip()

    def available(self) -> bool:
        return bool(self.webhook_url)

    async def send_report(
        self,
        *,
        title: str,
        description: str,
        color: int = 0x5865F2,
        file_path: Path | None = None,
        username: str = "DevAI",
    ) -> discord.WebhookMessage | None:
        if not self.webhook_url:
            return None
        webhook = discord.Webhook.from_url(self.webhook_url, session=self.session)
        embed = discord.Embed(
            title=title[:256],
            description=redact_secrets(description, max_chars=3900),
            color=color,
        )
        embed.set_footer(text="DevAI • patch gerado automaticamente, aplicação manual")
        file = None
        try:
            if file_path is not None and file_path.exists():
                file = discord.File(str(file_path), filename=file_path.name)
            # IMPORTANTE: só passa `file=` se realmente tem arquivo. discord.py
            # 2.x crasha com `AttributeError: 'NoneType' object has no
            # attribute 'to_dict'` quando recebe `file=None` explicitamente
            # (chama file.to_dict() sem checar). Esse era o bug que matava
            # silenciosamente todos os comentários de patch review.
            kwargs: dict[str, Any] = {
                "username": username,
                "embed": embed,
                "wait": True,
            }
            if file is not None:
                kwargs["file"] = file
            return await webhook.send(**kwargs)
        finally:
            if file is not None:
                try:
                    file.close()
                except Exception:
                    pass

    async def send_plain(self, content: str, *, username: str = "DevAI") -> discord.WebhookMessage | None:
        if not self.webhook_url:
            return None
        webhook = discord.Webhook.from_url(self.webhook_url, session=self.session)
        return await webhook.send(username=username, content=redact_secrets(content, max_chars=1800), wait=True)
