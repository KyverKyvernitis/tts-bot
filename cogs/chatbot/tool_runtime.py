"""Ferramentas declaradas e delimitadas ao autor e ao canal desta conversa.

O modelo escolhe as chamadas nativas. Este módulo resolve objetos reais,
revalida o contexto e devolve resultados; nunca executa comandos de terminal,
consultas arbitrárias ao banco ou texto livre como uma ação.
"""
from __future__ import annotations

import asyncio
import inspect
import io
import json
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime

import discord

from . import constants as C
from . import action_policy as policy
from .action_protocol import MAX_PROPOSALS, parse_proposal, proposal_tool
from .memory import MemoryEpoch
from .tool_memory import FactStore
from .tool_registry import InvalidToolArguments, ToolRegistry, ToolSpec, validate_tool_arguments

log = logging.getLogger(__name__)
_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


def _schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {},
            "required": list(required), "additionalProperties": False}


def _string(maximum, *, minimum=0):
    return {"type": "string", "minLength": minimum, "maxLength": maximum}


def _result(data=None, *, status="available"):
    result = {"ok": True, "status": status}
    if data is not None:
        result["data"] = data
    return result


def _json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError("Resultado contém um objeto não serializável.")


def _actions_ready(cog):
    return getattr(getattr(cog, "_actions", None), "ready", False) is True


async def _after_delivery(operations):
    """Depois do envio confirmado, metadados nunca autorizam um segundo envio."""
    for label, operation in operations:
        try:
            await asyncio.wait_for(operation(), timeout=2)
        except asyncio.CancelledError:
            # O orçamento do turno acabou, mas o arquivo/texto já foi entregue.
            # Devolver o resultado confirmado impede o modelo de repetir o envio.
            return False
        except Exception as exc:
            log.warning("chatbot: %s após conversão indisponível (%s)", label, type(exc).__name__)
    return True


@dataclass
class ToolRuntimeContext:
    cog: object
    message: object
    config: object
    epoch: MemoryEpoch | None
    visibility_scope: str
    action_context: object = None
    reply_target: object = None
    action_proposals: list = field(default_factory=list)
    prepared_actions: list = field(default_factory=list)
    targets: dict = field(default_factory=dict)
    resources: dict = field(default_factory=dict)
    response_format: str | None = None
    response_sent: bool = False
    proposals_invalid: bool = False
    proposals_error: str = ""

    @property
    def guild_id(self):
        return int(self.message.guild.id)

    @property
    def channel_id(self):
        return int(self.message.channel.id)

    @property
    def user_id(self):
        return int(self.message.author.id)

    async def guard(self, *, audio=False):
        """Uma referência guardada não substitui acesso e configuração atuais."""
        if C.SAFE_MODE:
            raise policy.ActionDenied("As ferramentas estão suspensas neste momento.")
        guild = self.cog.bot.get_guild(self.guild_id)
        if guild is None:
            raise policy.ActionDenied("Este servidor não está mais disponível.")
        store = getattr(self.cog, "_config", None)
        if store is None:
            raise policy.ActionDenied("A configuração da conversa não está disponível.")
        config = await store.get_config(self.guild_id, fresh=True)
        channel = policy._origin_channel(guild, {"channel_id": self.channel_id}, config)
        member = await policy._fresh_member(guild, self.user_id)
        if member.bot:
            raise policy.ActionDenied("A conversa precisa pertencer a um membro do servidor.")
        await policy._requester_can_view(channel, member)
        memory = getattr(self.cog, "_memory", None)
        if self.epoch is None or memory is None:
            raise policy.ActionDenied("A memória desta conversa foi reiniciada. Comece um novo pedido.")
        me = await policy._fresh_member(guild, int(self.cog.bot.user.id)) if audio else None
        # Consultas de acesso e membro podem suspender a execução. Revalidar
        # configuração depois delas, e geração por último, impede usar o turno
        # anterior a uma revogação/reset ocorrido durante essas consultas.
        config = await store.get_config(self.guild_id, fresh=True)
        policy._origin_channel(guild, {"channel_id": self.channel_id}, config)
        if audio:
            if not policy._enabled(config, "send_audio"):
                raise policy.ActionDenied("O envio de áudio foi desativado neste servidor.")
            policy._audio_channel(guild, self.channel_id, me)
        if await memory.capture_epoch(self.guild_id, self.user_id) != self.epoch:
            raise policy.ActionDenied("A memória desta conversa foi reiniciada. Comece um novo pedido.")
        if C.SAFE_MODE:
            raise policy.ActionDenied("As ferramentas estão suspensas neste momento.")
        self.config = config
        return guild, channel, member, config

    def reference(self, mapping, obj, prefix):
        for ref, current in mapping.items():
            if getattr(current, "id", None) == getattr(obj, "id", None):
                return ref
        number = 1
        while f"{prefix}{number}" in mapping:
            number += 1
        ref = f"{prefix}{number}"
        mapping[ref] = obj
        return ref


