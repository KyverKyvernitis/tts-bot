from __future__ import annotations

import asyncio
import logging
from typing import Any

from cogs.musica import configuracao as config
from cogs.musica.diagnostico.servico import cleanup_music_diagnostics_temp_artifacts
from cogs.musica.legado.roteador_audio import AudioRouter
from cogs.musica.integracoes.status_canal import instalar_ponte_gateway_status_canal
from cogs.musica.agente_telefone.transporte_http import fechar_sessao_http
from cogs.musica.agente_telefone.roteamento import limpar_vinculos_worker
from cogs.musica.interface.tarefas import cancelar_tarefas_interface

LOG = logging.getLogger("music")


class IntegracaoMusicaBot:
    """Ciclo de vida da música dentro do processo principal do bot.

    O objeto centraliza estado e tarefas que antes ficavam espalhados em
    ``bot.py``. O atributo ``bot.audio_router`` permanece como ponte de
    compatibilidade enquanto os consumidores externos são migrados.
    """

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.router = AudioRouter(bot)
        self._bitrate_reconciliado = False
        self._status_voz_reconciliado = False
        self._tarefa_reconciliacao: asyncio.Task | None = None
        self._voice_status_parser_installed = False

    def instalar_compatibilidade(self) -> None:
        setattr(self.bot, "audio_router", self.router)
        self._instalar_ponte_gateway_status_canal()

    def _instalar_ponte_gateway_status_canal(self) -> None:
        """Expõe VOICE_CHANNEL_STATUS_UPDATE sem ligar debug global do websocket."""
        if self._voice_status_parser_installed:
            return
        self._voice_status_parser_installed = instalar_ponte_gateway_status_canal(self.bot)
        if self._voice_status_parser_installed:
            LOG.info("ponte VOICE_CHANNEL_STATUS_UPDATE instalada")

    def limpar_temporarios_diagnostico(self) -> str:
        return cleanup_music_diagnostics_temp_artifacts()

    def agendar_reconciliacao_inicial(self) -> None:
        tarefa = self._tarefa_reconciliacao
        if tarefa is not None and not tarefa.done():
            return
        if self._bitrate_reconciliado and self._status_voz_reconciliado:
            return
        self._tarefa_reconciliacao = asyncio.create_task(self._executar_reconciliacao_inicial())

    async def _executar_reconciliacao_inicial(self) -> None:
        try:
            await asyncio.sleep(
                max(1.0, float(getattr(config, "MUSIC_STARTUP_RESTORE_DELAY_SECONDS", 12.0) or 12.0))
            )
            if not self._bitrate_reconciliado and hasattr(self.router, "reconcile_auto_bitrate_records"):
                self._bitrate_reconciliado = True
                try:
                    await self.router.reconcile_auto_bitrate_records()
                except Exception as exc:
                    LOG.debug("reconciliação de bitrate automático falhou: %r", exc, exc_info=True)

            await asyncio.sleep(
                max(0.0, float(getattr(config, "MUSIC_STARTUP_RESTORE_STEP_DELAY_SECONDS", 0.75) or 0.75))
            )
            if not self._status_voz_reconciliado and hasattr(self.router, "reconcile_voice_status_records"):
                self._status_voz_reconciliado = True
                try:
                    await self.router.reconcile_voice_status_records()
                except Exception as exc:
                    LOG.debug("reconciliação de status de canal falhou: %r", exc, exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.debug("reconciliação pós-startup de música falhou", exc_info=True)

    async def fechar(self) -> None:
        tarefa = self._tarefa_reconciliacao
        if tarefa is not None:
            tarefa.cancel()
        await cancelar_tarefas_interface()
        limpar_vinculos_worker()
        try:
            await self.router.close()
        except Exception as exc:
            LOG.debug("falha ao fechar audio_router: %r", exc, exc_info=True)
        try:
            await fechar_sessao_http()
        except Exception as exc:
            LOG.debug("falha ao fechar sessão HTTP de música: %r", exc, exc_info=True)
