from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path


DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(slots=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int | None
    elapsed: float
    timed_out: bool = False


async def kill_process_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(int(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            pass


async def run_shell(command: str, *, cwd: Path, timeout: float | None = None) -> CommandResult:
    timeout = float(timeout or DEFAULT_TIMEOUT_SECONDS)
    start = time.perf_counter()
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        await kill_process_group(proc)
        out_b, err_b = await proc.communicate()
    elapsed = time.perf_counter() - start
    stdout = (out_b or b"").decode("utf-8", errors="replace")
    stderr = (err_b or b"").decode("utf-8", errors="replace")
    return CommandResult(stdout=stdout, stderr=stderr, exit_code=proc.returncode, elapsed=elapsed, timed_out=timed_out)
