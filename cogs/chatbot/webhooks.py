"""Gerenciamento de webhooks pra entregar mensagens "em nome do" profile.

Estratégia:
- UM webhook por canal, reutilizado pra TODOS os profiles daquele server.
- A cada mensagem, chamamos webhook.send(username=profile.name, avatar_url=...)
  que o Discord aplica só àquela mensagem (sobreescreve a identity do webhook).
- Cache em RAM mapeando channel_id → (webhook_id, webhook_token) para evitar
  re-fetch da API a cada mensagem.

Detalhes importantes:
- Discord limita 10 webhooks por canal. Nosso webhook leva nome fixo
  ("Chatbot Bridge") — se alguém apagar, recriamos. Nunca criamos duplicado.
- Rate limit efetivo: 5 requests/2s por webhook. Já protegido pelo semaphore
  global do cog (N=2 simultâneas), mas também aplicamos locking por canal
  para evitar que duas mensagens pro MESMO canal estourem o limite.
- Mensagem via webhook mostra "[BOT]" — é inerente do Discord, não dá pra
  esconder.

Uso:
    mgr = WebhookManager(bot=bot, session=aiohttp_session)
    msg = await mgr.send_as_profile(
        channel=text_channel,
        profile_name="Lua",
        avatar_url="https://...",
        content="olá!",
    )
    # msg é discord.WebhookMessage; mgr.is_profile_message(msg, bot) confirma
    # se é um webhook gerenciado por nós (útil pra detectar reply do usuário).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp
import discord

from . import constants as C
from .lru_cache import LRUCacheTTL

log = logging.getLogger(__name__)


# Nome fixo do webhook gerenciado pelo bot. Minúsculo e curto —
# vai aparecer como username default do webhook caso algum send falhe
# em aplicar o override (edge case).
_MANAGED_WEBHOOK_NAME = "Chatbot"


@dataclass
class _WebhookRef:
    """Referência leve a um webhook — apenas id+token, que é o suficiente
    pra instanciar um discord.Webhook com aiohttp."""
    webhook_id: int
    webhook_token: str


class WebhookManager:
    """Gerencia webhooks do chatbot.

    Instanciado UMA vez no setup do cog e passado às funções que enviam.
    Thread-safe entre asyncio tasks (usa asyncio.Lock por canal para evitar
    corrida em criar webhook duplicado).
    """

    def __init__(self, *, bot: discord.Client, session: aiohttp.ClientSession):
        self._bot = bot
        self._session = session
        # cache: channel_id → _WebhookRef
        self._cache: LRUCacheTTL[int, _WebhookRef] = LRUCacheTTL(
            max_entries=C.WEBHOOK_CACHE_MAX_ENTRIES,
            ttl_seconds=C.WEBHOOK_CACHE_TTL_SECONDS,
        )
        # lock por canal — evita 2 tasks criarem webhook simultâneos pro mesmo canal
        self._channel_locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, channel_id: int) -> asyncio.Lock:
        lock = self._channel_locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_locks[channel_id] = lock
        return lock

    async def _resolve_webhook(
        self, channel: discord.TextChannel
    ) -> Optional[discord.Webhook]:
        """Retorna um Webhook válido pra esse canal, criando se necessário.

        Retorna None se bot não tem permissão para gerenciar webhooks no canal.
        """
        # Cache hit? Basta reconstruir o Webhook partial.
        cached = self._cache.get(channel.id)
        if cached is not None:
            return discord.Webhook.partial(
                id=cached.webhook_id,
                token=cached.webhook_token,
                session=self._session,
            )

        # Miss — precisa buscar/criar. Serializa por canal pra evitar criação dupla.
        async with self._lock_for(channel.id):
            # Double-check inside lock (outra task pode ter criado no meantime)
            cached = self._cache.get(channel.id)
            if cached is not None:
                return discord.Webhook.partial(
                    id=cached.webhook_id,
                    token=cached.webhook_token,
                    session=self._session,
                )

            # Checa permissão. Manage Webhooks é o que importa.
            me = channel.guild.me
            if me is None or not channel.permissions_for(me).manage_webhooks:
                log.warning(
                    "chatbot: sem permissão Manage Webhooks | guild=%s channel=%s",
                    channel.guild.id, channel.id,
                )
                return None

            # Procura webhook existente com nosso nome
            try:
                existing = await channel.webhooks()
            except discord.Forbidden:
                log.warning("chatbot: Forbidden ao listar webhooks | channel=%s", channel.id)
                return None
            except discord.HTTPException as e:
                log.warning("chatbot: HTTPException ao listar webhooks | channel=%s err=%s", channel.id, e)
                return None

            managed = next(
                (w for w in existing
                 if w.name == _MANAGED_WEBHOOK_NAME and w.token is not None),
                None,
            )

            if managed is None:
                # Cria. O avatar default fica em branco (cada send sobrescreve).
                try:
                    managed = await channel.create_webhook(
                        name=_MANAGED_WEBHOOK_NAME,
                        reason="Chatbot profile bridge",
                    )
                except discord.HTTPException as e:
                    log.warning(
                        "chatbot: falha ao criar webhook | channel=%s err=%s",
                        channel.id, e,
                    )
                    return None

            if managed.token is None:
                log.warning("chatbot: webhook sem token (não devia acontecer) | channel=%s", channel.id)
                return None

            self._cache.set(
                channel.id,
                _WebhookRef(webhook_id=managed.id, webhook_token=managed.token),
            )
            # Reconstrói usando nossa session (o managed veio da session interna do bot)
            return discord.Webhook.partial(
                id=managed.id,
                token=managed.token,
                session=self._session,
            )

    async def send_as_profile(
        self,
        *,
        channel: discord.TextChannel,
        profile_name: str,
        avatar_url: str,
        content: str,
    ) -> Optional[discord.WebhookMessage]:
        """Envia `content` no canal com a identidade do profile.

        Retorna a WebhookMessage criada, ou None se falhou (sem permissão,
        rate limit persistente, etc).
        """
        webhook = await self._resolve_webhook(channel)
        if webhook is None:
            return None

        # Discord username de webhook NÃO pode conter "discord" ou ser vazio,
        # e é limitado a 80 chars.
        safe_name = (profile_name or "Chatbot").strip()
        safe_name = safe_name.replace("discord", "disc0rd")[:80] or "Chatbot"

        # Content também tem que caber em 2000 chars (limite do Discord).
        # O provider já gera respostas curtas, mas truncamos por segurança.
        safe_content = (content or "").strip()[:2000]
        if not safe_content:
            safe_content = "..."

        try:
            msg = await webhook.send(
                content=safe_content,
                username=safe_name,
                avatar_url=avatar_url or discord.utils.MISSING,
                wait=True,  # precisamos do obj de mensagem pra salvar ref
                allowed_mentions=discord.AllowedMentions.none(),  # bot não pinga ninguém
            )
            return msg
        except discord.NotFound:
            # Webhook foi deletado por alguém. Invalida cache e re-tenta UMA vez.
            log.info("chatbot: webhook NotFound, invalidando cache | channel=%s", channel.id)
            self._cache.pop(channel.id)
            webhook2 = await self._resolve_webhook(channel)
            if webhook2 is None:
                return None
            try:
                return await webhook2.send(
                    content=safe_content,
                    username=safe_name,
                    avatar_url=avatar_url or discord.utils.MISSING,
                    wait=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as e:
                log.warning("chatbot: falha no retry de send | channel=%s err=%s", channel.id, e)
                return None
        except discord.HTTPException as e:
            log.warning(
                "chatbot: falha ao enviar via webhook | channel=%s err=%s",
                channel.id, e,
            )
            return None

    def is_managed_webhook_id(self, webhook_id: Optional[int]) -> bool:
        """True se esse webhook_id está no nosso cache (é um webhook gerenciado).

        Usado pelo listener pra detectar se uma mensagem é "nossa" — importante
        pro trigger de reply (o usuário pode estar respondendo a um chatbot).

        Falso-negativo possível: webhook pode ser nosso mas não estar no cache
        (TTL expirou antes do reply). Nesse caso, o fallback é o cog olhar o
        `msg.author.bot` e o `msg.webhook_id` e comparar com lista conhecida.
        """
        if webhook_id is None:
            return False
        # Iteração linear no cache (tamanho bounded a 100 entries — custa ~microsseg).
        for _channel_id, ref in self._cache._data.items():
            if ref[1].webhook_id == int(webhook_id):
                return True
        return False

    def remember_webhook_id(self, webhook_id: int) -> bool:
        """Registra que um webhook_id específico é nosso — útil quando detectamos
        mensagem nossa via outras heurísticas (p.ex. nome do webhook).

        Retorna True se adicionou algo novo."""
        # Como cache é por channel_id, não dá pra "adicionar por webhook_id".
        # Este método é placeholder pra futura extensão (p.ex. índice reverso).
        return False

    def invalidate_channel(self, channel_id: int) -> None:
        """Força re-fetch na próxima chamada (útil se admin deletou webhook)."""
        self._cache.pop(channel_id)
