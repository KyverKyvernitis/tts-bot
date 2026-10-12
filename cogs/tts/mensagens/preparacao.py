"""Monta o payload de TTS a partir de uma mensagem do Discord.

Aplica o engine forçado pelo prefixo (gTTS/Edge/ATTS/Teto), preenche valores
default quando o user não configurou, renderiza o texto final (limpo +
prefixo de autor se ligado) e devolve o `QueueItem` pronto pro dispatcher.
"""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import logging

import discord
from typing import Any

import config

from ..audio import QueueItem, _has_speakable_tts_text
from ..ttv import resolve_preferences
from ..performance import parse_performance_text, performance_hash
from ..score import MAX_MIDI_BYTES, ScoreError, parse_midi
from ..melodies import score_for_melody
import re


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
            return await sender(f"TTV: {error}", allowed_mentions=discord.AllowedMentions.none(), view=view)
        except (discord.HTTPException, AttributeError):
            logger.warning("[ttv] não foi possível apresentar erro ou seleção de faixa")
    return None


class _MidiTrackView(discord.ui.View):
    """An author's melody choice resumes the original queue dispatcher once."""

    def __init__(self, *, cog, message, data: bytes, tracks: list[dict], guild_defaults, active_prefix, forced_engine):
        super().__init__(timeout=120)
        self.cog, self.message, self.data = cog, message, data
        self.guild_defaults = guild_defaults
        self.active_prefix, self.forced_engine = active_prefix, forced_engine
        self.reply_message = None
        self.used = False
        self.tracks = [track for track in tracks if track.get("monophonic") and track.get("note_count", 0)]
        self.page = 0
        self.select = discord.ui.Select(placeholder="Escolha a faixa da melodia", options=self._options())
        self.select.callback = self.choose
        self.add_item(self.select)
        if len(self.tracks) > 25:
            self.previous = discord.ui.Button(label="Faixas anteriores", style=discord.ButtonStyle.secondary, disabled=True)
            self.next = discord.ui.Button(label="Mais faixas", style=discord.ButtonStyle.secondary)
            self.previous.callback = self.previous_page
            self.next.callback = self.next_page
            self.add_item(self.previous)
            self.add_item(self.next)

    def _options(self):
        return [discord.SelectOption(
            label=str(track.get("name") or f"Faixa {int(track['index']) + 1}")[:100],
            value=str(track["index"]),
            description=f"{int(track.get('note_count', 0))} notas · faixa {int(track['index']) + 1}",
        ) for track in self.tracks[self.page * 25:(self.page + 1) * 25]]

    async def _change_page(self, interaction, change):
        if not await self.interaction_check(interaction):
            return
        if self.used:
            await interaction.response.send_message("Esta escolha já foi enviada.", ephemeral=True)
            return
        self.page = max(0, min((len(self.tracks) - 1) // 25, self.page + change))
        self.select.options = self._options()
        self.previous.disabled = self.page == 0
        self.next.disabled = (self.page + 1) * 25 >= len(self.tracks)
        await interaction.response.edit_message(view=self)

    async def previous_page(self, interaction):
        await self._change_page(interaction, -1)

    async def next_page(self, interaction):
        await self._change_page(interaction, 1)

    async def interaction_check(self, interaction):
        if int(interaction.user.id) != int(self.message.author.id):
            await interaction.response.send_message("Só quem enviou o MIDI pode escolher a melodia.", ephemeral=True)
            return False
        return True

    async def choose(self, interaction):
        if not await self.interaction_check(interaction):
            return
        if self.used:
            await interaction.response.send_message("Esta escolha já foi enviada.", ephemeral=True)
            return
        self.used = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="TTV: melodia escolhida; preparando o canto.", view=self)
        from .despacho import despachar_mensagem_tts

        async def prepare(cog, message, **options):
            return await preparar_payload_tts_mensagem(
                cog, message, **options, midi_data=self.data, midi_track_index=int(self.select.values[0]),
            )

        try:
            result = await despachar_mensagem_tts(
                self.cog, self.message, guild_defaults=self.guild_defaults,
                active_prefix=self.active_prefix, forced_engine=self.forced_engine, construir_payload=prepare,
            )
            if not result.enqueued:
                await interaction.followup.send("O canto não entrou na fila. Confira se você ainda está na call e tente novamente.", ephemeral=True)
        finally:
            self.data = b""
            self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        self.data = b""
        if self.reply_message is not None:
            try:
                await self.reply_message.edit(content="TTV: a escolha da melodia expirou. Envie o MIDI novamente.", view=self)
            except discord.HTTPException:
                pass
        self.stop()


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
    midi_data: bytes | None = None,
    midi_track_index: int | None = None,
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

    # Decline before fetching an attachment when there is nowhere to play it.
    author_voice = getattr(message.author, "voice", None)
    voice_channel = getattr(author_voice, "channel", None)
    if voice_channel is None:
        return None

    raw_text = message.content[len(active_prefix):].strip()
    score, segments = None, None
    is_ttv = resolved.get("engine") == "teto"
    midi_attachments = [attachment for attachment in (getattr(message, "attachments", None) or [])
                        if str(getattr(attachment, "filename", "")).lower().endswith((".mid", ".midi"))]
    catalog_singing = bool(is_ttv and forced_engine == "teto" and not midi_attachments and midi_data is None
                           and (re.match(r"^/cantar(?:\s|$)", raw_text, re.IGNORECASE)
                                or re.search(r"(?<!\S)~(?!\S)", raw_text)))
    if catalog_singing:
        raw_text = re.sub(r"^/cantar(?=\s|$)\s*", "", raw_text, count=1, flags=re.IGNORECASE)
    singing = is_ttv and forced_engine == "teto" and bool(midi_attachments or midi_data is not None or catalog_singing)
    try:
        if singing and not catalog_singing:
            if len(midi_attachments) > 1:
                raise ValueError("Envie um único MIDI por mensagem.")
            if midi_data is None:
                attachment = midi_attachments[0]
                declared_size = int(getattr(attachment, "size", 0) or 0)
                if declared_size <= 0 or declared_size > MAX_MIDI_BYTES:
                    raise ValueError("O MIDI deve ter no máximo 512 KiB.")
                midi_data = await asyncio.wait_for(attachment.read(), timeout=20)
            if not isinstance(midi_data, bytes) or not (0 < len(midi_data) <= MAX_MIDI_BYTES):
                raise ValueError("O MIDI baixado está vazio ou ultrapassa 512 KiB.")
            clean_text, segments = parse_performance_text(raw_text, singing=True)
            # Lyrics are never prefixed with the author or attachment descriptions.
            if segments:
                for segment in segments:
                    segment["text"] = cog._render_tts_text(_WithoutAttachments(message), segment["text"]).strip()
            # Standalone ~ and | belong to the score's lyric alignment.
            text = cog._render_tts_text(_WithoutAttachments(message), clean_text).strip()
            score = parse_midi(midi_data, lyrics=text, track_index=midi_track_index)
            ttv_preferences.update(ttv_pitch_semitones="+0.0", ttv_speech_rate=1.0)
            resolved.update(ttv_preferences)
            resolved["teto_pitch_semitones"] = "+0.0"
        elif catalog_singing:
            clean_text, segments = parse_performance_text(raw_text, singing=True)
            if segments:
                for segment in segments:
                    if segment.get("text") not in ("~", "|"):
                        segment["text"] = cog._render_tts_text(_WithoutAttachments(message), segment["text"]).strip()
            text = cog._render_tts_text(_WithoutAttachments(message), clean_text).strip()
            try:
                score = score_for_melody(resolved.get("ttv_melody_id"))
            except ValueError as exc:
                raise ValueError("A melodia salva ficou indisponível. Escolha outra em seu painel de canto.") from exc
            ttv_preferences.update(ttv_pitch_semitones="+0.0", ttv_speech_rate=1.0)
            resolved.update(ttv_preferences)
            resolved["teto_pitch_semitones"] = "+0.0"
        elif is_ttv:
            clean_text, segments = parse_performance_text(raw_text)
            if segments:
                text, segments = _render_effect_segments(
                    cog, message, segments, announce=cog._guild_announce_author_enabled(guild_defaults),
                )
            else:
                text = cog._render_tts_text(message, clean_text)
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
    except ScoreError as exc:
        if exc.code == "ambiguous_track" and midi_data is not None:
            view = _MidiTrackView(cog=cog, message=message, data=midi_data, tracks=exc.tracks,
                                  guild_defaults=guild_defaults, active_prefix=active_prefix, forced_engine=forced_engine)
            view.reply_message = await _reply_ttv_error(message, "Qual faixa contém a melodia que a vocaloid deve cantar?", view=view)
        else:
            await _reply_ttv_error(message, str(exc))
        return None
    except (ValueError, asyncio.TimeoutError, discord.HTTPException) as exc:
        detail = "O download do MIDI demorou demais. Tente enviar o arquivo novamente." if isinstance(exc, asyncio.TimeoutError) else str(exc)
        await _reply_ttv_error(message, detail)
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
        **(ttv_preferences if resolved.get("engine") == "teto" else {}),
        piper_fallback_engine=fallback_engine,
        piper_fallback_voice=fallback_voice,
        piper_fallback_language=fallback_language,
        piper_fallback_rate=fallback_rate,
        piper_fallback_pitch=fallback_pitch,
        piper_model=str(resolved.get("piper_model") or getattr(config, "TTS_PIPER_MODEL_NAME", "turbo-default") or "turbo-default"),
    )
    return PayloadTTSMensagem(text=text, resolved=resolved, queue_item=queue_item, forced_gtts=forced_gtts)
