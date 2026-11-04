"""Export a user's TTV message through the same preparation used by voice."""
from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
import traceback
import uuid

import discord

import config
from osaka_cd.runtime import pin_snapshot

from ..audio import TTVSingingError
from ..melodies import get_melody
from ..mensagens.preparacao import preparar_payload_tts_mensagem
from ..performance import singing_request
from ..singing_errors import singing_failure_code
from ..ttv import get_voice

_LOG = logging.getLogger(__name__)
_MAX_EXPORTS = 2
_MAX_BYTES = 8 * 1024 * 1024
_DISCORD_TIMEOUT = 15
_EXPORT_TIMEOUT = 240
_FAILURES = {
    'ttv_singing_timeout': 'A geração do MP3 excedeu o tempo de espera. Tente novamente.',
    'ttv_singing_busy': 'A voz está ocupada agora. Aguarde um pouco e tente novamente.',
    'ttv_singing_bank_missing': 'A voz selecionada está indisponível para cantar agora.',
    'ttv_singing_backend_unsupported': 'A voz selecionada não está configurada para cantar CDs.',
    'ttv_singing_lyrics_mismatch': 'Não consegui adaptar esta frase ao CD. Ajuste a frase ou escolha outro CD.',
    'ttv_singing_lyrics_too_long': 'A frase ficou longa para este CD. Reduza o texto e tente novamente.',
    'ttv_singing_score_invalid': 'Não consegui usar a partitura deste CD. Escolha outro no menu de CDs.',
    'ttv_export_too_long': 'Use um trecho de até 2 minutos para gerar o MP3.',
    'ttv_export_too_large': 'O MP3 ficou maior que o limite de anexos deste canal. Tente uma frase ou um trecho menor.',
    'ttv_export_worker_unavailable': 'O serviço de voz está indisponível agora. Tente novamente em instantes.',
    'ttv_export_ffmpeg_missing': 'Não encontrei o FFmpeg no ambiente do bot para converter o áudio em MP3.',
    'ttv_export_probe_failed': 'Não consegui verificar a duração do áudio gerado. Tente novamente.',
    'ttv_export_encode_failed': 'O áudio foi gerado, mas a conversão para MP3 falhou.',
    'ttv_export_audio_invalid': 'O sintetizador não devolveu um arquivo de áudio válido.',
    'ttv_export_restart_required': 'A exportação ainda não foi carregada. Reinicie o bot após aplicar a atualização.',
}
_STAGES = frozenset(('prepare', 'validate', 'health', 'render', 'read', 'probe', 'encode', 'send'))


def _diagnostic(error, *, reference: str, stage: str, guild_id: int, user_id: int):
    """Record the failing code location without prompts, URLs or error bodies."""
    frames = traceback.extract_tb(error.__traceback__)[-3:]
    location = ' > '.join(f'{Path(frame.filename).name}:{frame.name}:{frame.lineno}' for frame in frames)
    raw_code = getattr(error, 'code', None)
    code = raw_code if isinstance(raw_code, str) and raw_code in _FAILURES else singing_failure_code(error)
    number = getattr(error, 'errno', None)
    attribute = getattr(error, 'name', None) if isinstance(error, AttributeError) else None
    if not isinstance(attribute, str) or not attribute.isidentifier() or len(attribute) > 80:
        attribute = '-'
    _LOG.warning('[ttv_mp3] ref=%s stage=%s type=%s code=%s errno=%s attribute=%s at=%s guild=%s user=%s',
                 reference, stage, type(error).__name__, code,
                 number if type(number) is int else '-', attribute, location or '-', guild_id, user_id)
    return code


def _label(value: object) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))[:120]


async def _send(message, content: str):
    return await asyncio.wait_for(message.channel.send(
        content, allowed_mentions=discord.AllowedMentions.none(),
    ), timeout=_DISCORD_TIMEOUT)


async def _edit(progress, content: str):
    try:
        await asyncio.wait_for(progress.edit(
            content=content, allowed_mentions=discord.AllowedMentions.none(),
        ), timeout=_DISCORD_TIMEOUT)
    except (discord.HTTPException, asyncio.TimeoutError):
        _LOG.info('[ttv_mp3] could not update export response')


def _summary(item, melody_name: str | None) -> str:
    details = [_label(get_voice(item.ttv_voice_id).name)]
    if melody_name:
        details.append(_label(melody_name))
    for label, field in (
        ('Nightcore', 'advanced_nightcore_level'),
        ('Slowed', 'advanced_slowed_level'),
        ('Reverb', 'advanced_reverb_level'),
    ):
        level = int(getattr(item, field, 0) or 0)
        if level:
            details.append(f'{label} {level}')
    return '**TextToVocaloid · MP3**\n' + ' · '.join(details)


