"""Avisos efêmeros de update nos canais TTS ativos (sem polling próprio)."""
from __future__ import annotations

import asyncio
import json
from itertools import islice
import logging
import os
import re
import time
from pathlib import Path

import discord

LOG = logging.getLogger('zip_update.voice_notices')
RUN_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,90}$')
START_MESSAGE = '-# Vou atualizar rapidinho, talvez eu pare de responder por alguns segundos <:speed:1534287805424074772>'
SUCCESS_MESSAGE = '-# Volteii, tô normal agr <:osaka:1539137127852539944>'
FAILURE_MESSAGE = '-# Atualização deu ruim, talvez tenha outra a caminho logo logo'
TTL_SECONDS = 10
MAX_GUILDS = 60


def _save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _load_json(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding='utf-8'))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


class AvisosServidoresUpdaterMixin:
    def _server_notice_root(self) -> Path:
        return self._repo_root / 'data' / 'runtime' / 'update-voice-notices'

    def record_tts_channel_for_updates(self, guild_id: int, channel_id: int) -> None:
        """Índice minúsculo, no máximo uma gravação por canal/tamanho de janela."""
        if guild_id <= 0 or channel_id <= 0:
            return
        now = time.time()
        index = getattr(self, '_server_tts_channel_index', None)
        if index is None:
            index = _load_json(self._server_notice_root() / 'channels.json')
            self._server_tts_channel_index = index
        key = str(guild_id)
        old = index.get(key) or {}
        if old.get('channel') == channel_id and now - float(old.get('at') or 0) < 300:
            return
        index[key] = {'channel': channel_id, 'at': now}
        # Não manter infinitamente referências a servidores antigos.
        if len(index) > 150:
            index = {k: v for k, v in index.items() if now - float(v.get('at') or 0) < 86400}
            self._server_tts_channel_index = index
        try:
            _save_json(self._server_notice_root() / 'channels.json', index)
        except OSError:
            LOG.debug('falha persistindo índice do último canal TTS', exc_info=True)

    def _server_notice_destinations(self) -> list[tuple[object, int]]:
        index = getattr(self, '_server_tts_channel_index', None)
        if index is None:
            index = _load_json(self._server_notice_root() / 'channels.json')
            self._server_tts_channel_index = index
        cog = self.get_cog('TTSVoice')
        guild_states = getattr(cog, 'guild_states', {}) if cog is not None else {}
        now = time.time()
        results = []
        for guild in self.guilds:
            voice = guild.voice_client
            if voice is None or not voice.is_connected() or not voice.channel:
                continue
            # Somente calls com pelo menos uma pessoa: sem consultas externas.
            if not any(not getattr(m, 'bot', False) for m in getattr(voice.channel, 'members', [])):
                continue
            entry = index.get(str(guild.id), {})
            try:
                fresh = now - float(entry.get('at') or 0) <= 1800
            except (TypeError, ValueError):
                fresh = False
            state = guild_states.get(guild.id)
            # Dados legados em memória continuam válidos, mas um índice antigo
            # não deve disparar avisos em calls sem TTS recente.
            channel_id = int(getattr(state, 'last_text_channel_id', 0) or 0) if (fresh or not entry) else 0
            if not channel_id and fresh:
                channel_id = int(entry.get('channel') or 0)
            channel = (guild.get_channel_or_thread(channel_id) if hasattr(guild, 'get_channel_or_thread')
                       else guild.get_channel(channel_id)) if channel_id else None
            if channel is None or not hasattr(channel, 'send'):
                continue
            try:
                perms = channel.permissions_for(guild.me)
                if not perms.view_channel or not (perms.send_messages or getattr(perms, 'send_messages_in_threads', False)):
                    continue
            except (AttributeError, TypeError):
                continue
            results.append((channel, guild.id))
            if len(results) >= MAX_GUILDS:
                break
        return results

    async def _server_notice_start(self, payload: dict) -> dict:
        run_id = str(payload.get('run_id') or '')
        if not RUN_ID_RE.fullmatch(run_id):
            return {'ok': False, 'error': 'run_id inválido'}
        if not self.is_ready():
            return {'ok': True, 'sent': 0, 'skipped': 'discord não pronto'}
        root = self._server_notice_root()
        path = root / f'{run_id}.json'
        if path.exists():
            return {'ok': True, 'sent': len(_load_json(path).get('messages', [])), 'duplicate': True}
        destinations = self._server_notice_destinations()
        if not destinations:
            return {'ok': True, 'sent': 0}
        data = {'run_id': run_id, 'created_at': time.time(), 'messages': []}
        _save_json(path, data)
        semaphore = asyncio.Semaphore(3)
        async def send(channel, guild_id):
            async with semaphore:
                try:
                    message = await asyncio.wait_for(
                        channel.send(START_MESSAGE, allowed_mentions=discord.AllowedMentions.none()), 2.0,
                    )
                    return {'guild_id': guild_id, 'channel_id': channel.id, 'start_id': message.id}
                except (discord.HTTPException, asyncio.TimeoutError, OSError):
                    LOG.debug('aviso inicial não enviado ao servidor %s', guild_id, exc_info=True)
                    return None
        # Orçamento total curto: a microetapa nunca espera por um servidor lento.
        tasks = [asyncio.create_task(send(channel, gid)) for channel, gid in destinations]
        done, pending = await asyncio.wait(tasks, timeout=2.4)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if not task.cancelled() and task.exception() is None:
                result = task.result()
                if result:
                    data['messages'].append(result)
        # Serialização única por lote; não reescrever a lista inteira para
        # cada servidor que respondeu ao Discord.
        if data['messages']:
            _save_json(path, data)
        else:
            path.unlink(missing_ok=True)
        return {'ok': True, 'sent': len(data['messages'])}

    async def _server_notice_reconcile_once(self) -> None:
        lock = getattr(self, '_server_notice_reconcile_lock', None)
        if lock is None:
            lock = self._server_notice_reconcile_lock = asyncio.Lock()
        async with lock:
            await self._server_notice_reconcile_unlocked()

    async def _server_notice_reconcile_unlocked(self) -> None:
        root = self._server_notice_root()
        if not self.is_ready():
            return
        try:
            directory_stamp = root.stat().st_mtime_ns
        except OSError:
            return
        # Sem atualizações pendentes, uma única stat substitui o glob a cada
        # 5 segundos. Arquivos de resultado/criação são escritos por rename
        # atômico e invalidam o mtime do diretório.
        if getattr(self, '_server_notice_idle_directory_stamp', None) == directory_stamp:
            return
        found_pending = False
        # islice não carrega toda a pasta, mesmo que tenha arquivos antigos.
        for path in islice((candidate for candidate in root.glob('*.json')
                           if candidate.name != 'channels.json' and not candidate.name.endswith('.result.json')), 100):
            found_pending = True
            data = _load_json(path)
            if not data.get('messages'):
                path.unlink(missing_ok=True)
                continue
            result_path = path.with_name(path.stem + '.result.json')
            result = _load_json(result_path)
            if not result and time.time() - float(data.get('created_at') or 0) > 1800:
                # Updater interrompido: não deixar o aviso inicial eternamente.
                result = {'result': 'failure'}
            if result.get('result') not in ('success', 'failure'):
                continue
            messages = data['messages']
            changed = False
            for entry in messages:
                if entry.get('deleted'):
                    continue
                channel = self.get_channel(int(entry['channel_id']))
                if channel is None:
                    entry['deleted'] = True
                    changed = True
                    continue
                if not entry.get('finish_id') and not entry.get('finish_attempted'):
                    # A mensagem final é enviada apenas uma vez por ciclo. Uma
                    # falha de envio não pode gerar spam a cada reconciliação.
                    entry['finish_attempted'] = True
                    entry['delete_at'] = time.time() + TTL_SECONDS
                    changed = True
                    _save_json(path, data)
                    try:
                        message = await asyncio.wait_for(channel.send(
                            SUCCESS_MESSAGE if result['result'] == 'success' else FAILURE_MESSAGE,
                            allowed_mentions=discord.AllowedMentions.none(),
                        ), 2.0)
                        entry['finish_id'] = message.id
                        entry['delete_at'] = time.time() + TTL_SECONDS
                    except (discord.HTTPException, asyncio.TimeoutError, OSError):
                        entry['delete_at'] = time.time() + TTL_SECONDS
                    changed = True
                    _save_json(path, data)
                if time.time() < float(entry.get('delete_at') or 0):
                    continue
                # Ordem exigida: apagar segunda mensagem, depois a primeira.
                for field in ('finish_id', 'start_id'):
                    if not entry.get(field) or entry.get(field + '_deleted'):
                        continue
                    try:
                        await asyncio.wait_for(channel.get_partial_message(int(entry[field])).delete(), 2.0)
                    except discord.NotFound:
                        pass
                    except (discord.HTTPException, asyncio.TimeoutError, OSError):
                        LOG.debug('falha removendo aviso %s', field, exc_info=True)
                        entry['delete_attempts'] = int(entry.get('delete_attempts') or 0) + 1
                        changed = True
                        # Se falhar a remoção do aviso novo, não apagar o
                        # antigo antes dele. Próxima rodada retoma daqui.
                        break
                    entry[field + '_deleted'] = True
                    changed = True
                else:
                    entry['deleted'] = True
                    changed = True
            if changed:
                _save_json(path, data)
            upcoming = [float(e.get('delete_at') or 0) for e in messages
                        if not e.get('deleted') and e.get('delete_at') and float(e['delete_at']) > time.time()]
            if upcoming:
                self._server_notice_schedule_cleanup(path.stem, min(upcoming))
            if all(e.get('deleted') or int(e.get('delete_attempts') or 0) >= 3 for e in messages):
                path.unlink(missing_ok=True)
                result_path.unlink(missing_ok=True)
        self._server_notice_idle_directory_stamp = root.stat().st_mtime_ns if not found_pending else None

    def _server_notice_schedule_cleanup(self, run_id: str, at: float) -> None:
        tasks = getattr(self, '_server_notice_cleanup_tasks', None)
        if tasks is None:
            tasks = self._server_notice_cleanup_tasks = {}
        existing = tasks.get(run_id)
        if existing is not None and not existing.done():
            return
        async def cleanup():
            try:
                await asyncio.sleep(max(0.0, at - time.time()))
                await self._server_notice_reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.warning('limpeza temporária de avisos falhou', exc_info=True)
            finally:
                tasks.pop(run_id, None)
                state = _load_json(self._server_notice_root() / f'{run_id}.json')
                remaining = [float(e['delete_at']) for e in state.get('messages', [])
                             if not e.get('deleted') and float(e.get('delete_at') or 0) > time.time()]
                if remaining:
                    self._server_notice_schedule_cleanup(run_id, min(remaining))
        tasks[run_id] = asyncio.create_task(cleanup(), name=f'voice-notice-cleanup-{run_id}')

    async def _server_notice_action(self, payload: dict) -> dict:
        if payload.get('phase') == 'start':
            return await self._server_notice_start(payload)
        if payload.get('phase') == 'finish':
            await self._server_notice_reconcile_once()
            return {'ok': True}
        return {'ok': False, 'error': 'phase inválida'}
