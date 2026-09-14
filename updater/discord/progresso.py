from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone

import discord


class ProgressoUpdaterMixin:
    def handle_internal_update_action(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        if action == "zip_status":
            future = asyncio.run_coroutine_threadsafe(self._edit_zip_status_from_update(payload), self.loop)
            return future.result(timeout=15)
        if action == "create_zip_status":
            future = asyncio.run_coroutine_threadsafe(self._create_zip_status_from_update(payload), self.loop)
            return future.result(timeout=15)
        if action == "flush_logs":
            future = asyncio.run_coroutine_threadsafe(self._zip_update_flush_raw_logs_once(), self.loop)
            return future.result(timeout=20)
        if action != "reload_cogs":
            return {"ok": False, "error": "ação interna desconhecida"}
        modules_raw = payload.get("modules") if isinstance(payload, dict) else None
        if not isinstance(modules_raw, list):
            return {"ok": False, "error": "lista de módulos ausente"}
        modules = [str(item).strip() for item in modules_raw if str(item).strip()]
        if not modules:
            return {"ok": False, "error": "nenhuma cog para recarregar"}
        check_app_commands = bool(payload.get("check_app_commands")) if isinstance(payload, dict) else False
        future = asyncio.run_coroutine_threadsafe(
            self._reload_cogs_for_update(modules, check_app_commands=check_app_commands),
            self.loop,
        )
        return future.result(timeout=90 if check_app_commands else 25)


    async def _create_zip_status_from_update(self, payload: dict[str, object]) -> dict[str, object]:
        title = str(payload.get("title") or "Atualização").strip() or "Atualização"
        description = str(payload.get("description") or "").strip()
        status = str(payload.get("status") or "info").lower().strip()
        title = self._zip_update_normalize_title(title, status)
        color = self._zip_update_status_color(status)
        try:
            channel = self.get_channel(self.ZIP_UPDATE_CHANNEL_ID)
            if channel is None:
                channel = await self.fetch_channel(self.ZIP_UPDATE_CHANNEL_ID)
            if channel is None or not hasattr(channel, "send"):
                return {"ok": False, "error": "canal de update indisponível"}
            view = self._make_zip_update_view(title, description, color)
            msg = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
            return {"ok": True, "channel_id": str(getattr(channel, "id", self.ZIP_UPDATE_CHANNEL_ID)), "message_id": str(msg.id)}
        except Exception as exc:
            logging.getLogger("zip_update").warning("falha ao criar mensagem de update pelo updater", exc_info=True)
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


    def _zip_update_progress_should_render(
        self,
        channel_id: int,
        message_id: int,
        presentation: dict[str, object] | None,
    ) -> tuple[bool, str]:
        if not isinstance(presentation, dict):
            return True, ""
        kind = str(presentation.get("kind") or "").strip().lower()
        if kind not in {"progress", "recovery"}:
            return True, ""
        key = (int(channel_id), int(message_id))
        now = time.monotonic()
        signature_payload = {
            "kind": kind,
            "macro_index": presentation.get("macro_index"),
            "recovery_step": presentation.get("recovery_step"),
            "stage": str(presentation.get("stage") or ""),
            "detail": str(presentation.get("detail") or ""),
            "action": str(presentation.get("action") or ""),
            "failure_code": str(presentation.get("failure_code") or ""),
        }
        signature = json.dumps(signature_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        previous = self._zip_update_progress_render_state.get(key)
        if isinstance(previous, dict):
            previous_kind = str(previous.get("kind") or "")
            previous_macro = previous.get("macro_index")
            previous_step = previous.get("recovery_step")
            try:
                age = now - float(previous.get("rendered_at") or 0.0)
            except (TypeError, ValueError):
                age = 999.0
            if str(previous.get("signature") or "") == signature and age < 8.0:
                return False, "microetapa idêntica"
            if previous_kind == kind == "progress":
                try:
                    previous_macro_int = int(previous_macro)
                    incoming_macro_int = int(presentation.get("macro_index"))
                except (TypeError, ValueError):
                    previous_macro_int = incoming_macro_int = -1
                if previous_macro_int >= 0 and incoming_macro_int >= 0 and incoming_macro_int < previous_macro_int:
                    return False, "macroetapa regressiva"
            if previous_kind == kind == "recovery":
                try:
                    previous_step_int = int(previous_step)
                    incoming_step_int = int(presentation.get("recovery_step"))
                except (TypeError, ValueError):
                    previous_step_int = incoming_step_int = -1
                if previous_step_int >= 0 and incoming_step_int >= 0 and incoming_step_int < previous_step_int:
                    return False, "etapa de recuperação regressiva"
            same_phase = (
                previous_kind == kind
                and previous_macro == presentation.get("macro_index")
                and previous_step == presentation.get("recovery_step")
            )
            # Microetapas muito rápidas na mesma macrofase são intencionalmente
            # coalescidas. Mudança de macrofase/recovery sempre é imediata.
            if kind == "progress" and same_phase and age < 1.0:
                return False, "microetapa coalescida"
        return True, signature


    def _zip_update_progress_mark_rendered(
        self,
        channel_id: int,
        message_id: int,
        presentation: dict[str, object] | None,
        signature: str,
    ) -> None:
        if not isinstance(presentation, dict):
            return
        kind = str(presentation.get("kind") or "").strip().lower()
        if kind not in {"progress", "recovery"}:
            self._zip_update_progress_render_state.pop((int(channel_id), int(message_id)), None)
            return
        if not signature:
            return
        self._zip_update_progress_render_state[(int(channel_id), int(message_id))] = {
            "signature": signature,
            "kind": kind,
            "macro_index": presentation.get("macro_index"),
            "recovery_step": presentation.get("recovery_step"),
            "rendered_at": time.monotonic(),
        }


    async def _edit_zip_status_from_update(self, payload: dict[str, object]) -> dict[str, object]:
        try:
            channel_id = int(str(payload.get("channel_id") or "0"))
            message_id = int(str(payload.get("message_id") or "0"))
        except Exception:
            return {"ok": False, "error": "channel_id/message_id inválido"}
        if not channel_id or not message_id:
            return {"ok": False, "error": "channel_id/message_id ausente"}

        title = str(payload.get("title") or "Atualização").strip() or "Atualização"
        description = str(payload.get("description") or "").strip()
        status = str(payload.get("status") or "info").lower().strip()
        title = self._zip_update_normalize_title(title, status)
        color = self._zip_update_status_color(status)
        presentation_raw = payload.get("ui") if isinstance(payload, dict) else None
        presentation = dict(presentation_raw) if isinstance(presentation_raw, dict) else None
        should_render, progress_signature = self._zip_update_progress_should_render(channel_id, message_id, presentation)
        if not should_render:
            return {"ok": True, "ignored": "progresso coalescido"}
        control_raw = payload.get("control") if isinstance(payload, dict) else None
        control: dict[str, object] | None = None
        state_record: dict[str, object] | None = None
        old_state = self._zip_update_state_load()
        previous = old_state.get("latest") if isinstance(old_state.get("latest"), dict) else None

        def parse_event_at(value: object) -> datetime | None:
            text = str(value or "").strip()
            if not text:
                return None
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)

        now_event = datetime.now(timezone.utc)
        incoming_event = parse_event_at(payload.get("event_at")) or now_event
        previous_event = parse_event_at(previous.get("event_at") or previous.get("updated_at")) if isinstance(previous, dict) else None
        # Eventos com o mesmo timestamp são retries idempotentes. Eles precisam
        # tentar editar novamente caso a chamada anterior tenha falhado depois de
        # preparar o estado. Somente eventos estritamente mais antigos são stale.
        stale_event = bool(previous_event and incoming_event < previous_event)
        same_message = bool(
            isinstance(previous, dict)
            and str(previous.get("channel_id") or "") == str(channel_id)
            and str(previous.get("message_id") or "") == str(message_id)
        )
        if stale_event and same_message:
            return {"ok": True, "ignored": "estado final antigo"}

        if isinstance(control_raw, dict) and control_raw.get("enabled"):
            mode = str(control_raw.get("mode") or "rollback").lower().strip()
            if mode == "cancel":
                candidate_id = str(control_raw.get("candidate_id") or payload.get("candidate_id") or "").strip()
                control = self._zip_update_cancel_control(candidate_id, disabled=bool(control_raw.get("disabled")))
            else:
                token = str(control_raw.get("token") or uuid.uuid4().hex[:16])[:24]
                if mode not in {"rollback", "redo"}:
                    mode = "rollback"
                state_record = {
                    "token": token,
                    "mode": mode,
                    "channel_id": str(channel_id),
                    "message_id": str(message_id),
                    "title": title,
                    "description": description,
                    "status": status,
                    "branch": str(control_raw.get("branch") or payload.get("branch") or "main"),
                    "expected_head": str(control_raw.get("expected_head") or control_raw.get("head_commit") or ""),
                    "revert_commit": str(control_raw.get("revert_commit") or control_raw.get("head_commit") or ""),
                    "source_author_id": str(control_raw.get("source_author_id") or ""),
                    "event_at": incoming_event.isoformat(),
                    "updated_at": now_event.isoformat(),
                }
                for key in ("update_from", "update_to", "rollback_commit", "redo_commit"):
                    if control_raw.get(key):
                        state_record[key] = str(control_raw.get(key))
                if presentation is not None:
                    state_record["presentation"] = presentation
                control = self._zip_update_control_for_record(state_record, disabled=stale_event)

        # Detalhes/Arquivos pertencem à mensagem final, não ao botão de rollback.
        # Mesmo quando não há reversão disponível, persista um token somente de
        # leitura para que os botões informativos sobrevivam a restart do bot.
        if (
            state_record is None
            and isinstance(presentation, dict)
            and str(presentation.get("kind") or "").strip().lower() == "final"
            and not stale_event
        ):
            state_record = {
                "token": uuid.uuid4().hex[:16],
                "mode": "info",
                "channel_id": str(channel_id),
                "message_id": str(message_id),
                "title": title,
                "description": description,
                "status": status,
                "branch": str(presentation.get("branch") or payload.get("branch") or "main"),
                "event_at": incoming_event.isoformat(),
                "updated_at": now_event.isoformat(),
                "presentation": presentation,
            }

        clear_previous_control = bool(payload.get("clear_previous_control"))
        preserve_existing_control = bool(payload.get("preserve_existing_control"))
        final_without_control = bool(
            status in {"success", "ok", "warn", "error", "done", "failed"}
            and state_record is None
            and not stale_event
            and clear_previous_control
            and not preserve_existing_control
        )

        try:
            status_message = await self._zip_update_fetch_message(channel_id, message_id)
            if status_message is None:
                return {"ok": False, "error": "mensagem não encontrada"}
            info_token = str(state_record.get("token") or "") if isinstance(state_record, dict) else ""
            view = self._make_zip_update_view(
                title,
                description,
                color,
                control=control,
                presentation=presentation,
                info_token=info_token,
            )
            await status_message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
            self._zip_update_progress_mark_rendered(channel_id, message_id, presentation, progress_signature)
            # O estado persistente só pode avançar depois que o Discord confirmou
            # a edição. A ordem antiga salvava antes do await; uma falha tornava o
            # retry "antigo" e o outbox removia a notificação sem nunca publicá-la.
            if state_record is not None and not stale_event:
                await self._zip_update_clear_previous_control(previous, keep_message_id=message_id)
                state_record["delivery_state"] = "delivered"
                state_record["delivered_at"] = datetime.now(timezone.utc).isoformat()
                if not self._zip_update_state_save({"latest": state_record}):
                    # Não deixe um botão ativo que o bot não conseguirá resolver
                    # depois de um restart. A confirmação final continua visível,
                    # porém sem oferecer uma ação de rollback inconsistente.
                    await status_message.edit(
                        view=self._make_zip_update_view(title, description, color, presentation=presentation),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return {
                        "ok": True,
                        "delivered": True,
                        "warning": "controle de rollback indisponível",
                    }
            elif final_without_control:
                # Um estado finalizado sem controle explícito deixa controles
                # antigos inativos, mas somente após a nova mensagem existir.
                await self._zip_update_clear_previous_control(previous, keep_message_id=message_id)
                self._zip_update_state_save({})
            return {"ok": True, "delivered": True}
        except Exception:
            logging.getLogger("zip_update").warning("falha ao finalizar mensagem do ZIP pelo updater", exc_info=True)
            return {"ok": False, "error": "falha ao editar mensagem"}