def refresh_action_spec(registry):
    runtime = registry.runtime
    context = runtime.action_context
    if context is None:
        return
    # Habilitar uma opção não torna um backend, alvo ou permissão disponível.
    # A fábrica e os resolvedores recompõem este snapshot pela política real.
    actions = context.actions if _actions_ready(runtime.cog) else ()
    refs = list(runtime.targets)
    for member in runtime.targets.values():
        refs.extend((str(member.id), f"<@{member.id}>", f"<@!{member.id}>"))
    declaration = proposal_tool(actions, tuple(refs))
    registry.unregister_owner("runtime_actions")

    async def propose(arguments):
        if runtime.proposals_invalid:
            raise policy.ActionDenied("Esta sequência foi interrompida porque uma etapa não pôde ser preparada.")
        _guild, _channel, _member, config = await runtime.guard()
        if not _actions_ready(runtime.cog):
            raise policy.ActionDenied("O serviço de ações ainda não está disponível. Tente mais tarde.")
        proposal = parse_proposal("propor_acao", arguments, runtime.action_context.actions)
        if len(runtime.action_proposals) >= MAX_PROPOSALS:
            raise policy.ActionDenied("Uma sequência pode ter até quatro ações.")
        kwargs = {"targets": runtime.targets, "config": config}
        if "resources" in inspect.signature(policy.prepare_action).parameters:
            kwargs["resources"] = runtime.resources
        previous_voice = next((step for step in reversed(runtime.prepared_actions)
                               if step["action"] in {"join_voice", "move_voice"}), None)
        if proposal.action == "speak_voice" and previous_voice:
            kwargs["deferred_voice_channel_id"] = previous_voice["payload"]["voice_channel_id"]
            if previous_voice["action"] == "move_voice":
                kwargs["deferred_voice_source_channel_id"] = previous_voice["payload"]["source_voice_channel_id"]
        prepared = await policy.prepare_action(runtime.cog.bot, runtime.message, proposal, **kwargs)
        if not _actions_ready(runtime.cog):
            raise policy.ActionDenied("O serviço de ações foi interrompido. Comece um novo pedido mais tarde.")
        runtime.action_proposals.append(proposal)
        runtime.prepared_actions.append(prepared)
        return _result({"action": proposal.action,
                        "requires_staff": bool(prepared.get("ask_permission")),
                        "executed": False}, status="proposed")

    registry.register(ToolSpec(declaration["name"], declaration["description"], declaration["parameters"],
                               permission="áudio/fala automáticos; staff para ações privilegiadas", handler=propose, available=bool(actions),
                               why=("" if actions else "O serviço de ações não está disponível." if not _actions_ready(runtime.cog) else "Não há ações habilitadas."),
                               availability=lambda _context: (bool(actions) and _actions_ready(runtime.cog),
                                   "" if actions and _actions_ready(runtime.cog) else "O serviço de ações não está disponível." if not _actions_ready(runtime.cog) else "Não há ações habilitadas.")), owner="runtime_actions")


def drain_action_proposals(registry):
    runtime = registry.runtime
    result = () if runtime.proposals_invalid else tuple(runtime.action_proposals)
    runtime.action_proposals.clear()
    return result


