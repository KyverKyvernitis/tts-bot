"""Monta o payload de TTS a partir de uma mensagem do Discord.

Aplica o engine forçado pelo prefixo (gtts/edge/gcloud), preenche valores
default quando o user não configurou, renderiza o texto final (limpo +
prefixo de autor se ligado) e devolve o `QueueItem` pronto pro dispatcher.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import config

from ..audio import QueueItem


@dataclass(slots=True)
class MessageTTSPayload:
    text: str
    resolved: dict[str, Any]
    queue_item: QueueItem
    forced_gtts: bool


async def build_message_tts_payload(
    cog,
    message,
    *,
    guild_defaults: dict | None,
    active_prefix: str,
    forced_engine: str,
) -> MessageTTSPayload | None:
    db = cog._get_db()
    if db is None:
        print("[tts_voice] ignorado | settings_db indisponível")
        return None

    # `resolve_tts` retorna a configuração efetiva do user (mistura
    # config pessoal + defaults da guild). Pode falhar se o Mongo cair.
    try:
        resolved = await cog._maybe_await(db.resolve_tts(message.guild.id, message.author.id))
    except Exception as e:
        print(f"[tts_voice] erro em resolve_tts | guild={message.guild.id} user={message.author.id} erro={e}")
        return None

    resolved = dict(resolved or {})
    forced_gtts = False

    # Override pelo prefixo: se o user usou o prefixo de Edge, ele quer Edge
    # nessa fala mesmo que o engine padrão dele seja gTTS (e idem pros outros).
    if forced_engine == "gtts":
        resolved["engine"] = "gtts"
        resolved["language"] = resolved.get("language") or getattr(config, "GTTS_DEFAULT_LANGUAGE", "pt-br")
    elif forced_engine == "edge":
        resolved["engine"] = "edge"
        resolved["voice"] = resolved.get("voice") or "pt-BR-FranciscaNeural"
        resolved["rate"] = resolved.get("rate") or "+0%"
        resolved["pitch"] = resolved.get("pitch") or "+0Hz"
    elif forced_engine == "gcloud":
        resolved["engine"] = "gcloud"
        resolved["language"] = resolved.get("gcloud_language") or str(getattr(config, "GOOGLE_CLOUD_TTS_LANGUAGE_CODE", "pt-BR") or "pt-BR")
        resolved["voice"] = resolved.get("gcloud_voice") or str(getattr(config, "GOOGLE_CLOUD_TTS_VOICE_NAME", "pt-BR-Standard-A") or "pt-BR-Standard-A")
        resolved["rate"] = resolved.get("gcloud_rate") or str(getattr(config, "GOOGLE_CLOUD_TTS_SPEAKING_RATE", 1.0) or 1.0)
        resolved["pitch"] = resolved.get("gcloud_pitch") or str(getattr(config, "GOOGLE_CLOUD_TTS_PITCH", 0.0) or 0.0)

    # Texto final: tira o prefixo de fala, limpa marcadores e prepende o
    # nome falado do autor quando o servidor tem essa opção ligada.
    text = cog._render_tts_text(message, message.content[len(active_prefix):].strip())
    text = cog._apply_author_prefix_if_needed(
        message.guild.id,
        message.author,
        text,
        enabled=cog._guild_announce_author_enabled(guild_defaults),
    )
    if not text:
        print("[tts_voice] ignorado | texto vazio após prefixo")
        return None

    # Sem voice channel não tem onde tocar — descarta cedo.
    author_voice = getattr(message.author, "voice", None)
    voice_channel = getattr(author_voice, "channel", None)
    if voice_channel is None:
        return None

    queue_item = QueueItem(
        guild_id=message.guild.id,
        channel_id=voice_channel.id,
        author_id=message.author.id,
        text=text,
        engine=str(resolved.get("engine") or "gtts"),
        voice=str(resolved.get("voice") or ""),
        language=str(resolved.get("language") or ""),
        rate=str(resolved.get("rate") or "+0%"),
        pitch=str(resolved.get("pitch") or "+0Hz"),
    )
    return MessageTTSPayload(text=text, resolved=resolved, queue_item=queue_item, forced_gtts=forced_gtts)