async def handle_export_ttv_mp3(cog, message, text: str) -> bool:
    guild = getattr(message, 'guild', None)
    if guild is None or getattr(message.author, 'bot', False):
        return True
    if not getattr(config, 'TTS_TETO_ENABLED', True):
        await _send(message, 'O TextToVocaloid está desativado agora.')
        return True
    db = cog._get_db()
    if db is None:
        await _send(message, 'Não consegui carregar seus ajustes de voz. Tente novamente.')
        return True
    try:
        defaults = dict(await cog._maybe_await(db.get_guild_tts_defaults(guild.id)) or {})
    except Exception:
        await _send(message, 'Não consegui carregar seus ajustes de voz. Tente novamente.')
        return True
    prefix = str(defaults.get('bot_prefix') or getattr(config, 'BOT_PREFIX', '_') or '_')
    text = str(text or '').strip()
    if not text:
        await _send(message,
            f'Use `{prefix}ttvmp3 texto` para falar ou `{prefix}ttvmp3 texto~~` para cantar.\n'
            f'O arquivo usa sua voz, o CD escolhido em `{prefix}cd` e os efeitos do `{prefix}advanced`.')
        return True
    _, singing = singing_request(text)
    limit = 2048 if singing else int(getattr(config, 'TTS_TETO_MAX_TEXT_LENGTH', 180) or 180)
    if len(text) > limit or not any(char.isalnum() for char in text):
        await _send(message, f'Escreva uma frase com até {limit} caracteres para gerar o MP3.')
        return True
    member = getattr(guild, 'me', None)
    permissions_for = getattr(message.channel, 'permissions_for', None)
    if member is not None and callable(permissions_for):
        permissions = permissions_for(member)
        if not getattr(permissions, 'attach_files', False):
            await _send(message, 'Preciso da permissão `Anexar arquivos` neste canal para enviar o MP3.')
            return True
    running = getattr(cog, '_ttv_mp3_exports', None)
    if running is None:
        running = set()
        cog._ttv_mp3_exports = running
    key = (int(guild.id), int(message.author.id))
    if key in running:
        await _send(message, 'Seu MP3 anterior ainda está sendo gerado.')
        return True
    if len(running) >= _MAX_EXPORTS:
        await _send(message, 'Já há arquivos sendo gerados. Tente novamente em instantes.')
        return True
    running.add(key)
    progress = None
    item = None
    stage = 'prepare'
    reference = 'MP3-' + uuid.uuid4().hex[:8].upper()
    try:
        progress = await _send(message, 'Gerando seu MP3…')
        async def generate_and_send():
            nonlocal item, stage
            # Keep the catalog generation and personal CD draw frozen throughout
            # preparation, rendering and the label attached to this one file.
            with pin_snapshot():
                payload = await preparar_payload_tts_mensagem(
                    cog, message, guild_defaults=defaults, active_prefix='',
                    forced_engine='teto', require_voice_channel=False, text_override=text,
                )
                if payload is None:
                    await _edit(progress, 'Não consegui preparar esse MP3. Confira a frase e seus ajustes de voz e CD.')
                    return True
                item = payload.queue_item
                melody_name = get_melody(payload.resolved.get('ttv_melody_id')).name if item.ttv_score is not None else None
                max_bytes = min(_MAX_BYTES, max(1, int(getattr(guild, 'filesize_limit', _MAX_BYTES) or _MAX_BYTES)))
                stage = 'render'
                render = getattr(cog, '_ttv_message_mp3', None)
                if not callable(render):
                    raise TTVSingingError('Export adapter not loaded', code='ttv_export_restart_required')
                audio = await render(item, max_bytes=max_bytes)
                content = _summary(item, melody_name)
            stage = 'send'
            with io.BytesIO(audio) as buffer:
                attachment = discord.File(buffer, filename='texttovocaloid.mp3')
                try:
                    await asyncio.wait_for(progress.edit(
                        content=content, attachments=[attachment],
                        allowed_mentions=discord.AllowedMentions.none(),
                    ), timeout=_DISCORD_TIMEOUT)
                finally:
                    attachment.close()
        await asyncio.wait_for(generate_and_send(), timeout=_EXPORT_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as error:
        failure_stage = getattr(item, '_ttv_export_stage', stage) if stage == 'render' else stage
        if failure_stage not in _STAGES:
            failure_stage = stage
        _diagnostic(error, reference=reference, stage=failure_stage, guild_id=guild.id, user_id=message.author.id)
        if progress is not None:
            notice = ('O MP3 foi gerado, mas o envio excedeu o tempo de espera. Tente novamente.'
                      if stage == 'send' else 'A geração do MP3 excedeu o tempo de espera. Tente novamente.')
            await _edit(progress, f'{notice}\n-# Referência: `{reference}`')
    except Exception as error:
        # Worker commands, response bodies and filesystem paths stay out of chat.
        failure_stage = getattr(item, '_ttv_export_stage', stage) if stage == 'render' else stage
        if failure_stage not in _STAGES:
            failure_stage = stage
        code = _diagnostic(error, reference=reference, stage=failure_stage,
                           guild_id=guild.id, user_id=message.author.id)
        if progress is not None:
            if isinstance(error, discord.Forbidden):
                notice = 'Não consegui anexar o MP3. Confira as permissões do bot neste canal.'
            elif isinstance(error, discord.NotFound):
                return True
            else:
                fallback = {
                    'prepare': 'Não consegui preparar esse áudio. A falha foi registrada para diagnóstico.',
                    'render': 'Não consegui sintetizar esse texto. A falha foi registrada para diagnóstico.',
                    'read': 'Não consegui acessar o áudio gerado. Tente novamente.',
                    'probe': 'Não consegui verificar a duração do áudio gerado.',
                    'encode': 'O áudio foi gerado, mas a conversão para MP3 falhou.',
                    'send': 'O MP3 foi gerado, mas não consegui anexá-lo neste canal. Tente novamente.',
                }
                notice = _FAILURES.get(code, fallback.get(failure_stage,
                    'Não consegui gerar o MP3 agora. A falha foi registrada para diagnóstico.'))
            await _edit(progress, f'{notice}\n-# Referência: `{reference}`')
    finally:
        running.discard(key)
    return True
