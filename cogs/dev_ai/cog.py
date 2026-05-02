from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any

import aiohttp
import discord
from discord.ext import commands

import config

from .ai_client import DevAIClient, AIResult, SYSTEM_PROMPT_FIX, SYSTEM_PROMPT_REVIEW, SYSTEM_PROMPT_CHAT
from .log_watcher import LogEvent, LogWatcher
from .patch_builder import BuiltPatch, HistoryItem, PatchBuilder
from .project_indexer import ProjectIndexer
from .safety import redact_secrets
from .webhook_reporter import WebhookReporter

log = logging.getLogger(__name__)


# Schema do JSON de patch — fica fora do _build_prompt pra ficar fácil de
# ajustar e pra não pesar todo prompt build.
PATCH_JSON_SCHEMA = """{
  "cause": "causa-raiz em 1-2 frases — quem disparou o erro e por quê",
  "summary": "o que você mudou em 1 frase",
  "effect": "o que essa mudança faz na prática (sintoma -> resultado esperado)",
  "risk": "baixo|médio|alto",
  "recommendations": ["recomendação curta para o dono após aplicar"],
  "tests": ["teste manual ou comando útil para validar"],
  "files": [
    {
      "path": "cogs/exemplo.py",
      "content": "# conteúdo COMPLETO do arquivo, sem '...' nem trecho omitido"
    }
  ]
}"""

REVIEW_JSON_SCHEMA = """{
  "summary": "resumo curto do patch (1 frase)",
  "what_changed": ["mudança importante em 1 linha"],
  "effect": "o que a alteração faz na prática",
  "risk": "baixo|médio|alto",
  "recommendations": ["recomendação útil pro dono"],
  "tests": ["teste/comando pra validar"]
}"""


class DevAI(commands.Cog):
    """IA de manutenção: lê logs, pede correção para providers grátis e entrega zip no webhook."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.repo_root = Path(__file__).resolve().parents[2]
        self.data_dir = self.repo_root / "data" / "dev_ai"
        self.generated_dir = self.data_dir / "generated_patches"
        self.session: aiohttp.ClientSession | None = None
        self.ai: DevAIClient | None = None
        self.reporter: WebhookReporter | None = None
        self.indexer = ProjectIndexer(self.repo_root, self.data_dir)
        self.patch_builder = PatchBuilder(
            self.repo_root,
            self.generated_dir,
            max_files=int(getattr(config, "DEVAI_MAX_FILES_PER_PATCH", 5) or 5),
            max_file_bytes=int(getattr(config, "DEVAI_MAX_FILE_BYTES", 220000) or 220000),
        )
        self.watcher: LogWatcher | None = None
        self.worker_task: asyncio.Task | None = None
        self.queue: asyncio.Queue[LogEvent] = asyncio.Queue(maxsize=20)
        self._analysis_lock = asyncio.Lock()
        self._last_auto_started_at = 0.0
        # IDs de mensagens postadas pela DevAI no webhook configurado.
        # Separados pra que reply em CHAT não dispare re-análise de erro:
        #   _report_message_ids → reply leva pro fluxo de re-análise/patch
        #   _chat_message_ids   → reply continua a conversa em modo chat
        self._report_message_ids: set[int] = set()
        self._chat_message_ids: set[int] = set()
        self._last_event_by_message: dict[int, LogEvent] = {}

    def _enabled(self) -> bool:
        """A DevAI considera-se habilitada se:
        (a) `DEVAI_ENABLED=true` explícito no .env, OU
        (b) há webhook URL E canal de comentário configurados — config
            implícita: se o dono se deu o trabalho de setar essas duas
            variáveis, claramente quer a DevAI ligada.
        Esse "auto-enable" cobre o caso comum de esquecer a flag explícita."""
        if bool(getattr(config, "DEVAI_ENABLED", False)):
            return True
        webhook_set = bool(getattr(config, "DEVAI_WEBHOOK_URL", "") or "")
        channel_set = bool(int(getattr(config, "DEVAI_COMMENT_CHANNEL_ID", 0) or 0))
        return webhook_set and channel_set

    def _devai_webhook_id(self) -> int:
        """Extrai o ID do webhook do `DEVAI_WEBHOOK_URL` pra comparar com o
        `webhook_id` de mensagens citadas em replies.

        URL formato: `https://discord.com/api/webhooks/{ID}/{TOKEN}`. Retorna
        0 se não der match (URL vazia/inválida)."""
        url = str(getattr(config, "DEVAI_WEBHOOK_URL", "") or "")
        match = re.search(r"/webhooks/(\d+)/", url)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                return 0
        return 0

    async def cog_load(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        self.session = aiohttp.ClientSession()
        self.ai = DevAIClient(self.session, config)
        self.reporter = WebhookReporter(self.session, str(getattr(config, "DEVAI_WEBHOOK_URL", "") or ""))

        # Mantém um entendimento inicial da estrutura do projeto, mas sem travar o bot.
        asyncio.create_task(asyncio.to_thread(self.indexer.build_index))

        if self._enabled():
            self.watcher = LogWatcher(
                self.repo_root,
                self._configured_log_paths(),
                max_lines=int(getattr(config, "DEVAI_MAX_LOG_LINES", 180) or 180),
                scan_existing=bool(getattr(config, "DEVAI_SCAN_EXISTING_LOGS_ON_BOOT", False)),
            )
            self.worker_task = asyncio.create_task(self._watch_loop())
            log.info("DevAI habilitada. Logs monitoradas: %s", ", ".join(p.as_posix() for p in self._configured_log_paths()))

            # Verifica se há reviews pendentes que ficaram interrompidos por
            # restart do systemd. Se houver, retoma em background — não trava
            # o startup do bot.
            asyncio.create_task(self._resume_pending_reviews())
        else:
            log.info("DevAI carregada, mas desabilitada. Defina DEVAI_ENABLED=true para ativar.")

    async def cog_unload(self):
        if self.worker_task:
            self.worker_task.cancel()
        if self.session:
            await self.session.close()

    def _configured_log_paths(self) -> list[Path]:
        raw = str(getattr(config, "DEVAI_LOG_PATHS", "") or "").strip()
        if raw:
            items = [item.strip() for item in raw.split(",") if item.strip()]
        else:
            items = ["logs/*.log", "bot.log", "logs/bot.log"]
        return [(self.repo_root / item).resolve() if not Path(item).is_absolute() else Path(item) for item in items]

    def _owner_ids(self) -> set[int]:
        ids = set(int(x) for x in getattr(config, "DEVAI_OWNER_IDS", []) or [] if int(x or 0))
        for attr in ("BOT_OWNER_ID", "OWNER_ID", "TTS_VOICE_FAILURE_DM_USER_ID"):
            val = int(getattr(config, attr, 0) or 0)
            if val:
                ids.add(val)
        return ids

    async def _is_ownerish(self, user: discord.abc.User) -> bool:
        if int(getattr(user, "id", 0) or 0) in self._owner_ids():
            return True
        try:
            return await self.bot.is_owner(user)
        except Exception:
            return False

    async def _watch_loop(self):
        poll_interval = float(getattr(config, "DEVAI_POLL_INTERVAL_SECONDS", 8.0) or 8.0)
        while not self.bot.is_closed():
            try:
                if self.watcher is not None:
                    for event in self.watcher.poll():
                        try:
                            self.queue.put_nowait(event)
                        except asyncio.QueueFull:
                            log.warning("DevAI: fila cheia, erro ignorado: %s", event.signature)
                    await self._drain_queue_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("DevAI: falha no loop: %r", exc)
            await asyncio.sleep(poll_interval)

    async def _drain_queue_once(self):
        if self._analysis_lock.locked():
            return
        now = time.time()
        cooldown = float(getattr(config, "DEVAI_COOLDOWN_SECONDS", 300) or 300)
        if now - self._last_auto_started_at < cooldown:
            return
        try:
            event = self.queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        self._last_auto_started_at = now
        asyncio.create_task(self._analyze_event(event))

    # ---------------------------------------------------------------- prompt

    def _format_history_block(self, history: list[HistoryItem]) -> str:
        if not history:
            return "Nenhum patch recente. Você está vendo este projeto pela 1ª vez nesta sessão."
        lines: list[str] = []
        for item in history:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(item.created_at))
            files = ", ".join(item.changed_files[:5])
            if len(item.changed_files) > 5:
                files += f" (+{len(item.changed_files) - 5})"
            cause = redact_secrets((item.cause or item.summary or "")[:240])
            lines.append(
                f"- [{ts}] arquivos={files or '-'} | risco={item.risk or '-'} | causa: {cause or '(sem descrição)'}"
            )
        return "\n".join(lines)

    def _build_prompt(self, *, event: LogEvent, comment: str | None = None) -> str:
        index = self.indexer.load_or_build(max_age_seconds=int(getattr(config, "DEVAI_INDEX_MAX_AGE_SECONDS", 1800) or 1800))
        project_context = self.indexer.compact_context(index, max_chars=int(getattr(config, "DEVAI_MAX_INDEX_CHARS", 12000) or 12000))

        # Expande arquivos do traceback usando o grafo de imports do indexador
        # em vez de chute por palavra-chave. O fallback por palavra-chave fica
        # como reforço quando o traceback não traz path nenhum.
        candidate_files = list(event.file_paths or [])
        if candidate_files:
            candidate_files = self.indexer.expand_related_files(
                candidate_files,
                index,
                max_total=int(getattr(config, "DEVAI_MAX_CONTEXT_FILES", 4) or 4),
            )
        else:
            candidate_files = self._guess_related_files(event.text)

        seen: set[str] = set()
        candidate_files = [p for p in candidate_files if not (p in seen or seen.add(p))]
        file_context = self.indexer.read_files_for_context(
            candidate_files,
            max_files=int(getattr(config, "DEVAI_MAX_CONTEXT_FILES", 4) or 4),
            max_chars_per_file=int(getattr(config, "DEVAI_MAX_FILE_CONTEXT_CHARS", 24000) or 24000),
        )

        context_blocks: list[str] = []
        for path, text in file_context.items():
            context_blocks.append(f"### {path}\n```py\n{text}\n```")

        # Histórico recente — evita repetir tentativa que já foi feita.
        history = self.patch_builder.recent_history(
            limit=int(getattr(config, "DEVAI_HISTORY_ITEMS", 5) or 5),
            max_age_seconds=int(getattr(config, "DEVAI_HISTORY_MAX_AGE_SECONDS", 7 * 24 * 3600) or 7 * 24 * 3600),
        )
        history_block = self._format_history_block(history)

        # O comentário do dono vai NO TOPO do user prompt (em vez de no fim)
        # porque modelos pequenos costumam ancorar mais no início.
        comment_top = ""
        if comment:
            comment_top = (
                "\n>>> INSTRUÇÃO PRIORITÁRIA DO DONO (siga antes de tudo):\n"
                f">>> {redact_secrets(comment, max_chars=2500)}\n"
            )

        return f"""{comment_top}
