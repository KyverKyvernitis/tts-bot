"""Propostas da IA, autorização e execução de ações com estado persistente."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace

import discord

from . import constants as C
from .action_execution import ActionExecutionUncertain, execute_action
from .action_policy import ActionDenied, build_action_context, prepare_action, validate_action
from .action_store import ActionStore
from .action_views import ActionRequestView, render_action_requests
from .memory import MemoryEpoch

log = logging.getLogger(__name__)


@dataclass
class ActionPlan:
    base_reply: str
    requests: list[dict]


class ActionService:
    def __init__(self, cog, collection):
        self.cog = cog
        self.bot = cog.bot
        self.store = ActionStore(collection)
        self.ready = False
        self._views = {}
        self._active_users = set()
        self._admission_lock = asyncio.Lock()

    async def _reserve(self, request):
        key = (int(request["guild_id"]), int(request["requester_id"]))
        async with self._admission_lock:
            if key in self._active_users or len(self._active_users) >= C.MAX_CONCURRENT_ACTIONS:
                return False
            self._active_users.add(key)
            return True

    def _release(self, request):
        self._active_users.discard((int(request["guild_id"]), int(request["requester_id"])))

    def shutdown(self):
        self.ready = False
        for view in self._views.values():
            view.stop()
        self._views.clear()

    def track_view(self, message_id, view):
        old = self._views.pop(int(message_id), None)
        if old is not None and old is not view:
            old.stop()
        if view is not None:
            if any(not item.disabled for item in view.children):
                self._views[int(message_id)] = view
            else:
                view.stop()

    async def initialize(self):
        await self.cleanup()
        requests = await self.store.pending()
        groups = {}
        for request in requests:
            key = (request["guild_id"], request["channel_id"], request["message_id"])
            groups.setdefault(key, []).append(request)
        for (_, _, message_id), group in groups.items():
            view = ActionRequestView(self, group)
            if view.children:
                self.bot.add_view(view, message_id=message_id)
                self.track_view(message_id, view)
        self.ready = True

    async def cleanup(self):
        changed = await self.store.expire_pending()
        changed += await self.store.recover_stale_executing()
        for request in changed:
            if request.get("message_id"):
                await self.refresh_message(request)

    async def describe(self, message, config, *, reply_target=None):
        context = await build_action_context(self.bot, message, config, reply_target=reply_target)
        if not self.ready or C.SAFE_MODE:
            return replace(context, actions=(), description="Neste turno não há ações executáveis disponíveis. Responda apenas em texto.")
        # Resultados operacionais são isolados pelo mesmo canal e solicitante.
        # Não trazer texto privado de fala ou pedidos pendentes para o prompt.
        try:
            results = await self.store.recent_results(
                message.guild.id, message.channel.id, message.author.id,
            )
            summaries = []
            for result in results:
                action = result.get("action", "")
                state = result.get("state", "")
                if action in {"send_audio", "speak_voice", "join_voice", "ban_member"}:
                    summaries.append(f"{action}: {state}")
            if summaries:
                context = replace(context, description=context.description + "\nResultados reais recentes: " + "; ".join(summaries))
        except Exception as exc:
            log.warning("chatbot: resultados de ações indisponíveis (%s)", type(exc).__name__)
        return context

    async def plan(self, message, reply, context, config, *, epoch=None, visibility_scope="") -> ActionPlan:
        base = self.cog._sanitize_model_reply(reply.text)[:1200]
        proposals = reply.proposals[:2]
        private_requests = [p for p in proposals if p.action in {"send_audio", "speak_voice"} and p.ask_permission]
        if private_requests:
            # Uma outra proposta poderia copiar a fala para seu motivo público.
            # Pedidos opcionais de áudio ficam sozinhos, sem campos de prévia.
            proposals = (private_requests[0],)
            base = ""
        requests = []
        seen = set()
        for proposal in proposals:
            if proposal.action not in context.actions or proposal.action in seen:
                continue
            try:
                data = await prepare_action(
                    self.bot, message, proposal, targets=context.targets, config=config,
                )
            except ActionDenied:
                # Uma proposta malformada nunca vira ação. Não reproduzir os
                # argumentos privados da ferramenta numa mensagem de erro.
                continue
            seen.add(proposal.action)
            if data["action"] in {"send_audio", "speak_voice"} and data["ask_permission"]:
                base = ""
            data["base_reply"] = base
            if epoch is not None:
                data["memory_epoch"] = {
                    "global_generation": epoch.global_generation,
                    "guild_generation": epoch.guild_generation,
                    "user_generation": epoch.user_generation,
                }
                data["visibility_scope"] = visibility_scope
            requests.append(await self.store.create(data))
        # Um pedido opcional de áudio não pode expor o texto de outro campo
        # da resposta, inclusive quando há mais de uma proposta no mesmo turno.
        if any(r["action"] in {"send_audio", "speak_voice"} and r["ask_permission"] for r in requests):
            base = ""
        for request in requests:
            request["base_reply"] = base
        return ActionPlan(base, requests)

    @staticmethod
    def content(plan: ActionPlan) -> str:
        footer = render_action_requests(plan.requests)
        base = plan.base_reply[:max(0, 2000 - len(footer) - 2)].rstrip()
        return "\n\n".join(part for part in (base, footer) if part).strip()[:2000]

    def view(self, plan: ActionPlan):
        view = ActionRequestView(self, plan.requests)
        return view if view.children else None

    async def bind_and_start(self, plan: ActionPlan, sent):
        for request in plan.requests:
            if not await self.store.bind(request["request_id"], sent.id):
                continue
            request["message_id"] = sent.id
            request["state"] = "pending"
            if not request["ask_permission"]:
                self.cog._supervisor.create(
                    self._start_automatic(request), name=f"chatbot-action-{request['request_id']}",
                )

    async def _current_config(self, request):
        store = getattr(self.cog, "_config", None)
        if store is None or C.SAFE_MODE or not self.ready:
            raise ActionDenied("As ações do chatbot estão indisponíveis agora.")
        config = await store.get_config(request["guild_id"], fresh=True)
        channel = self.bot.get_channel(request["channel_id"])
        if channel is None:
            raise ActionDenied("O canal original não está disponível.")
        if not config.enabled or not config.actions_enabled or not config.allows_channel(
            request["channel_id"], parent_id=getattr(channel, "parent_id", None),
        ):
            raise ActionDenied("O chatbot ou as ações foram desativados neste canal.")
        action = request["action"]
        enabled = {
            "send_audio": config.audio_actions_enabled,
            "speak_voice": config.voice_actions_enabled,
            "join_voice": config.voice_actions_enabled,
            "ban_member": config.moderation_actions_enabled,
        }.get(action, False)
        if not enabled:
            raise ActionDenied("Esta ação foi desativada pela staff.")
        return config

    async def _start_automatic(self, request):
        # Somente áudio/fala podem dispensar aprovação. Não confiar apenas no
        # booleano devolvido pelo modelo ou num documento alterado.
        if request["action"] not in {"send_audio", "speak_voice"}:
            return
        reserved, transferred = False, False
        try:
            await self._current_config(request)
            await validate_action(self.bot, request, request["requester_id"])
            reserved = await self._reserve(request)
            if not reserved:
                raise ActionDenied("Já estou executando outras ações. Tente novamente quando terminarem.")
            claimed = await self.store.claim(
                request["request_id"], guild_id=request["guild_id"], channel_id=request["channel_id"],
                message_id=request["message_id"], actor_id=request["requester_id"],
            )
            if claimed:
                transferred = True
                await self._execute(claimed, request["requester_id"])
        except ActionDenied:
            await self.store.reject(
                request["request_id"], guild_id=request["guild_id"], channel_id=request["channel_id"],
                message_id=request["message_id"], actor_id=request["requester_id"],
            )
            await self.refresh_message(request)
        except Exception as exc:
            log.warning("chatbot: falha ao iniciar áudio automático (%s)", type(exc).__name__)
        finally:
            if reserved and not transferred:
                self._release(request)

    @staticmethod
    async def _notice(interaction, text):
        kwargs = {"ephemeral": True, "allowed_mentions": discord.AllowedMentions.none()}
        if interaction.response.is_done():
            await interaction.followup.send(text, **kwargs)
        else:
            await interaction.response.send_message(text, **kwargs)

    async def handle_interaction(self, interaction, request_id, *, approve):
        # Conferir vínculo antes de usar qualquer ID ou alvo do pedido.
        reserved, transferred, request = False, False, None
        try:
            request = await self.store.get(request_id)
            guild_id = getattr(getattr(interaction, "guild", None), "id", None)
            channel_id = getattr(interaction, "channel_id", None)
            message_id = getattr(getattr(interaction, "message", None), "id", None)
            if request is None or (guild_id, channel_id, message_id) != (
                request["guild_id"], request["channel_id"], request.get("message_id"),
            ):
                await self._notice(interaction, "Este pedido não pertence a esta mensagem.")
                return
            if request["state"] != "pending":
                await self._notice(interaction, "Este pedido já foi encerrado ou está sendo executado.")
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            actor_id = int(interaction.user.id)
            if approve:
                await self._current_config(request)
            await validate_action(self.bot, request, actor_id, reject=not approve)
            arguments = dict(guild_id=guild_id, channel_id=channel_id, message_id=message_id, actor_id=actor_id)
            if not approve:
                changed = await self.store.reject(request_id, **arguments)
                await self._notice(interaction, "Pedido rejeitado." if changed else "O pedido expirou ou já foi atendido.")
                await self.refresh_message(request)
                return
            reserved = await self._reserve(request)
            if not reserved:
                await self._notice(interaction, "Já estou executando uma ação para esse membro ou estou ocupado. Tente novamente quando terminar.")
                return
            claimed = await self.store.claim(request_id, **arguments)
            if claimed is None:
                await self._notice(interaction, "O pedido expirou ou já foi atendido.")
                await self.cleanup()
                await self.refresh_message(request)
                return
            await self._notice(interaction, "Aprovado. Vou executar a ação.")
            await self.refresh_message(claimed)
            self.cog._supervisor.create(
                self._execute(claimed, actor_id), name=f"chatbot-action-{request_id}",
            )
            transferred = True
        except ActionDenied as exc:
            await self._notice(interaction, str(exc))
        except Exception as exc:
            log.warning("chatbot: falha no pedido de ação (%s)", type(exc).__name__)
            await self._notice(interaction, "Não consegui processar este pedido. Tente novamente.")
        finally:
            if reserved and not transferred and request is not None:
                self._release(request)

    async def _execute(self, request, actor_id):
        try:
            await self._execute_reserved(request, actor_id)
        finally:
            self._release(request)

    async def _execute_reserved(self, request, actor_id):
        state, public_result = "failed", "Não consegui executar a ação."
        try:
            await self._current_config(request)
            await validate_action(self.bot, request, actor_id)
            result = await asyncio.wait_for(
                execute_action(self.bot, request, actor_id=actor_id), timeout=C.ACTION_EXECUTION_TIMEOUT_SECONDS,
            )
            state, public_result = "succeeded", result.public_result
            if result.message_id:
                await self.cog._remember_sent_message(
                    guild_id=request["guild_id"], channel_id=request["channel_id"], message_id=result.message_id,
                )
            stored_epoch = request.get("memory_epoch")
            spoken = request.get("payload", {}).get("text")
            if request["action"] in {"send_audio", "speak_voice"} and stored_epoch and spoken:
                # Continuidade da conversa depois de ouvir o áudio. A fala só
                # entra na memória após envio/reprodução confirmados, nunca na
                # prévia pública do pedido. O epoch original respeita resets.
                await self.cog._persist_turn(
                    guild_id=request["guild_id"], user_id=request["requester_id"],
                    channel_id=request["channel_id"], visibility_scope=request["visibility_scope"],
                    epoch=MemoryEpoch(**stored_epoch), user_name="",
                    user_message="[Resposta em áudio à mensagem anterior]",
                    assistant_message=self.cog._sanitize_model_reply(spoken),
                )
        except (ActionExecutionUncertain, asyncio.TimeoutError):
            state, public_result = "uncertain", "Não consegui confirmar o resultado. Não vou repetir automaticamente."
        except ActionDenied as exc:
            public_result = str(exc)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self.store.finish(
                    request["request_id"], request["execution_token"], state="uncertain",
                    public_result="A execução foi interrompida; confira o resultado antes de tentar novamente.",
                ))
            finally:
                raise
        except Exception as exc:
            # Depois do início, uma falha de rede pode ter ocorrido após o
            # efeito no Discord. Nunca repetir uma operação em dúvida.
            state, public_result = "uncertain", "Não consegui confirmar o resultado. Não vou repetir automaticamente."
            log.warning("chatbot: resultado de ação incerto (%s)", type(exc).__name__)
        await self.store.finish(
            request["request_id"], request["execution_token"], state=state, public_result=public_result,
        )
        await self.refresh_message(request)

    async def refresh_message(self, request):
        if not request.get("message_id"):
            return
        try:
            channel = self.bot.get_channel(request["channel_id"])
            if channel is None or getattr(getattr(channel, "guild", None), "id", None) != request["guild_id"]:
                return
            group = await self.store.list_for_message(
                request["guild_id"], request["channel_id"], request["message_id"],
            )
            if not group:
                return
            plan = ActionPlan(group[0].get("base_reply", ""), group)
            message = await channel.fetch_message(request["message_id"])
            if getattr(getattr(message, "author", None), "id", None) != getattr(self.bot.user, "id", None):
                return
            view = self.view(plan)
            await message.edit(
                content=self.content(plan), view=view, allowed_mentions=discord.AllowedMentions.none(),
            )
            self.track_view(request["message_id"], view)
        except Exception as exc:
            log.warning("chatbot: cartão de ação não atualizado (%s)", type(exc).__name__)
