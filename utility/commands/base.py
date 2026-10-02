"""Private prefix command that sends the current light Git base directly."""

from __future__ import annotations

import asyncio
from contextlib import closing
import io
import logging
import time

import discord
from discord.ext import commands

from utility.base_archive import (
    BASE_ARCHIVE_MAX_BYTES,
    BaseArchiveError,
    get_base_archive_service,
)
from utility.technical_commands import can_use_technical_command


logger = logging.getLogger(__name__)


class BaseCommandMixin:
    @commands.command(name="base", hidden=True)
    async def base(self, ctx: commands.Context) -> None:
        """Anexa a base Git leve; arquivos sensíveis são excluídos pelas regras da base."""
        if not await can_use_technical_command(self.bot, ctx.author, ctx.guild):
            await ctx.send(
                "Esse comando técnico é exclusivo do dono do bot na guilda configurada.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        service = get_base_archive_service()
        preparing = None
        started = time.monotonic()
        # A fast cached/cold result needs only the final message. Slower generation
        # receives one acknowledgement without cancelling the shared worker.
        request = asyncio.create_task(service.get_archive())
        try:
            done, _pending = await asyncio.wait({request}, timeout=1.0)
            if not done:
                preparing = await ctx.send("📦 Preparando a base Git leve…", allowed_mentions=discord.AllowedMentions.none())
            result = await request
            try:
                guild_limit = int(ctx.guild.filesize_limit)
            except (AttributeError, TypeError, ValueError):
                raise BaseArchiveError("O limite de anexos desta guilda está indisponível. Tente novamente.")
            limit = min(BASE_ARCHIVE_MAX_BYTES, max(0, guild_limit))
            if len(result.payload) > limit:
                raise BaseArchiveError(
                    f"A base tem {len(result.payload) / 1_000_000:.2f} MB e ultrapassa "
                    f"o limite de anexos desta guilda ({limit / 1_000_000:.2f} MB)."
                )
            text = f"📦 Base Git leve · {len(result.payload) / 1_000_000:.2f} MB · {result.file_count} arquivos"
            send_started = time.monotonic()
            with closing(discord.File(io.BytesIO(result.payload), filename=result.filename)) as attachment:
                if preparing:
                    await preparing.edit(content=text, attachments=[attachment], allowed_mentions=discord.AllowedMentions.none())
                else:
                    await ctx.send(text, file=attachment, allowed_mentions=discord.AllowedMentions.none())
            logger.info("[utility/base] send=%.3fs total=%.3fs cache=%s bytes=%d",
                        time.monotonic() - send_started, time.monotonic() - started,
                        "hit" if result.cache_hit else "miss", len(result.payload))
        except BaseArchiveError as exc:
            await self._send_base_failure(ctx, preparing, str(exc))
        except asyncio.CancelledError:
            # Cancelling this command ends its waiter; the service's shielded task
            # keeps ownership of the worker until its deadline or completion.
            request.cancel()
            raise
        except Exception:
            logger.exception("[utility/base] command or attachment send failed")
            await self._send_base_failure(ctx, preparing, "Não consegui enviar a base agora. Tente novamente.")
        finally:
            if not request.done():
                request.cancel()
            elif not request.cancelled():
                request.exception()

    @staticmethod
    async def _send_base_failure(ctx: commands.Context, preparing, text: str) -> None:
        if preparing:
            try:
                await preparing.edit(content=f"⚠️ {text}", attachments=[], allowed_mentions=discord.AllowedMentions.none())
                return
            except Exception:
                logger.warning("[utility/base] preparation message update failed", exc_info=True)
        await ctx.send(f"⚠️ {text}", allowed_mentions=discord.AllowedMentions.none())