ERRO DETECTADO:
- Fonte: {event.source}
- Assinatura: {event.signature}
```txt
{redact_secrets(event.text, max_chars=int(getattr(config, 'DEVAI_MAX_LOG_CHARS', 18000) or 18000))}
```

ARQUIVOS RELACIONADOS LIDOS DO PROJETO:
{chr(10).join(context_blocks) if context_blocks else 'Nenhum arquivo relacionado foi lido automaticamente.'}

PATCHES RECENTES DA DEVAI (não repita o que já tentou):
{history_block}

ESTRUTURA RESUMIDA DO PROJETO:
```txt
{project_context}
```

REGRAS DE SAÍDA:
- Responda SOMENTE com JSON válido (UTF-8), sem markdown, sem ``` antes ou depois.
- Patch mínimo: idealmente 1 arquivo. Máximo {int(getattr(config, 'DEVAI_MAX_FILES_PER_PATCH', 5) or 5)} arquivos.
- Cada `files[i].content` deve ser o ARQUIVO COMPLETO (não use '...' nem 'mantém o resto igual').
- Não invente módulos. Use só o que existe nos arquivos lidos ou no índice.
- Se não tiver certeza suficiente, retorne `"files": []` e explique em `cause`.

SCHEMA EXATO DO JSON:
{PATCH_JSON_SCHEMA}
""".strip()

    def _guess_related_files(self, text: str) -> list[str]:
        """Fallback de palavras-chave para quando o traceback não tem path.
        Mantido com mesma cobertura do código antigo + mais alguns mapeamentos."""
        text_lower = text.lower()
        guesses: list[str] = []
        if "tts" in text_lower or "voice" in text_lower or "call" in text_lower:
            guesses.extend(["cogs/tts/cog.py", "cogs/tts/audio.py", "cogs/tts/ui.py"])
        if "webhook" in text_lower or "zip" in text_lower or "update" in text_lower:
            guesses.extend(["bot.py", "alert.sh"])
        if "mongodb" in text_lower or "mongo" in text_lower or "database" in text_lower or "duplicate key" in text_lower:
            guesses.extend(["db.py", "config.py"])
        if "help" in text_lower or "health" in text_lower:
            guesses.append("cogs/utility.py")
        if "chatbot" in text_lower or "imagegen" in text_lower or "imagem" in text_lower:
            guesses.extend(["cogs/chatbot/cog.py", "cogs/chatbot/imagegen.py"])
        if "gincana" in text_lower:
            guesses.append("cogs/gincana/cog.py")
        if "color" in text_lower or "role" in text_lower:
            guesses.extend(["cogs/color_roles.py", "cogs/role_cooldown.py"])
        if "devai" in text_lower or "patch" in text_lower:
            guesses.extend(["cogs/dev_ai/cog.py", "cogs/dev_ai/ai_client.py"])
        return guesses

    # --------------------------------------------------------------- analyze

    async def _analyze_event(self, event: LogEvent, *, comment: str | None = None) -> None:
        if self.ai is None or self.reporter is None:
            return
        async with self._analysis_lock:
            try:
                prompt = await asyncio.to_thread(self._build_prompt, event=event, comment=comment)
                prompt = self._truncate_prompt_if_needed(prompt)
                result, errors = await self.ai.generate_patch_json(prompt, system=SYSTEM_PROMPT_FIX)
                if result is None:
                    await self._report_failure(event, errors, comment=comment)
                    return

                # Tenta montar o patch. Se JSON ou compilação falhar, pede UMA
                # rodada de repair pra mesma cadeia de providers — modelos
                # menores quase sempre acertam quando recebem o erro de volta.
                try:
                    built = await asyncio.to_thread(
                        self.patch_builder.build_from_ai_response, result.text, label=event.signature
                    )
                except Exception as build_exc:
                    if not bool(getattr(config, "DEVAI_REPAIR_ENABLED", True)):
                        await self._report_build_failure(event, result, build_exc, comment=comment)
                        return
                    log.info("DevAI: tentando repair após falha: %s", build_exc)
                    repair_result, repair_errors = await self.ai.repair_patch_json(
                        original_prompt=prompt,
                        bad_response=result.text,
                        error_message=str(build_exc),
                        system=SYSTEM_PROMPT_FIX,
                    )
                    if repair_result is None:
                        merged_errors = errors + ["repair: " + e for e in repair_errors]
                        await self._report_build_failure(
                            event,
                            result,
                            RuntimeError(
                                f"falha original: {build_exc}; repair também falhou: {merged_errors[-3:]}"
                            ),
                            comment=comment,
                        )
                        return
                    try:
                        built = await asyncio.to_thread(
                            self.patch_builder.build_from_ai_response,
                            repair_result.text,
                            label=event.signature,
                        )
                        result = repair_result  # passa a usar o resultado bem-sucedido
                    except Exception as build_exc2:
                        await self._report_build_failure(event, repair_result, build_exc2, comment=comment)
                        return

                # Re-indexa pra IA "aprender" a estrutura nova nas próximas rodadas.
                await asyncio.to_thread(self.indexer.build_index)
                await self._report_patch(event, result, built, comment=comment)
            except Exception as exc:
                log.exception("DevAI: falha analisando evento")
                if self.reporter is not None and self.reporter.available():
                    await self.reporter.send_report(
                        title="⚠️ DevAI falhou durante análise",
                        description=f"Assinatura: `{event.signature}`\nErro interno: `{type(exc).__name__}: {redact_secrets(str(exc), max_chars=700)}`",
                        color=0xED4245,
                    )

    # -------------------------------------------------------------- relatos

    async def _report_patch(self, event: LogEvent, result: AIResult, built: BuiltPatch, *, comment: str | None = None):
        if self.reporter is None or not self.reporter.available():
            return
        files = "\n".join(f"• `{p}`" for p in built.changed_files)
        validation = "\n".join(f"• {v}" for v in built.validation) or "• Sem arquivos Python para compilar."
        recommendations = self._format_bullets(built.recommendations, fallback="• Nenhuma recomendação extra informada.", max_items=6)
        tests = self._format_bullets(built.tests, fallback="• Validar o fluxo afetado manualmente após aplicar.", max_items=6)
        title = "🧠 DevAI corrigiu um erro e gerou um patch"
        description = (
            f"**Provider:** `{result.provider}` · `{result.model}` · `{result.elapsed_ms} ms`\n"
            f"**Assinatura:** `{event.signature}`\n\n"
            f"**Causa provável**\n{redact_secrets(built.cause, max_chars=900)}\n\n"
            f"**Alteração**\n{redact_secrets(built.summary, max_chars=900)}\n\n"
            f"**O que faz**\n{redact_secrets(built.effect, max_chars=900)}\n\n"
            f"**Recomendações**\n{recommendations}\n\n"
            f"**Como validar**\n{tests}\n\n"
            f"**Arquivos no zip**\n{files}\n\n"
            f"**Validação**\n{validation}\n\n"
            f"⚠️ O patch foi **gerado automaticamente**, mas **não foi aplicado**. Revise o `.zip` antes de enviar no canal de update."
        )
        msg = await self.reporter.send_report(title=title, description=description, color=0x57F287, file_path=built.zip_path)
        if msg is not None:
            self._report_message_ids.add(int(msg.id))
            self._last_event_by_message[int(msg.id)] = event

    async def _report_failure(self, event: LogEvent, errors: list[str], *, comment: str | None = None):
        if self.reporter is None or not self.reporter.available():
            return
        err_text = "\n".join(f"• {redact_secrets(e, max_chars=500)}" for e in errors[-8:]) or "• nenhum detalhe"
        await self.reporter.send_report(
            title="⚠️ DevAI não conseguiu chamar nenhum provider",
            description=f"Assinatura: `{event.signature}`\n\n{err_text}",
            color=0xFEE75C,
        )

    async def _report_build_failure(self, event: LogEvent, result: AIResult, exc: Exception, *, comment: str | None = None):
        if self.reporter is None or not self.reporter.available():
            return
        await self.reporter.send_report(
            title="⚠️ DevAI recebeu resposta, mas não gerou zip",
            description=(
                f"**Provider:** `{result.provider}` · `{result.model}`\n"
                f"**Assinatura:** `{event.signature}`\n"
                f"**Erro ao montar patch:** `{type(exc).__name__}: {redact_secrets(str(exc), max_chars=1000)}`\n\n"
                "Isso geralmente acontece quando a IA devolve JSON inválido, arquivo sem conteúdo completo ou caminho bloqueado."
            ),
            color=0xFEE75C,
        )

    def _format_bullets(self, items: list[str] | tuple[str, ...] | None, *, fallback: str, max_items: int = 6) -> str:
        values: list[str] = []
        for item in list(items or [])[:max_items]:
            text = redact_secrets(str(item).strip(), max_chars=450)
            if text:
                values.append(f"• {text}")
        return "\n".join(values) if values else fallback

    # ---------------------------------------------------- patch review (post-apply)

    def _read_zip_patch_context(self, zip_path: Path, changed_files: list[str]) -> dict[str, str]:
        """Devolve mapa {path -> conteúdo novo} pra os arquivos do ZIP que
        bateram com `changed_files`. O texto fica truncado se o arquivo for
        muito grande."""
        max_files = int(getattr(config, "DEVAI_PATCH_REVIEW_MAX_FILES", 8) or 8)
        max_chars_per_file = int(getattr(config, "DEVAI_PATCH_REVIEW_MAX_CHARS_PER_FILE", 9000) or 9000)
        wanted = {str(path).replace("\\", "/").lstrip("/") for path in changed_files}
        out: dict[str, str] = {}
        if not zip_path or not zip_path.exists():
            return out
        try:
            with zipfile.ZipFile(zip_path) as zf:
                file_infos = [info for info in zf.infolist() if not info.is_dir()]
                for info in file_infos:
                    normalized = info.filename.replace("\\", "/").lstrip("/")
                    if wanted and normalized not in wanted and normalized.split("/", 1)[-1] not in wanted:
                        if not any(normalized.endswith("/" + item) for item in wanted):
                            continue
                    if len(out) >= max_files:
                        break
                    if info.file_size > int(getattr(config, "DEVAI_MAX_FILE_BYTES", 220000) or 220000):
                        out[normalized] = f"<arquivo grande demais para ler inteiro: {info.file_size} bytes>"
                        continue
                    try:
                        raw = zf.read(info)
                        text = raw.decode("utf-8", errors="replace")
                    except Exception as exc:
                        out[normalized] = f"<não consegui ler: {type(exc).__name__}: {exc}>"
                        continue
                    if len(text) > max_chars_per_file:
                        text = text[: max_chars_per_file // 2] + "\n\n# ... trecho central omitido ...\n\n" + text[-max_chars_per_file // 2 :]
                    out[normalized] = redact_secrets(text, max_chars=max_chars_per_file)
        except Exception:
            return out
        return out

    def _make_diff_block(self, new_files: dict[str, str]) -> str:
        """Computa diff unificado entre o conteúdo NOVO (do ZIP) e o que está
        no disco AGORA. Como a review roda DEPOIS do auto-updater já ter
        commitado, na maioria dos casos o disco == novo, e o diff fica vazio.
        Quando há diferença (ex: o updater não conseguiu aplicar tudo), o diff
        revela. Sempre que o arquivo é novo, mostra o diff contra vazio.

        Esse diff é muito mais informativo pra IA do que o arquivo todo,
        especialmente em arquivos grandes — modelos pequenos focam melhor
        com diff."""
        max_diff_chars = int(getattr(config, "DEVAI_PATCH_REVIEW_MAX_DIFF_CHARS", 14000) or 14000)
        blocks: list[str] = []
        for path, new_text in new_files.items():
            disk_path = self.repo_root / path
            old_text = ""
            if disk_path.exists() and disk_path.is_file():
                try:
                    old_text = disk_path.read_text("utf-8", errors="replace")
                except OSError:
                    old_text = ""
            if old_text == new_text:
                continue
            diff = difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                n=3,
            )
            joined = "".join(diff)
            if not joined:
                continue
            if len(joined) > max_diff_chars // max(1, len(new_files)):
                cap = max_diff_chars // max(1, len(new_files))
                # Corta SEMPRE em quebras de linha completas — corte no meio
                # de uma linha de código (ex: `BASE_DIR = os.path.di` →
                # `pname(...)`) gera fragmentos que IA interpreta como linhas
                # quebradas/erros de sintaxe.
                head_target = cap // 2
                tail_target = cap // 2
                head_end = joined.rfind("\n", 0, head_target)
                if head_end < head_target // 2:
                    head_end = head_target
                else:
                    head_end += 1
                tail_start = joined.find("\n", len(joined) - tail_target)
                if tail_start < 0 or tail_start > len(joined) - tail_target // 2:
                    tail_start = len(joined) - tail_target
                else:
                    tail_start += 1
                joined = (
                    joined[:head_end]
                    + "\n=== ⚠️ DIFF TRUNCADO AQUI — NÃO INFIRA O QUE FOI CORTADO ===\n"
                    + joined[tail_start:]
                )
            blocks.append(f"### diff: {path}\n```diff\n{joined}\n```")
        return "\n\n".join(blocks) if blocks else "Disco já está idêntico aos arquivos do ZIP — sem diff a mostrar."

    def _build_patch_review_prompt(
        self,
        *,
        changed_files: list[str],
        commit_hash: str | None,
        branch: str,
        zip_filename: str,
        triggered_update: bool,
        zip_files: dict[str, str],
        diff_block: str,
    ) -> str:
        index = self.indexer.load_or_build(max_age_seconds=int(getattr(config, "DEVAI_INDEX_MAX_AGE_SECONDS", 1800) or 1800))
        project_context = self.indexer.compact_context(index, max_chars=int(getattr(config, "DEVAI_MAX_INDEX_CHARS", 12000) or 12000))
        files_text = "\n".join(f"- {path}" for path in changed_files) or "- nenhum"

        # Histórico ajuda a IA notar tendências ("é a 3ª vez que mexem em
        # cogs/tts essa semana").
        history = self.patch_builder.recent_history(limit=4)
        history_block = self._format_history_block(history)

        full_files_block_parts: list[str] = []
        for path, content in zip_files.items():
            full_files_block_parts.append(f"### {path}\n```txt\n{content}\n```")
        full_files_block = "\n\n".join(full_files_block_parts) if full_files_block_parts else "Nenhum arquivo lido do ZIP."

        return f"""
METADADOS DO PATCH:
- ZIP: {zip_filename}
- Branch: {branch}
- Commit: {commit_hash or 'desconhecido'}
- Updater systemd encontrado: {'sim' if triggered_update else 'não'}

ARQUIVOS ALTERADOS:
{files_text}

DIFF UNIFICADO (o que efetivamente mudou no disco vs. ZIP):
{diff_block}

CONTEÚDO COMPLETO DOS ARQUIVOS DO ZIP (referência se o diff for insuficiente):
{full_files_block}

PATCHES RECENTES DA DEVAI:
{history_block}

ESTRUTURA RESUMIDA DO PROJETO:
```txt
{project_context}
```

REGRAS DE SAÍDA:
- Responda SOMENTE com JSON válido, sem markdown.
- Não gere arquivos novos — este fluxo é apenas comentário/revisão.
- Foque no diff. O conteúdo completo está só pra contexto.

SCHEMA EXATO DO JSON:
{REVIEW_JSON_SCHEMA}
""".strip()

    async def review_successful_patch(
        self,
        *,
        changed_files: list[str],
        commit_hash: str | None,
        branch: str,
        zip_filename: str,
        zip_path: Path | None = None,
        triggered_update: bool = False,
    ) -> None:
        if not self._enabled() or not bool(getattr(config, "DEVAI_PATCH_REVIEW_ENABLED", True)):
            return
        if self.ai is None or self.reporter is None or not self.reporter.available():
            return
        if not changed_files:
            return

        # Persiste o pedido ANTES de fazer o trabalho. Se o bot for morto pelo
        # systemd updater no meio do review, o próximo startup vê esta entrada
        # e retoma (usando git pra reconstruir o diff).
        pending_id = self._persist_pending_review(
            changed_files=changed_files,
            commit_hash=commit_hash,
            branch=branch,
            zip_filename=zip_filename,
            triggered_update=triggered_update,
        )

        try:
            await self._run_patch_review_inner(
                changed_files=changed_files,
                commit_hash=commit_hash,
                branch=branch,
                zip_filename=zip_filename,
                zip_path=zip_path,
                triggered_update=triggered_update,
            )
        finally:
            # Remove a entrada se o review terminou (com sucesso ou com
            # _report_patch_review_fallback). Só fica pendente se o bot foi
            # KILLED no meio (sem chance de chegar até o finally).
            if pending_id:
                self._remove_pending_review(pending_id)

    async def _run_patch_review_inner(
        self,
        *,
        changed_files: list[str],
        commit_hash: str | None,
        branch: str,
        zip_filename: str,
        zip_path: Path | None,
        triggered_update: bool,
    ) -> None:
        # Timeout duro pra review inteiro — sem isso, um provider lento poderia
        # segurar o `_analysis_lock` indefinidamente e bloquear o próximo
        # review (ex: review forçado disputando lock com resume pendente).
        timeout_s = float(getattr(config, "DEVAI_PATCH_REVIEW_TIMEOUT_SECONDS", 120) or 120)
        try:
            await asyncio.wait_for(
                self._run_patch_review_inner_locked(
                    changed_files=changed_files,
                    commit_hash=commit_hash,
                    branch=branch,
                    zip_filename=zip_filename,
                    zip_path=zip_path,
                    triggered_update=triggered_update,
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            log.error(
                "DevAI patch review: TIMEOUT após %ss (commit=%s zip=%s)",
                timeout_s, commit_hash, zip_filename,
            )
            try:
                await self._report_patch_review_fallback(
                    changed_files=changed_files,
                    commit_hash=commit_hash,
                    branch=branch,
                    zip_filename=zip_filename,
                    errors=[f"timeout após {timeout_s}s — provider muito lento ou travou"],
                )
            except Exception:
                log.exception("DevAI patch review: nem o fallback pôde ser enviado")

    async def _run_patch_review_inner_locked(
        self,
        *,
        changed_files: list[str],
        commit_hash: str | None,
        branch: str,
        zip_filename: str,
        zip_path: Path | None,
        triggered_update: bool,
    ) -> None:
        log.info(
            "DevAI patch review: iniciando (commit=%s files=%d zip=%s)",
            commit_hash, len(changed_files), zip_filename,
        )
        async with self._analysis_lock:
            zip_files = await asyncio.to_thread(self._read_zip_patch_context, zip_path or Path(), changed_files)
            # Se zip_files está vazio (zip já apagado, p.ex. retomada após
            # restart), tenta reconstruir lendo do disco — depois do systemd
            # updater, o disco tem o estado pós-patch e podemos comparar com
            # `git show <hash>~1:<file>` pra ter o estado pré-patch.
            if not zip_files:
                zip_files = await asyncio.to_thread(self._read_files_from_disk, changed_files)
            log.info("DevAI patch review: zip_files=%d arquivos", len(zip_files))
            # Diff: tenta o caminho normal (zip vs disco). Se vazio, recorre
            # ao git pra mostrar o que o commit mudou.
            diff_block = await asyncio.to_thread(self._make_diff_block, zip_files)
            if "Disco já está idêntico" in diff_block and commit_hash:
                git_diff = await asyncio.to_thread(self._git_diff_for_commit, commit_hash, changed_files)
                if git_diff:
                    diff_block = git_diff
                    log.info("DevAI patch review: usando git diff (disco idêntico ao zip)")

            prompt = await asyncio.to_thread(
                self._build_patch_review_prompt,
                changed_files=changed_files,
                commit_hash=commit_hash,
                branch=branch,
                zip_filename=zip_filename,
                triggered_update=triggered_update,
                zip_files=zip_files,
                diff_block=diff_block,
            )
            prompt = self._truncate_prompt_if_needed(prompt)
            log.info("DevAI patch review: prompt montado (%d chars), chamando IA…", len(prompt))
            # Review usa cadeia restrita (sem modelos médios que alucinam
            # remoções). Veja `review_provider_order()` em ai_client.py.
            result, errors = await self.ai.generate_patch_json(
                prompt,
                system=SYSTEM_PROMPT_REVIEW,
                provider_order=self.ai.review_provider_order(),
            )
            if result is None:
                log.warning(
                    "DevAI patch review: NENHUM PROVIDER respondeu (errors=%s)",
                    errors[-3:] if errors else "vazio",
                )
                await self._report_patch_review_fallback(
                    changed_files=changed_files,
                    commit_hash=commit_hash,
                    branch=branch,
                    zip_filename=zip_filename,
                    errors=errors,
                )
                return
            log.info(
                "DevAI patch review: provider %s respondeu em %dms (%d chars)",
                result.provider, result.elapsed_ms, len(result.text),
            )

            try:
                data = self.patch_builder.parse_ai_json(result.text)
            except Exception as exc:
                log.warning("DevAI patch review: JSON inválido de %s, tentando repair: %s", result.provider, exc)
                # Tenta repair também na review.
                if bool(getattr(config, "DEVAI_REPAIR_ENABLED", True)):
                    repair_result, repair_errors = await self.ai.repair_patch_json(
                        original_prompt=prompt,
                        bad_response=result.text,
                        error_message=str(exc),
                        system=SYSTEM_PROMPT_REVIEW,
                    )
                    if repair_result is not None:
                        try:
                            data = self.patch_builder.parse_ai_json(repair_result.text)
                            result = repair_result
                            log.info("DevAI patch review: repair bem-sucedido via %s", result.provider)
                        except Exception as exc2:
                            log.error("DevAI patch review: repair também devolveu JSON inválido: %s", exc2)
                            await self._report_patch_review_fallback(
                                changed_files=changed_files,
                                commit_hash=commit_hash,
                                branch=branch,
                                zip_filename=zip_filename,
                                errors=[
                                    f"JSON inválido do provider {result.provider}: {type(exc).__name__}: {exc}",
                                    f"repair também falhou: {type(exc2).__name__}: {exc2}",
                                ],
                            )
                            return
                    else:
                        log.error("DevAI patch review: repair não conseguiu chamar nenhum provider: %s", repair_errors[-3:])
                        await self._report_patch_review_fallback(
                            changed_files=changed_files,
                            commit_hash=commit_hash,
                            branch=branch,
                            zip_filename=zip_filename,
                            errors=[
                                f"JSON inválido do provider {result.provider}: {type(exc).__name__}: {exc}",
                                *(f"repair: {e}" for e in repair_errors[-3:]),
                            ],
                        )
                        return
                else:
                    log.error("DevAI patch review: JSON inválido e DEVAI_REPAIR_ENABLED=false")
                    await self._report_patch_review_fallback(
                        changed_files=changed_files,
                        commit_hash=commit_hash,
                        branch=branch,
                        zip_filename=zip_filename,
                        errors=[f"JSON inválido do provider {result.provider}: {type(exc).__name__}: {exc}"],
                    )
                    return

            log.info("DevAI patch review: enviando comentário pro webhook…")
            try:
                await asyncio.to_thread(self.indexer.build_index)
            except Exception:
                log.exception("DevAI patch review: falha re-indexando (ignorado, segue com comentário)")
            try:
                await self._report_patch_review(
                    data=data,
                    result=result,
                    changed_files=changed_files,
                    commit_hash=commit_hash,
                    branch=branch,
                    zip_filename=zip_filename,
                    triggered_update=triggered_update,
                )
                log.info("DevAI patch review: COMENTÁRIO ENVIADO COM SUCESSO (commit=%s)", commit_hash)
            except Exception:
                log.exception("DevAI patch review: falha enviando comentário pro webhook")
                # Última cartada: tenta o fallback simples.
                try:
                    await self._report_patch_review_fallback(
                        changed_files=changed_files,
                        commit_hash=commit_hash,
                        branch=branch,
                        zip_filename=zip_filename,
                        errors=["render do comentário falhou — veja bot.log"],
                    )
                except Exception:
                    log.exception("DevAI patch review: nem o fallback funcionou")

    @staticmethod
    def _is_empty_review(data: dict[str, Any]) -> bool:
        """Detecta quando um review veio com conteúdo essencialmente vazio.

        Acontece quando o fallback cai em modelo pequeno (ex: Cerebras
        Llama 3.1 8B) que segue o system prompt anti-alucinação à risca e,
        por não ter capacidade pra analisar diff, devolve JSON com todos os
        campos vazios. Isso é tecnicamente correto (admitir limitação >
        inventar), mas o comentário no Discord fica inútil — só mostra
        'Não informado pela IA' em todos os campos.

        A detecção checa se os 3 campos principais estão todos vazios ou
        com placeholders genéricos. Se sim, o cog renderiza o embed com
        título distinto que avisa o dono pra re-rodar `_devai review` quando
        o modelo principal estiver disponível.
        """
        what_changed = data.get("what_changed") or data.get("changes") or []
        effect = str(data.get("effect") or data.get("what_it_does") or "").strip().lower()
        cause = str(data.get("cause") or data.get("analysis") or "").strip().lower()
        summary = str(data.get("summary") or "").strip().lower()

        # what_changed vazio (lista vazia ou só strings vazias)
        wc_empty = not what_changed or all(
            not str(item).strip() for item in (what_changed if isinstance(what_changed, list) else [what_changed])
        )
        # effect/cause vazios ou genéricos
        generic_phrases = ("não informado", "n/a", "não disponível", "sem informação", "")
        effect_empty = not effect or any(p in effect for p in generic_phrases if p)
        cause_empty = not cause or any(p in cause for p in generic_phrases if p)
        summary_generic = (
            not summary
            or "patch comentado" in summary
            or "patch aplicado" in summary
            or len(summary) < 30
        )
        # 3 dos 4 indicadores vazios = fallback
        empty_count = sum([wc_empty, effect_empty, cause_empty, summary_generic])
        return empty_count >= 3

    async def _report_patch_review(
        self,
        *,
        data: dict[str, Any],
        result: AIResult,
        changed_files: list[str],
        commit_hash: str | None,
        branch: str,
        zip_filename: str,
        triggered_update: bool,
    ) -> None:
        if self.reporter is None or not self.reporter.available():
            return
        files = "\n".join(f"• `{p}`" for p in changed_files[:12])
        if len(changed_files) > 12:
            files += f"\n• ... e mais {len(changed_files) - 12} arquivo(s)"

        # Detecta review vazio (modelo pequeno respondeu mas não analisou).
        # Quando isso acontece, mostra título de aviso em vez de fingir que
        # tem conteúdo útil. Cor amarela igual ao fallback total.
        is_empty = self._is_empty_review(data)
        if is_empty:
            short_hash = str(commit_hash or "desconhecido")[:7]
            description = (
                f"**Provider:** `{result.provider}` · `{result.model}` · `{result.elapsed_ms} ms`\n"
                f"**ZIP:** `{redact_secrets(zip_filename, max_chars=120)}`\n"
                f"**Branch:** `{branch}` · **Commit:** `{short_hash}`\n"
                f"**Aplicação:** {'updater systemd deve aplicar automaticamente' if triggered_update else 'commit enviado, mas updater systemd não foi detectado'}\n\n"
                f"**Por que isso aconteceu**\n"
                f"O provider principal (Gemini Pro) provavelmente estava com rate limit ou 503, "
                f"então o fallback caiu em `{result.provider}` (`{result.model}`) — modelo "
                f"pequeno demais pra analisar diff complexo. Ele seguiu o system prompt "
                f"anti-alucinação corretamente: em vez de inventar, devolveu campos vazios.\n\n"
                f"**O que fazer**\n"
                f"Quando o Gemini Pro estiver disponível novamente, rode no canal:\n"
                f"```\n_devai review {short_hash}\n```\n"
                f"para gerar uma análise completa do mesmo commit.\n\n"
                f"**Arquivos alterados**\n{files}"
            )
            await self.reporter.send_report(
                title="⚠️ DevAI registrou patch (modelo pequeno não analisou)",
                description=description,
                color=0xFEE75C,
            )
            return

        what_changed = self._format_bullets(data.get("what_changed") or data.get("changes") or [], fallback="• Não informado pela IA.", max_items=8)
        recommendations = self._format_bullets(data.get("recommendations") or data.get("next_steps") or [], fallback="• Revisar logs após o deploy e validar o fluxo alterado.", max_items=6)
        tests = self._format_bullets(data.get("tests") or data.get("tests_to_run") or [], fallback="• Reiniciar o bot e testar manualmente os comandos/menus afetados.", max_items=6)
        short_hash = str(commit_hash or "desconhecido")[:7]
        description = (
            f"**Provider:** `{result.provider}` · `{result.model}` · `{result.elapsed_ms} ms`\n"
            f"**ZIP:** `{redact_secrets(zip_filename, max_chars=120)}`\n"
            f"**Branch:** `{branch}` · **Commit:** `{short_hash}`\n"
            f"**Aplicação:** {'updater systemd deve aplicar automaticamente' if triggered_update else 'commit enviado, mas updater systemd não foi detectado'}\n\n"
            f"**Resumo**\n{redact_secrets(str(data.get('summary') or 'Patch comentado pela DevAI.'), max_chars=900)}\n\n"
            f"**O que mudou**\n{what_changed}\n\n"
            f"**O que faz**\n{redact_secrets(str(data.get('effect') or data.get('what_it_does') or 'Não informado pela IA.'), max_chars=900)}\n\n"
            f"**Risco**\n`{redact_secrets(str(data.get('risk') or 'médio'), max_chars=80)}`\n\n"
            f"**Recomendações**\n{recommendations}\n\n"
            f"**Como validar**\n{tests}\n\n"
            f"**Arquivos alterados**\n{files}"
        )
        await self.reporter.send_report(
            title="🧠 DevAI comentou um patch aplicado com sucesso",
            description=description,
            color=0x57F287,
        )

    async def _report_patch_review_fallback(
        self,
        *,
        changed_files: list[str],
        commit_hash: str | None,
        branch: str,
        zip_filename: str,
        errors: list[str],
    ) -> None:
        if self.reporter is None or not self.reporter.available():
            return
        files = "\n".join(f"• `{p}`" for p in changed_files[:12])
        err_text = "\n".join(f"• {redact_secrets(err, max_chars=450)}" for err in errors[-6:]) or "• nenhum detalhe"
        await self.reporter.send_report(
            title="🧠 DevAI registrou patch, mas não conseguiu comentar com IA",
            description=(
                f"**ZIP:** `{redact_secrets(zip_filename, max_chars=120)}`\n"
                f"**Branch:** `{branch}` · **Commit:** `{str(commit_hash or 'desconhecido')[:7]}`\n\n"
                f"**Arquivos alterados**\n{files}\n\n"
                f"**Falhas dos providers**\n{err_text}\n\n"
                "Recomendação padrão: revisar logs após o deploy e testar os fluxos dos arquivos alterados."
            ),
            color=0xFEE75C,
        )

    # ----- persistência de reviews pendentes (sobrevive a restart) -----

    @property
    def _pending_reviews_path(self) -> Path:
        return self.data_dir / "pending_reviews.jsonl"

    def _persist_pending_review(
        self,
        *,
        changed_files: list[str],
        commit_hash: str | None,
        branch: str,
        zip_filename: str,
        triggered_update: bool,
    ) -> str:
        """Append review request to pending queue. Returns ID for later removal.

        Sobrevive a restart do bot: se o systemd updater matar o processo no
        meio do review, esta entrada continua e `_resume_pending_reviews` vai
        retomá-la no próximo startup, usando `git show` pra reconstruir o
        diff (já que o ZIP original foi removido).
        """
        import time as _time
        import uuid as _uuid
        entry_id = _uuid.uuid4().hex[:12]
        record = {
            "id": entry_id,
            "created_at": _time.time(),
            "changed_files": list(changed_files),
            "commit_hash": commit_hash,
            "branch": branch,
            "zip_filename": zip_filename,
            "triggered_update": bool(triggered_update),
        }
        try:
            self._pending_reviews_path.parent.mkdir(parents=True, exist_ok=True)
            with self._pending_reviews_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(record, ensure_ascii=False) + "\n")
            return entry_id
        except OSError:
            log.warning("DevAI: falha ao persistir review pendente (vai rodar mas não sobrevive a restart)", exc_info=True)
            return ""

    def _remove_pending_review(self, entry_id: str) -> None:
        """Remove entry by ID. Reescreve o arquivo sem ela."""
        if not entry_id:
            return
        path = self._pending_reviews_path
        if not path.exists():
            return
        try:
            lines = path.read_text("utf-8", errors="replace").splitlines()
            kept: list[str] = []
            for line in lines:
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                try:
                    rec = json.loads(line_stripped)
                except json.JSONDecodeError:
                    kept.append(line)
                    continue
                if rec.get("id") == entry_id:
                    continue  # remove
                kept.append(line)
            if kept:
                path.write_text("\n".join(kept) + "\n", "utf-8")
            else:
                path.unlink(missing_ok=True)
        except OSError:
            log.warning("DevAI: falha ao remover review pendente %s", entry_id, exc_info=True)

    async def _resume_pending_reviews(self) -> None:
        """Lê pending_reviews.jsonl e roda cada review remanescente.

        Chamado uma vez no `cog_load`. Reviews antigas (>24h) são descartadas
        pra não enviar comentário sobre patch que o dono já esqueceu. Cada
        review usa `git show` pra reconstruir o diff já que o ZIP original
        foi apagado pelo finally do bot.py.
        """
        path = self._pending_reviews_path
        if not path.exists():
            return
        # Pequeno delay pra dar tempo do bot conectar ao Discord — sem isso,
        # a primeira chamada ao webhook pode falhar com "session not started".
        await asyncio.sleep(8)
        try:
            lines = path.read_text("utf-8", errors="replace").splitlines()
        except OSError:
            return

        import time as _time
        cutoff = _time.time() - 24 * 3600  # 24h
        pending: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if float(rec.get("created_at") or 0) < cutoff:
                continue  # muito velho — descarta
            pending.append(rec)

        if not pending:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return

        log.info("DevAI: retomando %d review(s) pendente(s) após restart", len(pending))
        for rec in pending:
            entry_id = str(rec.get("id") or "")
            try:
                # Avisa no webhook que esta é uma retomada — útil pra debug.
                if self.reporter is not None and self.reporter.available():
                    try:
                        await self.reporter.send_plain(
                            f"🔄 DevAI retomando review do commit `{str(rec.get('commit_hash') or '?')[:7]}` "
                            f"(interrompido por restart)",
                            username="DevAI",
                        )
                    except Exception:
                        pass

                await self._run_patch_review_inner(
                    changed_files=list(rec.get("changed_files") or []),
                    commit_hash=str(rec.get("commit_hash") or "") or None,
                    branch=str(rec.get("branch") or "main"),
                    zip_filename=str(rec.get("zip_filename") or "patch.zip"),
                    zip_path=None,  # sem ZIP — vai cair no path do git diff
                    triggered_update=bool(rec.get("triggered_update")),
                )
            except Exception:
                log.exception("DevAI: falha retomando review pendente %s", entry_id)
            finally:
                if entry_id:
                    self._remove_pending_review(entry_id)

    # ----- leitura alternativa: do disco e via git ---------------------

    def _read_files_from_disk(self, rel_paths: list[str]) -> dict[str, str]:
        """Lê os arquivos diretamente do disco (estado atual). Usado quando
        o ZIP original já não existe — após restart, o disco já contém o
        estado pós-patch e podemos usar isso como referência."""
        out: dict[str, str] = {}
        max_chars_per_file = int(getattr(config, "DEVAI_PATCH_REVIEW_MAX_CHARS_PER_FILE", 9000) or 9000)
        for raw in rel_paths:
            try:
                rel = str(raw).replace("\\", "/").lstrip("/")
                full = self.repo_root / rel
                if not full.exists() or not full.is_file():
                    continue
                text = full.read_text("utf-8", errors="replace")
                if len(text) > max_chars_per_file:
                    text = text[: max_chars_per_file // 2] + "\n\n# ... trecho central omitido ...\n\n" + text[-max_chars_per_file // 2 :]
                out[rel] = redact_secrets(text, max_chars=max_chars_per_file)
            except OSError:
                continue
        return out

    def _git_diff_for_commit(self, commit_hash: str, changed_files: list[str]) -> str:
        """Reconstrói o diff exato que esse commit aplicou usando
        `git show <hash> -- <files>`. Útil quando o ZIP original foi removido
        (ex: review está sendo retomada após restart do bot)."""
        import subprocess
        if not commit_hash:
            return ""
        commit = commit_hash.strip()
        if not commit or len(commit) < 4:
            return ""
        # Sanitiza pra evitar argumentos injetados em commit_hash exótico.
        if not all(c.isalnum() for c in commit):
            return ""
        max_diff_chars = int(getattr(config, "DEVAI_PATCH_REVIEW_MAX_DIFF_CHARS", 14000) or 14000)
        cmd = ["git", "-C", str(self.repo_root), "show", "--no-color", commit, "--"]
        cmd.extend(str(p).replace("\\", "/").lstrip("/") for p in changed_files[:20])
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ""
        if proc.returncode != 0:
            return ""
        out = proc.stdout or ""
        # `git show` mostra header (autor, data, msg) + diff. Pega só do
        # primeiro `diff --git` em diante pra a IA focar nas mudanças.
        idx = out.find("diff --git")
        if idx > 0:
            out = out[idx:]
        out = redact_secrets(out, max_chars=max_diff_chars)
        if len(out) > max_diff_chars:
            head_target = max_diff_chars // 2
            tail_target = max_diff_chars // 2
            head_end = out.rfind("\n", 0, head_target)
            if head_end < head_target // 2:
                head_end = head_target
            else:
                head_end += 1
            tail_start = out.find("\n", len(out) - tail_target)
            if tail_start < 0 or tail_start > len(out) - tail_target // 2:
                tail_start = len(out) - tail_target
            else:
                tail_start += 1
            out = (
                out[:head_end]
                + "\n=== ⚠️ DIFF TRUNCADO AQUI — NÃO INFIRA O QUE FOI CORTADO ===\n"
                + out[tail_start:]
            )
        return f"### diff reconstruído via `git show {commit[:12]}`\n```diff\n{out}\n```"

    # ---------------------------------------------------------- Discord listeners

    def _truncate_prompt_if_needed(self, prompt: str) -> str:
        """Garante que o prompt cabe no limite do free tier de cada provider.

        IMPORTANTE: corta SEMPRE em quebras de linha completas. Cortar no
        meio de uma linha (como `BASE_DIR = os.path.di` → `pname(...)`)
        gera fragmentos que IA interpreta como linhas adicionadas/quebradas
        (`+me(os.path.abspath(...))`) e dispara alucinações de "código
        corrompido". Esse foi o bug que causou Gemini Pro a alucinar
        "patch corrompido, reverter imediatamente" sobre código intacto.

        Free tiers comuns (May/2026):
        - Groq: TPM 8000 (~32k chars total, mas 16k são output → ~14k prompt)
        - Cloudflare Workers AI: max context 32768 tokens
        - Cerebras llama3.1-8b: TPM 60k
        - Gemini 2.5 Pro: 250K TPM (sem problema)
        """
        max_chars = int(getattr(config, "DEVAI_MAX_PROMPT_CHARS", 14000) or 14000)
        if len(prompt) <= max_chars:
            return prompt
        # Estratégia: 60% pro head (instruções, schema), 40% pro tail (final
        # do diff e instrução de output). Sempre alinhar a quebras de linha.
        head_target = max_chars * 6 // 10
        tail_target = max_chars - head_target - 250  # marker tem ~250 chars

        # Encontra quebra de linha mais próxima do alvo no head (corta APÓS
        # uma quebra completa).
        head_end = prompt.rfind("\n", 0, head_target)
        if head_end < head_target // 2:
            # Sem quebra próxima — usa o alvo bruto (ruim mas evita head vazio).
            head_end = head_target
        else:
            head_end += 1  # inclui o \n

        # Encontra quebra de linha mais próxima do alvo no tail (começa após
        # uma quebra completa).
        tail_start = prompt.find("\n", len(prompt) - tail_target)
        if tail_start < 0 or tail_start > len(prompt) - tail_target // 2:
            tail_start = len(prompt) - tail_target
        else:
            tail_start += 1  # pula o \n inicial

        truncated = (
            prompt[:head_end]
            + f"\n\n=== ⚠️ TRECHO DO MEIO TRUNCADO — {tail_start - head_end} chars cortados pra caber no rate limit. NÃO INFIRA O QUE FOI CORTADO; comente apenas o que está visível. Linhas que parecem incompletas no FIM/INÍCIO de cada metade são por causa do CORTE, não do código real. ===\n\n"
            + prompt[tail_start:]
        )
        log.info(
            "DevAI: prompt cortado de %d → %d chars (head=%d tail=%d limite=%d)",
            len(prompt), len(truncated), head_end, len(prompt) - tail_start, max_chars,
        )
        return truncated

    @commands.Cog.listener("on_message")
    async def _devai_comment_listener(self, message: discord.Message):
        if getattr(message.author, "bot", False):
            return
        if not self._enabled():
            return

        # Owner check tem nuance: se a mensagem está no canal de comentário
        # configurado (que normalmente é privado/restrito ao dono), aceitamos
        # sem checar bot.is_owner — porque is_owner pode falhar se a
        # application info ainda não foi carregada (dá False silenciosamente
        # logo depois do startup) OU se OWNER_ID estiver vazio no .env.
        # Em qualquer outro canal, a checagem é estrita.
        channel_id_cfg = int(getattr(config, "DEVAI_COMMENT_CHANNEL_ID", 0) or 0)
        msg_channel_id = int(getattr(message.channel, "id", 0) or 0)
        in_comment_channel = channel_id_cfg != 0 and msg_channel_id == channel_id_cfg

        if not in_comment_channel:
            if not await self._is_ownerish(message.author):
                return

        content = (message.content or "").strip()
        if not content:
            return
        lower = content.lower()

        # ---- detecção de gatilhos ---------------------------------------
        # 1) Mention real do bot (autocompletada pelo Discord como <@id>).
        bot_user = getattr(self.bot, "user", None)
        bot_mentioned = bool(bot_user) and bot_user in (message.mentions or [])

        # 2) Reply pra alguma mensagem da DevAI. Detecção em 3 camadas
        #    (cada uma cobre o gap da anterior):
        #    (a) `message.reference.resolved` — populado pelo gateway, zero
        #        API calls, sobrevive a restart do bot. Verifica `webhook_id`
        #        contra o ID extraído de DEVAI_WEBHOOK_URL.
        #    (b) sets em RAM (`_chat_message_ids`/`_report_message_ids`) —
        #        cobre o caso raro do gateway não enviar `resolved`.
        #    (c) fetch_message ativo — último recurso, requer Read Message
        #        History no canal.
        ref = getattr(message, "reference", None)
        ref_id = int(getattr(ref, "message_id", 0) or 0) if ref is not None else 0
        is_reply_to_report = False
        is_reply_to_chat = False
        reply_diag = "no-ref"
        if ref_id:
            classification = self._classify_reply_via_resolved(message)
            if classification == "report":
                is_reply_to_report = True
                reply_diag = "resolved-report"
            elif classification == "chat":
                is_reply_to_chat = True
                reply_diag = "resolved-chat"
            # Camada (b): sets em RAM.
            if not (is_reply_to_report or is_reply_to_chat):
                if ref_id in self._report_message_ids:
                    is_reply_to_report = True
                    reply_diag = "ram-report"
                elif ref_id in self._chat_message_ids:
                    is_reply_to_chat = True
                    reply_diag = "ram-chat"
            # Camada (c): fetch ativo — só vai aqui se as outras falharam.
            if not (is_reply_to_report or is_reply_to_chat):
                classification = await self._classify_reply_via_webhook(message, ref_id)
                if classification == "report":
                    is_reply_to_report = True
                    reply_diag = "fetch-report"
                elif classification == "chat":
                    is_reply_to_chat = True
                    reply_diag = "fetch-chat"
                else:
                    reply_diag = "ref-not-devai"

        # 3) Prefixo textual ou @DevAI literal — só conta no canal de
        #    comentário pra não vazar pra outros canais.
        text_prefix = (
            lower.startswith("devai")
            or lower.startswith("@devai")
            or lower.startswith("@dev ai")  # tolera quebra natural
            or lower.startswith("ia ")
            or lower == "ia"
        ) and in_comment_channel

        triggered = bot_mentioned or is_reply_to_report or is_reply_to_chat or text_prefix

        # Logging de diagnóstico — sempre que está no canal de comentário,
        # registra o que o listener viu. Você vê em logs/bot.log e o LogWatcher
        # da DevAI também pode ver. Isso é a chave pra debugar mensagens
        # ignoradas: se você manda algo e nada acontece, esse log conta o porquê.
        if in_comment_channel or bot_mentioned:
            log.info(
                "DevAI listener: ch=%s author=%s wh_id=%s ref_id=%s reply_diag=%s "
                "mentioned=%s prefix=%s triggered=%s | %r",
                msg_channel_id,
                int(getattr(message.author, "id", 0) or 0),
                self._devai_webhook_id(),
                ref_id,
                reply_diag,
                bot_mentioned,
                text_prefix,
                triggered,
                content[:80],
            )

        if not triggered:
            return

        # Limpa prefixo/mention pra deixar só a pergunta/comentário.
        cleaned = self._strip_prefix_and_mentions(content)
        if not cleaned and not (is_reply_to_report or is_reply_to_chat):
            cleaned = content  # fallback

        # ---- decisão de modo --------------------------------------------
        # Reply em RELATÓRIO de erro/patch → modo re-analyze (gera ZIP novo).
        # SÓ vai pra esse caminho se ainda temos o LogEvent associado em RAM.
        # Se perdemos (restart), cai pro modo chat — usuário ainda recebe
        # uma resposta útil em vez de silêncio.
        if is_reply_to_report:
            event = self._last_event_by_message.get(ref_id)
            if event is None and self._last_event_by_message:
                event = list(self._last_event_by_message.values())[-1]
            if event is not None:
                try:
                    await message.add_reaction("🧠")
                except Exception:
                    pass
                asyncio.create_task(self._analyze_event(event, comment=cleaned))
                return
            # Sem evento associado (reinício do bot, p.ex.) — cai pro chat.

        # Caso contrário (mention, prefixo, ou reply em CHAT/qualquer DevAI):
        # conversa livre. A reação entra/sai dentro de `_chat_with_owner`.
        asyncio.create_task(self._chat_with_owner(message, cleaned))

    def _classify_reply_via_resolved(self, message: discord.Message) -> str:
        """Detecção de reply usando `message.reference.resolved`.

        O resolved é populado automaticamente pelo gateway do Discord quando
        a mensagem citada existe — sem precisar fazer fetch_message (sem custo
        de API, sem precisar de Read Message History). Funciona em ~100% dos
        replies em condições normais.

        Retorna "report"/"chat"/"none" igual a `_classify_reply_via_webhook`.
        """
        ref = getattr(message, "reference", None)
        if ref is None:
            return "none"
        resolved = getattr(ref, "resolved", None)
        # discord.py também expõe `cached_message` como fallback antigo.
        if resolved is None:
            resolved = getattr(ref, "cached_message", None)
        if resolved is None:
            return "none"
        # `resolved` pode ser DeletedReferencedMessage — sem webhook_id.
        if not hasattr(resolved, "webhook_id"):
            return "none"
        devai_wh_id = self._devai_webhook_id()
        if not devai_wh_id:
            return "none"
        msg_wh_id = int(getattr(resolved, "webhook_id", 0) or 0)
        if msg_wh_id != devai_wh_id:
            return "none"
        # É do nosso webhook. Tem embed → relatório; texto puro → chat.
        if getattr(resolved, "embeds", None):
            return "report"
        return "chat"

    async def _classify_reply_via_webhook(self, message: discord.Message, ref_id: int) -> str:
        """Verifica se a mensagem citada veio do webhook DevAI. Sobrevive a
        restart do bot (não depende dos sets em RAM).

        Retorna:
            "report" - mensagem citada é embed do webhook DevAI (relatório)
            "chat"   - mensagem citada é texto do webhook DevAI (chat anterior)
            "none"   - mensagem citada não é da DevAI ou não conseguimos ver
        """
        devai_wh_id = self._devai_webhook_id()
        if not devai_wh_id:
            return "none"
        # cached_message é raríssimo pra webhook — fetch ativo.
        try:
            fetched = await message.channel.fetch_message(ref_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return "none"
        except Exception:
            return "none"
        msg_wh_id = int(getattr(fetched, "webhook_id", 0) or 0)
        if msg_wh_id != devai_wh_id:
            return "none"
        # É do nosso webhook. Heurística pra distinguir relatório de chat:
        # relatórios são enviados via send_report() com embed; chat usa
        # send_plain() sem embed. Se tem embed → tratamos como relatório
        # (mas a re-análise só dispara se houver LogEvent em RAM).
        if getattr(fetched, "embeds", None):
            return "report"
        return "chat"

    def _strip_prefix_and_mentions(self, content: str) -> str:
        """Remove `devai:`, `ia:`, `@DevAI`, `@dev ai` no começo e mentions
        reais do bot pra deixar a pergunta limpa."""
        # Remove mention real do bot (formato <@id> ou <@!id>).
        bot_user = getattr(self.bot, "user", None)
        bot_id = int(getattr(bot_user, "id", 0) or 0)
        if bot_id:
            content = re.sub(rf"<@!?{bot_id}>", "", content)
        # Remove `@DevAI`/`@dev ai` literal (texto puro) no começo. Tolera
        # múltiplos seguidos e espaço entre "dev" e "ai".
        content = re.sub(r"^\s*(?:@dev\s*ai\s*)+", "", content, flags=re.I)
        # Remove prefixo textual `devai:`, `ia:`, etc.
        content = re.sub(r"^\s*(devai|ia)[:,\s-]*", "", content, flags=re.I)
        return content.strip()

    async def _chat_with_owner(self, message: discord.Message, question: str) -> None:
        """Modo conversa: monta um prompt com contexto do projeto + opcional
        contexto do erro/patch citado e responde em texto puro.

        Resposta sai via webhook gerenciado da DevAI (mesma identidade visual
        que os relatórios), com reação de "processando" 🧠 enquanto a IA
        pensa — removida assim que a resposta vai pro canal.
        """
        if self.ai is None:
            return

        # Busca a mensagem citada AGORA (em async) pra ter o conteúdo. O
        # `cached_message` quase nunca tem mensagens de webhook, então fazemos
        # fetch ativo. Custo: 1 HTTP call quando há reply.
        referenced_text = await self._fetch_referenced_text(message)

        # Reação de processamento: entra antes de chamar a IA, sai depois.
        reaction_used = await self._add_processing_reaction(message)

        try:
            try:
                prompt = await asyncio.to_thread(
                    self._build_chat_prompt,
                    message=message,
                    question=question,
                    referenced_text=referenced_text,
                )
                prompt = self._truncate_prompt_if_needed(prompt)
            except Exception as exc:
                log.exception("DevAI chat: falha montando prompt")
                await self._send_chat_response(
                    message,
                    f"⚠️ Não consegui montar o contexto: `{type(exc).__name__}: {exc}`",
                )
                return

            try:
                result, errors = await self.ai.chat_freeform(prompt, system=SYSTEM_PROMPT_CHAT)
            except Exception as exc:
                log.exception("DevAI chat: falha chamando IA")
                await self._send_chat_response(
                    message,
                    f"⚠️ Falha ao chamar a IA: `{type(exc).__name__}: {exc}`",
                )
                return

            if result is None or not result.text.strip():
                err_text = "; ".join(errors[-3:]) if errors else "sem detalhe"
                await self._send_chat_response(
                    message,
                    f"⚠️ Nenhum provider respondeu. Últimos erros: `{redact_secrets(err_text, max_chars=400)}`",
                )
                return

            text = redact_secrets(result.text.strip(), max_chars=3500)
            footer = f"\n\n_— {result.provider} · `{result.model}` · {result.elapsed_ms} ms_"
            await self._send_chat_response(message, text + footer)
        finally:
            # Garante que a reação some mesmo se algum send falhar.
            if reaction_used is not None:
                await self._remove_processing_reaction(message, reaction_used)

    async def _fetch_referenced_text(self, message: discord.Message) -> str:
        """Quando a mensagem é reply, retorna o conteúdo da mensagem citada
        (incluindo respostas anteriores da DevAI via webhook). Tenta:
        1. `reference.cached_message.content` (rápido, mas raro pra webhook)
        2. `channel.fetch_message(reference.message_id)` (1 HTTP call)
        Volta string vazia se nada deu certo."""
        ref = getattr(message, "reference", None)
        if ref is None:
            return ""
        cached = getattr(ref, "cached_message", None)
        if cached is not None and getattr(cached, "content", None):
            return str(cached.content)
        ref_id = int(getattr(ref, "message_id", 0) or 0)
        if not ref_id:
            return ""
        try:
            fetched = await message.channel.fetch_message(ref_id)
            return str(getattr(fetched, "content", "") or "")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return ""
        except Exception:
            return ""

    # ----- reações de processamento (mesmo padrão do cogs/chatbot) -----

    _PROCESSING_REACTIONS: tuple[str, ...] = ("🧠", "⏳")

    async def _add_processing_reaction(self, message: discord.Message) -> str | None:
        """Tenta colocar 🧠 na mensagem do usuário. Cai pra ⏳ se falhar.
        Retorna a string aplicada (pra `_remove_processing_reaction` saber qual
        tirar) ou None se nenhuma deu certo."""
        for emoji in self._PROCESSING_REACTIONS:
            try:
                await message.add_reaction(emoji)
                return emoji
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                continue
            except Exception:
                continue
        return None

    async def _remove_processing_reaction(self, message: discord.Message, emoji: str | None) -> None:
        """Remove a reação aplicada por `_add_processing_reaction`. Silencioso
        em qualquer falha (mensagem deletada, perda de permissão, etc)."""
        if not emoji:
            return
        try:
            me = self.bot.user
            if me is None:
                return
            await message.remove_reaction(emoji, me)
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            pass
        except Exception:
            pass

    # ----- envio via webhook configurado da DevAI ---------------------

    async def _send_chat_response(self, message: discord.Message, content: str) -> None:
        """Envia resposta de chat com a identidade DevAI usando o webhook
        configurado em `DEVAI_WEBHOOK_URL`.

        Faz chunking automático em pedaços de 1900 chars. Cada mensagem
        enviada tem o ID guardado em `_chat_message_ids` pra que o user possa
        REPLICAR a mensagem da DevAI e a conversa continuar (em vez do listener
        ignorar por não reconhecer o reference).

        Estratégia:
        1. Webhook fixo do `.env` (canal de relatórios — onde o dono fala
           normalmente com a DevAI).
        2. Fallback: `message.reply` no canal original se o webhook falhar.
        """
        chunks: list[str] = []
        remaining = content
        while remaining:
            chunks.append(remaining[:1900])
            remaining = remaining[1900:]
        if not chunks:
            return

        # 1) Webhook configurado (DEVAI_WEBHOOK_URL).
        sent_via_webhook = False
        if self.reporter is not None and self.reporter.available():
            try:
                for chunk in chunks:
                    sent = await self.reporter.send_plain(chunk, username="DevAI")
                    if sent is not None:
                        # Registra pro listener reconhecer reply na resposta.
                        self._chat_message_ids.add(int(sent.id))
                        sent_via_webhook = True
                if sent_via_webhook:
                    return
            except Exception:
                log.exception("DevAI chat: falha enviando pelo webhook configurado")

        # 2) Fallback final: resposta como bot no canal original.
        try:
            first = True
            for chunk in chunks:
                if first:
                    sent = await message.reply(chunk, mention_author=False)
                    first = False
                else:
                    sent = await message.channel.send(chunk)
                if sent is not None:
                    self._chat_message_ids.add(int(sent.id))
        except Exception:
            log.exception("DevAI chat: todos os caminhos de envio falharam")

    def _build_chat_prompt(self, *, message: discord.Message, question: str, referenced_text: str = "") -> str:
        """Monta o prompt do modo chat. Inclui:
        - estrutura compacta do projeto
        - últimos 3 patches (pra "lembrar" o que aconteceu)
        - se a mensagem for reply de algo (incluindo resposta anterior da
          DevAI via webhook), o conteúdo da mensagem referenciada — passada
          em `referenced_text` pelo `_chat_with_owner` (que faz fetch async).
        """
        index = self.indexer.load_or_build(
            max_age_seconds=int(getattr(config, "DEVAI_INDEX_MAX_AGE_SECONDS", 1800) or 1800)
        )
        project_context = self.indexer.compact_context(
            index,
            max_chars=int(getattr(config, "DEVAI_MAX_INDEX_CHARS", 12000) or 12000),
        )
        history = self.patch_builder.recent_history(limit=3)
        history_block = self._format_history_block(history)

        ref_block = ""
        ref = getattr(message, "reference", None)
        if ref is not None:
            ref_id = int(getattr(ref, "message_id", 0) or 0)
            event = self._last_event_by_message.get(ref_id)
            if event is not None:
                # Reply num relatório de erro/patch — não chega aqui no fluxo
                # normal (re-analyze é tratado antes), mas mantemos por
                # robustez se alguém forçar.
                ref_block = (
                    "MENSAGEM CITADA (relatório de erro/patch):\n"
                    f"- Assinatura: {event.signature}\n"
                    f"- Fonte: {event.source}\n"
                    f"```txt\n{redact_secrets(event.text, max_chars=4000)}\n```\n\n"
                )
            elif referenced_text:
                # Reply numa mensagem de chat anterior da DevAI ou em
                # qualquer outra coisa relevante.
                label = (
                    "MENSAGEM CITADA (resposta anterior da DevAI)"
                    if ref_id in self._chat_message_ids
                    else "MENSAGEM CITADA"
                )
                ref_block = (
                    f"{label}:\n"
                    f"```\n{redact_secrets(referenced_text, max_chars=2500)}\n```\n\n"
                )

        return f"""
PERGUNTA DO DONO:
{redact_secrets(question, max_chars=2500)}

{ref_block}PATCHES RECENTES:
{history_block}

ESTRUTURA RESUMIDA DO PROJETO:
```txt
{project_context}
```

Responda em texto puro (markdown leve permitido). Não devolva JSON.
""".strip()

    # ----------------------------------------------------- alerta externo

    async def notify_external_event(self, *, source: str, text: str, signature_hint: str = "") -> None:
        """Hook público: o bot.py chama isso quando detecta uma falha que NÃO
        passou pelos arquivos de log (ex: erro do auto-update que só virou
        `print` ou exception capturada antes do logger).

        Cria um LogEvent sintético e enfileira pra análise normal.
        """
        if not self._enabled() or self.watcher is None:
            return
        import hashlib
        sig = signature_hint or hashlib.sha256(
            (source + "|" + text[:1000]).encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        # Usa o LogWatcher pra deduplicar: se essa assinatura já apareceu nos
        # últimos 15 min, ele ignora.
        now = time.time()
        last = self.watcher.recent_signatures.get(sig, 0.0)
        if now - last < 900:
            return
        self.watcher.recent_signatures[sig] = now
        event = LogEvent(
            source=source,
            text=redact_secrets(text, max_chars=int(getattr(config, "DEVAI_MAX_LOG_CHARS", 18000) or 18000)),
            signature=sig,
            file_paths=[],
            created_at=now,
        )
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("DevAI: fila cheia, alerta externo ignorado: %s", sig)

    def _is_reply_to_report(self, message: discord.Message) -> bool:
        ref = getattr(message, "reference", None)
        if ref is None:
            return False
        mid = int(getattr(ref, "message_id", 0) or 0)
        return mid in self._report_message_ids

    # ---------------------------------------------------------- Discord commands

    @commands.group(name="devai", hidden=True, invoke_without_command=True)
    async def devai_group(self, ctx: commands.Context):
        if not await self._is_ownerish(ctx.author):
            return
        await ctx.reply(
            "DevAI: `_devai status`, `_devai providers`, `_devai diag`, `_devai scan`, `_devai index`, `_devai ask <pergunta>`.",
            mention_author=False,
        )

    @devai_group.command(name="status")
    async def devai_status(self, ctx: commands.Context):
        if not await self._is_ownerish(ctx.author):
            return
        providers = ", ".join(getattr(config, "DEVAI_PROVIDER_ORDER", []) or [])
        await ctx.reply(
            f"**DevAI**\n"
            f"Enabled: `{self._enabled()}`\n"
            f"Webhook: `{bool(getattr(config, 'DEVAI_WEBHOOK_URL', ''))}`\n"
            f"Providers: `{providers}`\n"
            f"Fila: `{self.queue.qsize()}`\n"
            f"Gerados: `{self.generated_dir}`",
            mention_author=False,
        )

    @devai_group.command(name="providers")
    async def devai_providers(self, ctx: commands.Context):
        """Mostra estatísticas de cada provider — útil pra ver qual está
        falhando muito ou se a chave tá ausente."""
        if not await self._is_ownerish(ctx.author):
            return
        if self.ai is None:
            await ctx.reply("DevAI ainda não inicializou.", mention_author=False)
            return

        order = self.ai.provider_order()
        stats = self.ai.stats_summary()
        lines = ["**DevAI providers:**"]
        for name in order:
            st = stats.get(name)
            model = self._configured_model_for(name)
            key_present = self._has_credentials_for(name)
            line = f"• `{name}` → `{model or '?'}`  "
            line += f"key={'✅' if key_present else '❌'}  "
            if st:
                line += f"ok={st['success']} err={st['failure']} "
                if st["last_latency_ms"]:
                    line += f"({st['last_latency_ms']} ms) "
                if st["last_error"]:
                    line += f"último_erro=`{st['last_error']}`"
            else:
                line += "(ainda não usado)"
            lines.append(line)
        await ctx.reply("\n".join(lines)[:1900], mention_author=False)

    def _configured_model_for(self, name: str) -> str:
        mapping = {
            "gemini": "DEVAI_GEMINI_MODEL",
            "groq": "DEVAI_GROQ_MODEL",
            "openrouter": "DEVAI_OPENROUTER_MODEL",
            "cerebras": "DEVAI_CEREBRAS_MODEL",
            "cloudflare": "DEVAI_CLOUDFLARE_MODEL",
            "huggingface": "DEVAI_HUGGINGFACE_MODEL",
            "pollinations": "DEVAI_POLLINATIONS_MODEL",
        }
        attr = mapping.get(name)
        if not attr:
            return ""
        return str(getattr(config, attr, "") or "")

    def _has_credentials_for(self, name: str) -> bool:
        if name == "gemini":
            return bool(getattr(config, "GEMINI_API_KEY", "") or os.getenv("GEMINI_API_KEY", ""))
        if name == "groq":
            return bool(getattr(config, "GROQ_API_KEY", "") or os.getenv("GROQ_API_KEY", ""))
        if name == "openrouter":
            return bool(getattr(config, "OPENROUTER_API_KEY", "") or os.getenv("OPENROUTER_API_KEY", ""))
        if name == "cerebras":
            return bool(getattr(config, "CEREBRAS_API_KEY", "") or os.getenv("CEREBRAS_API_KEY", ""))
        if name == "cloudflare":
            return bool(
                (getattr(config, "CLOUDFLARE_API_TOKEN", "") or os.getenv("CLOUDFLARE_API_TOKEN", ""))
                and (getattr(config, "CLOUDFLARE_ACCOUNT_ID", "") or os.getenv("CLOUDFLARE_ACCOUNT_ID", ""))
            )
        if name == "huggingface":
            return bool(getattr(config, "HUGGINGFACE_API_KEY", "") or os.getenv("HUGGINGFACE_API_KEY", ""))
        if name == "pollinations":
            return bool(getattr(config, "POLLINATIONS_API_KEY", "") or os.getenv("POLLINATIONS_API_KEY", ""))
        return False

    @devai_group.command(name="index")
    async def devai_index(self, ctx: commands.Context):
        if not await self._is_ownerish(ctx.author):
            return
        index = await asyncio.to_thread(self.indexer.build_index)
        await ctx.reply(f"Índice atualizado: `{index.get('file_count', 0)}` arquivo(s).", mention_author=False)

    @devai_group.command(name="scan")
    async def devai_scan(self, ctx: commands.Context):
        if not await self._is_ownerish(ctx.author):
            return
        watcher = LogWatcher(
            self.repo_root,
            self._configured_log_paths(),
            max_lines=int(getattr(config, "DEVAI_MAX_LOG_LINES", 180) or 180),
            scan_existing=True,
        )
        events = watcher.poll()
        if not events:
            await ctx.reply("Não encontrei tracebacks/erros novos nas logs configuradas.", mention_author=False)
            return
        event = events[-1]
        await ctx.reply(f"Analisando último erro encontrado: `{event.signature}`", mention_author=False)
        asyncio.create_task(self._analyze_event(event, comment="scan manual solicitado pelo dono"))

    @devai_group.command(name="ask")
    async def devai_ask(self, ctx: commands.Context, *, question: str = ""):
        """Conversa livre com a DevAI: `_devai ask como funciona o auto-update?`"""
        if not await self._is_ownerish(ctx.author):
            return
        question = (question or "").strip()
        if not question:
            await ctx.reply(
                "Uso: `_devai ask <pergunta>`\nEx: `_devai ask explique como funciona o sistema de TTS`",
                mention_author=False,
            )
            return
        # Reação 🧠 é colocada e removida dentro de _chat_with_owner — entra
        # quando começa a pensar, sai assim que a resposta vai pro canal.
        asyncio.create_task(self._chat_with_owner(ctx.message, question))

    @devai_group.command(name="review")
    async def devai_review(self, ctx: commands.Context, commit: str = "HEAD"):
        """Força a DevAI a comentar um commit específico (passado, geralmente
        o último). Útil quando o auto-comentário foi pulado por algum motivo
        (cog desabilitado na hora do auto-update, p.ex.).

        Uso:
          `_devai review`           — comenta o commit HEAD atual
          `_devai review f730f28`   — comenta o commit f730f28
          `_devai review HEAD~1`    — comenta o commit anterior
        """
        if not await self._is_ownerish(ctx.author):
            return
        commit = (commit or "HEAD").strip()

        # Resolve o ref pra hash absoluto e pega lista de arquivos alterados.
        import subprocess
        try:
            hash_proc = subprocess.run(
                ["git", "-C", str(self.repo_root), "rev-parse", commit],
                capture_output=True, text=True, timeout=8,
            )
            if hash_proc.returncode != 0:
                await ctx.reply(
                    f"❌ Não consegui resolver `{commit}`: `{(hash_proc.stderr or '').strip()[:200]}`",
                    mention_author=False,
                )
                return
            commit_hash = hash_proc.stdout.strip()

            files_proc = subprocess.run(
                ["git", "-C", str(self.repo_root), "show", "--name-only", "--pretty=format:", commit_hash],
                capture_output=True, text=True, timeout=8,
            )
            if files_proc.returncode != 0:
                await ctx.reply(
                    f"❌ git show falhou: `{(files_proc.stderr or '').strip()[:200]}`",
                    mention_author=False,
                )
                return
            changed_files = [line.strip() for line in (files_proc.stdout or "").splitlines() if line.strip()]
            if not changed_files:
                await ctx.reply(
                    f"⚠️ Commit `{commit_hash[:7]}` não tem arquivos alterados.",
                    mention_author=False,
                )
                return

            branch_proc = subprocess.run(
                ["git", "-C", str(self.repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=8,
            )
            branch = (branch_proc.stdout or "main").strip() or "main"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            await ctx.reply(f"❌ Erro chamando git: `{exc}`", mention_author=False)
            return

        await ctx.reply(
            f"🧠 Forçando review do commit `{commit_hash[:7]}` "
            f"({len(changed_files)} arquivo(s)) — comentário vai pro webhook.",
            mention_author=False,
        )
        try:
            await self.review_successful_patch(
                changed_files=changed_files,
                commit_hash=commit_hash,
                branch=branch,
                zip_filename=f"manual_review_{commit_hash[:7]}.zip",
                zip_path=None,  # sem zip — vai cair no caminho de git diff
                triggered_update=False,
            )
        except Exception as exc:
            log.exception("DevAI: falha em review manual")
            await ctx.reply(f"❌ Review falhou: `{type(exc).__name__}: {exc}`", mention_author=False)

    @devai_group.command(name="diag")
    async def devai_diag(self, ctx: commands.Context):
        """Diagnóstico do listener: mostra exatamente o que a DevAI vê.
        Usa `_devai diag` em qualquer canal (idealmente o de comentário) pra
        confirmar que o cog está vivo e identificar configs erradas."""
        if not await self._is_ownerish(ctx.author):
            return
        cfg_channel = int(getattr(config, "DEVAI_COMMENT_CHANNEL_ID", 0) or 0)
        cur_channel = int(getattr(ctx.channel, "id", 0) or 0)
        wh_id = self._devai_webhook_id()
        wh_url_set = bool(getattr(config, "DEVAI_WEBHOOK_URL", "") or "")
        owner_ids = sorted(self._owner_ids())
        author_id = int(getattr(ctx.author, "id", 0) or 0)
        try:
            is_app_owner = await self.bot.is_owner(ctx.author)
        except Exception:
            is_app_owner = False
        report = (
            "**DevAI diagnóstico**\n"
            f"• Cog enabled: `{self._enabled()}` "
            f"(flag explícita: `{bool(getattr(config, 'DEVAI_ENABLED', False))}`, "
            f"auto-enable por config: `{(not bool(getattr(config, 'DEVAI_ENABLED', False))) and self._enabled()}`)\n"
            f"• Webhook URL configurado: `{wh_url_set}`\n"
            f"• Webhook ID extraído: `{wh_id or 'não detectado'}`\n"
            f"• Reporter disponível: `{self.reporter is not None and self.reporter.available()}`\n"
            f"• Comment channel cfg: `{cfg_channel or 'não definido (qualquer canal)'}`\n"
            f"• Canal atual: `{cur_channel}` "
            f"{'✅ MATCH' if cfg_channel == cur_channel else '❌ diferente' if cfg_channel else ''}\n"
            f"• OWNER_IDS na config: `{owner_ids or 'vazio'}`\n"
            f"• Seu ID: `{author_id}` "
            f"{'✅ na lista' if author_id in owner_ids else '❌ NÃO na lista'}\n"
            f"• bot.is_owner(você): `{is_app_owner}`\n"
            f"• Mensagens chat tracked (RAM): `{len(self._chat_message_ids)}`\n"
            f"• Mensagens report tracked (RAM): `{len(self._report_message_ids)}`\n"
            f"• Providers stats: `{sum(s.success for s in self.ai.stats.values()) if self.ai else 0}` ok / "
            f"`{sum(s.failure for s in self.ai.stats.values()) if self.ai else 0}` falhas\n\n"
            "Se algum item está vermelho, ajuste o `.env` e reinicie. "
            "Logs detalhados de cada mensagem agora vão pro `bot.log` "
            "(prefixo `DevAI listener:`)."
        )
        await ctx.reply(report, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(DevAI(bot))
