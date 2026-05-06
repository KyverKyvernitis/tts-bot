from __future__ import annotations


MAX_OUTPUT_CHARS = 120_000
CHUNK_SIZE = 1680


def chunk_text(content: str) -> list[str]:
    if not content:
        return []
    safe = content.replace("```", "`\u200b``")
    if len(safe) <= 1950:
        return [safe]

    parts = [safe[i:i + CHUNK_SIZE] for i in range(0, len(safe), CHUNK_SIZE)]
    total = len(parts)
    chunks: list[str] = []
    for index, part in enumerate(parts, start=1):
        chunks.append(f"🖥️ Resultado — parte {index}/{total}\n```txt\n{part}\n```")
    return chunks


def format_result(
    *,
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    elapsed: float,
    timed_out: bool = False,
) -> str:
    stdout = stdout or ""
    stderr = stderr or ""
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
        body = body[:MAX_OUTPUT_CHARS] + "\n\n[saída cortada pelo limite interno do _cmd]"
    return body
