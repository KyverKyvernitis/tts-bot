from __future__ import annotations

from typing import Any

from ..utils.embed import (
    build_status_embed,
    spoken_name_status_text,
    status_badge,
    status_engine_label,
    status_source_badge,
    status_voice_channel_text,
)


def origem_configuracao_status(configuracao_usuario: dict, chave: str) -> str:
    return "Usuário" if str((configuracao_usuario or {}).get(chave, "") or "").strip() else "Servidor"


def texto_booleano_status(valor: bool) -> str:
    return "Ativado" if bool(valor) else "Desativado"


def distintivo_status(valor: bool, *, ligado: str = "Ativo", desligado: str = "Inativo") -> str:
    return status_badge(valor, on=ligado, off=desligado)


def distintivo_origem_status(origem: str) -> str:
    return status_source_badge(origem)


def rotulo_motor_status(motor: str) -> str:
    return status_engine_label(motor)


def texto_canal_voz_status(guild: Any, id_usuario_alvo: int) -> str:
    return status_voice_channel_text(guild, id_usuario_alvo)


def texto_apelido_status(
    guild_id: int,
    membro: Any,
    *,
    resolvido: dict | None,
    resolver_apelido,
    normalizar_espacos,
) -> tuple[str, str]:
    nome_ativo, origem_ativa = resolver_apelido(membro, guild_id=guild_id)
    nome_personalizado = normalizar_espacos(str((resolvido or {}).get("speaker_name", "") or ""))
    return spoken_name_status_text(
        active_name=nome_ativo,
        active_source=origem_ativa,
        custom_name=nome_personalizado,
    )


async def construir_embed_status_tts(
    contexto: Any,
    guild_id: int,
    user_id: int,
    *,
    viewer_user_id: int | None = None,
    target_user_name: str | None = None,
    public: bool = False,
):
    """Monta o embed de status sem acoplar o módulo à classe concreta do cog."""
    banco = contexto._get_db()
    configuracao_usuario = await contexto._maybe_await(banco.get_user_tts(guild_id, user_id)) if banco else {}
    resolvido = await contexto._maybe_await(banco.resolve_tts(guild_id, user_id)) if banco else {}

    configuracao_usuario = configuracao_usuario or {}
    resolvido = resolvido or {}

    guild = contexto.bot.get_guild(guild_id)
    cliente_voz = contexto._get_voice_client_for_guild(guild)
    estado = contexto.guild_states.get(guild_id)
    tamanho_fila = int(getattr(getattr(estado, "queue", None), "qsize", lambda: 0)() if estado else 0)
    conectado = bool(cliente_voz and contexto._voice_client_is_connected(cliente_voz))
    reproduzindo = bool(cliente_voz and contexto._voice_client_is_playing_or_paused(cliente_voz))
    canal_cliente = contexto._voice_client_channel(cliente_voz)
    canal_bot = getattr(canal_cliente, "mention", None) or (
        f"`{getattr(canal_cliente, 'name', 'Desconhecido')}`" if canal_cliente is not None else "Desconectado"
    )
    canal_usuario = contexto._status_voice_channel_text(guild, user_id)
    membro = guild.get_member(user_id) if guild else None
    nome_alvo = str(target_user_name or contexto._member_panel_name(membro))
    texto_apelido, _ = contexto._spoken_name_status_text(guild_id, membro, resolved=resolvido)

    return build_status_embed(
        member=membro,
        target_name=nome_alvo,
        user_id=user_id,
        viewer_user_id=int(viewer_user_id or user_id or 0),
        public=public,
        is_connected=conectado,
        is_playing=reproduzindo,
        queue_size=tamanho_fila,
        resolved=resolvido,
        user_settings=configuracao_usuario,
        user_channel=canal_usuario,
        bot_channel=canal_bot,
        spoken_name_text=texto_apelido,
    )
