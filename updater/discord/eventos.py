from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

import discord

from updater.utilitarios.seguranca import UpdateSecurityError
from .constantes import UPDATE_EMOJI_PROGRESS, UPDATE_LOG


_COG_EXTENSION_ALIASES = {"cogs.gincana": "cogs.games"}

def _env_truthy(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "sim", "s"}

def _normalize_extension_name(value: str) -> str:
    value = str(value or "").strip().replace("/", ".").replace("\\", ".")
    if value.endswith(".py"):
        value = value[:-3]
    value = value.strip(".")
    if not value:
        return ""
    if not value.startswith("cogs."):
        value = f"cogs.{value}"
    return _COG_EXTENSION_ALIASES.get(value, value)


class EventosUpdaterMixin:
    def _schedule_update_interaction_notice(self, interaction: discord.Interaction) -> None:
        presence = getattr(self, "application_presence", None)
        if presence is None or not bool(getattr(presence, "maintenance_active", False)):
            return
        interaction_type = getattr(discord.InteractionType, "application_command", None)
        if interaction_type is None or getattr(interaction, "type", None) != interaction_type:
            return

        user_id = int(getattr(getattr(interaction, "user", None), "id", 0) or 0)
        if user_id <= 0:
            return
        try:
            cooldown = max(30.0, min(600.0, float(os.getenv("UPDATE_NOTICE_COOLDOWN_SECONDS", "120") or 120)))
        except (TypeError, ValueError):
            cooldown = 120.0
        now = time.monotonic()
        if now - self._update_notice_by_user.get(user_id, 0.0) < cooldown:
            return
        self._update_notice_by_user[user_id] = now
        asyncio.create_task(
            self._send_update_interaction_notice(interaction, user_id, now),
            name=f"update-notice-{user_id}",
        )


    async def _send_update_interaction_notice(
        self,
        interaction: discord.Interaction,
        user_id: int,
        reservation: float,
    ) -> None:
        try:
            for _ in range(25):
                if interaction.response.is_done():
                    break
                await asyncio.sleep(0.1)
            if not interaction.response.is_done():
                if self._update_notice_by_user.get(user_id) == reservation:
                    self._update_notice_by_user.pop(user_id, None)
                return
            presence = getattr(self, "application_presence", None)
            if presence is None or not bool(getattr(presence, "maintenance_active", False)):
                return
            await interaction.followup.send(
                "⚠️ Atualização em andamento. A resposta pode demorar.",
                ephemeral=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._update_notice_by_user.get(user_id) == reservation:
                self._update_notice_by_user.pop(user_id, None)
            UPDATE_LOG.debug("falha ao enviar aviso curto de atualização", exc_info=True)


    async def on_interaction(self, interaction: discord.Interaction):
        self._schedule_update_interaction_notice(interaction)
        try:
            custom_id = ""
            data = getattr(interaction, "data", None)
            if isinstance(data, dict):
                custom_id = str(data.get("custom_id") or "")
            if custom_id.startswith("zip_update_confirm:"):
                await self._on_zip_update_confirmation_click(interaction)
            elif custom_id.startswith("zip_update_info:"):
                await self._on_zip_update_info_click(interaction)
            elif custom_id.startswith("zip_update:"):
                await self._on_zip_update_control_click(interaction)
        except Exception:
            UPDATE_LOG.exception("falha ao processar controle de update")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Falha ao processar esta ação.", ephemeral=True)
            except Exception:
                pass


    async def _reload_cogs_for_update(self, modules: list[str], *, check_app_commands: bool = False) -> dict[str, object]:
        results: list[dict[str, str]] = []
        for module in modules:
            module = _normalize_extension_name(module)
            try:
                if module in self.extensions:
                    await self.reload_extension(module)
                    results.append({"module": module, "status": "reloaded"})
                else:
                    await self.load_extension(module)
                    results.append({"module": module, "status": "loaded"})
            except Exception as exc:
                logging.getLogger("zip_update").exception("falha ao recarregar cog %s pelo updater", module)
                return {"ok": False, "error": f"{module}: {type(exc).__name__}: {str(exc)[:300]}", "results": results}
        app_commands_status: dict[str, object] | None = None
        if check_app_commands:
            guild_ids = self._resolve_app_command_sync_guild_ids()
            await self._cleanup_removed_slash_commands_if_needed(guild_ids)
            app_commands_status = await self._smart_sync_app_commands(
                guild_ids,
                should_sync=self._app_command_sync_enabled(),
                allow_global_sync=self._app_command_global_sync_allowed(),
                clear_globals_allowed=_env_truthy("CLEAR_GLOBAL_COMMANDS"),
                trigger="reload_cogs",
            )
        response: dict[str, object] = {"ok": True, "results": results}
        if app_commands_status is not None:
            response["app_commands"] = app_commands_status
        return response


    async def _handle_zip_update_message(self, message: discord.Message):
        if not self._zip_update_can_submit(message):
            await self._send_zip_update_message(
                message,
                "❌ Atualização não autorizada",
                "Você não tem permissão para enviar atualizações por este canal.\n-# Nenhum arquivo foi processado.",
                discord.Color.red(),
            )
            return

        zip_attachments = [
            attachment
            for attachment in message.attachments
            if str(getattr(attachment, "filename", "")).lower().endswith(".zip")
        ]
        if not zip_attachments:
            await self._send_zip_update_message(
                message,
                "❌ Arquivo inválido",
                "Envie um arquivo **.zip** neste canal.",
                discord.Color.red(),
            )
            return

        limits = self._zip_update_limits()
        total_zips = len(zip_attachments)
        for index, zip_attachment in enumerate(zip_attachments, start=1):
            prefix = f"ZIP {index}/{total_zips}" if total_zips > 1 else ""
            zip_hint = f"\n-# {prefix}" if prefix else ""
            attachment_size = int(getattr(zip_attachment, "size", 0) or 0)
            if attachment_size > limits.max_archive_bytes:
                await self._send_zip_update_message(
                    message,
                    "❌ Arquivo muito grande",
                    (
                        f"O ZIP excede o limite de {limits.max_archive_bytes // (1024 * 1024)} MB."
                        + zip_hint
                    ),
                    discord.Color.red(),
                )
                continue

            received_at = getattr(message, "created_at", None)
            try:
                progress_started_epoch_ms = int(received_at.timestamp() * 1000)
            except Exception:
                progress_started_epoch_ms = int(time.time() * 1000)

            def progress_elapsed_text() -> str:
                elapsed_ms = max(0, int(time.time() * 1000) - progress_started_epoch_ms)
                return f"{elapsed_ms // 1000}s"

            status_message: discord.Message | None = await self._send_zip_update_message(
                message,
                "Atualização recebida",
                (
                    f"**Conferindo o anexo**\n"
                    f"A estrutura e a segurança serão validadas antes de alterar a VPS.{zip_hint}"
                ),
                discord.Color.blurple(),
                presentation={
                    "kind": "progress",
                    "stage": "Recebendo pacote",
                    "detail": str(getattr(zip_attachment, "filename", "update.zip") or "update.zip"),
                    "identifier": prefix,
                    "elapsed": progress_elapsed_text(),
                    "macro_index": 0,
                    "action": "update",
                },
            )

            async with self._zip_update_lock:
                self._update_temp_root.mkdir(parents=True, exist_ok=True)
                work_dir = Path(tempfile.mkdtemp(prefix="discord-auto-update-msg-", dir=str(self._update_temp_root)))
                safe_zip_name = Path(str(zip_attachment.filename or "update.zip")).name or "update.zip"
                zip_path = work_dir / safe_zip_name
                try:
                    await zip_attachment.save(zip_path)
                    if zip_path.stat().st_size > limits.max_archive_bytes:
                        raise UpdateSecurityError(
                            f"ZIP excede o limite de {limits.max_archive_bytes // (1024 * 1024)} MB"
                        )
                    status_message = await self._edit_zip_update_message(
                        message,
                        status_message,
                        "Preparando atualização",
                        (
                            f"{UPDATE_EMOJI_PROGRESS} **Validando pacote**\n"
                            "-# A mensagem será atualizada em cada etapa." + zip_hint
                        ),
                        discord.Color.blurple(),
                        presentation={
                            "kind": "progress",
                            "stage": "Validando pacote",
                            "detail": "Integridade e segurança do ZIP",
                            "identifier": prefix,
                            "elapsed": progress_elapsed_text(),
                            "macro_index": 0,
                            "action": "update",
                        },
                    )

                    status_context = {
                        "guild_id": getattr(message.guild, "id", None),
                        "channel_id": getattr(status_message.channel, "id", None) if status_message else getattr(message.channel, "id", None),
                        "message_id": getattr(status_message, "id", None),
                        "source_message_id": getattr(message, "id", None),
                        "source_author_id": getattr(message.author, "id", None),
                    }
                    progress_queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
                    preparation_history: list[str] = []
                    preparation_last_completed = {"stage": "", "duration": ""}

                    def format_elapsed_ms(value: object) -> str:
                        try:
                            elapsed_ms = max(0, int(value or 0))
                        except (TypeError, ValueError):
                            elapsed_ms = 0
                        if elapsed_ms <= 0:
                            return "<1ms"
                        if elapsed_ms < 1000:
                            return f"{elapsed_ms}ms"
                        if elapsed_ms < 60000:
                            seconds = elapsed_ms / 1000
                            text = f"{seconds:.1f}".rstrip("0").rstrip(".").replace(".", ",")
                            return f"{text}s"
                        total_seconds = round(elapsed_ms / 1000)
                        minutes, seconds = divmod(total_seconds, 60)
                        return f"{minutes}min {seconds:02d}s"

                    async def consume_preparation_progress() -> None:
                        last_render_at = 0.0
                        while True:
                            event = await progress_queue.get()
                            if event is None:
                                return
                            batch: list[dict[str, object]] = [event]
                            stop_after_batch = False
                            # Etapas de preparação podem terminar em poucos ms. Espere uma
                            # fração curta e drene o burst para não piscar dezenas de edits.
                            since_last = time.monotonic() - last_render_at
                            if last_render_at and since_last < 0.75:
                                await asyncio.sleep(0.75 - since_last)
                            while True:
                                try:
                                    queued = progress_queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    break
                                if queued is None:
                                    stop_after_batch = True
                                    break
                                batch.append(queued)
                            current = "Processando pacote"
                            detail = "Preparação segura do candidato"
                            for item in batch:
                                completed = str(item.get("completed") or "").strip()
                                current = str(item.get("current") or current).strip() or current
                                if completed:
                                    completed_duration = format_elapsed_ms(item.get("elapsed_ms"))
                                    preparation_history.append(f"-# {completed}")
                                    preparation_last_completed["stage"] = completed
                                    preparation_last_completed["duration"] = completed_duration
                                    detail = f"{completed} concluído"
                            view = self._make_zip_update_view(
                                "Preparando atualização",
                                "",
                                discord.Color.blurple(),
                                presentation={
                                    "kind": "progress",
                                    "stage": current,
                                    "detail": detail,
                                    "identifier": prefix,
                                    "elapsed": progress_elapsed_text(),
                                    "completed_stage": preparation_last_completed["stage"],
                                    "completed_duration": preparation_last_completed["duration"],
                                    "completed_macro_index": 0,
                                    "macro_index": 0,
                                    "action": "update",
                                },
                            )
                            try:
                                await asyncio.wait_for(
                                    status_message.edit(view=view, allowed_mentions=discord.AllowedMentions.none()),
                                    timeout=15,
                                )
                                last_render_at = time.monotonic()
                            except Exception:
                                UPDATE_LOG.warning("falha ao editar microetapa de preparação", exc_info=True)
                            if stop_after_batch:
                                return

                    progress_consumer = asyncio.create_task(consume_preparation_progress())

                    def report_preparation_progress(event: dict[str, object]) -> None:
                        self.loop.call_soon_threadsafe(progress_queue.put_nowait, event)

                    try:
                        result = await asyncio.to_thread(
                            self._process_zip_update_sync,
                            zip_path,
                            status_context,
                            report_preparation_progress,
                            progress_started_epoch_ms,
                        )
                    finally:
                        progress_queue.put_nowait(None)
                        await progress_consumer
                    changed_files = list(result.get("changed_files") or [])
                    if not changed_files:
                        await self._edit_zip_update_message(
                            message,
                            status_message,
                            "ℹ️ Nenhuma alteração",
                            f"O pacote é válido, mas não altera o repositório.{zip_hint}",
                            discord.Color.gold(),
                        )
                        continue

                    diff_stats = result.get("diff_stats") if isinstance(result.get("diff_stats"), dict) else {}
                    diff_summary = str(diff_stats.get("summary") or "").strip()
                    queue_position = max(1, int(result.get("queue_position") or 1))
                    candidate_id = str(result.get("candidate_id") or "").strip()
                    display_id = str(result.get("display_id") or candidate_id).strip()

                    file_count = len(changed_files)
                    file_text = "1 arquivo preparado" if file_count == 1 else f"{file_count} arquivos preparados"
                    file_summary = f"{file_text} · {diff_summary}" if diff_summary else file_text

                    if queue_position <= 1:
                        # O primeiro item não está esperando outro update. Mantenha o
                        # painel de microetapas e faça a transição direta para o updater.
                        visible_history = preparation_history[-8:]
                        hidden_count = max(0, len(preparation_history) - len(visible_history))
                        details: list[str] = []
                        if hidden_count:
                            noun = "etapa anterior concluída" if hidden_count == 1 else "etapas anteriores concluídas"
                            details.append(f"-# … {hidden_count} {noun}")
                        details.extend(visible_history)
                        details.append("<a:loading:1510065277868445796> **Iniciando atualização**")
                        details.append(f"-# `{display_id}` · {file_summary}")
                        if prefix:
                            details.append(f"-# {prefix}")
                        status_message = await self._edit_zip_update_message(
                            message,
                            status_message,
                            "Preparando atualização",
                            "\n".join(details),
                            discord.Color.blurple(),
                            control=self._zip_update_cancel_control(candidate_id),
                            presentation={
                                "kind": "progress",
                                "stage": "Iniciando atualização",
                                "detail": file_summary,
                                "identifier": display_id,
                                "elapsed": progress_elapsed_text(),
                                "completed_stage": preparation_last_completed["stage"],
                                "completed_duration": preparation_last_completed["duration"],
                                "completed_macro_index": 0,
                                "macro_index": 0,
                                "action": "update",
                            },
                        )
                    else:
                        before = queue_position - 1
                        before_text = "1 atualização antes desta" if before == 1 else f"{before} atualizações antes desta"
                        details = [
                            f"Atualização `{display_id}`",
                            f"Posição na fila: **{queue_position}**",
                            f"{before_text}.",
                            f"-# {file_summary}",
                        ]
                        if prefix:
                            details.append(f"-# {prefix}")
                        status_message = await self._edit_zip_update_message(
                            message,
                            status_message,
                            "📦 Atualização na fila",
                            "\n".join(details),
                            discord.Color.blurple(),
                            control=self._zip_update_cancel_control(candidate_id),
                        )
                    await self._dispatch_updater_candidate(candidate_id, display_id)
                except zipfile.BadZipFile:
                    await self._edit_zip_update_message(
                        message,
                        status_message,
                        "❌ ZIP inválido",
                        f"O arquivo não pôde ser aberto como um ZIP válido.{zip_hint}",
                        discord.Color.red(),
                    )
                except (UpdateSecurityError, RuntimeError) as exc:
                    reason = str(exc).strip() or type(exc).__name__
                    if len(reason) > 600:
                        reason = reason[:597].rstrip() + "..."
                    await self._edit_zip_update_message(
                        message,
                        status_message,
                        "❌ Atualização rejeitada",
                        f"Nenhuma alteração foi aplicada.\n**Motivo:** {reason}{zip_hint}",
                        discord.Color.red(),
                    )
                except Exception as exc:
                    UPDATE_LOG.exception("Falha no auto-update via ZIP do Discord")
                    reason = str(exc).strip() or type(exc).__name__
                    if len(reason) > 600:
                        reason = reason[:597].rstrip() + "..."
                    await self._edit_zip_update_message(
                        message,
                        status_message,
                        "❌ Falha ao preparar atualização",
                        f"Nenhuma alteração foi aplicada.\n**Motivo:** {reason}{zip_hint}",
                        discord.Color.red(),
                    )
                finally:
                    shutil.rmtree(work_dir, ignore_errors=True)
