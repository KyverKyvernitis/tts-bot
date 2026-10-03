"""Etapas persistentes e cartões temporários de permissão."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
import discord
from . import constants as C
from .action_execution import ActionExecutionUncertain, execute_action
from .action_policy import ActionDenied, _can_view_voice, _fresh_member, _requester_can_view, build_action_context, prepare_action, validate_action
from .action_protocol import MAX_PROPOSALS
from .action_store import ActionStore
from .action_views import ActionRequestView, render_action_requests
from .memory import MemoryEpoch

log = logging.getLogger(__name__)
_AUDIO = {"send_audio", "speak_voice"}
_STAFF = {"join_voice", "ban_member"}

@dataclass
class ActionPlan:
    base_reply: str
    requests: list[dict]
    public_error: str = ""

class ActionService:
    def __init__(self, cog, collection):
        self.cog, self.bot = cog, cog.bot
        self.store = ActionStore(collection)
        self.ready = False
        self._views, self._active_users, self._scheduled = {}, set(), set()
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
        if view is not None and any(not item.disabled for item in view.children):
            self._views[int(message_id)] = view
        elif view is not None:
            view.stop()

    async def initialize(self):
        await self.store.cancel_legacy_audio_approvals()
        await self.cleanup(schedule=False)
        for request in await self.store.pending():
            if request["action"] in _STAFF:
                view = ActionRequestView(self, [request])
                self.bot.add_view(view, message_id=request["message_id"])
                self.track_view(request["message_id"], view)
        self.ready = True
        await self._schedule_ready()

    async def cleanup(self, *, schedule=True):
        await self.store.expire_pending()
        await self.store.recover_stale_executing()
        await self.store.recover_stale_publishing()
        for request in await self.store.cards_to_remove():
            await self._delete_card(request)
        if schedule and self.ready:
            await self._schedule_ready()

    async def describe(self, message, config, *, reply_target=None):
        context = await build_action_context(self.bot, message, config, reply_target=reply_target)
        if not self.ready or C.SAFE_MODE:
            return replace(context, actions=(), description="Neste turno não há ações executáveis disponíveis. Responda apenas em texto.")
        try:
            summaries = [f"{r['action']}: {r.get('state', '')}" for r in await self.store.recent_results(
                message.guild.id, message.channel.id, message.author.id) if r.get("action") in _AUDIO | _STAFF]
            if summaries:
                context = replace(context, description=context.description + "\nResultados reais recentes: " + "; ".join(summaries))
        except Exception as exc:
            log.warning("chatbot: resultados de ações indisponíveis (%s)", type(exc).__name__)
        return context

    async def plan(self, message, reply, context, config, *, epoch=None, visibility_scope=""):
        proposals = reply.proposals[:MAX_PROPOSALS]
        base = "" if any(p.action in _AUDIO for p in proposals) else self.cog._sanitize_model_reply(reply.text)[:1200]
        steps, seen, join_channel = [], set(), None
        for proposal in proposals:
            if proposal.action not in context.actions:
                return ActionPlan(base, [], "Essa ação não está disponível neste momento.")
            try:
                options = {"deferred_voice_channel_id": join_channel} if proposal.action == "speak_voice" and join_channel is not None else {}
                data = await prepare_action(self.bot, message, proposal, targets=context.targets, config=config, **options)
            except ActionDenied as exc:
                return ActionPlan(base, [], str(exc))
            payload = data["payload"]
            fingerprint = (data["action"], payload.get("target_id"), payload.get("voice_channel_id"),
                           payload.get("text", ""), payload.get("reason", ""))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            if data["action"] in _AUDIO and any(step["action"] in _AUDIO for step in steps):
                return ActionPlan(base, [], "Cada resposta pode ter um único áudio ou fala na call.")
            if data["action"] == "join_voice":
                if join_channel is not None:
                    return ActionPlan(base, [], "Uma sequência pode pedir entrada em uma única call.")
                join_channel = payload["voice_channel_id"]
            data["ask_permission"], data["base_reply"] = data["action"] in _STAFF, ""
            if epoch is not None:
                data["memory_epoch"] = {"global_generation": epoch.global_generation, "guild_generation": epoch.guild_generation, "user_generation": epoch.user_generation}
                data["visibility_scope"] = visibility_scope
            steps.append(data)
        return ActionPlan(base, await self.store.create_plan(steps) if steps else [])

    @staticmethod
    def content(plan):
        return plan.base_reply[:2000]

    def view(self, plan):
        return None  # controles só pertencem ao cartão temporário

    async def bind_and_start(self, plan, sent=None):
        if sent is not None:
            for request in plan.requests:
                await self.store.bind_context(request["request_id"], sent.id)
        if plan.requests:
            self._schedule(plan.requests[0])

    def _schedule(self, request):
        request_id = request["request_id"]
        if not self.ready or request_id in self._scheduled:
            return
        self._scheduled.add(request_id)
        async def run():
            try:
                await self._activate(request_id)
            finally:
                self._scheduled.discard(request_id)
        self.cog._supervisor.create(run(), name=f"chatbot-step-{request_id}")

    async def _schedule_ready(self):
        for request in await self.store.recover_ready():
            self._schedule(request)

    async def _activate(self, request_id):
        request = await self.store.get(request_id)
        if request is None:
            return
        if request["action"] in _AUDIO and not request.get("ask_permission"):
            if request["state"] == "created":
                if not await self.store.arm_automatic(request_id):
                    return
                request = await self.store.get(request_id)
            if request["state"] == "pending":
                await self._start_automatic(request)
        elif request["action"] in _STAFF and request["state"] == "created":
            await self._publish_card(request)

    async def _current_config(self, request):
        store = getattr(self.cog, "_config", None)
        if store is None or C.SAFE_MODE or not self.ready:
            raise ActionDenied("As ações do chatbot estão indisponíveis agora.")
        config = await store.get_config(request["guild_id"], fresh=True)
        channel = self.bot.get_channel(request["channel_id"])
        if channel is None or getattr(getattr(channel, "guild", None), "id", None) != request["guild_id"]:
            raise ActionDenied("O canal original não está disponível.")
        if not config.enabled or not config.actions_enabled or not config.allows_channel(request["channel_id"], parent_id=getattr(channel, "parent_id", None)):
            raise ActionDenied("O chatbot ou as ações foram desativados neste canal.")
        enabled = {"send_audio": config.audio_actions_enabled, "speak_voice": config.voice_actions_enabled,
                   "join_voice": config.voice_actions_enabled, "ban_member": config.moderation_actions_enabled}.get(request["action"], False)
        if not enabled:
            raise ActionDenied("Esta ação foi desativada pela staff.")
        return config

    async def _publish_card(self, request):
        claimed = await self.store.claim_publication(request["request_id"])
        if claimed is None:
            return
        try:
            await self._current_config(claimed)
            guild = self.bot.get_guild(claimed["guild_id"])
            requester = await _fresh_member(guild, claimed["requester_id"])
            channel = self.bot.get_channel(claimed["channel_id"])
            await _requester_can_view(channel, requester)
            if claimed["action"] == "join_voice" and not _can_view_voice(guild.get_channel(claimed["payload"]["voice_channel_id"]), requester):
                raise ActionDenied("O solicitante precisa poder ver a call autorizada.")
            view = ActionRequestView(self, [claimed])
            reference = discord.MessageReference(message_id=claimed["origin_message_id"], channel_id=channel.id, guild_id=guild.id, fail_if_not_exists=False)
            sent = await channel.send(render_action_requests([claimed]), view=view, reference=reference, allowed_mentions=discord.AllowedMentions.none())
            if not await self.store.bind(claimed["request_id"], sent.id):
                await sent.delete()
                view.stop()
                return
            self.track_view(sent.id, view)
        except ActionDenied as exc:
            await self.store.fail_unpublished(claimed["request_id"], public_result=str(exc))
        except Exception as exc:
            await self.store.fail_unpublished(claimed["request_id"], public_result="Não consegui confirmar a publicação do pedido.", uncertain=True)
            log.warning("chatbot: pedido não publicado (%s)", type(exc).__name__)

    async def _start_automatic(self, request):
        if request["action"] not in _AUDIO or request.get("ask_permission"):
            return
        reserved = await self._reserve(request)
        if not reserved:
            return  # conserva pendente para retomar quando houver vaga
        transferred = False
        try:
            claimed = await self.store.claim_automatic(request["request_id"], guild_id=request["guild_id"], channel_id=request["channel_id"], actor_id=request["requester_id"])
            if claimed:
                transferred = True
                await self._execute(claimed, request["requester_id"])
        finally:
            if not transferred:
                self._release(request)

    @staticmethod
    async def _notice(interaction, text):
        kwargs = {"ephemeral": True, "allowed_mentions": discord.AllowedMentions.none()}
        if interaction.response.is_done():
            await interaction.followup.send(text, **kwargs)
        else:
            await interaction.response.send_message(text, **kwargs)

    async def handle_interaction(self, interaction, request_id, *, approve):
        reserved, transferred, request = False, False, None
        try:
            request = await self.store.get(request_id)
            scope = (getattr(getattr(interaction, "guild", None), "id", None), getattr(interaction, "channel_id", None), getattr(getattr(interaction, "message", None), "id", None))
            if request is None or scope != (request["guild_id"], request["channel_id"], request.get("message_id")):
                await self._notice(interaction, "Este pedido não pertence a esta mensagem.")
                return
            if request["action"] not in _STAFF or request["state"] != "pending":
                await self._notice(interaction, "Este pedido já foi encerrado ou não está disponível.")
                return
            await interaction.response.defer(thinking=False)
            actor_id = int(interaction.user.id)
            if approve:
                await self._current_config(request)
            await validate_action(self.bot, request, actor_id, reject=not approve)
            arguments = dict(guild_id=scope[0], channel_id=scope[1], message_id=scope[2], actor_id=actor_id)
            if not approve:
                if await self.store.reject(request_id, **arguments):
                    await self._delete_card(request)
                else:
                    await self._notice(interaction, "O pedido expirou ou já foi atendido.")
                    await self.cleanup()
                return
            reserved = await self._reserve(request)
            if not reserved:
                await self._notice(interaction, "Já estou executando uma ação para esse membro ou estou ocupado. Tente quando terminar.")
                return
            claimed = await self.store.claim(request_id, **arguments)
            if claimed is None:
                await self._notice(interaction, "O pedido expirou ou já foi atendido.")
                await self.cleanup()
                return
            await self._delete_card(claimed)
            self.cog._supervisor.create(self._execute(claimed, actor_id, interaction=interaction), name=f"chatbot-action-{request_id}")
            transferred = True
        except ActionDenied as exc:
            await self._notice(interaction, str(exc))
        except Exception as exc:
            log.warning("chatbot: falha no pedido de ação (%s)", type(exc).__name__)
            await self._notice(interaction, "Não consegui processar este pedido. Tente novamente.")
        finally:
            if reserved and not transferred and request is not None:
                self._release(request)

    async def _execute(self, request, actor_id, *, interaction=None):
        try:
            outcome = await self._execute_reserved(request, actor_id)
            if interaction is not None and outcome[0] in {"failed", "uncertain"}:
                try:
                    await self._notice(interaction, outcome[1])
                except Exception as exc:
                    log.warning("chatbot: erro de ação não informado ao aprovador (%s)", type(exc).__name__)
        finally:
            self._release(request)
        await self._schedule_ready()  # sucessor somente depois de liberar a reserva

    async def _record_spoken(self, request, *, audio=True):
        epoch, spoken = request.get("memory_epoch"), request.get("payload", {}).get("text")
        if request["action"] in _AUDIO and epoch and spoken:
            try:
                await self.cog._persist_turn(guild_id=request["guild_id"], user_id=request["requester_id"], channel_id=request["channel_id"],
                    visibility_scope=request["visibility_scope"], epoch=MemoryEpoch(**epoch), user_name="",
                    user_message="[Resposta em áudio à mensagem anterior]" if audio else "[Resposta textual após falha do áudio]",
                    assistant_message=self.cog._sanitize_model_reply(spoken))
            except Exception as exc:
                # Memória indisponível não muda o resultado de um efeito que
                # já foi confirmado nem autoriza repeti-lo.
                log.warning("chatbot: memória da resposta indisponível (%s)", type(exc).__name__)

    async def _audio_fallback(self, request):
        text = request.get("payload", {}).get("text")
        if request["action"] != "send_audio" or not text:
            return
        try:
            config = await self.cog._config.get_config(request["guild_id"], fresh=True)
            channel = self.bot.get_channel(request["channel_id"])
            if (channel is None or getattr(getattr(channel, "guild", None), "id", None) != request["guild_id"]
                    or not config.enabled or not config.allows_channel(channel.id, parent_id=getattr(channel, "parent_id", None))):
                return
            requester = await _fresh_member(channel.guild, request["requester_id"])
            await _requester_can_view(channel, requester)
            epoch, memory = request.get("memory_epoch"), getattr(self.cog, "_memory", None)
            if epoch and memory is not None and await memory.capture_epoch(channel.guild.id, requester.id) != MemoryEpoch(**epoch):
                return
            me = await _fresh_member(channel.guild, self.bot.user.id)
            config = await self.cog._config.get_config(request["guild_id"], fresh=True)
            permissions = channel.permissions_for(me)
            send_permission = "send_messages_in_threads" if isinstance(channel, discord.Thread) else "send_messages"
            if (not config.enabled or not config.allows_channel(channel.id, parent_id=getattr(channel, "parent_id", None))
                    or not getattr(channel.permissions_for(requester), "view_channel", False)
                    or not all(getattr(permissions, name, False) for name in ("view_channel", send_permission))):
                return
            sent = await channel.send(self.cog._sanitize_model_reply(text)[:2000], allowed_mentions=discord.AllowedMentions.none())
            await self.cog._remember_sent_message(guild_id=request["guild_id"], channel_id=channel.id, message_id=sent.id)
            await self._record_spoken(request, audio=False)
        except Exception as exc:
            log.warning("chatbot: resposta textual após falha do áudio indisponível (%s)", type(exc).__name__)

    async def _execute_reserved(self, request, actor_id):
        state, public_result = "failed", "Não consegui executar a ação."
        try:
            await self._current_config(request)
            await validate_action(self.bot, request, actor_id)
            result = await asyncio.wait_for(execute_action(self.bot, request, actor_id=actor_id), timeout=C.ACTION_EXECUTION_TIMEOUT_SECONDS)
            state, public_result = "succeeded", result.public_result
            if result.message_id:
                await self.cog._remember_sent_message(guild_id=request["guild_id"], channel_id=request["channel_id"], message_id=result.message_id)
            await self._record_spoken(request)
        except (ActionExecutionUncertain, asyncio.TimeoutError):
            state, public_result = "uncertain", "Não consegui confirmar o resultado. Não vou repetir automaticamente."
        except ActionDenied as exc:
            public_result = str(exc)
            await self._audio_fallback(request)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self.store.finish(request["request_id"], request["execution_token"], state="uncertain",
                    public_result="A execução foi interrompida; confira o resultado antes de tentar novamente."))
            finally:
                raise
        except Exception as exc:
            state, public_result = "uncertain", "Não consegui confirmar o resultado. Não vou repetir automaticamente."
            log.warning("chatbot: resultado de ação incerto (%s)", type(exc).__name__)
        await self.store.finish(request["request_id"], request["execution_token"], state=state, public_result=public_result)
        await self._delete_card(request)
        return state, public_result

    async def _delete_card(self, request):
        if not request.get("message_id") or (request["action"] not in _STAFF and not (
                request["action"] in _AUDIO and request.get("ask_permission"))):
            return
        current = await self.store.get(request["request_id"])
        if current is not None and current.get("card_removed"):
            return
        removed = False
        message = None
        try:
            channel = self.bot.get_channel(request["channel_id"])
            if channel is None or getattr(getattr(channel, "guild", None), "id", None) != request["guild_id"]:
                return
            message = await channel.fetch_message(request["message_id"])
            if getattr(getattr(message, "author", None), "id", None) != getattr(self.bot.user, "id", None):
                return
            await message.delete()
            removed = True
        except discord.NotFound:
            removed = True
        except Exception as exc:
            log.warning("chatbot: pedido temporário não removido (%s)", type(exc).__name__)
            if message is not None and getattr(getattr(message, "author", None), "id", None) == getattr(self.bot.user, "id", None):
                try:
                    await message.edit(view=None)
                except Exception:
                    pass
        finally:
            self.track_view(request["message_id"], None)
        if removed:
            await self.store.mark_card_removed(request["request_id"])

    async def refresh_message(self, request):
        if request.get("state") not in {"created", "publishing", "pending", "blocked"}:
            await self._delete_card(request)
