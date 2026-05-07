from __future__ import annotations

import asyncio
import io
import logging
import secrets
import time
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands

import config

from .executor import run_shell
from .formatting import build_result_attachment, chunk_text, format_result
from .security import is_destructive, is_non_emulable_interactive
from .shortcuts import resolve_service_shortcut
from .sessions import RemoteSessionManager

log = logging.getLogger(__name__)


class DevAIRemoteCommandService:
    """Terminal remoto privado da DevAI.

    Mantém toda a lógica do `_cmd` fora do `cog.py`: confirmação,
    atalhos de serviço, execução, sessões simples e envio da resposta.
    """

    CONFIRM_TTL_SECONDS = 90.0

    def __init__(self, *, repo_root: Path, chat_message_ids: set[int]):
        self.repo_root = repo_root
        self.chat_message_ids = chat_message_ids
        self.session: aiohttp.ClientSession | None = None
        self.pending_confirm: dict[int, dict[str, object]] = {}
        self.sessions = RemoteSessionManager(repo_root)

    def set_session(self, session: aiohttp.ClientSession | None) -> None:
        self.session = session

    async def close(self) -> None:
        await self.sessions.close_all()

    def in_devai_channel(self, ctx: commands.Context) -> bool:
        channel_id_cfg = int(getattr(config, "DEVAI_COMMENT_CHANNEL_ID", 0) or 0)
        if not channel_id_cfg:
            return False
        return int(getattr(ctx.channel, "id", 0) or 0) == channel_id_cfg

    def _make_file(self, attachment: tuple[str, bytes] | None) -> discord.File | None:
        if not attachment:
            return None
        filename, payload = attachment
        return discord.File(io.BytesIO(payload), filename=filename)

    async def send(self, ctx: commands.Context, content: str, *, attachment: tuple[str, bytes] | None = None) -> None:
        """Envia pelo webhook da DevAI sem redigir segredos.

        O `WebhookReporter` normal redige secrets. O `_cmd` é terminal privado,
        então aqui a saída é bruta e apenas bloqueia mentions para não pingar.
        """
        chunks = chunk_text(content)
        use_attachment = attachment if len(chunks) > 1 else None
        webhook_url = str(getattr(config, "DEVAI_WEBHOOK_URL", "") or "")

        # Webhook comum só posta no canal onde foi criado. Como `_cmd` agora
        # funciona em qualquer lugar, usa webhook apenas no canal da DevAI;
        # fora dele, responde no próprio canal/DM via bot.
        if self.session is not None and webhook_url and self.in_devai_channel(ctx):
            try:
                webhook = discord.Webhook.from_url(webhook_url, session=self.session)
                for index, chunk in enumerate(chunks, start=1):
                    file = self._make_file(use_attachment) if index == len(chunks) else None
                    try:
                        kwargs = {
                            "username": "DevAI",
                            "content": chunk,
                            "wait": True,
                            "allowed_mentions": discord.AllowedMentions.none(),
                        }
                        if file is not None:
                            kwargs["file"] = file
                        sent = await webhook.send(**kwargs)
                    finally:
                        if file is not None:
                            try:
                                file.close()
                            except Exception:
                                pass
                    if sent is not None:
                        self.chat_message_ids.add(int(sent.id))
                return
            except Exception:
                log.exception("DevAI _cmd: falha enviando via webhook; usando fallback")

        first = True
        for index, chunk in enumerate(chunks, start=1):
            file = self._make_file(use_attachment) if index == len(chunks) else None
            try:
                if first:
                    sent = await ctx.reply(
                        chunk,
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                        file=file,
                    )
                    first = False
                else:
                    sent = await ctx.channel.send(
                        chunk,
                        allowed_mentions=discord.AllowedMentions.none(),
                        file=file,
                    )
            finally:
                if file is not None:
                    try:
                        file.close()
                    except Exception:
                        pass
            if sent is not None:
                self.chat_message_ids.add(int(sent.id))

    async def handle(self, ctx: commands.Context, command: str) -> None:
        command = (command or "").strip()
        if not command:
            await self.send(
                ctx,
                "Uso: `_cmd <comando>`\n"
                "Ex: `_cmd journalctl -u tts-bot -n 80 --no-pager`",
            )
            return

        user_id = int(getattr(ctx.author, "id", 0) or 0)
        lower = command.lower().strip()
        confirmed_destructive = False

        if lower.startswith("confirm "):
            command = await self._consume_confirmation(ctx, user_id, command)
            if not command:
                return
            lower = command.lower().strip()
            confirmed_destructive = True

        elif lower.startswith("session "):
            await self.sessions.start_session(ctx, command.split(maxsplit=1)[1].strip(), self.send)
            return

        elif lower.startswith("input "):
            parts = command.split(maxsplit=2)
            if len(parts) < 3:
                await self.send(ctx, "Uso: `_cmd input <sessão> <texto>`")
                return
            await self.sessions.input(ctx, parts[1], parts[2], self.send)
            return

        elif lower.startswith("close "):
            parts = command.split(maxsplit=1)
            if len(parts) < 2:
                await self.send(ctx, "Uso: `_cmd close <sessão>`")
                return
            await self.sessions.close(ctx, parts[1], self.send)
            return

        shortcut = resolve_service_shortcut(command)
        if shortcut is not None:
            if shortcut.self_affecting:
                await self.send(ctx, shortcut.pre_ack or f"🖥️ Solicitação enviada para `{shortcut.unit}`.")
                asyncio.create_task(self._run_self_affecting_shortcut(shortcut.command))
                return
            command = shortcut.command
            lower = command.lower().strip()
            confirmed_destructive = True

        blocked, reason = is_non_emulable_interactive(command)
        if blocked:
            await self.send(
                ctx,
                "⚠️ Comando interativo bloqueado\n"
                f"{reason}.",
            )
            return

        destructive, reason = is_destructive(command)
        if destructive and not confirmed_destructive:
            code = secrets.token_hex(3).upper()
            self.pending_confirm[user_id] = {
                "code": code,
                "command": command,
                "created_at": time.time(),
            }
            await self.send(
                ctx,
                "⚠️ Confirmação necessária\n"
                f"Motivo: {reason}.\n\n"
                f"Para executar, envie: `_cmd confirm {code}`",
            )
            return

        try:
            result = await run_shell(command, cwd=self.repo_root)
            attachment = build_result_attachment(
                command=command,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                elapsed=result.elapsed,
                timed_out=result.timed_out,
            )
            await self.send(
                ctx,
                format_result(
                    command=command,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    exit_code=result.exit_code,
                    elapsed=result.elapsed,
                    timed_out=result.timed_out,
                ),
                attachment=attachment,
            )
        except Exception as exc:
            log.exception("DevAI _cmd: falha executando comando")
            await self.send(ctx, f"❌ Falha ao executar comando: `{type(exc).__name__}: {exc}`")

    async def _run_self_affecting_shortcut(self, command: str) -> None:
        await asyncio.sleep(1.0)
        try:
            await run_shell(command, cwd=self.repo_root, timeout=10.0)
        except Exception:
            log.exception("DevAI _cmd: falha executando atalho self-affecting: %s", command)

    async def _consume_confirmation(self, ctx: commands.Context, user_id: int, command: str) -> str:
        code = command.split(maxsplit=1)[1].strip().upper()
        pending = self.pending_confirm.get(user_id)
        if not pending or str(pending.get("code", "")).upper() != code:
            await self.send(ctx, "⚠️ Confirmação inválida ou expirada.")
            return ""
        if time.time() - float(pending.get("created_at") or 0) > self.CONFIRM_TTL_SECONDS:
            self.pending_confirm.pop(user_id, None)
            await self.send(ctx, "⚠️ Essa confirmação expirou.")
            return ""
        self.pending_confirm.pop(user_id, None)
        return str(pending.get("command") or "").strip()
