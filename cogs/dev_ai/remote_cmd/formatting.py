from __future__ import annotations

from datetime import datetime

from .redactor import redact_text


MAX_OUTPUT_CHARS = 120_000
CHUNK_SIZE = 1680
MAX_TEXT_ATTACHMENT_BYTES = 24 * 1024 * 1024


def _escape_code_fences(content: str) -> str:
    return (content or "").replace("```", "`\u200b``")


def chunk_text(content: str) -> list[str]:
    if not content:
        return []
    safe = _escape_code_fences(content)
    if len(safe) <= 1950:
        return [safe]

    parts = [safe[i:i + CHUNK_SIZE] for i in range(0, len(safe), CHUNK_SIZE)]
    total = len(parts)
    chunks: list[str] = []
    for index, part in enumerate(parts, start=1):
        chunks.append(f"🖥️ Resultado — parte {index}/{total}\n```txt\n{part}\n```")
    return chunks


def build_full_result_text(
    *,
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    elapsed: float,
    timed_out: bool = False,
) -> str:
    command = redact_text(command)
    stdout = redact_text(stdout or "")
    stderr = redact_text(stderr or "")
    return (
        f"Comando: {command}\n"
        f"Código: {exit_code if exit_code is not None else 'sem código'}\n"
        f"Tempo: {elapsed:.2f}s\n"
        f"Timeout: {'sim' if timed_out else 'não'}\n"
        "\n"
        "----- STDOUT -----\n"
        f"{stdout or '(sem saída)'}\n"
        "\n"
        "----- STDERR -----\n"
        f"{stderr or '(sem saída)'}\n"
    )


def build_result_attachment(
    *,
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    elapsed: float,
    timed_out: bool = False,
) -> tuple[str, bytes] | None:
    content = build_full_result_text(
        command=command,
        stdout=redact_text(stdout),
        stderr=redact_text(stderr),
        exit_code=exit_code,
        elapsed=elapsed,
        timed_out=timed_out,
    )
    payload = content.encode("utf-8", errors="replace")
    if len(payload) > MAX_TEXT_ATTACHMENT_BYTES:
        return None
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"cmd_result_{now}.txt", payload


def format_result(
    *,
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    elapsed: float,
    timed_out: bool = False,
) -> str:
    command = redact_text(command)
    stdout = redact_text(stdout or "")
    stderr = redact_text(stderr or "")
    body = (
        f"🖥️ Comando executado\n"
        f"Comando: {command}\n"
        f"Código: {exit_code if exit_code is not None else 'sem código'}"
        f" • Tempo: {elapsed:.2f}s"
        f"{' • timeout' if timed_out else ''}\n\n"
        f"--- stdout ---\n{stdout.strip() or '(sem saída)'}\n\n"
        f"--- stderr ---\n{stderr.strip() or '(sem saída)'}"
    )
    if len(body) > MAX_OUTPUT_CHARS:
        body = body[:MAX_OUTPUT_CHARS] + "\n\n[saída cortada na prévia; o .txt anexado contém o resultado completo]"
    return body
