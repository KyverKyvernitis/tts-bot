from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass, field
from typing import Any

import discord

from cogs.musica import AudioRouter
from cogs.musica.diagnostico.servico import (
    DiagnosticsOptions,
    build_music_diagnostics_archive,
    build_music_diagnostics_emergency_report,
    build_music_diagnostics_report,
)

LOG = logging.getLogger(__name__)
TIMEOUT_DIAGNOSTICO_MUSICAL_SEGUNDOS = 115.0
TIMEOUT_DIAGNOSTICO_EMERGENCIAL_SEGUNDOS = 18.0


@dataclass(slots=True)
class ResultadoDiagnosticoVpsMusica:
    arquivos: list[discord.File] = field(default_factory=list)
    linhas_anexos: list[str] = field(default_factory=list)
    erros: list[str] = field(default_factory=list)
    gerado: bool = False


def obter_roteador_musica(bot: Any) -> AudioRouter:
    router = getattr(bot, "audio_router", None)
    if router is None:
        router = AudioRouter(bot)
        setattr(bot, "audio_router", router)
    return router


def _formatar_tamanho_anexo(size_bytes: int | None) -> str:
    try:
        size = max(0, int(size_bytes or 0))
    except Exception:
        size = 0
    if size < 1_000:
        return f"{size} B"
    if size < 1_000_000:
        return f"{round(size / 1_000)} kB"
    return f"{size / 1_000_000:.2f} MB"


async def _com_timeout(awaitable, segundos: float):
    return await asyncio.wait_for(awaitable, timeout=max(0.1, float(segundos)))


async def gerar_diagnostico_musical_vps(
    bot: Any,
    opcoes: DiagnosticsOptions,
    *,
    stamp: str,
) -> ResultadoDiagnosticoVpsMusica:
    resultado = ResultadoDiagnosticoVpsMusica()
    router = obter_roteador_musica(bot)
    try:
        payload, filename, summary, fallback_report = await _com_timeout(
            build_music_diagnostics_archive(router, opcoes),
            TIMEOUT_DIAGNOSTICO_MUSICAL_SEGUNDOS,
        )
        if payload and filename:
            resultado.arquivos.append(discord.File(io.BytesIO(payload), filename=filename))
            resultado.linhas_anexos.append(
                f"🎵 Diagnóstico musical anexado ({_formatar_tamanho_anexo(len(payload))})."
            )
            resultado.gerado = True
            return resultado

        resultado.erros.append(
            f"Diagnóstico modular não foi anexado: {summary or 'falha sem detalhes'}"
        )
        report = fallback_report or await _com_timeout(
            build_music_diagnostics_report(router, opcoes),
            TIMEOUT_DIAGNOSTICO_MUSICAL_SEGUNDOS,
        )
        report_bytes = report.encode("utf-8", "replace")
        resultado.arquivos.append(
            discord.File(io.BytesIO(report_bytes), filename=f"music-diag-{stamp}.txt")
        )
        resultado.linhas_anexos.append(
            f"🎵 Diagnóstico musical anexado ({_formatar_tamanho_anexo(len(report_bytes))})."
        )
        resultado.gerado = True
        return resultado
    except Exception as exc:
        LOG.exception("falha ao gerar diagnóstico musical via /vps")
        try:
            report = await _com_timeout(
                build_music_diagnostics_emergency_report(
                    router,
                    opcoes,
                    reason=f"{type(exc).__name__}: {str(exc)[:500]}",
                ),
                TIMEOUT_DIAGNOSTICO_EMERGENCIAL_SEGUNDOS,
            )
        except Exception as emergency_exc:
            report = (
                "# Diagnóstico musical falhou\n"
                f"Tipo: {type(exc).__name__}\n"
                f"Erro: {str(exc)[:500]}\n\n"
                "# Diagnóstico emergencial também falhou\n"
                f"Tipo: {type(emergency_exc).__name__}\n"
                f"Erro: {str(emergency_exc)[:500]}\n"
            )
        report_bytes = report.encode("utf-8", "replace")
        resultado.arquivos.append(
            discord.File(io.BytesIO(report_bytes), filename=f"music-diag-emergency-{stamp}.txt")
        )
        resultado.linhas_anexos.append(
            f"⚠️ Diagnóstico musical emergencial anexado ({_formatar_tamanho_anexo(len(report_bytes))})."
        )
        resultado.gerado = True
        return resultado
