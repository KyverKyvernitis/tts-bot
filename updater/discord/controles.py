from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import discord

from .constantes import UPDATE_EMOJI_DATABASE, UPDATE_EMOJI_FILES, UPDATE_LOG


class ControlesUpdaterMixin:
    def _zip_update_find_candidate_sync(self, candidate_id: str) -> dict[str, object]:
        candidate_id = str(candidate_id or "").strip()
        queue_root = self._update_staging_root / "candidates" / "queue"
        for state in ("pending", "active"):
            state_dir = queue_root / state
            for queue_file in sorted(state_dir.glob("*.json")) if state_dir.is_dir() else []:
                try:
                    payload = json.loads(queue_file.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if str(payload.get("id") or "") != candidate_id:
                    continue
                candidate_dir = Path(str(payload.get("candidate_dir") or ""))
                manifest: dict[str, object] = {}
                try:
                    raw = json.loads((candidate_dir / "manifest.json").read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        manifest = raw
                except Exception:
                    pass
                return {
                    "state": state,
                    "queue_file": str(queue_file),
                    "candidate_dir": str(candidate_dir),
                    "manifest": manifest,
                }
        return {"state": "missing"}


    def _zip_update_confirm_candidate_sync(
        self, candidate_id: str, confirmed_by: str = ""
    ) -> dict[str, object]:
        found = self._zip_update_find_candidate_sync(candidate_id)
        if found.get("state") != "pending":
            return found
        queue_file = Path(str(found.get("queue_file") or ""))
        try:
            payload = json.loads(queue_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                payload = {}
        except Exception as exc:
            return {"state": "error", "error": f"{type(exc).__name__}: {exc}"}

        if not bool(payload.get("confirmation_required")):
            found["confirmation_status"] = "not_required"
            return found
        if str(payload.get("confirmed_at") or "").strip():
            found["confirmation_status"] = "already_confirmed"
            return found

        now = datetime.now(timezone.utc)
        payload.update(
            {
                "state": "queued",
                "confirmed_at": now.isoformat(),
                "confirmed_by": str(confirmed_by or "")[:80],
                "heartbeat_at": now.isoformat(),
            }
        )
        tmp = queue_file.with_name(f".{queue_file.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, queue_file)
        except FileNotFoundError:
            with contextlib.suppress(Exception):
                tmp.unlink(missing_ok=True)
            return self._zip_update_find_candidate_sync(candidate_id)
        except OSError as exc:
            with contextlib.suppress(Exception):
                tmp.unlink(missing_ok=True)
            return {"state": "error", "error": f"{type(exc).__name__}: {exc}"}

        found["confirmation_status"] = "confirmed"
        found["confirmed_at"] = payload["confirmed_at"]
        found["confirmed_by"] = payload["confirmed_by"]
        return found


    def _zip_update_cancel_candidate_sync(self, candidate_id: str) -> dict[str, object]:
        found = self._zip_update_find_candidate_sync(candidate_id)
        if found.get("state") != "pending":
            return found
        queue_file = Path(str(found.get("queue_file") or ""))
        candidate_dir = Path(str(found.get("candidate_dir") or ""))
        queue_cancelled = queue_file.parent.parent / "cancelled"
        candidate_cancelled = self._update_staging_root / "candidates" / "cancelled"
        queue_cancelled.mkdir(parents=True, exist_ok=True)
        candidate_cancelled.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%d%H%M%S")
        destination = candidate_cancelled / f"{candidate_dir.name}.{stamp}"
        cancelled_queue = queue_cancelled / f"{queue_file.name}.cancelled.{stamp}"

        try:
            queue_payload = json.loads(queue_file.read_text(encoding="utf-8"))
            if not isinstance(queue_payload, dict):
                queue_payload = {}
        except Exception:
            queue_payload = {}
        queue_payload.update(
            {
                "state": "cancelled",
                "cancelled_at": now.isoformat(),
                "archived_candidate_dir": str(destination),
            }
        )
        tmp_queue = queue_file.with_name(f".{queue_file.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp_queue.write_text(json.dumps(queue_payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp_queue, queue_file)
            os.replace(queue_file, cancelled_queue)
        except FileNotFoundError:
            with contextlib.suppress(Exception):
                tmp_queue.unlink(missing_ok=True)
            return self._zip_update_find_candidate_sync(candidate_id)
        except OSError as exc:
            with contextlib.suppress(Exception):
                tmp_queue.unlink(missing_ok=True)
            return {"state": "error", "error": f"{type(exc).__name__}: {exc}"}

        if candidate_dir.is_dir():
            try:
                state_path = candidate_dir / "state.json"
                state_tmp = candidate_dir / f".state.json.{uuid.uuid4().hex}.tmp"
                state_tmp.write_text(
                    json.dumps({"state": "cancelled", "updated_at": now.isoformat()}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(state_tmp, state_path)
                os.replace(candidate_dir, destination)
                found["candidate_dir"] = str(destination)
            except OSError:
                shutil.rmtree(candidate_dir, ignore_errors=True)
        found["state"] = "cancelled"
        found["queue_file"] = str(cancelled_queue)
        return found


    def _make_zip_update_info_view(self, title: str, body: str) -> discord.ui.LayoutView:
        text = f"# {title}\n{body.strip()}".strip()
        if len(text) > 3900:
            text = text[:3897].rstrip() + "..."
        view = discord.ui.LayoutView(timeout=120)
        view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_color=discord.Color.blurple()))
        return view


    async def _on_zip_update_info_click(self, interaction: discord.Interaction) -> None:
        data = getattr(interaction, "data", None)
        custom_id = str(data.get("custom_id") or "") if isinstance(data, dict) else ""
        parts = custom_id.split(":", 2)
        if len(parts) != 3 or parts[0] != "zip_update_info":
            return
        kind, token = parts[1], parts[2]
        state = self._zip_update_state_load()
        record = state.get("latest") if isinstance(state.get("latest"), dict) else None
        if not isinstance(record, dict) or str(record.get("token") or "") != token:
            await interaction.response.send_message("Os detalhes desta atualização não estão mais disponíveis.", ephemeral=True)
            return
        presentation = record.get("presentation") if isinstance(record.get("presentation"), dict) else {}
        if kind == "files":
            files_text = str(presentation.get("files_text") or "Nenhum arquivo listado.").strip()
            count = str(presentation.get("file_count_text") or "Arquivos alterados").strip()
            view = self._make_zip_update_info_view(f"{UPDATE_EMOJI_FILES} {count}", files_text)
        else:
            lines: list[str] = []
            display_id = str(presentation.get("display_id") or "").strip()
            branch = str(presentation.get("branch") or record.get("branch") or "main").strip()
            old_commit = str(presentation.get("from") or record.get("update_from") or "").strip()
            new_commit = str(presentation.get("to") or record.get("update_to") or "").strip()
            if display_id:
                lines.append(f"`{display_id}` · branch `{branch}`")
            if old_commit or new_commit:
                lines.append(f"`{old_commit or '?'}` → `{new_commit or '?'}`")
            checks = str(presentation.get("checks_text") or "").strip()
            if checks:
                lines.extend(["", "**Verificações**", checks])
            cache = str(presentation.get("cache_text") or "").strip()
            if cache and "0 hit/0 miss" not in cache:
                lines.extend(["", "**Cache**", cache])
            tests = str(presentation.get("tests_text") or "").strip()
            if tests and "não executado" not in tests:
                lines.extend(["", "**Testes**", tests])
            timings = str(presentation.get("timings_text") or "").strip()
            if timings:
                timing_labels = {
                    "receive_to_updater": "Recebido → updater",
                    "fetch": "Git fetch",
                    "candidate_apply": "Aplicação isolada",
                    "candidate_promote": "Promoção",
                    "preflight": "Preflight",
                    "frontend": "Frontend",
                    "backend": "Backend",
                    "bot": "Bot",
                    "worker": "Worker",
                    "commit": "Commit",
                    "push": "GitHub",
                    "execution": "Execução updater",
                    "total": "Total desde envio",
                }
                pretty_rows: list[str] = []
                for piece in (part.strip() for part in timings.split(",") if part.strip()):
                    key, sep, value = piece.partition("=")
                    label = timing_labels.get(key.strip(), key.strip().replace("_", " ").title())
                    pretty_rows.append(f"{label:<18} {value.strip()}" if sep else piece)
                lines.extend(["", "**Tempos**", "```text\n" + "\n".join(pretty_rows) + "\n```"])
            processes = str(presentation.get("processes") or "").strip()
            if processes and processes.casefold() not in {"nenhum", "nenhum processo alterado"}:
                lines.extend(["", "**Processos**", processes])
            view = self._make_zip_update_info_view(f"{UPDATE_EMOJI_DATABASE} Detalhes", "\n".join(lines) or "Sem detalhes adicionais.")
        await interaction.response.send_message(view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


    async def _on_zip_update_control_click(self, interaction: discord.Interaction) -> None:
        data = getattr(interaction, "data", None)
        custom_id = str(data.get("custom_id") or "") if isinstance(data, dict) else ""
        if not custom_id.startswith("zip_update:"):
            return
        parts = custom_id.split(":", 2)
        if len(parts) != 3:
            return
        requested_mode, token = parts[1], parts[2]

        if requested_mode == "approve":
            found = await asyncio.to_thread(self._zip_update_find_candidate_sync, token)
            manifest = found.get("manifest") if isinstance(found.get("manifest"), dict) else {}
            if found.get("state") == "active":
                await interaction.response.send_message(
                    "Essa atualização já foi confirmada e iniciada.", ephemeral=True
                )
                return
            if found.get("state") != "pending":
                await interaction.response.send_message(
                    "Essa atualização não está mais aguardando confirmação.", ephemeral=True
                )
                return
            if not self._zip_update_can_control(interaction, manifest):
                await interaction.response.send_message(
                    "Você não pode confirmar esta atualização.", ephemeral=True
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=False)
            confirmed = await asyncio.to_thread(
                self._zip_update_confirm_candidate_sync,
                token,
                str(getattr(interaction.user, "id", "") or ""),
            )
            status = str(confirmed.get("confirmation_status") or "")
            if status == "already_confirmed":
                await interaction.followup.send("Essa atualização já foi confirmada.", ephemeral=True)
                return
            if status != "confirmed":
                detail = str(confirmed.get("error") or "o candidato mudou de estado")
                await interaction.followup.send(
                    f"Não consegui confirmar a atualização: {detail}", ephemeral=True
                )
                return

            display_id = str(manifest.get("display_id") or token)
            file_count = len(manifest.get("changed_files") or [])
            file_text = "1 arquivo" if file_count == 1 else f"{file_count} arquivos"
            try:
                if interaction.message is not None:
                    await interaction.message.edit(
                        view=self._make_zip_update_view(
                            "✅ Atualização confirmada",
                            f"`{display_id}` · **{file_text}**\nO mesmo candidato foi liberado e o updater será iniciado agora.",
                            discord.Color.green(),
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except Exception:
                UPDATE_LOG.warning("falha ao atualizar mensagem após confirmação de segurança", exc_info=True)

            triggered, detail = await self._dispatch_updater_candidate(token, display_id)
            UPDATE_LOG.info(
                "confirmação humana liberou candidato %s: dispatch=%s detalhe=%s",
                display_id,
                triggered,
                detail,
            )
            await interaction.followup.send(
                "Atualização confirmada e liberada para o updater.", ephemeral=True
            )
            return

        if requested_mode == "cancel":
            found = await asyncio.to_thread(self._zip_update_find_candidate_sync, token)
            manifest = found.get("manifest") if isinstance(found.get("manifest"), dict) else {}
            if found.get("state") == "active":
                await interaction.response.send_message(
                    "Essa atualização já começou e não pode mais ser cancelada.", ephemeral=True
                )
                return
            if found.get("state") != "pending":
                await interaction.response.send_message(
                    "Essa atualização não está mais aguardando na fila.", ephemeral=True
                )
                return
            if not self._zip_update_can_control(interaction, manifest):
                await interaction.response.send_message("Você não pode cancelar esta atualização.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=False)
            cancelled = await asyncio.to_thread(self._zip_update_cancel_candidate_sync, token)
            if cancelled.get("state") != "cancelled":
                await interaction.followup.send(
                    "A atualização já foi assumida pelo updater e não pôde ser cancelada.", ephemeral=True
                )
                return
            display_id = str(manifest.get("display_id") or token)
            try:
                if interaction.message is not None:
                    await interaction.message.edit(
                        view=self._make_zip_update_view(
                            "✖️ Atualização cancelada",
                            f"`{display_id}`\nO pacote foi removido da fila antes de alterar a VPS.",
                            discord.Color.dark_grey(),
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except Exception:
                UPDATE_LOG.warning("falha ao atualizar mensagem de candidato cancelado", exc_info=True)
            await asyncio.to_thread(self._trigger_updater_service_sync)
            await interaction.followup.send("Atualização cancelada.", ephemeral=True)
            return

        if requested_mode not in {"rollback", "redo"}:
            return
        state = self._zip_update_state_load()
        record = state.get("latest") if isinstance(state.get("latest"), dict) else None
        if not isinstance(record, dict) or str(record.get("token") or "") != token:
            await interaction.response.send_message("Esse controle não está mais disponível.", ephemeral=True)
            return
        if str(record.get("mode") or "") != requested_mode:
            await interaction.response.send_message("Esse controle não está mais disponível.", ephemeral=True)
            return
        if not self._zip_update_can_control(interaction, record):
            await interaction.response.send_message("Você não pode controlar esta atualização.", ephemeral=True)
            return

        if requested_mode == "redo":
            current_commit = str(record.get("rollback_commit") or record.get("expected_head") or "")
            target_commit = str(record.get("update_to") or record.get("redo_commit") or "")
        else:
            current_commit = str(record.get("expected_head") or record.get("update_to") or "")
            target_commit = str(record.get("update_from") or "")
        await interaction.response.send_message(
            view=self._make_zip_update_confirmation_view(
                mode=requested_mode,
                token=token,
                current_commit=current_commit,
                target_commit=target_commit,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


    async def _on_zip_update_confirmation_click(self, interaction: discord.Interaction) -> None:
        data = getattr(interaction, "data", None)
        custom_id = str(data.get("custom_id") or "") if isinstance(data, dict) else ""
        parts = custom_id.split(":", 2)
        if len(parts) != 3 or parts[0] != "zip_update_confirm":
            return
        requested_mode, token = parts[1], parts[2]
        if requested_mode == "cancel":
            await interaction.response.send_message("Ação cancelada.", ephemeral=True)
            return
        if requested_mode not in {"rollback", "redo"}:
            return
        state = self._zip_update_state_load()
        record = state.get("latest") if isinstance(state.get("latest"), dict) else None
        if not isinstance(record, dict) or str(record.get("token") or "") != token:
            await interaction.response.send_message("Esse controle não está mais disponível.", ephemeral=True)
            return
        if str(record.get("mode") or "") != requested_mode or not self._zip_update_can_control(interaction, record):
            await interaction.response.send_message("Você não pode executar esta ação.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=False)
        await self._start_zip_update_rollback_flow(interaction, record, requested_mode)


    async def _start_zip_update_rollback_flow(self, interaction: discord.Interaction, record: dict[str, object], mode: str) -> None:
        mode = "redo" if str(mode).lower().strip() == "redo" else "rollback"
        branch = str(record.get("branch") or "main")
        channel_id, message_id = self._zip_update_latest_message_ref(interaction, record)
        update_from = str(record.get("update_from") or "").strip()
        update_to = str(record.get("update_to") or "").strip()
        rollback_commit = str(record.get("rollback_commit") or "").strip()
        redo_commit = str(record.get("redo_commit") or "").strip()
        expected_head = str(record.get("expected_head") or "").strip()
        revert_commit = str(record.get("revert_commit") or "").strip()

        # O controle usa `git revert`, então o alvo técnico da ação é o commit
        # que precisa ser revertido, não o commit para onde queremos "voltar".
        # Para ◀️, o HEAD esperado é o update aplicado e o commit revertido é
        # esse próprio update. Para ▶️, o HEAD esperado é o commit de rollback e
        # o commit revertido é o rollback. Mantemos fallback explícito para
        # estados antigos/legados já salvos em disco.
        if mode == "rollback":
            # O commit técnico a reverter é sempre o HEAD atual salvo no controle.
            # `update_to` é mantido como metadado do update original e pode ficar
            # diferente depois de uma reaplicação; por isso ele só serve como
            # fallback para estados antigos que ainda não gravavam expected_head.
            expected_head = expected_head or revert_commit or update_to
            revert_commit = revert_commit or expected_head or update_to
        else:
            # Para refazer, revertemos o commit de rollback. Estados novos salvam
            # `rollback_commit`; em estados legados, `revert_commit`/expected_head
            # apontam para o commit atual que precisa ser revertido.
            expected_head = rollback_commit or expected_head or revert_commit or update_to
            revert_commit = rollback_commit or revert_commit or redo_commit or expected_head

        commit_re = re.compile(r"^[0-9a-fA-F]{7,40}$")
        if expected_head and not commit_re.fullmatch(expected_head):
            UPDATE_LOG.warning("rollback/redo com expected_head inválido: %r", expected_head)
            expected_head = ""
        if revert_commit and not commit_re.fullmatch(revert_commit):
            UPDATE_LOG.warning("rollback/redo com revert_commit inválido: %r", revert_commit)
            revert_commit = ""

        if not expected_head or not revert_commit or not channel_id or not message_id:
            try:
                await interaction.followup.send("Não encontrei os dados necessários para essa ação.", ephemeral=True)
            except Exception:
                pass
            return

        if self._zip_update_has_pending_rollback_request():
            try:
                await interaction.followup.send("Já existe uma ação de update em andamento.", ephemeral=True)
            except Exception:
                pass
            return

        request_root, permission_details = self._zip_update_find_writable_rollback_request_root()
        if request_root is None:
            detail = "; ".join(permission_details[-3:]) or "nenhuma pasta gravável encontrada"
            UPDATE_LOG.warning("falha ao preparar pasta de pedido de rollback/redo: %s", detail)
            action_name = "reaplicação" if mode == "redo" else "reversão"
            try:
                await interaction.followup.send(f"Não consegui iniciar a {action_name}: pasta de controle sem permissão de escrita.", ephemeral=True)
            except Exception:
                pass
            return

        pending = request_root / "pending.json"
        active = request_root / "active.json"

        request_id = f"{mode}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        payload = {
            "id": request_id,
            "mode": mode,
            "branch": branch,
            "expected_head": expected_head,
            "revert_commit": revert_commit,
            "update_from": update_from,
            "update_to": update_to,
            "rollback_commit": rollback_commit,
            "redo_commit": redo_commit,
            "message": {"channel_id": channel_id, "message_id": message_id},
            "source_author_id": str(record.get("source_author_id") or ""),
            "requested_by": str(getattr(interaction.user, "id", "") or ""),
            "previous_record": record,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        tmp = pending.with_name(f"pending.{request_id}.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, pending)
            # Confirma que o pedido existe antes de mexer visualmente na mensagem.
            saved = json.loads(pending.read_text(encoding="utf-8"))
            if str(saved.get("id") or "") != request_id:
                raise RuntimeError("pedido de rollback não foi salvo corretamente")
        except Exception:
            with contextlib.suppress(Exception):
                tmp.unlink()
            UPDATE_LOG.warning("falha ao salvar pedido de rollback/redo em %s", request_root, exc_info=True)
            action_name = "reaplicação" if mode == "redo" else "reversão"
            try:
                await interaction.followup.send(f"Não consegui iniciar a {action_name}: não foi possível gravar o pedido de controle.", ephemeral=True)
            except Exception:
                pass
            return

        title = "<a:areia:1496606578395189473> Reaplicando update..." if mode == "redo" else "<a:areia:1496606578395189473> Revertendo update..."
        description = "<a:loading:1510065277868445796> **Preparando validação local**"
        processing_record = dict(record)
        processing_record["status"] = "processing"
        processing_record["processing_mode"] = mode
        processing_record["processing_request_id"] = request_id
        processing_record["updated_at"] = datetime.now(timezone.utc).isoformat()
        state = self._zip_update_state_load()
        state["latest"] = processing_record
        state["active_request"] = payload
        self._zip_update_state_save(state)

        try:
            msg = await self._zip_update_fetch_message(int(channel_id), int(message_id))
            if msg is not None:
                view = self._make_zip_update_view(
                    title,
                    description,
                    discord.Color.gold(),
                    control=self._zip_update_control_for_record(record, disabled=True),
                )
                await msg.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            UPDATE_LOG.warning("falha ao colocar update em estado de processamento", exc_info=True)

        action_label = "reaplicação" if mode == "redo" else "rollback"
        triggered, detail = await self._dispatch_updater_control_request(
            pending, active, request_id, action_label
        )
        if not triggered:
            UPDATE_LOG.warning("%s aguardando fallback do updater: %s", action_label, detail)

        async def _rollback_start_watchdog() -> None:
            await asyncio.sleep(90)
            try:
                # Se o pedido ainda existir igual após um tempo razoável, o updater não consumiu.
                for path in (pending, active):
                    if not path.exists():
                        continue
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if str(data.get("id") or "") != request_id:
                        continue
                    msg = await self._zip_update_fetch_message(int(channel_id), int(message_id))
                    if msg is not None:
                        fail_title = "Falha ao reaplicar" if mode == "redo" else "Falha ao reverter"
                        fail_desc = "A ação não avançou. O estado atual foi mantido."
                        await msg.edit(
                            view=self._make_zip_update_view(
                                fail_title,
                                fail_desc,
                                discord.Color.red(),
                                control=self._zip_update_control_for_record(record),
                            ),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    # Restaura o estado para permitir nova tentativa manual, mas não apaga o arquivo;
                    # o updater ainda poderá arquivar/limpar no próximo ciclo se necessário.
                    state = self._zip_update_state_load()
                    state.pop("active_request", None)
                    state["latest"] = record
                    self._zip_update_state_save(state)
                    return
            except Exception:
                UPDATE_LOG.warning("falha no watchdog de rollback/redo", exc_info=True)

        asyncio.create_task(_rollback_start_watchdog())
