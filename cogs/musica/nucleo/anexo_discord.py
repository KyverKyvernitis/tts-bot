"""Identifica um vídeo anexado à mensagem respondida, sem buscar por título."""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any


_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi"}
_GENERIC_MIME = {"", "application/octet-stream", "binary/octet-stream"}


@dataclass(frozen=True)
class VideoRespondido:
    status: str
    mensagem: Any = None
    anexo: Any = None


def _is_video(anexo: Any) -> bool:
    mime = str(getattr(anexo, "content_type", "") or "").split(";", 1)[0].lower()
    if mime.startswith("video/"):
        return True
    # O Discord pode omitir ou generalizar o MIME; uma extensão conhecida ainda precisa
    # passar pela confirmação de áudio e duração no worker.
    return mime in _GENERIC_MIME and os.path.splitext(str(getattr(anexo, "filename", "") or "").lower())[1] in _VIDEO_EXTENSIONS


async def video_respondido(comando: Any) -> VideoRespondido:
    referencia = getattr(comando, "reference", None)
    if referencia is None or not getattr(referencia, "message_id", None):
        return VideoRespondido("sem_resposta")
    mensagem = getattr(referencia, "resolved", None) or getattr(referencia, "cached_message", None)
    if mensagem is None or not hasattr(mensagem, "attachments"):
        try:
            canal = getattr(comando, "channel", None)
            if int(getattr(canal, "id", 0) or 0) != int(getattr(referencia, "channel_id", 0) or 0):
                # Referências entre canais não devem ler um canal arbitrário.
                return VideoRespondido("inacessivel")
            mensagem = await asyncio.wait_for(canal.fetch_message(int(referencia.message_id)), timeout=6)
        except Exception:
            return VideoRespondido("inacessivel")
    anexos = [item for item in (getattr(mensagem, "attachments", None) or ()) if _is_video(item)]
    if not anexos:
        return VideoRespondido("sem_video", mensagem)
    if len(anexos) != 1:
        return VideoRespondido("multiplos", mensagem)
    return VideoRespondido("video", mensagem, anexos[0])


def metadados_video(resultado: VideoRespondido, comando: Any) -> dict[str, Any]:
    mensagem, anexo = resultado.mensagem, resultado.anexo
    if resultado.status != "video" or not anexo:
        raise ValueError("vídeo não selecionado")
    ids = {
        "guild_id": int(comando.guild.id),
        "channel_id": int(mensagem.channel.id),
        "message_id": int(mensagem.id),
        "attachment_id": int(anexo.id),
    }
    filename = str(getattr(anexo, "filename", "") or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    label = str(getattr(anexo, "title", "") or "").strip() or os.path.splitext(filename)[0]
    label = re.sub(r"[\x00-\x1f\x7f]+", " ", label).strip()[:160]
    if label.casefold() in {"música", "musica", "music", "video", "vídeo"}:
        label = f"Vídeo: {filename or anexo.id}"[:160]
    title = label or f"Vídeo anexado #{ids['attachment_id']}"
    author = getattr(mensagem, "author", None)
    return {
        "title": title,
        "webpage_url": f"https://discord.com/channels/{ids['guild_id']}/{ids['channel_id']}/{ids['message_id']}",
        "uploader": str(getattr(author, "display_name", "") or getattr(author, "name", "") or "").strip()[:120],
        "source": "Discord",
        "attachment_ref": ids,
        "attachment_filename": filename[:160],
        "attachment_duration_hint": getattr(anexo, "duration", None),
    }
