"""Seleciona áudio ou vídeo da mensagem respondida sem buscar por título."""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any


_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi"}
_AUDIO_EXTENSIONS = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".flac", ".wma", ".aiff", ".weba"}
_GENERIC_MIME = {"", "application/octet-stream", "binary/octet-stream"}
_AUDIO_FILE_MIME = {"application/ogg", "application/x-ogg", "application/flac", "application/x-flac"}


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


def _is_audio(anexo: Any) -> bool:
    mime = str(getattr(anexo, "content_type", "") or "").split(";", 1)[0].lower()
    ext = os.path.splitext(str(getattr(anexo, "filename", "") or "").lower())[1]
    return mime.startswith("audio/") or (mime in (_GENERIC_MIME | _AUDIO_FILE_MIME) and ext in _AUDIO_EXTENSIONS)


def _is_voice_message(anexo: Any) -> bool:
    checker = getattr(anexo, "is_voice_message", None)
    try:
        return bool(checker()) if callable(checker) else False
    except Exception:
        return False


async def midia_respondida(comando: Any) -> VideoRespondido:
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
    anexos = [item for item in (getattr(mensagem, "attachments", None) or ()) if _is_video(item) or _is_audio(item)]
    if not anexos:
        return VideoRespondido("sem_midia", mensagem)
    if len(anexos) != 1:
        return VideoRespondido("multiplos", mensagem)
    return VideoRespondido("video" if _is_video(anexos[0]) else "audio", mensagem, anexos[0])


def metadados_midia(resultado: VideoRespondido, comando: Any) -> dict[str, Any]:
    mensagem, anexo = resultado.mensagem, resultado.anexo
    if resultado.status not in {"video", "audio"} or not anexo:
        raise ValueError("mídia não selecionada")
    ids = {
        "guild_id": int(comando.guild.id),
        "channel_id": int(mensagem.channel.id),
        "message_id": int(mensagem.id),
        "attachment_id": int(anexo.id),
    }
    filename = str(getattr(anexo, "filename", "") or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    voice = resultado.status == "audio" and _is_voice_message(anexo)
    author = getattr(mensagem, "author", None)
    uploader = str(getattr(author, "display_name", "") or getattr(author, "name", "") or "").strip()[:120]
    label = str(getattr(anexo, "title", "") or "").strip() or os.path.splitext(filename)[0]
    label = re.sub(r"[\x00-\x1f\x7f]+", " ", label).strip()[:160]
    if voice:
        label = f"Mensagem de voz de {uploader}" if uploader else "Mensagem de voz"
    elif not label or label.casefold() in {"música", "musica", "music", "video", "vídeo", "audio", "áudio"} or re.fullmatch(r"(?:video|vídeo|audio|áudio)[-_ ]?\d+(?:[-_ ]\d+)*", label, re.I):
        tipo = "Vídeo" if resultado.status == "video" else "Áudio"
        label = f"{tipo} de {uploader}" if uploader else f"{tipo} anexado #{ids['attachment_id']}"
    title = label[:160]
    return {
        "title": title,
        "webpage_url": f"https://discord.com/channels/{ids['guild_id']}/{ids['channel_id']}/{ids['message_id']}",
        "uploader": uploader,
        "source": "Discord",
        "attachment_ref": ids,
        "attachment_filename": filename[:160],
        "attachment_duration_hint": getattr(anexo, "duration", None),
    }


# Nomes antigos usados por outros pontos do projeto e por instalações em atualização.
video_respondido = midia_respondida
metadados_video = metadados_midia
