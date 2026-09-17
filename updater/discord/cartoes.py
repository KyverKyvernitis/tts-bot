from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import discord

from updater.utilitarios.seguranca import ZipLimits
from .constantes import (
    UPDATE_EMOJI_CHECK, UPDATE_EMOJI_DATABASE, UPDATE_EMOJI_ERROR,
    UPDATE_EMOJI_FILES, UPDATE_EMOJI_GITHUB, UPDATE_EMOJI_PROGRESS,
    UPDATE_EMOJI_PROGRESS_TITLE, UPDATE_LOG,
)


class CartoesUpdaterMixin:
    def _zip_update_render_card_text(
        self,
        title: str,
        description: str,
        presentation: dict[str, object] | None = None,
    ) -> str:
        presentation = presentation if isinstance(presentation, dict) else {}
        kind = str(presentation.get("kind") or "").strip().lower()
        if kind == "progress":
            action = str(presentation.get("action") or "update").strip().lower()
            headline = {
                "rollback": "Revertendo atualização",
                "redo": "Reaplicando atualização",
            }.get(action, "Atualizando")
            identifier = str(presentation.get("identifier") or "").strip()
            elapsed = str(presentation.get("elapsed") or "").strip()
            stage = str(presentation.get("stage") or "Processando atualização").strip()
            detail = str(presentation.get("detail") or "").strip()
            completed_stage = str(presentation.get("completed_stage") or "").strip()
            completed_duration = str(presentation.get("completed_duration") or "").strip()
            try:
                completed_macro = int(presentation.get("completed_macro_index") if presentation.get("completed_macro_index") is not None else -1)
            except (TypeError, ValueError):
                completed_macro = -1
            try:
                current = max(0, min(9, int(presentation.get("macro_index") or 0)))
            except (TypeError, ValueError):
                current = 0
            macros = ("Pacote", "Segurança", "Preparação", "Isolamento", "Validação", "Release", "Promoção", "Aplicação", "Verificação", "GitHub")
            lines = [f"# {UPDATE_EMOJI_PROGRESS_TITLE} {headline}"]
            meta = " · ".join(piece for piece in (f"`{identifier}`" if identifier else "", elapsed) if piece)
            if meta:
                lines.append(f"-# {meta}")
            lines.append("")
            raw_macro_durations = presentation.get("macro_durations")
            macro_durations: dict[int, str] = {}
            if isinstance(raw_macro_durations, dict):
                for raw_index, raw_duration in raw_macro_durations.items():
                    try:
                        duration_index = int(raw_index)
                    except (TypeError, ValueError):
                        continue
                    duration_text = str(raw_duration or "").strip()
                    if 0 <= duration_index < len(macros) and duration_text:
                        macro_durations[duration_index] = duration_text
            # Compatibilidade com eventos produzidos pelo updater anterior: ele
            # conhecia apenas a última duração concluída.
            if completed_duration and completed_macro >= 0:
                macro_durations.setdefault(completed_macro, completed_duration)

            # Etapas futuras não são pré-renderizadas. O card cresce conforme o
            # updater realmente alcança cada macroetapa, preservando a sensação
            # de progresso da UI antiga sem criar mensagens adicionais.
            for index, label in enumerate(macros[: current + 1]):
                # Cada macroetapa concluída preserva seu próprio tempo. A etapa
                # atual não mostra tempo parcial para não sugerir conclusão.
                duration_text = macro_durations.get(index, "") if index < current else ""
                duration_suffix = f" ({duration_text})" if duration_text else ""
                if index < current:
                    lines.append(f"{UPDATE_EMOJI_CHECK} {label}{duration_suffix}")
                else:
                    lines.append(f"{UPDATE_EMOJI_PROGRESS} **{label}{duration_suffix}**")
                if completed_stage and completed_macro == index:
                    lines.append(f"-# {completed_stage}"[:240])
                if index == current:
                    micro_parts = [stage]
                    if detail and detail.casefold() not in stage.casefold():
                        micro_parts.append(detail)
                    micro = " · ".join(part for part in micro_parts if part)
                    if micro and micro.casefold() != completed_stage.casefold():
                        lines.append(f"-# {micro[:240]}")
            return "\n".join(lines)

        if kind == "recovery":
            identifier = str(presentation.get("identifier") or "").strip()
            failure_code = str(presentation.get("failure_code") or "UPDATE_STAGE_FAILED").strip()
            stage = str(presentation.get("stage") or "Restaurando versão anterior").strip()
            detail = str(presentation.get("detail") or "").strip()
            elapsed = str(presentation.get("elapsed") or "").strip()
            try:
                step = max(0, min(2, int(presentation.get("recovery_step") or 0)))
            except (TypeError, ValueError):
                step = 0
            recovery_steps = ("Código", "Runtimes", "Verificação")
            lines = [f"# {UPDATE_EMOJI_ERROR} Atualização falhou"]
            meta = " · ".join(piece for piece in (f"`{identifier}`" if identifier else "", f"`{failure_code}`", elapsed) if piece)
            if meta:
                lines.append(f"-# {meta}")
            lines.append("")
            for index, label in enumerate(recovery_steps):
                if index < step:
                    lines.append(f"{UPDATE_EMOJI_CHECK} {label}")
                elif index == step:
                    lines.append(f"{UPDATE_EMOJI_PROGRESS} **{label}**")
                    micro = " · ".join(part for part in (stage, detail) if part)
                    if micro:
                        lines.append(f"-# {micro[:240]}")
                else:
                    lines.append(f"○ {label}")
            return "\n".join(lines)

        if kind == "final":
            status = str(presentation.get("status") or "success").strip().lower()
            if status in {"error", "failed", "failure"}:
                icon = UPDATE_EMOJI_ERROR
                headline = "Atualização não aplicada"
            elif status in {"warn", "warning"}:
                icon = "⚠️"
                headline = "Atualização concluída com avisos"
            else:
                icon = UPDATE_EMOJI_CHECK
                headline = "Atualização concluída"
            headline = str(presentation.get("headline") or headline).strip() or headline
            summary = str(presentation.get("summary") or description or "Atualização aplicada e validada.").strip()
            identifier = str(presentation.get("display_id") or "").strip()
            branch = str(presentation.get("branch") or "main").strip() or "main"
            old_commit = str(presentation.get("from") or "").strip()
            new_commit = str(presentation.get("to") or "").strip()
            files = str(presentation.get("file_count_text") or "arquivos alterados").strip()
            diff_summary = str(presentation.get("diff_summary") or "").strip()
            impact = str(presentation.get("impact") or "").strip()
            impact = {
                "sem reinício do bot": "Sem reinício",
                "reinício completo": "Bot reiniciado",
                "recarga controlada de cog": "Cog recarregada",
            }.get(impact.casefold(), impact)
            duration = str(presentation.get("duration") or "").strip()
            total_duration = str(presentation.get("total_duration") or "").strip()
            bot_health = str(presentation.get("bot_health") or "").strip()
            github_synced = bool(presentation.get("github_synced", True))
            failure_code = str(presentation.get("failure_code") or "").strip()
            rollback_ok = presentation.get("rollback_ok")
            recovery_duration = str(presentation.get("recovery_duration") or "").strip()
            if status in {"error", "failed", "failure"} and rollback_ok is True:
                headline = str(presentation.get("headline") or "Atualização não aplicada").strip() or "Atualização não aplicada"
            elif status in {"error", "failed", "failure"} and rollback_ok is False:
                headline = str(presentation.get("headline") or "Recuperação necessária").strip() or "Recuperação necessária"
            lines = [f"# {icon} {headline}"]
            # No sucesso, o título já comunica "aplicada e validada". Preserve
            # apenas resumos que realmente acrescentem contexto (avisos/erros).
            if summary and not (status not in {"error", "failed", "failure", "warn", "warning"} and summary.casefold() == "atualização aplicada e validada."):
                lines.append(summary)
            lines.append("")
            if identifier:
                lines.append(f"`{identifier}` · `{branch}`")
            if old_commit or new_commit:
                lines.append(f"`{old_commit or '?'}` → `{new_commit or '?'}`")
            if failure_code:
                lines.append(f"{UPDATE_EMOJI_ERROR} `{failure_code}`")
            if rollback_ok is True:
                lines.append(f"{UPDATE_EMOJI_CHECK} Versão anterior restaurada")
            elif rollback_ok is False:
                lines.append("⚠️ Rollback incompleto · verificação manual necessária")
            file_line = f"{UPDATE_EMOJI_FILES} **{files}**"
            if diff_summary:
                file_line += f" · `{diff_summary}`"
            lines.append(file_line)
            if impact:
                lines.append(impact)
            final_duration = total_duration or duration or recovery_duration
            if final_duration:
                lines.append(f"⏱ **{final_duration}**")
            health_bits: list[str] = []
            if bot_health:
                health_label = "Bot saudável" if bot_health == "OK" or bot_health.startswith("estável") else f"Bot: {bot_health}"
                health_bits.append(f"{UPDATE_EMOJI_CHECK} {health_label}")
            if github_synced:
                health_bits.append(f"{UPDATE_EMOJI_GITHUB} GitHub sincronizado")
            if health_bits:
                lines.append("")
                lines.append(" · ".join(health_bits))
            return "\n".join(lines)

        title = self._zip_update_normalize_title(title, "")
        return f"# {title}\n{description.strip()}".strip()


    def _make_zip_update_view(
        self,
        title: str,
        description: str,
        color: discord.Color,
        control: dict[str, object] | None = None,
        *,
        presentation: dict[str, object] | None = None,
        info_token: str = "",
    ) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        text = self._zip_update_render_card_text(title, description, presentation)
        if len(text) > 3900:
            text = text[:3897].rstrip() + "..."
        children: list[discord.ui.Item] = [discord.ui.TextDisplay(text)]

        info_buttons: list[discord.ui.Button] = []
        if info_token and isinstance(presentation, dict) and str(presentation.get("kind") or "").lower() == "final":
            token = str(info_token)[:40]
            info_buttons.append(
                discord.ui.Button(
                    label="Detalhes",
                    emoji=UPDATE_EMOJI_DATABASE,
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"zip_update_info:details:{token}"[:100],
                )
            )
            if str(presentation.get("files_text") or "").strip():
                info_buttons.append(
                    discord.ui.Button(
                        label="Arquivos",
                        emoji=UPDATE_EMOJI_FILES,
                        style=discord.ButtonStyle.secondary,
                        custom_id=f"zip_update_info:files:{token}"[:100],
                    )
                )
        if info_buttons:
            children.append(discord.ui.Separator())
            children.append(discord.ui.ActionRow(*info_buttons))

        if isinstance(control, dict) and control.get("enabled"):
            raw_buttons = control.get("buttons")
            control_items = raw_buttons if isinstance(raw_buttons, list) else [control]
            buttons: list[discord.ui.Button] = []
            for item in control_items[:5]:
                if not isinstance(item, dict):
                    continue
                emoji = str(item.get("emoji") or "").strip() or None
                label = str(item.get("label") or "").strip()[:80] or None
                custom_id = str(item.get("custom_id") or "")[:100]
                disabled = bool(item.get("disabled"))
                style_name = str(item.get("style") or "secondary").strip().lower()
                style = {
                    "primary": discord.ButtonStyle.primary,
                    "success": discord.ButtonStyle.success,
                    "danger": discord.ButtonStyle.danger,
                }.get(style_name, discord.ButtonStyle.secondary)
                if custom_id and (label or emoji):
                    buttons.append(
                        discord.ui.Button(
                            label=label,
                            emoji=emoji,
                            style=style,
                            custom_id=custom_id,
                            disabled=disabled,
                        )
                    )
            if buttons:
                if not info_buttons:
                    children.append(discord.ui.Separator())
                children.append(discord.ui.ActionRow(*buttons))
        container_kwargs: dict[str, object] = {}
        if not (isinstance(presentation, dict) and str(presentation.get("kind") or "").lower() == "progress"):
            container_kwargs["accent_color"] = color
        view.add_item(discord.ui.Container(*children, **container_kwargs))
        return view


    def _make_zip_update_confirmation_view(
        self,
        *,
        mode: str,
        token: str,
        current_commit: str,
        target_commit: str,
    ) -> discord.ui.LayoutView:
        action = "reaplicação" if mode == "redo" else "reversão"
        style = discord.ButtonStyle.success if mode == "redo" else discord.ButtonStyle.danger
        current = current_commit[:12] or "desconhecido"
        target = target_commit[:12] or "desconhecido"
        view = discord.ui.LayoutView(timeout=90)
        text = (
            f"# Confirmar {action}\n"
            "A ação criará um novo commit e poderá reiniciar processos.\n\n"
            f"`{current}` → `{target}`\n"
            "-# Nenhuma alteração será feita antes da confirmação."
        )
        confirm = discord.ui.Button(
            label=f"Confirmar {action}",
            style=style,
            custom_id=f"zip_update_confirm:{mode}:{token}"[:100],
        )
        cancel = discord.ui.Button(
            label="Cancelar",
            style=discord.ButtonStyle.secondary,
            custom_id=f"zip_update_confirm:cancel:{token}"[:100],
        )
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text),
                discord.ui.Separator(),
                discord.ui.ActionRow(confirm, cancel),
                accent_color=discord.Color.gold(),
            )
        )
        return view


    def _zip_update_state_load(self) -> dict[str, object]:
        try:
            if self._zip_rollback_state_path.is_file():
                data = json.loads(self._zip_rollback_state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            logging.getLogger("zip_update").warning("falha ao ler estado de rollback do updater", exc_info=True)
        return {}


    def _zip_update_state_save(self, data: dict[str, object]) -> bool:
        tmp: Path | None = None
        for attempt in range(3):
            try:
                self._zip_rollback_state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._zip_rollback_state_path.with_name(
                    f".{self._zip_rollback_state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                )
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
                os.replace(tmp, self._zip_rollback_state_path)
                return True
            except Exception:
                if tmp is not None:
                    with contextlib.suppress(Exception):
                        tmp.unlink()
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                logging.getLogger("zip_update").warning(
                    "falha ao salvar estado de rollback do updater após 3 tentativas",
                    exc_info=True,
                )
        return False


    def _zip_update_component_text(self, message: discord.Message) -> str:
        """Extrai texto visível dos Components V2 sem depender de uma versão específica do discord.py."""
        parts: list[str] = []
        visited: set[int] = set()

        def visit(value: object, depth: int = 0) -> None:
            if value is None or depth > 8:
                return
            if isinstance(value, str):
                text = value.strip()
                if text:
                    parts.append(text)
                return
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    visit(item, depth + 1)
                return
            if isinstance(value, dict):
                for key in ("content", "text", "label", "description", "components", "children", "items"):
                    if key in value:
                        visit(value.get(key), depth + 1)
                return

            identity = id(value)
            if identity in visited:
                return
            visited.add(identity)
            for attr in ("content", "text", "label", "description", "components", "children", "items"):
                with contextlib.suppress(Exception):
                    visit(getattr(value, attr), depth + 1)

        visit(getattr(message, "components", None))
        visit(getattr(message, "content", None))
        return "\n".join(dict.fromkeys(parts))


    def _zip_update_recovery_receipt_load(self, candidate_dir: Path) -> dict[str, object]:
        path = candidate_dir / "delivery.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


    def _zip_update_recovery_receipt_save(self, candidate_dir: Path, data: dict[str, object]) -> None:
        try:
            path = candidate_dir / "delivery.json"
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            UPDATE_LOG.warning("falha ao salvar recibo de recuperação do update em %s", candidate_dir, exc_info=True)


    def _zip_update_alert_receipt_save_sync(self, path: Path, event_id: str) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "event_id": event_id,
                        "delivered_at": datetime.now(timezone.utc).isoformat(),
                        "source": "bot_reconcile",
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, path)
            return True
        except Exception:
            UPDATE_LOG.warning("falha ao salvar recibo global do log de update em %s", path, exc_info=True)
            return False


    def _zip_update_log_channel_id_sync(self) -> int:
        """Resolve o canal técnico sem usar o webhook para envio.

        A migração é automática: `DISCORD_AUTO_UPDATE_LOG_CHANNEL_ID` tem
        prioridade; na primeira execução sem esse valor, o bot consulta apenas os
        metadados do webhook antigo, persiste o channel_id e nunca depende dele
        novamente para entregar mensagens.
        """
        for name in ("DISCORD_AUTO_UPDATE_LOG_CHANNEL_ID", "UPDATE_LOG_CHANNEL_ID", "ALERT_CHANNEL_ID"):
            raw = str(os.getenv(name, "") or "").strip()
            if raw.isdigit() and int(raw) > 0:
                return int(raw)

        try:
            data = json.loads(self._zip_update_log_channel_state_path.read_text(encoding="utf-8"))
            channel_id = int(str(data.get("channel_id") or "0")) if isinstance(data, dict) else 0
            if channel_id > 0:
                return channel_id
        except Exception:
            pass

        webhook_url = str(os.getenv("ALERT_WEBHOOK_URL", "") or "").strip()
        if not webhook_url.startswith("https://"):
            return 0
        try:
            request = urllib.request.Request(webhook_url, headers={"User-Agent": "tts-bot-update-log-migration/1"})
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8", errors="ignore") or "{}")
            channel_id = int(str(payload.get("channel_id") or "0")) if isinstance(payload, dict) else 0
            if channel_id <= 0:
                return 0
            self._zip_update_log_channel_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._zip_update_log_channel_state_path.with_name(
                f".{self._zip_update_log_channel_state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            tmp.write_text(
                json.dumps(
                    {
                        "channel_id": str(channel_id),
                        "resolved_at": datetime.now(timezone.utc).isoformat(),
                        "source": "legacy_webhook_metadata",
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self._zip_update_log_channel_state_path)
            return channel_id
        except Exception:
            UPDATE_LOG.warning(
                "não consegui resolver o canal de logs a partir do webhook legado; defina DISCORD_AUTO_UPDATE_LOG_CHANNEL_ID",
                exc_info=True,
            )
            return 0


    def _zip_update_claim_log_jobs_sync(self, *, limit: int = 12) -> list[tuple[Path, dict[str, object]]]:
        root = self._zip_update_log_outbox_dir
        root.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for stale in root.glob(".sending.*.json"):
            try:
                if now - stale.stat().st_mtime < 120:
                    continue
                parts = stale.name.split(".", 3)
                original = parts[3] if len(parts) == 4 and parts[3] else f"recovered-{int(now)}.json"
                target = root / original
                if target.exists():
                    target = root / f"recovered-{int(now)}-{uuid.uuid4().hex[:6]}-{original}"
                os.replace(stale, target)
            except OSError:
                continue

        claimed: list[tuple[Path, dict[str, object]]] = []
        candidates = sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime if item.exists() else 0.0)
        for path in candidates[: max(1, limit)]:
            claim = path.with_name(f".sending.{os.getpid()}.{path.name}")
            try:
                os.replace(path, claim)
                data = json.loads(claim.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("job de log não é objeto JSON")
                try:
                    next_attempt = float(data.get("next_attempt_at") or 0)
                except (TypeError, ValueError):
                    next_attempt = 0.0
                if next_attempt > now:
                    os.replace(claim, path)
                    continue
                claimed.append((claim, data))
            except Exception as exc:
                failed = root / "failed"
                failed.mkdir(parents=True, exist_ok=True)
                try:
                    fallback = {"attempts": 20, "last_error": f"{type(exc).__name__}: {exc}", "failed_at": datetime.now(timezone.utc).isoformat()}
                    claim.write_text(json.dumps(fallback, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(claim, failed / path.name)
                except Exception:
                    with contextlib.suppress(Exception):
                        claim.unlink()
        return claimed


    def _zip_update_requeue_log_job_sync(self, claim: Path, data: dict[str, object], exc: BaseException) -> None:
        root = self._zip_update_log_outbox_dir
        try:
            attempts = int(data.get("attempts") or 0) + 1
        except (TypeError, ValueError):
            attempts = 1
        data["attempts"] = attempts
        data["last_error"] = f"{type(exc).__name__}: {exc}"[:800]
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        if attempts >= 20:
            data["failed_at"] = datetime.now(timezone.utc).isoformat()
            failed = root / "failed"
            failed.mkdir(parents=True, exist_ok=True)
            claim.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(claim, failed / claim.name.split(".", 3)[-1])
            return
        data["next_attempt_at"] = time.time() + min(300, 5 * (2 ** min(attempts, 6)))
        original = claim.name.split(".", 3)[-1]
        target = root / original
        claim.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(claim, target)


    def _zip_update_log_attachment_path(self, raw: object) -> Path | None:
        text = str(raw or "").strip()
        if not text:
            return None
        root = self._zip_update_log_outbox_dir.resolve(strict=False)
        candidate = Path(text).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("anexo do log fora do outbox") from exc
        return candidate if candidate.is_file() else None


    async def _zip_update_flush_raw_logs_once(self) -> dict[str, object]:
        channel_id = await asyncio.to_thread(self._zip_update_log_channel_id_sync)
        if channel_id <= 0:
            return {"ok": False, "error": "canal de logs não configurado"}
        try:
            channel = self.get_channel(channel_id)
            if channel is None:
                channel = await self.fetch_channel(channel_id)
            if channel is None or not hasattr(channel, "send"):
                return {"ok": False, "error": "canal de logs indisponível"}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

        jobs = await asyncio.to_thread(self._zip_update_claim_log_jobs_sync)
        delivered = 0
        for claim, data in jobs:
            event_id = str(data.get("event_id") or claim.stem).strip() or claim.stem
            safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", event_id)[:120]
            receipt = self._zip_update_delivery_receipts_dir / f"{safe_id}.alert.done"
            attachment: Path | None = None
            try:
                attachment = self._zip_update_log_attachment_path(data.get("attachment"))
                if receipt.is_file():
                    claim.unlink(missing_ok=True)
                    if attachment is not None:
                        attachment.unlink(missing_ok=True)
                    continue

                title = str(data.get("title") or "Log do updater").strip()[:180]
                # O tipo já fornece o ícone do cabeçalho. Remova emoji/pictograma
                # legado do título para não produzir "✅ ✅ Atualização...".
                title = re.sub(r"^[^\wÀ-ÿ<]+\s*", "", title, count=1).strip() or "Log do updater"
                body = str(data.get("body") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
                kind = str(data.get("type") or "info").strip().lower()
                icon = {
                    "success": "<:checkmark:1548838297806311445>",
                    "error": "<:x_mark:1548838423169605654>",
                    "warn": "⚠️",
                    "warning": "⚠️",
                }.get(kind, "<:Files:1548838468665475193>")
                header = f"{icon} **{title}**\n-# `{event_id}` · resumo técnico · log anexado"
                files: list[discord.File] = []
                if attachment is not None:
                    files.append(discord.File(str(attachment), filename=str(data.get("attachment_name") or attachment.name)[:120]))
                if body and len(body) <= 1600 and "```" not in body:
                    content = f"{header}\n```text\n{body}\n```"
                else:
                    content = header
                    if body:
                        files.insert(0, discord.File(io.BytesIO(body.encode("utf-8", errors="replace")), filename=f"{safe_id or 'update'}.log.txt"))
                await channel.send(content=content, files=files, allowed_mentions=discord.AllowedMentions.none())
                await asyncio.to_thread(self._zip_update_alert_receipt_save_sync, receipt, event_id)
                claim.unlink(missing_ok=True)
                if attachment is not None:
                    attachment.unlink(missing_ok=True)
                delivered += 1
            except Exception as exc:
                UPDATE_LOG.warning("falha ao entregar log técnico %s", event_id, exc_info=True)
                await asyncio.to_thread(self._zip_update_requeue_log_job_sync, claim, data, exc)
        return {"ok": True, "delivered": delivered}


    def _zip_update_current_head_sync(self) -> str:
        try:
            completed = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self._repo_root),
                env=self._git_env(),
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            if completed.returncode == 0:
                return completed.stdout.strip()
        except Exception:
            UPDATE_LOG.debug("falha ao consultar HEAD durante reconciliação de update", exc_info=True)
        return ""


    def _zip_update_updater_active_sync(self) -> bool:
        """Evita reconciliar enquanto o updater ainda está finalizando o candidato."""
        try:
            completed = subprocess.run(
                ["systemctl", "is-active", "--quiet", "bot-updater.service"],
                text=True,
                capture_output=True,
                timeout=3,
                check=False,
            )
            return completed.returncode == 0
        except Exception:
            UPDATE_LOG.debug("falha ao consultar estado do updater durante reconciliação", exc_info=True)
            return False


    def _zip_update_recent_archives_sync(self) -> list[dict[str, object]]:
        """Localiza candidatos recentes que podem ter sido aplicados sem confirmação visual."""
        root = self._update_staging_root / "candidates"
        try:
            max_age = max(60, int(os.getenv("DISCORD_AUTO_UPDATE_RECONCILE_MAX_AGE_SECONDS", "1800") or 1800))
        except (TypeError, ValueError):
            max_age = 1800
        cutoff = time.time() - max_age
        records: list[dict[str, object]] = []
        for archive_state in ("done", "failed"):
            state_root = root / archive_state
            if not state_root.is_dir():
                continue
            def candidate_mtime(path: Path) -> float:
                try:
                    return path.stat().st_mtime
                except OSError:
                    return 0.0

            try:
                candidates = sorted(
                    (path for path in state_root.iterdir() if path.is_dir()),
                    key=candidate_mtime,
                    reverse=True,
                )
            except OSError:
                continue
            for candidate_dir in candidates[:25]:
                try:
                    if candidate_dir.stat().st_mtime < cutoff:
                        continue
                    manifest = json.loads((candidate_dir / "manifest.json").read_text(encoding="utf-8"))
                    if not isinstance(manifest, dict):
                        continue
                    status_ref = manifest.get("discord_status")
                    if not isinstance(status_ref, dict):
                        continue
                    channel_id = int(str(status_ref.get("channel_id") or "0"))
                    message_id = int(str(status_ref.get("message_id") or "0"))
                    if not channel_id or not message_id:
                        continue
                    state_data: dict[str, object] = {}
                    state_path = candidate_dir / "state.json"
                    if state_path.is_file():
                        loaded = json.loads(state_path.read_text(encoding="utf-8"))
                        if isinstance(loaded, dict):
                            state_data = loaded
                    records.append(
                        {
                            "candidate_dir": str(candidate_dir),
                            "archive_state": archive_state,
                            "manifest": manifest,
                            "state": state_data,
                            "channel_id": channel_id,
                            "message_id": message_id,
                        }
                    )
                except Exception:
                    UPDATE_LOG.debug("candidato arquivado inválido ignorado: %s", candidate_dir, exc_info=True)
        return records[:30]


    async def _zip_update_reconcile_archived_messages_once(self) -> None:
        final_markers = (
            "atualização concluída",
            "atualização concluida",
            "nenhuma alteração",
            "nenhuma alteracao",
            "atualização bloqueada",
            "atualizacao bloqueada",
            "atualização rejeitada",
            "atualizacao rejeitada",
            "falha ao aplicar",
            "falha na atualização",
            "atualização não aplicada",
            "atualizacao nao aplicada",
            "recuperação necessária",
            "recuperacao necessaria",
            "update aplicado",
            "update revertido",
            "reversão concluída",
            "reaplicação concluída",
        )
        if await asyncio.to_thread(self._zip_update_updater_active_sync):
            return
        records = await asyncio.to_thread(self._zip_update_recent_archives_sync)
        live_head = await asyncio.to_thread(self._zip_update_current_head_sync)
        for record in records:
            candidate_dir = Path(str(record.get("candidate_dir") or ""))
            receipt = await asyncio.to_thread(self._zip_update_recovery_receipt_load, candidate_dir)
            if bool(receipt.get("status_delivered")) and bool(receipt.get("log_delivered")):
                continue

            manifest = record.get("manifest") if isinstance(record.get("manifest"), dict) else {}
            state_data = record.get("state") if isinstance(record.get("state"), dict) else {}
            display_id = str(manifest.get("display_id") or manifest.get("id") or candidate_dir.name)
            branch = str(manifest.get("branch") or "main")
            base_commit = str(manifest.get("base_commit") or "").strip()
            applied_commit = str(state_data.get("commit") or "").strip()
            alert_event_id = f"{display_id}-final-{applied_commit[:7] if applied_commit else 'unknown'}"
            safe_alert_event_id = re.sub(r"[^A-Za-z0-9._-]", "_", alert_event_id)[:120]
            alert_receipt = self._repo_root / "data" / "runtime" / "update-delivery-receipts" / f"{safe_alert_event_id}.alert.done"
            if alert_receipt.is_file():
                receipt["log_delivered"] = True

            channel_id = int(record.get("channel_id") or 0)
            message_id = int(record.get("message_id") or 0)
            message = await self._zip_update_fetch_message(channel_id, message_id)
            if message is None:
                continue
            visible_text = self._zip_update_component_text(message).casefold()
            already_final = any(marker in visible_text for marker in final_markers)
            if already_final:
                receipt.update(
                    {
                        "status_delivered": True,
                        "status_detected_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                # A mensagem pública final não prova que o log técnico também
                # chegou. O recibo global é compartilhado com a outbox do shell,
                # evitando que uma recuperação do bot e um replay posterior
                # publiquem o mesmo log duas vezes.
                await asyncio.to_thread(self._zip_update_recovery_receipt_save, candidate_dir, receipt)
                if bool(receipt.get("log_delivered")):
                    continue
            changed_files = [str(item) for item in (manifest.get("changed_files") or []) if str(item).strip()]
            diff_stats = manifest.get("diff_stats") if isinstance(manifest.get("diff_stats"), dict) else {}
            diff_summary = str(diff_stats.get("summary") or "").strip()
            source_author_id = str((manifest.get("discord_status") or {}).get("source_author_id") or "")
            archive_state = str(record.get("archive_state") or "failed")

            presentation: dict[str, object] | None = None
            if archive_state == "done" and applied_commit:
                commit_line = ""
                if base_commit:
                    commit_line = f"`{base_commit[:7]}` → `{applied_commit[:7]}`\n"
                count_text = "1 arquivo alterado" if len(changed_files) == 1 else f"{len(changed_files)} arquivos alterados"
                current_matches = bool(live_head and applied_commit == live_head)
                if not current_matches:
                    # Um diretório arquivado como done não é prova suficiente de
                    # sucesso se o HEAD já voltou para outro commit. Publicar verde
                    # nesse caso criava o falso positivo visto durante rollback.
                    title = "⚠️ Estado da atualização divergente"
                    description = (
                        "O registro da atualização indica conclusão, mas o commit atualmente instalado é diferente.\n\n"
                        f"Atualização `{display_id}`\n"
                        f"Registrado: `{applied_commit[:7]}`\n"
                        f"Instalado: `{live_head[:7] if live_head else 'desconhecido'}`\n"
                        "Nenhum controle de reversão foi disponibilizado."
                    )
                    control = None
                    status = "warn"
                    presentation = {
                        "kind": "final",
                        "status": "warn",
                        "headline": "Estado da atualização divergente",
                        "summary": "O registro indica conclusão, mas o commit instalado é diferente.",
                        "display_id": display_id,
                        "branch": branch,
                        "from": base_commit[:7],
                        "to": applied_commit[:7],
                        "file_count_text": count_text,
                        "diff_summary": diff_summary,
                        "github_synced": False,
                        "files_text": "\n".join(changed_files),
                    }
                    log_title = title
                    log_summary = "A reconciliação detectou divergência entre o candidato concluído e o HEAD local."
                else:
                    title = "✅ Atualização concluída"
                    description = (
                        "A atualização foi aplicada; a confirmação visual foi recuperada automaticamente após o reinício.\n\n"
                        f"Atualização `{display_id}`\n"
                        f"{commit_line}{count_text}{f' · {diff_summary}' if diff_summary else ''}\n"
                        "Processo ativo e bot conectado ao Discord."
                    )
                    control = {
                        "enabled": True,
                        "mode": "rollback",
                        "token": hashlib.sha256(f"{display_id}:{applied_commit}".encode()).hexdigest()[:16],
                        "branch": branch,
                        "expected_head": applied_commit,
                        "revert_commit": applied_commit,
                        "head_commit": applied_commit,
                        "update_from": base_commit,
                        "update_to": applied_commit,
                        "source_author_id": source_author_id,
                    }
                    status = "success"
                    presentation = {
                        "kind": "final",
                        "status": "success",
                        "headline": "Atualização concluída",
                        "summary": "A confirmação visual foi recuperada automaticamente após o reinício.",
                        "display_id": display_id,
                        "branch": branch,
                        "from": base_commit[:7],
                        "to": applied_commit[:7],
                        "file_count_text": count_text,
                        "diff_summary": diff_summary,
                        "bot_health": "estável após reconciliação",
                        "github_synced": True,
                        "files_text": "\n".join(changed_files),
                    }
                    log_title = title
                    log_summary = "Confirmação final recuperada após o bot reiniciar durante a atualização."
            elif archive_state == "done":
                title = "ℹ️ Nenhuma alteração necessária"
                description = f"A atualização `{display_id}` já correspondia ao estado atual. Nenhum arquivo foi modificado."
                control = None
                status = "success"
                presentation = {
                    "kind": "final",
                    "status": "success",
                    "headline": "Nenhuma alteração necessária",
                    "summary": "O pacote já correspondia ao estado atual da VPS.",
                    "display_id": display_id,
                    "branch": branch,
                    "from": base_commit[:7],
                    "to": base_commit[:7],
                    "file_count_text": "0 arquivos alterados",
                    "github_synced": True,
                    "files_text": "",
                }
                log_title = title
                log_summary = "Candidato concluído sem diferenças no repositório."
            else:
                error_text = str(state_data.get("last_error") or "A atualização não foi concluída.").strip()
                failure_code = str(state_data.get("failure_code") or "UPDATE_STAGE_FAILED").strip()
                rollback_ok_raw = state_data.get("rollback_ok")
                rollback_ok = rollback_ok_raw if isinstance(rollback_ok_raw, bool) else None
                target_commit = str(state_data.get("target_commit") or "").strip()
                recovery_duration = str(state_data.get("recovery_duration") or "").strip()
                bot_health = str(state_data.get("bot_health") or "").strip()
                if rollback_ok is True:
                    title = "❌ Atualização não aplicada"
                    summary_text = "A versão anterior foi restaurada e validada."
                elif rollback_ok is False:
                    title = "❌ Recuperação necessária"
                    summary_text = "O rollback não conseguiu restaurar completamente o estado anterior."
                else:
                    title = "❌ Falha ao aplicar atualização"
                    summary_text = "A confirmação de falha foi recuperada automaticamente."
                description = f"{summary_text}\n\nAtualização `{display_id}`\n{error_text[:900]}"
                control = None
                status = "error"
                presentation = {
                    "kind": "final",
                    "status": "error",
                    "headline": title.lstrip("❌ ").strip(),
                    "summary": summary_text,
                    "display_id": display_id,
                    "branch": branch,
                    "from": base_commit[:7],
                    "to": (target_commit or applied_commit)[:7],
                    "file_count_text": "1 arquivo alterado" if len(changed_files) == 1 else f"{len(changed_files)} arquivos alterados",
                    "diff_summary": diff_summary,
                    "bot_health": bot_health,
                    "github_synced": False,
                    "failure_code": failure_code,
                    "rollback_ok": rollback_ok,
                    "recovery_duration": recovery_duration,
                    "files_text": "\n".join(changed_files),
                }
                log_title = title
                log_summary = error_text[:1200]

            if not already_final or status == "warn":
                result = await self._edit_zip_status_from_update(
                    {
                        "channel_id": str(channel_id),
                        "message_id": str(message_id),
                        "status": status,
                        "title": title,
                        "description": description,
                        "candidate_id": str(manifest.get("id") or ""),
                        "display_id": display_id,
                        "event_at": datetime.now(timezone.utc).isoformat(),
                        "preserve_existing_control": control is None,
                        **({"ui": presentation} if isinstance(presentation, dict) else {}),
                        **({"control": control} if control else {}),
                    }
                )
                if not result.get("ok"):
                    continue

                receipt.update(
                    {
                        "status_delivered": True,
                        "status_recovered_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                await asyncio.to_thread(self._zip_update_recovery_receipt_save, candidate_dir, receipt)

            if bool(receipt.get("log_delivered")):
                continue
            body_lines = [
                f"Resumo: {log_summary}",
                f"Identificador: {display_id}",
                f"Branch: {branch}",
            ]
            if applied_commit:
                body_lines.append(f"Commit: {base_commit[:7] if base_commit else 'desconhecido'} → {applied_commit[:7]}")
            body_lines.append(f"Update: {'1 arquivo' if len(changed_files) == 1 else f'{len(changed_files)} arquivos'}{f' · {diff_summary}' if diff_summary else ''}")
            body_lines.append("Resultado: confirmação recuperada automaticamente")
            body_lines.append(f"Hora: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
            alert_type = "success" if status == "success" else ("warn" if status == "warn" else "error")
            try:
                env = self._git_env()
                env["REPO_DIR"] = str(self._repo_root)
                env["UPDATE_ALERT_OUTBOX_DIR"] = str(self._zip_update_log_outbox_dir)
                completed = await asyncio.to_thread(
                    subprocess.run,
                    [
                        "bash",
                        str(self._repo_root / "alert.sh"),
                        alert_type,
                        log_title,
                        "\n".join(body_lines),
                        "",
                        "",
                        alert_event_id,
                    ],
                    cwd=str(self._repo_root),
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                if completed.returncode == 0:
                    await self._zip_update_flush_raw_logs_once()
                    if alert_receipt.is_file():
                        receipt["log_delivered"] = True
                        receipt["log_recovered_at"] = datetime.now(timezone.utc).isoformat()
                        receipt["global_log_receipt_saved"] = True
                        await asyncio.to_thread(self._zip_update_recovery_receipt_save, candidate_dir, receipt)
                else:
                    UPDATE_LOG.warning("falha ao enfileirar log final recuperado de %s: %s", display_id, (completed.stderr or completed.stdout or "")[-500:])
            except Exception:
                UPDATE_LOG.warning("falha ao recuperar log final de %s", display_id, exc_info=True)


    async def _zip_update_reconcile_loop(self) -> None:
        await asyncio.sleep(4)
        reconcile_tick = 0
        while not self.is_closed():
            try:
                # Logs técnicos usam o mesmo bot, inclusive depois de um restart.
                # O polling curto reduz a latência sem editar a mensagem pública.
                await self._zip_update_flush_raw_logs_once()
                if reconcile_tick % 12 == 0:
                    await self._zip_update_reconcile_archived_messages_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                UPDATE_LOG.warning("falha na reconciliação de confirmações de update", exc_info=True)
            reconcile_tick += 1
            await asyncio.sleep(5)


    def _zip_update_rollback_request_roots(self) -> list[Path]:
        roots: list[Path] = []
        raw_env = os.getenv("DISCORD_AUTO_UPDATE_ROLLBACK_REQUEST_DIRS") or os.getenv("DISCORD_AUTO_UPDATE_ROLLBACK_REQUEST_DIR") or ""
        for raw in re.split(r"[:;,]", raw_env):
            raw = raw.strip()
            if raw:
                roots.append(Path(raw))
        roots.extend([
            self._update_staging_root / "candidates" / "rollback",
            self._repo_root / "data" / "runtime" / "update-rollback",
            Path(tempfile.gettempdir()) / "tts-bot-update-rollback",
        ])
        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            try:
                key = str(root.expanduser().resolve(strict=False))
            except Exception:
                key = str(root)
            if key in seen:
                continue
            seen.add(key)
            unique.append(root)
        return unique


    def _zip_update_find_writable_rollback_request_root(self) -> tuple[Path | None, list[str]]:
        details: list[str] = []
        for root in self._zip_update_rollback_request_roots():
            try:
                root.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(Exception):
                    root.chmod(0o775)
                probe = root / f".write-test.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
                return root, details
            except Exception as exc:
                details.append(f"{root}: {exc.__class__.__name__}: {exc}")
        return None, details


    def _zip_update_has_pending_rollback_request(self) -> bool:
        for root in self._zip_update_rollback_request_roots():
            try:
                if (root / "pending.json").exists() or (root / "active.json").exists():
                    return True
            except Exception:
                continue
        return False


    def _zip_update_control_for_record(self, record: dict[str, object], *, disabled: bool = False) -> dict[str, object] | None:
        token = str(record.get("token") or "").strip()
        mode = str(record.get("mode") or "rollback").strip().lower()
        if not token or mode not in {"rollback", "redo"}:
            return None
        return {
            "enabled": True,
            "emoji": "↪️" if mode == "redo" else "↩️",
            "label": "Reaplicar atualização" if mode == "redo" else "Reverter atualização",
            "style": "success" if mode == "redo" else "danger",
            "custom_id": f"zip_update:{mode}:{token}"[:100],
            "disabled": disabled,
        }


    def _zip_update_security_confirmation_control(
        self, candidate_id: str, *, disabled: bool = False
    ) -> dict[str, object] | None:
        candidate_id = str(candidate_id or "").strip()
        if not candidate_id:
            return None
        return {
            "enabled": True,
            "buttons": [
                {
                    "emoji": "✅",
                    "label": "Confirmar atualização",
                    "style": "success",
                    "custom_id": f"zip_update:approve:{candidate_id}"[:100],
                    "disabled": disabled,
                },
                {
                    "emoji": "✖️",
                    "label": "Cancelar",
                    "style": "secondary",
                    "custom_id": f"zip_update:cancel:{candidate_id}"[:100],
                    "disabled": disabled,
                },
            ],
        }


    def _zip_update_cancel_control(self, candidate_id: str, *, disabled: bool = False) -> dict[str, object] | None:
        candidate_id = str(candidate_id or "").strip()
        if not candidate_id:
            return None
        return {
            "enabled": True,
            "emoji": "✖️",
            "label": "Cancelar atualização",
            "style": "secondary",
            "custom_id": f"zip_update:cancel:{candidate_id}"[:100],
            "disabled": disabled,
        }


    def _zip_update_limits(self) -> ZipLimits:
        defaults = ZipLimits()

        def env_int(name: str, default: int, *, minimum: int = 1) -> int:
            try:
                return max(minimum, int(str(os.getenv(name, default)).strip()))
            except (TypeError, ValueError):
                return default

        def env_float(name: str, default: float, *, minimum: float = 1.0) -> float:
            try:
                return max(minimum, float(str(os.getenv(name, default)).strip()))
            except (TypeError, ValueError):
                return default

        return ZipLimits(
            max_archive_bytes=env_int("DISCORD_AUTO_UPDATE_MAX_ZIP_BYTES", defaults.max_archive_bytes),
            max_uncompressed_bytes=env_int("DISCORD_AUTO_UPDATE_MAX_UNCOMPRESSED_BYTES", defaults.max_uncompressed_bytes),
            max_entries=env_int("DISCORD_AUTO_UPDATE_MAX_ENTRIES", defaults.max_entries),
            max_file_bytes=env_int("DISCORD_AUTO_UPDATE_MAX_FILE_BYTES", defaults.max_file_bytes),
            max_compression_ratio=env_float("DISCORD_AUTO_UPDATE_MAX_COMPRESSION_RATIO", defaults.max_compression_ratio),
        )


    def _zip_update_authorized_user_ids(self, record: dict[str, object] | None = None) -> set[int]:
        ids: set[int] = set()
        raw_values = [
            os.getenv("DISCORD_AUTO_UPDATE_USER_IDS", ""),
            os.getenv("BOT_OWNER_IDS", ""),
            os.getenv("OWNER_IDS", ""),
            os.getenv("BOT_OWNER_ID", ""),
            os.getenv("OWNER_ID", ""),
        ]
        if isinstance(record, dict):
            raw_values.extend([
                str(record.get("source_author_id") or ""),
                str(record.get("requested_by") or ""),
            ])
        for raw in raw_values:
            for piece in re.split(r"[,;\s]+", str(raw or "")):
                piece = piece.strip()
                if piece.isdigit():
                    ids.add(int(piece))
        return ids


    def _zip_update_can_control(self, interaction: discord.Interaction, record: dict[str, object]) -> bool:
        user_id = int(getattr(interaction.user, "id", 0) or 0)
        if user_id in self._zip_update_authorized_user_ids(record):
            return True
        perms = getattr(getattr(interaction, "user", None), "guild_permissions", None)
        if perms and (getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False)):
            return True
        return False


    def _zip_update_can_submit(self, message: discord.Message) -> bool:
        author = getattr(message, "author", None)
        user_id = int(getattr(author, "id", 0) or 0)
        allowed = self._zip_update_authorized_user_ids()
        allowed.update(int(value) for value in (getattr(self, "owner_ids", None) or set()) if str(value).isdigit())
        owner_id = int(getattr(self, "owner_id", 0) or 0)
        if owner_id:
            allowed.add(owner_id)
        if user_id and user_id in allowed:
            return True
        perms = getattr(author, "guild_permissions", None)
        return bool(perms and (getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False)))


    async def _send_zip_update_message(
        self,
        message: discord.Message,
        title: str,
        description: str,
        color: discord.Color,
        control: dict[str, object] | None = None,
        *,
        presentation: dict[str, object] | None = None,
    ) -> discord.Message:
        view = self._make_zip_update_view(title, description, color, control=control, presentation=presentation)
        return await message.reply(view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())


    async def _edit_zip_update_message(
        self,
        source_message: discord.Message,
        status_message: discord.Message | None,
        title: str,
        description: str,
        color: discord.Color,
        control: dict[str, object] | None = None,
        *,
        presentation: dict[str, object] | None = None,
    ) -> discord.Message:
        view = self._make_zip_update_view(title, description, color, control=control, presentation=presentation)
        if status_message is not None:
            try:
                await status_message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
                return status_message
            except discord.HTTPException:
                logging.getLogger("zip_update").warning(
                    "não consegui editar a mensagem de status do ZIP; enviando resultado final",
                    exc_info=True,
                )
        return await source_message.reply(view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())


    async def _zip_update_fetch_message(self, channel_id: int, message_id: int) -> discord.Message | None:
        try:
            channel = self.get_channel(int(channel_id))
            if channel is None:
                channel = await self.fetch_channel(int(channel_id))
            if not hasattr(channel, "fetch_message"):
                return None
            return await channel.fetch_message(int(message_id))
        except Exception:
            logging.getLogger("zip_update").warning("falha ao buscar mensagem de update", exc_info=True)
            return None


    async def _zip_update_clear_previous_control(self, previous: dict[str, object] | None, *, keep_message_id: int = 0) -> None:
        if not isinstance(previous, dict):
            return
        try:
            channel_id = int(str(previous.get("channel_id") or "0"))
            message_id = int(str(previous.get("message_id") or "0"))
        except Exception:
            return
        if not channel_id or not message_id or message_id == keep_message_id:
            return
        msg = await self._zip_update_fetch_message(channel_id, message_id)
        if msg is None:
            return
        title = str(previous.get("title") or "Update aplicado")
        description = str(previous.get("description") or "")
        status = str(previous.get("status") or "success")
        color = self._zip_update_status_color(status)
        try:
            await msg.edit(view=self._make_zip_update_view(title, description, color), allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logging.getLogger("zip_update").warning("falha ao remover controle de update antigo", exc_info=True)


    def _zip_update_status_color(self, status: str) -> discord.Color:
        status = str(status or "info").lower().strip()
        if status in {"success", "ok", "done"}:
            return discord.Color.green()
        if status in {"warn", "warning", "progress"}:
            return discord.Color.gold()
        if status in {"error", "failed", "failure", "recovering"}:
            return discord.Color.red()
        if status in {"applying", "pending"}:
            return discord.Color.blurple()
        return discord.Color.blurple()


    def _zip_update_normalize_title(self, title: str, status: str = "") -> str:
        title = str(title or "").strip() or "Atualização"
        normalized = re.sub(r"\s+", " ", title).strip()
        lowered = normalized.casefold()
        status = str(status or "").lower().strip()
        replacements = {
            "update": "Atualização",
            "aplicando update...": "⚙️ Aplicando atualização",
            "revertendo update...": "↩️ Revertendo atualização",
            "reaplicando update...": "↪️ Reaplicando atualização",
            "update aplicado": "✅ Atualização concluída",
            "✅ update aplicado": "✅ Atualização concluída",
        }
        if lowered in replacements:
            return replacements[lowered]
        if status in {"success", "ok", "done"} and lowered in {"atualização", "update"}:
            return "✅ Atualização concluída"
        return normalized


    def _zip_update_latest_message_ref(self, interaction: discord.Interaction, record: dict[str, object]) -> tuple[str, str]:
        channel_id = str(record.get("channel_id") or "").strip()
        message_id = str(record.get("message_id") or "").strip()
        msg = getattr(interaction, "message", None)
        if msg is not None:
            if not channel_id:
                channel_id = str(getattr(getattr(msg, "channel", None), "id", "") or "")
            if not message_id:
                message_id = str(getattr(msg, "id", "") or "")
        return channel_id, message_id


    def _zip_update_restore_record_message_view(self, record: dict[str, object]) -> discord.ui.LayoutView:
        title = self._zip_update_normalize_title(str(record.get("title") or "Update aplicado"), str(record.get("status") or "success"))
        description = str(record.get("description") or "")
        status = str(record.get("status") or "success")
        presentation = record.get("presentation") if isinstance(record.get("presentation"), dict) else None
        return self._make_zip_update_view(
            title,
            description,
            self._zip_update_status_color(status),
            control=self._zip_update_control_for_record(record),
            presentation=presentation,
            info_token=str(record.get("token") or ""),
        )
