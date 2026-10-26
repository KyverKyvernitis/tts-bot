"""Monta o payload de TTS a partir de uma mensagem do Discord.

Aplica o engine forçado pelo prefixo (gTTS/Edge/ATTS/Teto), preenche valores
default quando o user não configurou, renderiza o texto final (limpo +
prefixo de autor se ligado) e devolve o `QueueItem` pronto pro dispatcher.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

import discord
from typing import Any

import config

from ..audio import QueueItem, _has_speakable_tts_text
from ..ttv import resolve_preferences
from ..performance import parse_performance_text, performance_hash, singing_request
from ..melodies import score_for_melody, fit_catalog_lyrics_to_score, arrangement_for_melody, fit_arrangement_lyrics
from ..arrangement_audio import execution_arrangement
from osaka_cd.lyrics import prepare_message_arrangement
from osaka_cd.arrangement import arrangement_sha256
from osaka_cd.runtime import pin_snapshot


logger = logging.getLogger(__name__)


class _WithoutAttachments:
    """Render lyric words through normal mention/link handling, without filenames."""

    attachments: tuple = ()

    def __init__(self, message):
        self.message = message

    def __getattr__(self, name):
        return getattr(self.message, name)


async def _reply_ttv_error(message, error: str, *, view=None):
    sender = getattr(message, "reply", None)
    if not callable(sender):
        sender = getattr(getattr(message, "channel", None), "send", None)
    if callable(sender):
        try:
            content = (
                "-# No modo de canto, use ~ depois de uma sílaba para continuar sua vogal, com espaçamento"
                if error == "No canto, use ~ depois de uma sílaba para continuar sua vogal."
                else f"TTV: {error}"
            )
            return await sender(content, allowed_mentions=discord.AllowedMentions.none(), view=view)
        except (discord.HTTPException, AttributeError):
            logger.warning("[ttv] não foi possível apresentar erro ou seleção de faixa")
    return None


class _WithoutUserMidiAttachments:
    """Prevent MIDI files from entering TTV as attachments or lyrics."""

    def __init__(self, message):
        self.message = message
        self.attachments = tuple(
            attachment for attachment in (getattr(message, "attachments", None) or ())
            if not str(getattr(attachment, "filename", "")).lower().endswith((".mid", ".midi"))
        )

    def __getattr__(self, name):
        return getattr(self.message, name)


def _render_effect_segments(cog, message, segments: list[dict], *, announce: bool):
    rendered = []
    lyrics_message = _WithoutAttachments(message)
    for segment in segments:
        value = dict(segment)
        value["text"] = cog._render_tts_text(lyrics_message, str(segment["text"])).strip()
        if value["text"]:
            rendered.append(value)
    # Unmarked attachments and the optional speaker announcement retain their
    # old speech behavior, without inheriting effects from a neighboring word.
    attachment_text = cog._render_tts_text(message, "").strip() if getattr(message, "attachments", None) else ""
    neutral = {"fade_in": False, "fade_out": False, "hold": False, "pause_after_ms": 0}
    if attachment_text:
        rendered.append({"text": attachment_text, **neutral})
    text = " ".join(segment["text"] for segment in rendered)
    announced = cog._apply_author_prefix_if_needed(message.guild.id, message.author, text, enabled=announce)
    if text and announced.endswith(text) and announced != text:
        rendered.insert(0, {"text": announced[:-len(text)].strip(), **neutral})
    return announced, rendered


@dataclass(slots=True)
class PayloadTTSMensagem:
    text: str
    resolved: dict[str, Any]
    queue_item: QueueItem
    forced_gtts: bool


async def preparar_payload_tts_mensagem(
    cog,
    message,
    *,
    guild_defaults: dict | None,
    active_prefix: str,
    forced_engine: str,
) -> PayloadTTSMensagem | None:
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
    fallback_engine = str(resolved.get("engine") or "gtts").strip().lower() or "gtts"
    fallback_voice = str(resolved.get("voice") or "")
    fallback_language = str(resolved.get("language") or getattr(config, "GTTS_DEFAULT_LANGUAGE", "pt-br"))
    fallback_rate = str(resolved.get("rate") or "+0%")
    fallback_pitch = str(resolved.get("pitch") or "+0Hz")

    ttv_values = resolved
    effective_engine = str(forced_engine or resolved.get("engine") or "gtts").lower()
    if effective_engine != "teto":
        # Invalid TTV selections cannot stop independent Edge/gTTS requests.
        # Character and language are validated when their own route is selected.
        ttv_values = {**resolved, "ttv_voice_id": "", "ttv_language": ""}
    try:
        ttv_preferences = resolve_preferences(
            ttv_values,
            default_pitch=getattr(config, "TTS_TETO_DEFAULT_PITCH_SEMITONES", 0.0),
        )
    except ValueError as exc:
        logger.warning("[tts_voice] configuração TTV indisponível | guild=%s user=%s erro=%s", message.guild.id, message.author.id, exc)
        return None

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
    elif forced_engine == "android_native":
        # ATTS usa o Android TTS nativo do APK/phone-worker. Guardamos a engine
        # efetiva antiga para fallback local se o celular estiver offline/lento.
        resolved["engine"] = "android_native"
        resolved["language"] = resolved.get("android_language") or "pt-BR"
        resolved["voice"] = resolved.get("android_voice") or ""
        resolved["rate"] = resolved.get("android_rate") or "1.0"
        resolved["pitch"] = resolved.get("android_pitch") or "1.0"
    elif forced_engine == "teto":
        # O identificador legado teto transporta a vocaloid escolhida. A voz
        # pessoal permanece explícita; não substituir uma personagem indisponível.
        resolved["engine"] = "teto"
        resolved["language"] = ttv_preferences["ttv_language"]
        resolved["voice"] = ttv_preferences["ttv_voice_id"]
        resolved["rate"] = "1.0"
        resolved["pitch"] = "C4"
        resolved.update(ttv_preferences)
        resolved["teto_pitch_semitones"] = ttv_preferences["ttv_pitch_semitones"]

    # No user-provided MIDI is downloaded or converted into a singing score.
    author_voice = getattr(message.author, "voice", None)
    voice_channel = getattr(author_voice, "channel", None)
    if voice_channel is None:
        return None

    import time
    preparation_started = time.perf_counter()
    adaptation_ms = 0.0
    raw_text = message.content[len(active_prefix):].strip()
    score, segments, arrangement = None, None, None
    is_ttv = resolved.get("engine") == "teto"
    # Attachments .mid/.midi have no special behavior on any TTV request.
    # Keep ordinary attachment handling for other file types unchanged.
    ttv_message = _WithoutUserMidiAttachments(message) if is_ttv else message
    catalog_singing = False
    if is_ttv:
        raw_text, catalog_singing = singing_request(raw_text)
    try:
        if catalog_singing:
            clean_text, segments = parse_performance_text(raw_text, singing=True)
            if segments:
                for segment in segments:
                    if segment.get("text") not in ("~", "|"):
                        segment["text"] = cog._render_tts_text(_WithoutAttachments(message), segment["text"]).strip()
            text = cog._render_tts_text(_WithoutAttachments(message), clean_text).strip()
            try:
                with pin_snapshot():
                    score = score_for_melody(resolved.get("ttv_melody_id"))
                    arrangement = execution_arrangement(
                        arrangement_for_melody(resolved.get('ttv_melody_id'), preview=True),
                        ttv_preferences['ttv_voice_id'])
            except ValueError as exc:
                raise ValueError("A melodia salva ficou indisponível. Escolha outra em seu painel de canto.") from exc
            if arrangement is not None:
                text, segments = fit_arrangement_lyrics(text, segments, arrangement)
            else:
                text, segments = fit_catalog_lyrics_to_score(text, segments, score)
                arrangement = {'score_schema':2, 'duration_ms':score['duration_ms'],
                    'parts':[{'id':'lead','role':'lead','voice_id':ttv_preferences['ttv_voice_id'],
                              'lyrics_source':'message','notes':score['notes']}]}
            if len(text)>2048:
                raise ValueError('Use até 2048 caracteres de letra por mensagem.')
            adaptation_started = time.perf_counter()
            arrangement = await asyncio.to_thread(prepare_message_arrangement, arrangement, text, segments,
                                                  language=ttv_preferences['ttv_language'])
            adaptation_ms = (time.perf_counter() - adaptation_started) * 1000
            ttv_preferences.update(ttv_pitch_semitones="+0.0", ttv_speech_rate=1.0)
            resolved.update(ttv_preferences)
            resolved["teto_pitch_semitones"] = "+0.0"
        elif is_ttv:
            clean_text, segments = parse_performance_text(raw_text)
            if segments:
                text, segments = _render_effect_segments(
                    cog, ttv_message, segments, announce=cog._guild_announce_author_enabled(guild_defaults),
                )
            else:
                text = cog._render_tts_text(ttv_message, clean_text)
                text = cog._apply_author_prefix_if_needed(
                    message.guild.id, message.author, text,
                    enabled=cog._guild_announce_author_enabled(guild_defaults),
                )
        else:
            text = cog._render_tts_text(message, raw_text)
            text = cog._apply_author_prefix_if_needed(
                message.guild.id, message.author, text,
                enabled=cog._guild_announce_author_enabled(guild_defaults),
            )
        limit = 2048 if score is not None else int(getattr(config, "TTS_TETO_MAX_TEXT_LENGTH", 180) or 180)
        if (score is not None or segments is not None) and len(text) > limit:
            raise ValueError(f"Use até {limit} caracteres {'de letra' if score is not None else 'de texto com efeitos'} por mensagem.")
    except (ValueError, discord.HTTPException) as exc:
        await _reply_ttv_error(message, str(exc))
        return None

    if score is None and not _has_speakable_tts_text(text):
        logger.info("[tts_voice] mensagem ignorada | texto sem caracteres faláveis após renderização | guild=%s user=%s",
                    message.guild.id, message.author.id)
        return None
    digest = performance_hash(text=text, score=score, segments=segments) if score is not None or segments is not None else ""

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
        # A normalização 0..3 acontece no snapshot do QueueItem. Manter o
        # valor bruto aqui evita derrubar uma fala por configuração legada
        # malformada; engines não elegíveis simplesmente não aplicam o DSP.
        advanced_nightcore_level=resolved.get("advanced_nightcore_level") or 0,
        advanced_slowed_level=resolved.get("advanced_slowed_level") or 0,
        advanced_reverb_level=resolved.get("advanced_reverb_level") or 0,
        teto_pitch_semitones=ttv_preferences["ttv_pitch_semitones"],
        ttv_score=score, ttv_segments=segments, ttv_performance_hash=digest,
        ttv_arrangement=arrangement,
        ttv_arrangement_hash=arrangement_sha256(arrangement) if arrangement is not None else '',
        **(ttv_preferences if resolved.get("engine") == "teto" else {}),
        piper_fallback_engine=fallback_engine,
        piper_fallback_voice=fallback_voice,
        piper_fallback_language=fallback_language,
        piper_fallback_rate=fallback_rate,
        piper_fallback_pitch=fallback_pitch,
        piper_model=str(resolved.get("piper_model") or getattr(config, "TTS_PIPER_MODEL_NAME", "turbo-default") or "turbo-default"),
    )
    if arrangement is not None:
        queue_item._cd_preparation_ms = round((time.perf_counter() - preparation_started) * 1000, 2)
        queue_item._cd_lyrics_adaptation_ms = round(adaptation_ms, 2)
    return PayloadTTSMensagem(text=text, resolved=resolved, queue_item=queue_item, forced_gtts=forced_gtts)
