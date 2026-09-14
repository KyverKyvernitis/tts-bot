from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import config
from updater.utilitarios.seguranca import (
    UPDATE_CONTROL_MANIFEST_NAME, UpdateSecurityError, build_file_integrity,
    canonical_path_key, inspect_zip_archive, is_forbidden_update_path,
    normalize_update_operations, sha256_file, update_operation_paths,
    update_payload_paths,
)
from .constantes import UPDATE_LOG


def _cfg(*names: str, default=None):
    for name in names:
        if hasattr(config, name):
            return getattr(config, name)
    return default


class PreparacaoUpdaterMixin:
    def _git_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("HOME", "/home/ubuntu")
        key_path = Path("/home/ubuntu/.ssh/id_ed25519")
        if key_path.is_file():
            env.setdefault("GIT_SSH_COMMAND", f"ssh -i {key_path} -o IdentitiesOnly=yes")
        return env


    def _run_cmd(self, args: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=str(cwd),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )


    def _normalize_zip_member_parts(self, raw_name: str) -> tuple[str, ...]:
        posix = PurePosixPath(raw_name.replace("\\", "/"))
        return tuple(part for part in posix.parts if part not in ("", "."))


    def _zip_update_should_ignore_generated_file(self, rel_path: Path) -> bool:
        """Ignora lixo de build que não deve virar commit pelo auto updater."""
        parts = tuple(str(part) for part in rel_path.parts)
        name = parts[-1] if parts else ""
        if not parts:
            return True
        if any(part in {"__pycache__", ".gradle", ".idea"} for part in parts):
            return True
        if name.endswith((".pyc", ".pyo", ".tmp")):
            return True
        if name.startswith("build.gradle.bak") or name.endswith(".bak-sdk35"):
            return True
        core_worker_prefix = ("android", "core-worker-app")
        if parts[:2] == core_worker_prefix:
            if len(parts) >= 4 and parts[2] == "app" and parts[3] == "build":
                return True
            if len(parts) >= 3 and parts[2] == "releases":
                return True
        return False


    def _zip_update_is_forbidden_path(self, rel_path: Path | str) -> bool:
        return is_forbidden_update_path(rel_path)


    def _prepare_update_staging_clone(self, origin_url: str, branch_name: str, env: dict[str, str]) -> Path:
        """Mantém um clone staging reutilizável para não clonar o repositório inteiro a cada ZIP."""
        self._update_staging_root.mkdir(parents=True, exist_ok=True)
        clone_dir = self._update_staging_root / "repo"

        def clone_fresh() -> Path:
            shutil.rmtree(clone_dir, ignore_errors=True)
            clone_result = self._run_cmd(["git", "clone", "--branch", branch_name, "--single-branch", origin_url, str(clone_dir)], self._update_staging_root, env=env)
            if clone_result.returncode != 0:
                err = (clone_result.stderr or clone_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao preparar staging persistente do update. {err}")
            return clone_dir

        if not (clone_dir / ".git").exists():
            return clone_fresh()

        # Se o staging antigo ficou corrompido, recria do zero. Caso contrário,
        # reset/clean é muito mais rápido do que um clone completo.
        for args in (
            ["git", "remote", "set-url", "origin", origin_url],
            ["git", "fetch", "--prune", "origin", branch_name],
            ["git", "checkout", "-B", branch_name, f"origin/{branch_name}"],
            ["git", "reset", "--hard", f"origin/{branch_name}"],
            ["git", "clean", "-fdx"],
        ):
            result = self._run_cmd(args, clone_dir, env=env)
            if result.returncode != 0:
                logging.getLogger("zip_update").warning("staging persistente falhou em %s; recriando clone", " ".join(args))
                return clone_fresh()
        return clone_dir


    def _git_diff_numstat_sync(self, repo_dir: Path, env: dict[str, str]) -> dict[str, object]:
        result = self._run_cmd(["git", "diff", "--cached", "--numstat", "--no-renames"], repo_dir, env=env)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Falha ao calcular diff de linhas. {err}")

        entries: list[dict[str, object]] = []
        total_added = 0
        total_removed = 0
        binary_files = 0
        for raw in (result.stdout or "").splitlines():
            parts = raw.split("\t")
            if len(parts) < 3:
                continue
            added_raw, removed_raw, path_raw = parts[0], parts[1], parts[-1]
            is_binary = added_raw == "-" or removed_raw == "-"
            added = 0 if is_binary else int(added_raw or 0)
            removed = 0 if is_binary else int(removed_raw or 0)
            if is_binary:
                binary_files += 1
            else:
                total_added += added
                total_removed += removed
            entries.append({"path": path_raw, "added": added, "removed": removed, "binary": is_binary})

        summary = f"+{total_added} -{total_removed}"
        if binary_files:
            summary += f" · {binary_files} binário(s)"
        return {"entries": entries, "summary": summary, "total_added": total_added, "total_removed": total_removed, "binary_files": binary_files}


    def _write_local_update_candidate_sync(
        self,
        *,
        branch_name: str,
        base_commit: str | None,
        changed_files: list[str],
        operations: list[dict[str, str]],
        diff_stats: dict[str, object],
        extracted_files: list[tuple[Path, Path]],
        zip_name: str,
        zip_inspection: dict[str, object],
        patch_diff_text: str = "",
        status_context: dict[str, object] | None = None,
        preparation_steps: list[dict[str, object]] | None = None,
        preparation_started_monotonic: float | None = None,
        candidate_step_started_monotonic: float | None = None,
        progress_started_epoch_ms: int | None = None,
    ) -> dict[str, object]:
        """Grava um candidato íntegro para validação e aplicação na VPS."""
        candidate_root = self._update_staging_root / "candidates"
        candidate_root.mkdir(parents=True, exist_ok=True)
        unique = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc)
        candidate_id = f"zip-{created_at.strftime('%Y%m%d%H%M%S')}-{unique[:8]}"
        display_id = f"UPD-{unique[:8].upper()}"
        candidate_dir = candidate_root / candidate_id
        files_dir = candidate_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=False)

        try:
            written_files: list[str] = []
            normalized_operations = normalize_update_operations(operations)
            expected_changed = set(update_operation_paths(normalized_operations))
            expected_payload = set(update_payload_paths(normalized_operations))
            if set(changed_files) != expected_changed:
                raise RuntimeError("Operações do candidato não correspondem ao diff staged.")
            for extracted_path, rel_path in extracted_files:
                rel_posix = rel_path.as_posix()
                if rel_posix not in expected_payload:
                    continue
                if self._zip_update_is_forbidden_path(rel_path):
                    raise RuntimeError(f"Caminho protegido não pode ser alterado: {rel_posix}")
                target = files_dir / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(extracted_path, target)
                written_files.append(rel_posix)

            if set(written_files) != expected_payload:
                missing = sorted(expected_payload - set(written_files))
                raise RuntimeError("Candidato incompleto; arquivo(s) ausente(s): " + ", ".join(missing[:5]))

            patch_path = candidate_dir / "patch.diff"
            if patch_diff_text.strip():
                patch_path.write_text(patch_diff_text, encoding="utf-8")

            max_age_seconds = max(300, int(os.getenv("DISCORD_AUTO_UPDATE_CANDIDATE_MAX_AGE_SECONDS", "86400") or 86400))
            expires_at = created_at + timedelta(seconds=max_age_seconds)
            file_integrity = build_file_integrity(files_dir, written_files)

            prepared_at = time.monotonic()
            candidate_prepare_elapsed_ms = max(
                0,
                int((prepared_at - float(candidate_step_started_monotonic or prepared_at)) * 1000),
            )
            preparation_total_ms = max(
                candidate_prepare_elapsed_ms,
                int((prepared_at - float(preparation_started_monotonic or prepared_at)) * 1000),
            )
            handoff_steps: list[dict[str, object]] = []
            seen_handoff_labels: set[str] = set()
            for raw_step in list(preparation_steps or []):
                if not isinstance(raw_step, dict):
                    continue
                label = " ".join(str(raw_step.get("label") or "").split())[:120]
                if not label or label in seen_handoff_labels:
                    continue
                try:
                    elapsed_ms = max(0, int(raw_step.get("elapsed_ms") or 0))
                except (TypeError, ValueError):
                    elapsed_ms = 0
                seen_handoff_labels.add(label)
                handoff_steps.append({"label": label, "elapsed_ms": elapsed_ms})
            final_preparation_label = "Candidato seguro preparado"
            if final_preparation_label not in seen_handoff_labels:
                handoff_steps.append(
                    {
                        "label": final_preparation_label,
                        "elapsed_ms": candidate_prepare_elapsed_ms,
                    }
                )

            manifest: dict[str, object] = {
                "schema_version": 3,
                "id": candidate_id,
                "display_id": display_id,
                "created_at": created_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "branch": branch_name,
                "base_commit": base_commit,
                "source": "discord_zip",
                "zip_name": zip_name,
                "zip_sha256": str(zip_inspection.get("sha256") or ""),
                "zip_stats": zip_inspection,
                "commit_message": f"update: aplicar {display_id}",
                "changed_files": changed_files,
                "operations": normalized_operations,
                "diff_stats": diff_stats,
                "file_integrity": file_integrity,
                "patch_sha256": sha256_file(patch_path) if patch_path.is_file() else "",
                "patch_size": patch_path.stat().st_size if patch_path.is_file() else 0,
                "progress_handoff": {
                    "schema_version": 1,
                    "completed_steps": handoff_steps,
                    "completed_count": len(handoff_steps),
                    "preparation_total_ms": preparation_total_ms,
                    "started_at_epoch_ms": max(0, int(progress_started_epoch_ms or 0)),
                },
            }
            if isinstance(status_context, dict) and status_context.get("message_id") and status_context.get("channel_id"):
                manifest["discord_status"] = {
                    "guild_id": str(status_context.get("guild_id") or ""),
                    "channel_id": str(status_context.get("channel_id") or ""),
                    "message_id": str(status_context.get("message_id") or ""),
                    "source_message_id": str(status_context.get("source_message_id") or ""),
                    "source_author_id": str(status_context.get("source_author_id") or ""),
                }
            manifest_tmp = candidate_dir / ".manifest.json.tmp"
            manifest_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(manifest_tmp, candidate_dir / "manifest.json")

            queue_root = candidate_root / "queue"
            pending_dir = queue_root / "pending"
            active_dir = queue_root / "active"
            pending_dir.mkdir(parents=True, exist_ok=True)
            active_dir.mkdir(parents=True, exist_ok=True)
            for path in (queue_root, pending_dir, active_dir):
                with contextlib.suppress(Exception):
                    path.chmod(0o775)

            active_count = len([item for item in active_dir.glob("*.json") if item.is_file()])
            pending_count = len([item for item in pending_dir.glob("*.json") if item.is_file()])
            if (candidate_root / "pending.json").exists():
                pending_count += 1
            queue_position = active_count + pending_count + 1

            queue_name = f"{created_at.strftime('%Y%m%d%H%M%S%f')}-{candidate_id}.json"
            pending_path = pending_dir / queue_name
            tmp_pending = pending_dir / f".{queue_name}.tmp"
            queue_payload = {
                "candidate_dir": str(candidate_dir),
                "id": candidate_id,
                "display_id": display_id,
                "state": "queued",
                "attempt": 0,
                "created_at": created_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "queue_position_at_enqueue": queue_position,
            }
            tmp_pending.write_text(json.dumps(queue_payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp_pending, pending_path)
            return {
                "candidate_id": candidate_id,
                "display_id": display_id,
                "candidate_dir": str(candidate_dir),
                "pending_path": str(pending_path),
                "queue_position": queue_position,
                "queue_pending_count": pending_count + 1,
                "candidate_prepare_elapsed_ms": candidate_prepare_elapsed_ms,
                "preparation_total_ms": preparation_total_ms,
            }
        except Exception:
            shutil.rmtree(candidate_dir, ignore_errors=True)
            raise


    def _updater_service_state_sync(self) -> str:
        service = Path("/etc/systemd/system/tts-bot-updater.service")
        if not service.exists():
            return "missing"
        result = self._run_cmd(
            ["systemctl", "is-active", "tts-bot-updater.service"],
            self._repo_root,
            env=self._git_env(),
        )
        state = (result.stdout or result.stderr or "").strip().splitlines()
        if state:
            return state[-1][:40].lower()
        return "active" if result.returncode == 0 else "unknown"


    def _trigger_updater_service_sync(self) -> tuple[bool, str]:
        service = Path("/etc/systemd/system/tts-bot-updater.service")
        if not service.exists():
            detail = "updater via systemd não encontrado"
            UPDATE_LOG.warning("dispatch do updater indisponível: %s", detail)
            return False, detail

        before_state = self._updater_service_state_sync()
        if before_state in {"active", "activating", "reloading"}:
            detail = f"updater já {before_state}; aguardando consumo da fila"
            UPDATE_LOG.info("dispatch do updater: %s", detail)
            return True, detail

        failures: list[str] = []
        commands = (
            ["sudo", "-n", "systemctl", "start", "--no-block", "tts-bot-updater.service"],
            ["systemctl", "start", "--no-block", "tts-bot-updater.service"],
        )
        for args in commands:
            result = self._run_cmd(args, self._repo_root, env=self._git_env())
            command_name = "sudo systemctl" if args[0] == "sudo" else "systemctl"
            output = (result.stderr or result.stdout or "").strip()
            output = output.splitlines()[-1][:180] if output else ""
            UPDATE_LOG.info(
                "dispatch do updater: comando=%s rc=%s estado_antes=%s detalhe=%s",
                command_name,
                result.returncode,
                before_state,
                output or "-",
            )
            if result.returncode == 0:
                return True, f"updater disparado agora via {command_name}"
            failures.append(f"{command_name} rc={result.returncode}" + (f" ({output})" if output else ""))

        detail = "; ".join(failures)[-500:] or "systemctl recusou o start"
        UPDATE_LOG.warning("dispatch imediato do updater falhou: %s", detail)
        return False, f"timer aplicará depois ({detail})"


    async def _watch_updater_candidate_dispatch(self, candidate_id: str, display_id: str = "") -> None:
        candidate_id = str(candidate_id or "").strip()
        if not candidate_id:
            return
        label = str(display_id or candidate_id).strip() or candidate_id
        started = time.monotonic()
        deadline = started + 60.0
        retriggers = 0
        last_service_state = ""
        try:
            # Dá tempo para um start --no-block normal mover pending -> active.
            await asyncio.sleep(1.0)
            while time.monotonic() < deadline:
                found = await asyncio.to_thread(self._zip_update_find_candidate_sync, candidate_id)
                queue_state = str(found.get("state") or "missing")
                elapsed = max(0.0, time.monotonic() - started)
                if queue_state != "pending":
                    UPDATE_LOG.info(
                        "dispatch do updater confirmado para %s: fila=%s em %.1fs retriggers=%d",
                        label,
                        queue_state,
                        elapsed,
                        retriggers,
                    )
                    return

                service_state = await asyncio.to_thread(self._updater_service_state_sync)
                if service_state != last_service_state:
                    UPDATE_LOG.info(
                        "dispatch aguardando claim de %s: fila=pending serviço=%s após %.1fs",
                        label,
                        service_state,
                        elapsed,
                    )
                    last_service_state = service_state

                # `systemctl start` em unidade já ativa não agenda uma nova execução.
                # Assim que ela ficar livre, reexecute o start em vez de esperar o timer.
                if service_state not in {"active", "activating", "reloading"}:
                    retriggers += 1
                    triggered, detail = await asyncio.to_thread(self._trigger_updater_service_sync)
                    UPDATE_LOG.info(
                        "retrigger imediato do updater para %s #%d: ok=%s detalhe=%s",
                        label,
                        retriggers,
                        triggered,
                        detail,
                    )

                await asyncio.sleep(2.0 if elapsed < 15.0 else 3.0)

            found = await asyncio.to_thread(self._zip_update_find_candidate_sync, candidate_id)
            if str(found.get("state") or "missing") == "pending":
                UPDATE_LOG.warning(
                    "candidato %s continuou pending por %.1fs após dispatch; timer permanece como fallback",
                    label,
                    max(0.0, time.monotonic() - started),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            UPDATE_LOG.warning("falha no watchdog de dispatch do updater para %s", label, exc_info=True)


    async def _dispatch_updater_candidate(self, candidate_id: str, display_id: str = "") -> tuple[bool, str]:
        triggered, detail = await asyncio.to_thread(self._trigger_updater_service_sync)
        label = str(display_id or candidate_id or "").strip()
        UPDATE_LOG.info(
            "dispatch inicial do updater para %s: ok=%s detalhe=%s",
            label or "candidato",
            triggered,
            detail,
        )
        asyncio.create_task(self._watch_updater_candidate_dispatch(candidate_id, display_id))
        return triggered, detail


    def _updater_control_request_state_sync(pending: Path, active: Path, request_id: str) -> str:
        for state, path in (("active", active), ("pending", pending)):
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(payload.get("id") or "") == str(request_id or ""):
                return state
        return "missing"


    async def _watch_updater_control_dispatch(
        self,
        pending: Path,
        active: Path,
        request_id: str,
        label: str,
    ) -> None:
        started = time.monotonic()
        deadline = started + 60.0
        retriggers = 0
        try:
            await asyncio.sleep(1.0)
            while time.monotonic() < deadline:
                state = await asyncio.to_thread(
                    self._updater_control_request_state_sync, pending, active, request_id
                )
                elapsed = max(0.0, time.monotonic() - started)
                if state != "pending":
                    UPDATE_LOG.info(
                        "dispatch de %s confirmado: pedido=%s estado=%s em %.1fs retriggers=%d",
                        label,
                        request_id,
                        state,
                        elapsed,
                        retriggers,
                    )
                    return
                service_state = await asyncio.to_thread(self._updater_service_state_sync)
                if service_state not in {"active", "activating", "reloading"}:
                    retriggers += 1
                    triggered, detail = await asyncio.to_thread(self._trigger_updater_service_sync)
                    UPDATE_LOG.info(
                        "retrigger de %s #%d: ok=%s detalhe=%s",
                        label,
                        retriggers,
                        triggered,
                        detail,
                    )
                await asyncio.sleep(2.0 if elapsed < 15.0 else 3.0)
            if await asyncio.to_thread(self._updater_control_request_state_sync, pending, active, request_id) == "pending":
                UPDATE_LOG.warning(
                    "pedido %s de %s continuou pending por %.1fs; timer permanece como fallback",
                    request_id,
                    label,
                    max(0.0, time.monotonic() - started),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            UPDATE_LOG.warning("falha no watchdog de dispatch de %s", label, exc_info=True)


    async def _dispatch_updater_control_request(
        self,
        pending: Path,
        active: Path,
        request_id: str,
        label: str,
    ) -> tuple[bool, str]:
        triggered, detail = await asyncio.to_thread(self._trigger_updater_service_sync)
        UPDATE_LOG.info(
            "dispatch inicial de %s: pedido=%s ok=%s detalhe=%s",
            label,
            request_id,
            triggered,
            detail,
        )
        asyncio.create_task(self._watch_updater_control_dispatch(pending, active, request_id, label))
        return triggered, detail


    def _guess_repo_name(self, origin_url: str) -> str:
        cleaned = (origin_url or "").strip().rstrip("/")
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        if "/" in cleaned:
            cleaned = cleaned.rsplit("/", 1)[-1]
        if ":" in cleaned:
            cleaned = cleaned.rsplit(":", 1)[-1]
        return cleaned.strip()


    def _pick_zip_strip_count(self, file_members: list[tuple[str, ...]], repo_name_hint: str, branch_name: str) -> int:
        if not file_members:
            return 0

        repo_root = self._repo_root.resolve()
        repo_top_names = {child.name for child in repo_root.iterdir()}
        max_strip = min(max(len(parts) - 1, 0) for parts in file_members)
        best_strip = 0
        best_score = (-1, -1, 0)

        for strip_count in range(max_strip + 1):
            mapped_members = [parts[strip_count:] for parts in file_members]
            if any(not parts for parts in mapped_members):
                continue

            exact_exists = 0
            top_level_exists = 0
            for mapped_parts in mapped_members:
                rel_path = Path(*mapped_parts)
                if (repo_root / rel_path).exists():
                    exact_exists += 1
                if mapped_parts[0] in repo_top_names:
                    top_level_exists += 1

            score = (exact_exists, top_level_exists, -strip_count)
            if score > best_score:
                best_score = score
                best_strip = strip_count

        if best_score[:2] != (0, 0):
            return best_strip

        common_first = file_members[0][0] if file_members[0] else ""
        if common_first and all(parts and parts[0] == common_first for parts in file_members):
            wrapper_names = {
                self._repo_root.name,
                repo_name_hint,
                f"{repo_name_hint}-main",
                f"{repo_name_hint}-master",
                f"{repo_name_hint}-{branch_name}",
            }
            if common_first in wrapper_names and common_first not in repo_top_names:
                return 1

        return best_strip


    def _phone_worker_base_url(self) -> str | None:
        if not bool(_cfg("PHONE_WORKER_ENABLED", default=False)):
            return None
        host = str(_cfg("PHONE_WORKER_HOST", default="") or "").strip()
        if not host:
            return None
        scheme = str(_cfg("PHONE_WORKER_SCHEME", default="http") or "http").strip() or "http"
        port = int(_cfg("PHONE_WORKER_PORT", default=8766) or 8766)
        return f"{scheme}://{host}:{port}"


    def _phone_worker_request_sync(self, task: str, payload: dict[str, object], *, timeout: float = 5.0) -> dict[str, object] | None:
        base_url = self._phone_worker_base_url()
        if not base_url:
            return None
        task = str(task or "").strip()
        now = time.monotonic()
        cooldown_default = 900.0 if task == "zip_validate" else 120.0
        cooldown = float(_cfg("PHONE_WORKER_UNAVAILABLE_COOLDOWN_SECONDS", default=cooldown_default) or cooldown_default)
        until = float(self._phone_worker_unavailable_until_by_task.get(task) or 0.0)
        if until and now < until:
            last_log = float(self._phone_worker_unavailable_last_log_by_task.get(task) or 0.0)
            if now - last_log >= 300.0:
                self._phone_worker_unavailable_last_log_by_task[task] = now
                logging.getLogger("zip_update").info(
                    "phone-worker indisponível para %s: pulando por cooldown %.0fs",
                    task, max(0.0, until - now),
                )
            return None
        token = str(_cfg("PHONE_WORKER_TOKEN", default="") or "").strip()
        payload = dict(payload)
        payload["task"] = task
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"{base_url}/task", data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                self._phone_worker_unavailable_until_by_task.pop(task, None)
                return parsed
            return None
        except Exception as exc:
            self._phone_worker_unavailable_until_by_task[task] = now + max(30.0, cooldown)
            last_log = float(self._phone_worker_unavailable_last_log_by_task.get(task) or 0.0)
            if now - last_log >= 300.0:
                self._phone_worker_unavailable_last_log_by_task[task] = now
                logging.getLogger("zip_update").info("phone-worker indisponível para %s: %r", task, exc)
            return None


    def _phone_worker_validate_zip_sync(self, zip_path: Path) -> dict[str, object] | None:
        if not bool(_cfg("PHONE_WORKER_ZIP_VALIDATE_ENABLED", default=True)):
            return None
        try:
            max_mb = int(_cfg("PHONE_WORKER_ZIP_VALIDATE_MAX_MB", default=24) or 24)
            if zip_path.stat().st_size > max_mb * 1024 * 1024:
                logging.getLogger("zip_update").info("zip grande demais para validação phone-worker: %.2f MB", zip_path.stat().st_size / 1048576)
                return None
            now = time.monotonic()
            until = float(self._phone_worker_unavailable_until_by_task.get("zip_validate") or 0.0)
            if until and now < until:
                return None
            timeout = float(_cfg("PHONE_WORKER_ZIP_VALIDATE_TIMEOUT_SECONDS", default=1.5) or 1.5)
            timeout = max(0.5, min(timeout, 5.0))
            result = self._phone_worker_request_sync(
                "zip_validate",
                {
                    "filename": zip_path.name,
                    "data_b64": base64.b64encode(zip_path.read_bytes()).decode("ascii"),
                    "max_entries": 800,
                    "max_preview": 40,
                },
                timeout=timeout,
            )
            if result:
                logging.getLogger("zip_update").info(
                    "phone-worker validou ZIP: ok=%s risk=%s files=%s size=%s",
                    result.get("ok"), result.get("risk"), result.get("files"), result.get("size"),
                )
            return result
        except Exception as exc:
            logging.getLogger("zip_update").info("falha ao preparar validação phone-worker do ZIP: %r", exc)
            return None


    def _safe_extract_patch(
        self,
        zip_path: Path,
        extract_dir: Path,
        repo_name_hint: str,
        branch_name: str,
    ) -> tuple[list[tuple[Path, Path]], dict[str, object], list[dict[str, str]]]:
        limits = self._zip_update_limits()
        inspection = inspect_zip_archive(zip_path, limits)
        accepted: list[tuple[Path, Path]] = []
        control_operations: list[dict[str, str]] = []
        control_seen = False
        mapped_entries: dict[str, str] = {}
        mapped_files: set[str] = set()
        mapped_dirs: set[str] = set()
        extract_root = extract_dir.resolve()

        with zipfile.ZipFile(zip_path) as zf:
            file_members: list[tuple[str, ...]] = []
            prepared_infos: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []

            for info in zf.infolist():
                raw_parts = self._normalize_zip_member_parts(info.filename)
                if not raw_parts:
                    continue
                if raw_parts[0] == "__MACOSX" or raw_parts[-1] == ".DS_Store":
                    continue
                if any(part == ".." for part in raw_parts):
                    raise RuntimeError(f"Caminho inválido no ZIP: {info.filename}")
                prepared_infos.append((info, raw_parts))
                if not info.is_dir():
                    file_members.append(raw_parts)

            strip_count = self._pick_zip_strip_count(file_members, repo_name_hint, branch_name)

            for info, raw_parts in prepared_infos:
                normalized_parts = raw_parts[strip_count:]
                if not normalized_parts:
                    continue
                normalized = PurePosixPath(*normalized_parts)
                if normalized.is_absolute() or any(part in ("", ".", "..") for part in normalized.parts):
                    raise RuntimeError(f"Caminho inválido no ZIP: {info.filename}")

                target_rel = Path(*normalized.parts)
                if normalized.as_posix() == UPDATE_CONTROL_MANIFEST_NAME:
                    if info.is_dir():
                        raise RuntimeError(f"{UPDATE_CONTROL_MANIFEST_NAME} precisa ser um arquivo JSON")
                    if control_seen:
                        raise RuntimeError(f"{UPDATE_CONTROL_MANIFEST_NAME} duplicado no ZIP")
                    control_seen = True
                    if int(info.file_size) > 64 * 1024:
                        raise RuntimeError(f"{UPDATE_CONTROL_MANIFEST_NAME} excede 64 KiB")
                    try:
                        payload = json.loads(zf.read(info).decode("utf-8"))
                    except Exception as exc:
                        raise RuntimeError(f"{UPDATE_CONTROL_MANIFEST_NAME} inválido: {type(exc).__name__}") from exc
                    if not isinstance(payload, dict):
                        raise RuntimeError(f"{UPDATE_CONTROL_MANIFEST_NAME} precisa ser um objeto JSON")
                    try:
                        control_schema = int(payload.get("schema_version") or 1)
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError(f"schema_version inválido em {UPDATE_CONTROL_MANIFEST_NAME}") from exc
                    if control_schema != 1:
                        raise RuntimeError(f"schema_version não suportado em {UPDATE_CONTROL_MANIFEST_NAME}: {control_schema}")
                    try:
                        control_operations = normalize_update_operations(
                            payload.get("operations"),
                            allowed_ops={"delete", "move"},
                        )
                    except UpdateSecurityError as exc:
                        raise RuntimeError(f"operações inválidas em {UPDATE_CONTROL_MANIFEST_NAME}: {exc}") from exc
                    continue

                if self._zip_update_should_ignore_generated_file(target_rel):
                    continue
                if self._zip_update_is_forbidden_path(target_rel):
                    raise RuntimeError(f"Caminho protegido não pode ser alterado: {target_rel.as_posix()}")

                key = canonical_path_key(normalized.parts)
                previous = mapped_entries.get(key)
                if previous is not None:
                    raise RuntimeError(
                        f"Caminho duplicado ou ambíguo após normalização: {previous} e {info.filename}"
                    )
                mapped_entries[key] = info.filename

                ancestor_keys = [canonical_path_key(normalized.parts[:idx]) for idx in range(1, len(normalized.parts))]
                if any(ancestor in mapped_files for ancestor in ancestor_keys):
                    raise RuntimeError(f"Conflito entre arquivo e diretório no ZIP: {info.filename}")

                if info.is_dir():
                    if key in mapped_files:
                        raise RuntimeError(f"Conflito entre arquivo e diretório no ZIP: {info.filename}")
                    mapped_dirs.add(key)
                    target_dir = (extract_dir / target_rel).resolve()
                    if extract_root not in target_dir.parents and target_dir != extract_root:
                        raise RuntimeError(f"Arquivo fora da extração: {target_rel.as_posix()}")
                    target_dir.mkdir(parents=True, exist_ok=True)
                    continue

                if key in mapped_dirs or any(existing.startswith(key + "/") for existing in mapped_files | mapped_dirs):
                    raise RuntimeError(f"Conflito entre arquivo e diretório no ZIP: {info.filename}")
                mapped_files.add(key)
                extract_path = (extract_dir / target_rel).resolve()
                if extract_root not in extract_path.parents:
                    raise RuntimeError(f"Arquivo fora da extração: {target_rel.as_posix()}")
                extract_path.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with zf.open(info, "r") as src, open(extract_path, "wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > limits.max_file_bytes or written > int(info.file_size):
                            raise RuntimeError(f"Tamanho descompactado inválido: {info.filename}")
                        dst.write(chunk)
                if written != int(info.file_size):
                    raise RuntimeError(f"Tamanho descompactado divergente: {info.filename}")
                accepted.append((extract_path, target_rel))

        if not accepted and not control_operations:
            raise RuntimeError("O ZIP não trouxe nenhum arquivo ou operação aplicável.")
        return accepted, inspection.as_dict(), control_operations


    def _apply_declarative_operations_to_clone(
        self,
        operations: list[dict[str, str]],
        clone_dir: Path,
        env: dict[str, str],
    ) -> list[dict[str, str]]:
        clone_root = clone_dir.resolve()
        normalized = normalize_update_operations(operations, allowed_ops={"delete", "move"})
        for item in normalized:
            op = item["op"]
            if op == "delete":
                rel = item["path"]
                target = clone_dir / rel
                resolved = target.resolve(strict=False)
                if resolved != clone_root and clone_root not in resolved.parents:
                    raise RuntimeError(f"delete resolve para fora do repositório: {rel}")
                if target.is_symlink() or not target.is_file():
                    raise RuntimeError(f"delete exige arquivo regular existente: {rel}")
                tracked = self._run_cmd(["git", "ls-files", "--error-unmatch", "--", rel], clone_dir, env=env)
                if tracked.returncode != 0:
                    raise RuntimeError(f"delete exige arquivo rastreado pelo Git: {rel}")
                result = self._run_cmd(["git", "rm", "-f", "--", rel], clone_dir, env=env)
                if result.returncode != 0:
                    err = (result.stderr or result.stdout or "").strip()
                    raise RuntimeError(f"git rm falhou para {rel}: {err}")
                continue

            source_rel = item["from"]
            target_rel = item["to"]
            source = clone_dir / source_rel
            target = clone_dir / target_rel
            source_resolved = source.resolve(strict=False)
            target_resolved = target.resolve(strict=False)
            for label, resolved in ((source_rel, source_resolved), (target_rel, target_resolved)):
                if resolved != clone_root and clone_root not in resolved.parents:
                    raise RuntimeError(f"move resolve para fora do repositório: {label}")
            if source.is_symlink() or not source.is_file():
                raise RuntimeError(f"move exige arquivo regular existente: {source_rel}")
            if target.exists() or target.is_symlink():
                raise RuntimeError(f"destino de move já existe: {target_rel}")
            tracked = self._run_cmd(["git", "ls-files", "--error-unmatch", "--", source_rel], clone_dir, env=env)
            if tracked.returncode != 0:
                raise RuntimeError(f"move exige origem rastreada pelo Git: {source_rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            result = self._run_cmd(["git", "mv", "--", source_rel, target_rel], clone_dir, env=env)
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(f"git mv falhou para {source_rel} -> {target_rel}: {err}")
        return normalized


    def _apply_patch_to_clone(self, extracted_files: list[tuple[Path, Path]], clone_dir: Path) -> list[dict[str, str]]:
        operations: list[dict[str, str]] = []
        for extracted_path, rel_path in extracted_files:
            destination = (clone_dir / rel_path).resolve()
            clone_root = clone_dir.resolve()
            if clone_root not in destination.parents and destination != clone_root:
                raise RuntimeError(f"Arquivo fora do repositório: {rel_path.as_posix()}")
            if (clone_dir / rel_path).is_symlink():
                raise RuntimeError(f"Destino é symlink e não pode ser sobrescrito: {rel_path.as_posix()}")

            destination.parent.mkdir(parents=True, exist_ok=True)
            before = destination.read_bytes() if destination.exists() else None
            data = extracted_path.read_bytes()
            if before == data:
                continue
            destination.write_bytes(data)
            operations.append({"op": "update" if before is not None else "add", "path": rel_path.as_posix()})
        return operations


    def _process_zip_update_sync(
        self,
        zip_path: Path,
        status_context: dict[str, object] | None = None,
        progress_callback=None,
        progress_started_epoch_ms: int | None = None,
    ) -> dict[str, object]:
        self._update_temp_root.mkdir(parents=True, exist_ok=True)
        env = self._git_env()
        origin_result = self._run_cmd(["git", "remote", "get-url", "origin"], self._repo_root, env=env)
        if origin_result.returncode != 0:
            raise RuntimeError(f"Não foi possível descobrir o origin do git. {origin_result.stderr.strip() or origin_result.stdout.strip()}")
        origin_url = (origin_result.stdout or "").strip()
        if not origin_url:
            raise RuntimeError("O repositório local não tem origin configurado.")

        branch_result = self._run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], self._repo_root, env=env)
        branch_name = (branch_result.stdout or "main").strip() or "main"
        if branch_result.returncode != 0 or branch_name == "HEAD":
            branch_name = "main"

        work_dir = Path(tempfile.mkdtemp(prefix="discord-auto-update-", dir=str(self._update_temp_root)))
        extract_dir = work_dir / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        timings: dict[str, int] = {}
        t0 = time.monotonic()
        last = t0
        preparation_steps: list[dict[str, object]] = []

        def publish_progress(current: str, completed: str = "", elapsed_ms: int | None = None) -> None:
            completed_label = " ".join(str(completed or "").split())[:120]
            safe_elapsed_ms = max(0, int(elapsed_ms or 0))
            if completed_label and not any(
                str(step.get("label") or "") == completed_label for step in preparation_steps
            ):
                preparation_steps.append(
                    {"label": completed_label, "elapsed_ms": safe_elapsed_ms}
                )
            if progress_callback is None:
                return
            try:
                progress_callback(
                    {
                        "current": str(current or "").strip(),
                        "completed": completed_label,
                        "elapsed_ms": safe_elapsed_ms,
                    }
                )
            except Exception:
                logging.getLogger("zip_update").debug("falha ao publicar microetapa de preparação", exc_info=True)

        def mark(label: str) -> int:
            nonlocal last
            now = time.monotonic()
            elapsed_ms = max(0, int((now - last) * 1000))
            timings[label] = elapsed_ms
            last = now
            return elapsed_ms

        try:
            repo_name_hint = self._guess_repo_name(origin_url)
            publish_progress("Inspecionando pacote")
            worker_zip_validation = self._phone_worker_validate_zip_sync(zip_path)
            elapsed = mark("phone_worker_zip_validate_ms")
            publish_progress("Conferindo estrutura do ZIP", "Pacote inspecionado", elapsed)
            if worker_zip_validation and not bool(worker_zip_validation.get("ok", True)):
                errors = worker_zip_validation.get("errors") or []
                if isinstance(errors, list) and errors:
                    raise RuntimeError("ZIP bloqueado pelo phone-worker: " + "; ".join(str(item) for item in errors[:3]))
                raise RuntimeError("ZIP bloqueado pelo phone-worker")

            extracted_files, zip_inspection, requested_operations = self._safe_extract_patch(zip_path, extract_dir, repo_name_hint, branch_name)
            elapsed = mark("extract_ms")
            publish_progress("Preparando base de comparação", "Estrutura do ZIP conferida", elapsed)

            clone_dir = self._prepare_update_staging_clone(origin_url, branch_name, env)
            elapsed = mark("staging_prepare_ms")
            publish_progress("Comparando arquivos", "Base de comparação preparada", elapsed)

            tree_operations = self._apply_declarative_operations_to_clone(requested_operations, clone_dir, env)
            content_operations = self._apply_patch_to_clone(extracted_files, clone_dir)
            operations = normalize_update_operations([*tree_operations, *content_operations])
            changed_files = update_operation_paths(operations)
            elapsed = mark("apply_patch_ms")
            publish_progress("Calculando alterações", "Arquivos comparados", elapsed)
            if not changed_files:
                timings["total_ms"] = int((time.monotonic() - t0) * 1000)
                return {
                    "changed_files": [],
                    "commit_hash": None,
                    "triggered_update": False,
                    "trigger_detail": "sem mudanças",
                    "branch": branch_name,
                    "timings": timings,
                }

            self._run_cmd(["git", "config", "user.name", "Discord Auto Update"], clone_dir, env=env)
            self._run_cmd(["git", "config", "user.email", "discord-auto-update@local"], clone_dir, env=env)

            # Operações declarativas (`git rm`/`git mv`) já deixam suas mudanças
            # staged. Repassar os paths removidos para `git add -A -- <path>`
            # faz o Git rejeitar o pathspec porque ele já saiu do index. Portanto,
            # stageamos aqui somente arquivos de conteúdo copiados do ZIP.
            content_changed_files = update_operation_paths(content_operations)
            if content_changed_files:
                add_result = self._run_cmd(["git", "add", "-A", "--", *content_changed_files], clone_dir, env=env)
                if add_result.returncode != 0:
                    err = (add_result.stderr or add_result.stdout or "").strip()
                    raise RuntimeError(f"Falha ao preparar arquivos para commit. {err}")

            status_result = self._run_cmd(["git", "status", "--porcelain"], clone_dir, env=env)
            if status_result.returncode != 0:
                err = (status_result.stderr or status_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao verificar alterações do staging persistente. {err}")
            if not (status_result.stdout or "").strip():
                timings["total_ms"] = int((time.monotonic() - t0) * 1000)
                return {
                    "changed_files": [],
                    "commit_hash": None,
                    "triggered_update": False,
                    "trigger_detail": "sem mudanças",
                    "branch": branch_name,
                    "timings": timings,
                }

            diff_stats = self._git_diff_numstat_sync(clone_dir, env)
            patch_result = self._run_cmd(["git", "diff", "--cached", "--binary"], clone_dir, env=env)
            if patch_result.returncode != 0:
                err = (patch_result.stderr or patch_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao gerar patch de fila. {err}")
            patch_diff_text = patch_result.stdout or ""
            changed_files = [str(item.get("path")) for item in diff_stats.get("entries", []) if item.get("path")] or changed_files
            if set(changed_files) != set(update_operation_paths(operations)):
                raise RuntimeError("O diff staged divergiu das operações declaradas no candidato.")
            elapsed = mark("diff_ms")
            publish_progress("Montando candidato seguro", "Alterações calculadas", elapsed)

            base_result = self._run_cmd(["git", "rev-parse", f"origin/{branch_name}"], clone_dir, env=env)
            base_commit = (base_result.stdout or "").strip() if base_result.returncode == 0 else None

            candidate = self._write_local_update_candidate_sync(
                branch_name=branch_name,
                base_commit=base_commit,
                changed_files=changed_files,
                operations=operations,
                diff_stats=diff_stats,
                extracted_files=extracted_files,
                zip_name=zip_path.name,
                zip_inspection=zip_inspection,
                patch_diff_text=patch_diff_text,
                status_context=status_context,
                preparation_steps=list(preparation_steps),
                preparation_started_monotonic=t0,
                candidate_step_started_monotonic=last,
                progress_started_epoch_ms=progress_started_epoch_ms,
            )
            measured_elapsed = mark("candidate_write_ms")
            elapsed = max(0, int(candidate.get("candidate_prepare_elapsed_ms") or measured_elapsed))
            timings["candidate_write_ms"] = elapsed
            publish_progress("Finalizando preparação", "Candidato seguro preparado", elapsed)
            timings["total_ms"] = int((time.monotonic() - t0) * 1000)

            return {
                "changed_files": changed_files,
                "diff_stats": diff_stats,
                "commit_hash": None,
                "candidate_id": candidate.get("candidate_id"),
                "display_id": candidate.get("display_id"),
                "candidate_dir": candidate.get("candidate_dir"),
                "queue_position": candidate.get("queue_position"),
                "queue_pending_count": candidate.get("queue_pending_count"),
                "triggered_update": False,
                "trigger_detail": "aguardando acionamento após publicar a fila",
                "branch": branch_name,
                "phone_worker_zip_validation": worker_zip_validation,
                "zip_inspection": zip_inspection,
                "timings": timings,
                "staging_dir": str(clone_dir),
            }
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
