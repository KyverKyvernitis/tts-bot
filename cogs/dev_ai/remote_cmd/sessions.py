from __future__ import annotations

import asyncio
import os
import secrets
import signal
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

from .security import SESSION_ALLOWED, is_non_emulable_interactive, normalize_session_command, parse_head

SendFunc = Callable[[commands.Context, str], Awaitable[None]]


class RemoteSessionManager:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.sessions: dict[str, dict[str, Any]] = {}

    async def start_session(self, ctx: commands.Context, command: str, send: SendFunc) -> None:
        blocked, reason = is_non_emulable_interactive(command)
        if blocked:
            await send(ctx, f"⚠️ Comando interativo bloqueado\n{reason}.")
            return

        head, _ = parse_head(command)
        if head not in SESSION_ALLOWED:
            await send(
                ctx,
                "⚠️ Sessão não suportada\n"
                "Use sessão apenas para stdin/stdout simples: bash, sh, python, python3 ou node.",
            )
            return

        command = normalize_session_command(command)
        session_id = secrets.token_hex(2).upper()
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.repo_root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        self.sessions[session_id] = {
            "proc": proc,
            "owner_id": int(getattr(ctx.author, "id", 0) or 0),
            "command": command,
            "created_at": time.time(),
            "last_used_at": time.time(),
        }
        initial = await self.read_output(proc, timeout=0.8)
        await send(
            ctx,
            f"🖥️ Sessão aberta `{session_id}`\n"
            f"Comando: {command}\n\n"
            f"{initial.strip() or '(sem saída inicial)'}\n\n"
            f"Envie: `_cmd input {session_id} <texto>`\n"
            f"Feche: `_cmd close {session_id}`",
        )

    async def read_output(self, proc: asyncio.subprocess.Process, *, timeout: float = 1.2, limit: int = 20_000) -> str:
        if proc.stdout is None:
            return ""
        deadline = time.perf_counter() + max(0.1, timeout)
        data = bytearray()
        while len(data) < limit and time.perf_counter() < deadline:
            remaining = max(0.05, deadline - time.perf_counter())
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            data.extend(chunk)
            if len(chunk) < 4096:
                await asyncio.sleep(0.05)
        return bytes(data).decode("utf-8", errors="replace")

    async def input(self, ctx: commands.Context, session_id: str, text: str, send: SendFunc) -> None:
        session_id = session_id.upper().strip()
        session = self.sessions.get(session_id)
        if not session:
            await send(ctx, f"⚠️ Sessão `{session_id}` não encontrada.")
            return
        if int(session.get("owner_id") or 0) != int(getattr(ctx.author, "id", 0) or 0):
            return

        proc: asyncio.subprocess.Process = session["proc"]
        if proc.returncode is not None:
            self.sessions.pop(session_id, None)
            await send(ctx, f"⚠️ Sessão `{session_id}` já encerrou com código `{proc.returncode}`.")
            return
        if proc.stdin is None:
            await send(ctx, f"⚠️ Sessão `{session_id}` não aceita entrada.")
            return

        try:
            proc.stdin.write((text + "\n").encode("utf-8", errors="replace"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self.sessions.pop(session_id, None)
            await send(ctx, f"⚠️ Sessão `{session_id}` encerrou antes de receber a entrada.")
            return

        session["last_used_at"] = time.time()
        output = await self.read_output(proc, timeout=1.5)
        await send(
            ctx,
            f"🖥️ Sessão `{session_id}`\n"
            f"Entrada: {text}\n\n"
            f"{output.strip() or '(sem saída)'}",
        )

    async def close(self, ctx: commands.Context, session_id: str, send: SendFunc) -> None:
        session_id = session_id.upper().strip()
        session = self.sessions.get(session_id)
        if not session:
            await send(ctx, f"⚠️ Sessão `{session_id}` não encontrada.")
            return
        if int(session.get("owner_id") or 0) != int(getattr(ctx.author, "id", 0) or 0):
            return

        self.sessions.pop(session_id, None)
        proc: asyncio.subprocess.Process = session["proc"]
        await self._terminate(proc)
        await send(ctx, f"🖥️ Sessão `{session_id}` fechada.")

    async def close_all(self) -> None:
        sessions = list(self.sessions.values())
        self.sessions.clear()
        for session in sessions:
            proc = session.get("proc")
            if isinstance(proc, asyncio.subprocess.Process):
                await self._terminate(proc)

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                os.killpg(int(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
