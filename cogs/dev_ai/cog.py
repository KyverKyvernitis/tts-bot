from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import aiohttp
import discord
from discord.ext import commands

import config

from .ai_client import DevAIClient, AIResult
from .log_watcher import LogEvent, LogWatcher
from .patch_builder import BuiltPatch, PatchBuilder
from .project_indexer import ProjectIndexer
from .safety import redact_secrets
from .webhook_reporter import WebhookReporter

log = logging.getLogger(__name__)


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
        self._report_message_ids: set[int] = set()
        self._last_event_by_message: dict[int, LogEvent] = {}

    def _enabled(self) -> bool:
        return bool(getattr(config, "DEVAI_ENABLED", False))

    async def cog_load(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        self.session = aiohttp.ClientSession()
        self.ai = DevAIClient(self.session, config)
        self.reporter = WebhookReporter(self.session, str(getattr(config, "DEVAI_WEBHOOK_URL", "") or ""))

        # Mantém um entendimento inicial da estrutura do projeto, mas sem travar o bot.
        asyncio.create_task(asyncio.to_thread(self.indexer.load_or_build, max_age_seconds=60))

        if self._enabled():
            self.watcher = LogWatcher(
                self.repo_root,
                self._configured_log_paths(),
                max_lines=int(getattr(config, "DEVAI_MAX_LOG_LINES", 180) or 180),
                scan_existing=bool(getattr(config, "DEVAI_SCAN_EXISTING_LOGS_ON_BOOT", False)),
            )
            self.worker_task = asyncio.create_task(self._watch_loop())
            log.info("DevAI habilitada. Logs monitoradas: %s", ", ".join(p.as_posix() for p in self._configured_log_paths()))
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
        asyncio.create_task(self._analyze_event(event, auto=True))

    def _build_prompt(self, *, event: LogEvent, comment: str | None = None) -> str:
        index = self.indexer.load_or_build(max_age_seconds=int(getattr(config, "DEVAI_INDEX_MAX_AGE_SECONDS", 1800) or 1800))
        project_context = self.indexer.compact_context(index, max_chars=int(getattr(config, "DEVAI_MAX_INDEX_CHARS", 12000) or 12000))

        candidate_files = list(event.file_paths or [])
        candidate_files.extend(self._guess_related_files(event.text))
        # Remove duplicatas preservando ordem.
        seen: set[str] = set()
        candidate_files = [p for p in candidate_files if not (p in seen or seen.add(p))]
        file_context = self.indexer.read_files_for_context(
            candidate_files,
            max_files=int(getattr(config, "DEVAI_MAX_CONTEXT_FILES", 4) or 4),
            max_chars_per_file=int(getattr(config, "DEVAI_MAX_FILE_CONTEXT_CHARS", 24000) or 24000),
        )

        context_blocks = []
        for path, text in file_context.items():
            context_blocks.append(f"### {path}\n```py\n{text}\n```")

        comment_block = f"\nComentário do dono sobre esse erro:\n{redact_secrets(comment, max_chars=2500)}\n" if comment else ""

        return f"""
Você é a DevAI do projeto. Sua tarefa é corrigir erro real em um bot Discord Python.

REGRAS OBRIGATÓRIAS:
- Responda SOMENTE JSON válido, sem markdown fora do JSON.
- Gere um patch mínimo.
- Retorne arquivos COMPLETOS, não diff.
- Não altere tokens, .env, credenciais, banco .db ou arquivos de segredo.
- Não invente arquivos se o erro puder ser corrigido nos arquivos existentes.
- Preserve o estilo do projeto.
- Se não tiver segurança suficiente para corrigir, retorne "files": [] e explique em "cause".
- O JSON deve seguir este formato:
{{
  "cause": "causa provável do erro",
  "summary": "o que foi alterado",
  "risk": "baixo|médio|alto",
  "files": [{{"path": "cogs/exemplo.py", "content": "conteúdo completo do arquivo corrigido"}}]
}}

ERRO DETECTADO:
Fonte: {event.source}
Assinatura: {event.signature}
```txt
{redact_secrets(event.text, max_chars=int(getattr(config, 'DEVAI_MAX_LOG_CHARS', 18000) or 18000))}
```
{comment_block}

ARQUIVOS RELACIONADOS LIDOS:
{chr(10).join(context_blocks) if context_blocks else 'Nenhum arquivo relacionado foi lido automaticamente.'}

RESUMO DA ESTRUTURA DO PROJETO:
```txt
{project_context}
```
""".strip()

    def _guess_related_files(self, text: str) -> list[str]:
        text_lower = text.lower()
        guesses: list[str] = []
        if "tts" in text_lower or "voice" in text_lower or "call" in text_lower:
            guesses.extend(["cogs/tts/cog.py", "cogs/tts/audio.py", "cogs/tts/ui.py"])
        if "webhook" in text_lower or "zip" in text_lower or "update" in text_lower:
            guesses.extend(["bot.py", "alert.sh"])
        if "mongodb" in text_lower or "mongo" in text_lower or "database" in text_lower:
            guesses.extend(["db.py", "config.py"])
        if "help" in text_lower or "health" in text_lower:
            guesses.append("cogs/utility.py")
        return guesses

    async def _analyze_event(self, event: LogEvent, *, auto: bool, comment: str | None = None) -> None:
        if self.ai is None or self.reporter is None:
            return
        async with self._analysis_lock:
            try:
                prompt = await asyncio.to_thread(self._build_prompt, event=event, comment=comment)
                result, errors = await self.ai.generate_patch_json(prompt)
                if result is None:
                    await self._report_failure(event, errors, comment=comment)
                    return

                try:
                    built = await asyncio.to_thread(self.patch_builder.build_from_ai_response, result.text, label=event.signature)
                except Exception as build_exc:
                    await self._report_build_failure(event, result, build_exc, comment=comment)
                    return

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

    async def _report_patch(self, event: LogEvent, result: AIResult, built: BuiltPatch, *, comment: str | None = None):
        if self.reporter is None or not self.reporter.available():
            return
        files = "\n".join(f"• `{p}`" for p in built.changed_files)
        validation = "\n".join(f"• {v}" for v in built.validation) or "• Sem arquivos Python para compilar."
        title = "🧠 DevAI corrigiu um erro e gerou um patch"
        description = (
            f"**Provider:** `{result.provider}` · `{result.model}` · `{result.elapsed_ms} ms`\n"
            f"**Assinatura:** `{event.signature}`\n\n"
            f"**Causa provável**\n{redact_secrets(built.cause, max_chars=900)}\n\n"
            f"**Alteração**\n{redact_secrets(built.summary, max_chars=900)}\n\n"
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

    @commands.Cog.listener("on_message")
    async def _devai_comment_listener(self, message: discord.Message):
        if getattr(message.author, "bot", False):
            return
        if not self._enabled():
            return
        channel_id = int(getattr(config, "DEVAI_COMMENT_CHANNEL_ID", 0) or 0)
        if channel_id and int(getattr(message.channel, "id", 0) or 0) != channel_id:
            return
        if not await self._is_ownerish(message.author):
            return

        content = (message.content or "").strip()
        lower = content.lower()
        if not (lower.startswith("devai") or lower.startswith("ia") or self._is_reply_to_report(message)):
            return

        if not self._is_reply_to_report(message):
            # Comentário solto só responde se houver um último evento em memória.
            if not self._last_event_by_message:
                return
            event = list(self._last_event_by_message.values())[-1]
        else:
            ref_id = int(getattr(getattr(message, "reference", None), "message_id", 0) or 0)
            event = self._last_event_by_message.get(ref_id)
            if event is None and self._last_event_by_message:
                event = list(self._last_event_by_message.values())[-1]
            if event is None:
                return

        comment = re.sub(r"^(devai|ia)[:,\s-]*", "", content, flags=re.I).strip() or content
        try:
            await message.add_reaction("🧠")
        except Exception:
            pass
        asyncio.create_task(self._analyze_event(event, auto=False, comment=comment))

    def _is_reply_to_report(self, message: discord.Message) -> bool:
        ref = getattr(message, "reference", None)
        if ref is None:
            return False
        mid = int(getattr(ref, "message_id", 0) or 0)
        return mid in self._report_message_ids

    @commands.group(name="devai", hidden=True, invoke_without_command=True)
    async def devai_group(self, ctx: commands.Context):
        if not await self._is_ownerish(ctx.author):
            return
        await ctx.reply(
            "DevAI: use `_devai status`, `_devai scan` ou `_devai index`.",
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
        asyncio.create_task(self._analyze_event(event, auto=False, comment="scan manual solicitado pelo dono"))


async def setup(bot: commands.Bot):
    await bot.add_cog(DevAI(bot))