async def rebuild_action_context(registry):
    runtime = registry.runtime
    context = await policy.build_action_context(runtime.cog.bot, runtime.message, runtime.config,
        reply_target=runtime.reply_target, trusted_targets=runtime.targets)
    if not _actions_ready(runtime.cog):
        context = replace(context, actions=(), description="O serviço de ações ainda não está disponível neste turno.")
    runtime.targets = context.targets
    runtime.action_context = replace(context, resources=runtime.resources)
    refresh_action_spec(registry)


async def build_tool_registry(cog, message, config, *, epoch, visibility_scope,
                              reply_target=None, action_context=None):
    if action_context is not None and not _actions_ready(cog):
        action_context = replace(action_context, actions=(), description="O serviço de ações ainda não está disponível neste turno.")
    registry = ToolRegistry()
    runtime = ToolRuntimeContext(cog, message, config, epoch, visibility_scope,
                                 action_context=action_context, reply_target=reply_target)
    registry.runtime = runtime
    if action_context is not None:
        runtime.targets = action_context.targets
        runtime.resources = getattr(action_context, "resources", {})
    else:
        runtime.targets = {"autor": message.author}
    runtime.resources.setdefault("canal_atual", message.channel)
    if reply_target is not None and getattr(getattr(reply_target, "channel", None), "id", None) == message.channel.id:
        runtime.reference(runtime.resources, reply_target, "msg")
    if action_context is None:
        await rebuild_action_context(registry)

    def register(name, description, schema, handler, *, permission="read", available=True, why=""):
        registry.register(ToolSpec(name, description, schema, permission=permission,
                                   handler=handler, available=available, why=why))

    async def operational_state(_arguments):
        guild, channel, member, _config = await runtime.guard()
        bot_member = await policy._fresh_member(guild, int(cog.bot.user.id))
        voice_channel = policy._bot_voice_channel(guild)
        visible_voice = voice_channel is not None and policy._can_view_voice(voice_channel, member)
        tts = cog.bot.get_cog("TTSVoice")
        connected = bool(voice_channel is not None)
        recent = []
        if getattr(cog, "_actions", None) is not None:
            recent = [{key: item[key] for key in ("action", "state", "public_result") if key in item}
                      for item in await cog._actions.store.recent_results(runtime.guild_id, runtime.channel_id, runtime.user_id)]
        return _result({
            "identity": {"name": str(getattr(bot_member, "display_name", cog.bot.user.name))[:80],
                         "id": str(cog.bot.user.id),
                         "avatar_url": str(getattr(getattr(bot_member, "display_avatar", None), "url", ""))[:500]},
            "guild_id": str(guild.id), "channel_id": str(channel.id),
            "voice": {"connected": connected, "listening": False,
                      "channel": str(voice_channel.name)[:80] if visible_voice else None,
                      "can_synthesize": callable(getattr(tts, "synthesize_chatbot_attachment", None))},
            "tools": [{"name": spec.name, "available": spec.available, "why": spec.why[:180]}
                      for spec in registry.snapshot()],
            "recent_action_results": recent,
        })

    register("get_operational_state", "Consulte identidade real do bot, conexão de voz e ferramentas disponíveis. O bot não escuta a call.", _EMPTY, operational_state)

    def member_data(guild, member, requester):
        ref = runtime.reference(runtime.targets, member, "m")
        call = policy._voice_channel(member)
        visible_call = call is not None and policy._can_view_voice(call, requester)
        return {"ref": ref, "id": str(member.id), "name": policy._label(member),
                "mention": f"<@{member.id}>", "bot": bool(member.bot),
                "voice_channel": str(call.name)[:80] if visible_call else None}

    async def resolve_member(arguments):
        guild, _channel, requester, _config = await runtime.guard()
        query = arguments["query"].strip()
        match = re.fullmatch(r"(?:<@!?)?([1-9][0-9]{0,20})>?", query)
        if query in runtime.targets:
            candidates = [await policy._fresh_member(guild, runtime.targets[query].id)]
        elif match:
            candidates = [await policy._fresh_member(guild, int(match.group(1)))]
        else:
            lowered = query.casefold()
            cached = list(getattr(guild, "members", ()))
            exact = [member for member in cached if lowered in {
                str(getattr(member, "name", "")).casefold(), str(getattr(member, "display_name", "")).casefold()}]
            candidates = exact or [member for member in cached if lowered in str(getattr(member, "display_name", "")).casefold()][:5]
            if not candidates and len(query) >= 2 and callable(getattr(guild, "query_members", None)):
                candidates = await guild.query_members(query=query, limit=5, cache=False)
        _guild, _channel, requester, _cfg = await runtime.guard()
        data = [member_data(guild, member, requester) for member in candidates[:5]
                if policy._member_in_guild(member, guild)]
        await rebuild_action_context(registry)
        return _result({"members": data, "ambiguous": len(data) > 1, "found": bool(data)})

    register("resolve_member", "Resolva uma menção Discord, ID ou nome do servidor em referências confiáveis. Nomes ambíguos retornam candidatos: confirme com o usuário, sem pedir códigos internos.", _schema({"query": _string(100, minimum=1)}, ("query",)), resolve_member)

    async def accessible_channels(arguments):
        guild, _channel, requester, _config = await runtime.guard()
        query = arguments.get("query", "").casefold()
        results = []
        for channel in list(getattr(guild, "channels", ()))[:500]:
            if query and query not in str(getattr(channel, "name", "")).casefold():
                continue
            try:
                await policy._requester_can_view(channel, requester)
            except policy.ActionDenied:
                continue
            results.append({"ref": runtime.reference(runtime.resources, channel, "c"),
                            "id": str(channel.id), "name": str(channel.name)[:80],
                            "type": str(channel.type)[:24]})
            if len(results) >= min(arguments.get("limit", 10), 20):
                break
        await runtime.guard()
        await rebuild_action_context(registry)
        return _result({"channels": results})

    register("list_accessible_channels", "Liste canais deste servidor que o autor pode acessar. As referências são internas e somente servem a ações permitidas pela política.", _schema({"query": _string(80), "limit": {"type": "integer", "minimum": 1, "maximum": 20}}), accessible_channels)

    async def resolve_role(arguments):
        guild, _channel, _requester, _cfg = await runtime.guard()
        query = arguments["query"].strip().casefold()
        roles = [role for role in getattr(guild, "roles", ()) if query == str(role.id) or query == str(role.name).casefold()]
        if not roles:
            roles = [role for role in getattr(guild, "roles", ()) if query in str(role.name).casefold()][:5]
        return _result({"roles": [{"ref": runtime.reference(runtime.resources, role, "r"),
                                   "name": str(role.name)[:80], "managed": bool(role.managed)} for role in roles[:5]],
                        "ambiguous": len(roles) > 1})

    register("resolve_role", "Encontre cargos pelo nome para referências locais. Encontrar um cargo não autoriza alterá-lo; a staff e a lista permitida continuam obrigatórias.", _schema({"query": _string(100, minimum=1)}, ("query",)), resolve_role)

    async def recent_messages(arguments):
        _guild, channel, _requester, _cfg = await runtime.guard()
        results = []
        async for item in channel.history(limit=arguments.get("limit", 5)):
            if item.guild.id != runtime.guild_id or item.channel.id != runtime.channel_id:
                continue
            results.append({"ref": runtime.reference(runtime.resources, item, "msg"),
                            "author": policy._label(item.author), "content": str(item.content or "")[:400]})
        await runtime.guard()
        return _result({"messages": results})

    register("get_recent_channel_messages", "Consulte até 25 mensagens recentes somente do canal atual. Retorna referências exatas para ações que a staff poderá aprovar; nunca pesquisa outros canais.", _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 25}}), recent_messages)

    async def get_preferences(_arguments):
        await runtime.guard()
        value = await cog._preferences.get_current(runtime.guild_id, runtime.channel_id, runtime.user_id, runtime.epoch)
        return _result(value.to_result())

    async def set_preferences(arguments):
        await runtime.guard()
        tts = cog.bot.get_cog("TTSVoice")
        if arguments.get("voice"):
            names = getattr(tts, "edge_voice_names", ())
            if arguments["voice"] not in names:
                raise policy.ActionDenied("Essa voz não está no catálogo disponível. Consulte list_tts_voices_languages primeiro.")
        if arguments.get("language"):
            languages = getattr(tts, "gtts_languages", {})
            if arguments["language"].lower() not in languages:
                raise policy.ActionDenied("Esse idioma não está no catálogo disponível. Consulte list_tts_voices_languages primeiro.")
        value = await cog._preferences.set_current(runtime.guild_id, runtime.channel_id, runtime.user_id, runtime.epoch, **arguments)
        return _result(value.to_result(), status="executed")

    prefs_available = getattr(cog, "_preferences", None) is not None
    register("get_conversation_preferences", "Consulte preferência pessoal de texto/áudio, voz e idioma desta conversa. Não altera a configuração de outros membros.", _EMPTY, get_preferences, available=prefs_available, why="Preferências não inicializadas.")
    register("set_conversation_preferences", "Guarde ou ajuste preferência que o autor expressou para esta conversa. mode=auto permite alternar; text prefere texto; audio prefere áudio. Voz/idioma vazios voltam ao padrão, campos omitidos são preservados.", _schema({"mode": {"type": "string", "enum": ["auto", "text", "audio"]}, "voice": _string(100), "language": _string(20)}), set_preferences, permission="automatic_effect", available=prefs_available, why="Preferências não inicializadas.")

    async def select_format(arguments):
        await runtime.guard()
        runtime.response_format = arguments["mode"]
        return _result({"mode": runtime.response_format, "persistent": False}, status="selected")

    register("select_response_format", "Escolha texto, áudio ou alternância somente para a resposta deste turno, como quando o autor pede áudio agora ou texto só desta vez. Não salva preferência permanente; use set_conversation_preferences quando o pedido vale para conversas futuras.", _schema({"mode": {"type": "string", "enum": ["auto", "text", "audio"]}}, ("mode",)), select_format, permission="automatic_effect")

    async def list_voices(arguments):
        await runtime.guard()
        tts = cog.bot.get_cog("TTSVoice")
        needle = arguments.get("query", "").casefold()
        names = sorted(name for name in getattr(tts, "edge_voice_names", ()) if isinstance(name, str))
        languages = getattr(tts, "gtts_languages", {})
        if needle:
            names = [name for name in names if needle in name.casefold()]
        rows = [{"code": str(code), "name": str(name)[:80]} for code, name in languages.items()
                if not needle or needle in str(code).casefold() or needle in str(name).casefold()]
        return _result({"voices": names[:20], "languages": rows[:20],
                        "voice_catalog_available": bool(getattr(tts, "edge_voice_names", ())),
                        "more_voices": len(names) > 20, "more_languages": len(rows) > 20})

    tts = cog.bot.get_cog("TTSVoice")
    register("list_tts_voices_languages", "Consulte vozes Edge e idiomas gTTS realmente carregados pelo módulo TTS. Filtre por idioma ou nome. Nunca invente IDs de voz nem escolha uma opção ausente do catálogo.", _schema({"query": _string(100)}), list_voices, available=tts is not None, why="O módulo de voz não está carregado.")

    async def interrupt_speech(_arguments):
        await runtime.guard()
        current = tts.chatbot_own_speech_ref(runtime.guild_id, runtime.user_id)
        if current is None:
            return _result({"interrupted": False, "speaking": False})
        async def before_effect():
            await runtime.guard()
        result = await tts.chatbot_interrupt_speech(
            guild_id=runtime.guild_id, user_id=runtime.user_id, channel_id=current["voice_channel_id"],
            request_id=f"interrupt-{runtime.message.id}", session_ref=current["session_ref"],
            speech_request_id=current["request_id"], before_effect=before_effect)
        return {"ok": bool(result.get("ok")), "status": result.get("status", "failed"),
                "data": {"interrupted": bool(result.get("ok"))},
                "reason": str(result.get("message", ""))[:300]}

    register("interrupt_own_speech", "Interrompa apenas a fala que está sendo reproduzida para o autor desta conversa. Não para música nem falas de outros membros, não move o bot e não entra novamente na call.", _EMPTY, interrupt_speech, permission="automatic_effect", available=callable(getattr(tts, "chatbot_interrupt_speech", None)) and callable(getattr(tts, "chatbot_own_speech_ref", None)), why="Interrupção de fala indisponível.")

    memory = getattr(cog, "_memory", None)
    collection = getattr(memory, "_coll", None)
    facts = FactStore(collection) if collection is not None else None
    def fact_args():
        return runtime.guild_id, runtime.channel_id, runtime.user_id, runtime.visibility_scope, runtime.epoch

    async def query_memory(arguments):
        await runtime.guard()
        reminders = await facts.list(*fact_args(), query=arguments.get("query", ""), limit=arguments.get("limit", 10))
        history = await memory.get_user_history(runtime.guild_id, runtime.user_id,
                                               channel_id=runtime.channel_id, visibility_scope=runtime.visibility_scope,
                                               epoch=runtime.epoch)
        needle = arguments.get("query", "").casefold()
        turns = [{"role": item.role, "text": str(item.content)[:500]} for item in history[-10:]
                 if not needle or needle in str(item.content).casefold()]
        await runtime.guard()
        return _result({"facts": reminders, "recent_conversation": turns})

    async def remember_memory(arguments):
        await runtime.guard()
        saved = await facts.remember(*fact_args(), content=arguments["content"])
        await runtime.guard()
        return _result(saved, status="executed")

    async def forget_memory(arguments):
        await runtime.guard()
        removed = await facts.forget(*fact_args(), ref=arguments["ref"])
        return _result({"removed": removed, "history_deleted": False}, status="executed")

    register("query_own_memory", "Consulte lembretes e histórico pessoal somente do autor neste canal, respeitando resets e privacidade. Não lê memória de outros membros.", _schema({"query": _string(120), "limit": {"type": "integer", "minimum": 1, "maximum": 20}}), query_memory, available=facts is not None, why="Memória indisponível.")
    register("remember_own_fact", "Salve um fato ou lembrete que o autor pediu para lembrar neste canal. A informação é pessoal e deixa de valer quando a memória é reiniciada.", _schema({"content": _string(500, minimum=1)}, ("content",)), remember_memory, permission="automatic_effect", available=facts is not None, why="Memória indisponível.")
    register("forget_own_fact", "Esqueça um lembrete pessoal retornado por query_own_memory. Não apaga conversas do Discord nem memória de outras pessoas; nunca peça a referência interna ao usuário.", _schema({"ref": _string(64, minimum=1)}, ("ref",)), forget_memory, permission="automatic_effect", available=facts is not None, why="Memória indisponível.")

    async def own_requests(_arguments):
        await runtime.guard()
        return _result({"requests": await cog._actions.list_pending(guild_id=runtime.guild_id,
                       channel_id=runtime.channel_id, requester_id=runtime.user_id)})

    async def cancel_request(arguments):
        await runtime.guard()
        changed = await cog._actions.cancel_pending(arguments["request_id"], guild_id=runtime.guild_id,
                                                  channel_id=runtime.channel_id, requester_id=runtime.user_id)
        return _result({"cancelled": bool(changed)}, status="executed")

    actions = getattr(cog, "_actions", None)
    register("list_own_action_requests", "Consulte pedidos pendentes feitos pelo autor neste canal. Resultados nunca incluem texto privado de áudio.", _EMPTY, own_requests, available=callable(getattr(actions, "list_pending", None)), why="Consulta de pedidos indisponível.")
    register("cancel_own_action_request", "Cancele um pedido pendente do autor e suas etapas dependentes. Uma execução já iniciada não é desfeita. A referência vem de list_own_action_requests.", _schema({"request_id": _string(64, minimum=1)}, ("request_id",)), cancel_request, permission="automatic_effect", available=callable(getattr(actions, "cancel_pending", None)), why="Cancelamento indisponível.")

    async def reply_record(arguments):
        await runtime.guard()
        message_id = arguments.get("message_id")
        if message_id is None and runtime.reply_target is not None and getattr(runtime.reply_target.author, "id", None) == getattr(cog.bot.user, "id", None):
            message_id = runtime.reply_target.id
        return await cog._reply_store.resolve(cog.bot, guild_id=runtime.guild_id, channel_id=runtime.channel_id,
                                             user_id=runtime.user_id, message_id=message_id)

    async def get_last_reply(arguments):
        record = await reply_record(arguments)
        if record is None:
            return _result({"found": False})
        return _result({"found": True, "message_id": str(record["message_id"]),
                        "format": record["format"], "text": record.get("text") or record.get("spoken_text", "")})

    async def convert_reply(arguments):
        from .audio import recorded_reply_audio
        from .reply_store import validate_recorded_reply
        _guild, channel, _requester, _cfg = await runtime.guard(audio=True)
        record = await reply_record(arguments)
        if record is None:
            raise policy.ActionDenied("Não encontrei uma resposta confirmada que possa converter neste canal.")
        async def before_effect():
            await runtime.guard(audio=True)
        audio = await recorded_reply_audio(cog.bot, record, user_id=runtime.user_id, before_effect=before_effect)
        if not audio:
            raise policy.ActionDenied("Não consegui obter o áudio dessa resposta agora.")
        await before_effect()
        if await validate_recorded_reply(cog.bot, record, user_id=runtime.user_id) is None:
            raise policy.ActionDenied("Essa resposta mudou ou não está mais disponível para conversão.")
        attachment = discord.File(io.BytesIO(audio), filename="resposta.mp3")
        try:
            sent = await channel.send(file=attachment, reference=discord.MessageReference(
                message_id=runtime.message.id, channel_id=runtime.channel_id, guild_id=runtime.guild_id,
                fail_if_not_exists=False), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            if 400 <= exc.status < 500:
                return {"ok": False, "status": "failed", "reason": "O Discord recusou o envio do áudio neste canal."}
            return {"ok": False, "status": "uncertain", "reason": "Não consegui confirmar o envio. Confira o canal antes de tentar novamente."}
        except asyncio.TimeoutError:
            return {"ok": False, "status": "uncertain", "reason": "Não consegui confirmar o envio. Confira o canal antes de tentar novamente."}
        finally:
            attachment.close()
        runtime.response_sent = True
        spoken = record.get("spoken_text") or record.get("text", "")
        files = getattr(sent, "attachments", ())
        auxiliary = [
            ("índice", lambda: cog._remember_sent_message(guild_id=runtime.guild_id, channel_id=runtime.channel_id, message_id=sent.id)),
            ("registro", lambda: cog._reply_store.record_sent(guild_id=runtime.guild_id, channel_id=runtime.channel_id,
                requester_id=runtime.user_id, origin_message_id=runtime.message.id, message_id=sent.id,
                original_user_text=str(runtime.message.content or "")[:8000], text="", spoken_text=spoken,
                format="audio", epoch=runtime.epoch, attachment=files[0] if files else None,
                provider=record.get("provider", ""), model=record.get("model", ""))),
        ]
        recorder = getattr(cog, "record_audio_reply_sent", None)
        if callable(recorder):
            auxiliary.append(("intervalo", lambda: recorder(guild_id=runtime.guild_id, channel_id=runtime.channel_id)))
        remaining = await _after_delivery(auxiliary)
        if remaining:
            await _after_delivery([("cópia na call", lambda: cog._mirror_sent_audio(guild_id=runtime.guild_id, user_id=runtime.user_id,
                channel_id=runtime.channel_id, parent_id=getattr(channel, "parent_id", None),
                message_id=sent.id, audio=audio, epoch=runtime.epoch))])
        return _result({"message_id": str(sent.id), "reused_audio": record["format"] == "audio"}, status="audio_sent")

    async def convert_text(arguments):
        from .reply_store import validate_recorded_reply
        _guild, channel, _requester, _cfg = await runtime.guard()
        record = await reply_record(arguments)
        text = (record.get("spoken_text") or record.get("text", "")) if record else ""
        if not text:
            raise policy.ActionDenied("Não encontrei o texto confirmado dessa resposta neste canal.")
        await runtime.guard()
        if await validate_recorded_reply(cog.bot, record, user_id=runtime.user_id) is None:
            raise policy.ActionDenied("Essa resposta mudou ou não está mais disponível para conversão.")
        try:
            sent = await channel.send(text, reference=discord.MessageReference(message_id=runtime.message.id,
                channel_id=runtime.channel_id, guild_id=runtime.guild_id, fail_if_not_exists=False),
                allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            status = "failed" if 400 <= exc.status < 500 else "uncertain"
            return {"ok": False, "status": status, "reason": "Não consegui confirmar o envio do texto. Confira o canal antes de tentar novamente."}
        except asyncio.TimeoutError:
            return {"ok": False, "status": "uncertain", "reason": "Não consegui confirmar o envio do texto. Confira o canal antes de tentar novamente."}
        runtime.response_sent = True
        await _after_delivery([
            ("índice", lambda: cog._remember_sent_message(guild_id=runtime.guild_id, channel_id=runtime.channel_id, message_id=sent.id)),
            ("registro", lambda: cog._reply_store.record_sent(guild_id=runtime.guild_id, channel_id=runtime.channel_id,
                requester_id=runtime.user_id, origin_message_id=runtime.message.id, message_id=sent.id,
                original_user_text=str(runtime.message.content or "")[:8000], text=text, format="text", epoch=runtime.epoch,
                provider=record.get("provider", ""), model=record.get("model", ""))),
        ])
        return _result({"message_id": str(sent.id), "exact_text": True}, status="reply_sent")

    replies_available = getattr(cog, "_reply_store", None) is not None
    reply_schema = _schema({"message_id": {"type": "integer", "minimum": 1}})
    register("get_last_bot_reply", "Recupere a resposta confirmada do bot na mensagem respondida ou a última resposta deste autor no canal. Nunca substitui o conteúdo por um texto inventado.", reply_schema, get_last_reply, available=replies_available, why="Registro de respostas indisponível.")
    register("convert_reply_audio", "Envie em áudio exatamente a resposta confirmada anterior. Se ela já tem áudio, reutilize os bytes; se é texto, sintetize esse texto uma vez. Não reescreva, não reduza e não invente uma nova resposta. Na call atual, a mesma fala é enfileirada sem nova aprovação.", reply_schema, convert_reply, permission="automatic_effect", available=replies_available, why="Registro de respostas indisponível.")
    register("convert_reply_text", "Envie em texto exatamente o conteúdo confirmado da resposta anterior, incluindo a transcrição originalmente falada em um áudio do bot. Não reformule e não gere uma resposta diferente.", reply_schema, convert_text, permission="automatic_effect", available=replies_available, why="Registro de respostas indisponível.")

    refresh_action_spec(registry)
    try:
        from .module_tools import register_module_tools
        register_module_tools(registry, cog, message, config, epoch=epoch,
                              visibility_scope=visibility_scope, guard=runtime.guard)
    except ImportError:
        pass
    return registry


async def execute_native_tool(registry, call):
    """Despacha somente nomes declarados e nunca inclui exceções privadas."""
    spec = next((item for item in registry.snapshot() if item.name == call.name), None)
    def failure(reason, *, status="failed"):
        if call.name == "propor_acao":
            runtime = registry.runtime
            runtime.proposals_invalid = True
            runtime.proposals_error = reason
            runtime.action_proposals.clear()
            runtime.prepared_actions.clear()
        return {"ok": False, "status": status, "reason": reason}
    if spec is None or not spec.available or not callable(spec.handler):
        return failure(spec.why if spec else "Esta ferramenta não está disponível.", status="unavailable")
    try:
        arguments = validate_tool_arguments(call.arguments, spec.parameters)
        result = spec.handler(arguments)
        result = await result if inspect.isawaitable(result) else result
        if not isinstance(result, dict):
            raise TypeError("Resultado da ferramenta precisa ser estruturado.")
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, default=_json_value)
        if len(encoded.encode("utf-8")) > 24000:
            status = "uncertain" if spec.permission != "read" and call.name != "propor_acao" else "failed"
            return failure("A consulta retornou informação demais. Faça uma consulta menor.", status=status)
        return json.loads(encoded)
    except (policy.ActionDenied, InvalidToolArguments) as exc:
        return failure(str(exc)[:500])
    except asyncio.CancelledError:
        failure("A preparação desta sequência foi interrompida.", status="uncertain")
        raise
    except Exception as exc:
        log.warning("chatbot: ferramenta falhou | tool=%s error_type=%s", spec.name, type(exc).__name__)
        status = "uncertain" if spec.permission != "read" and call.name != "propor_acao" else "failed"
        return failure("Não consegui confirmar essa operação agora.", status=status)
