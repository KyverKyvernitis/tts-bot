from __future__ import annotations

import asyncio
import os
from pathlib import Path

from .cartoes import CartoesUpdaterMixin
from .controles import ControlesUpdaterMixin
from .eventos import EventosUpdaterMixin
from .preparacao import PreparacaoUpdaterMixin
from .progresso import ProgressoUpdaterMixin


class IntegracaoDiscordUpdaterMixin(
    EventosUpdaterMixin,
    ControlesUpdaterMixin,
    ProgressoUpdaterMixin,
    PreparacaoUpdaterMixin,
    CartoesUpdaterMixin,
):
    """Integração Discord do updater, separada do runtime geral do bot."""

    ZIP_UPDATE_CHANNEL_ID = 1490093068706386131


def inicializar_integracao_updater(bot: object) -> None:
    """Inicializa somente o estado pertencente ao updater no objeto do bot."""
    bot._zip_update_lock = asyncio.Lock()
    bot._zip_update_reconcile_task = None
    bot._phone_worker_unavailable_until_by_task = {}
    bot._phone_worker_unavailable_last_log_by_task = {}

    repo_root = Path(bot._repo_root)
    bot._update_temp_root = Path("/tmp/discord-auto-update")
    bot._update_staging_root = Path(
        os.getenv("DISCORD_AUTO_UPDATE_STAGING_DIR", str(repo_root.parent / "bot-update-staging"))
    )
    bot._zip_rollback_state_path = bot._update_staging_root / "zip_update_rollback_state.json"
    bot._zip_update_log_outbox_dir = repo_root / "data" / "runtime" / "update-alert-outbox"
    bot._zip_update_delivery_receipts_dir = repo_root / "data" / "runtime" / "update-delivery-receipts"
    bot._zip_update_log_channel_state_path = repo_root / "data" / "runtime" / "update-log-channel.json"
    bot._zip_update_progress_render_state = {}
    bot._update_notice_by_user = {}
