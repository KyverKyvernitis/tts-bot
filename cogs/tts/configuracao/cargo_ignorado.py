from __future__ import annotations

from collections.abc import Callable
from typing import Any


ObterIdCargoIgnorado = Callable[..., int]
CargoIgnoradoAtivo = Callable[..., bool]
MembroTemCargoIgnorado = Callable[..., bool]


def obter_id_cargo_ignorado_tts(
    banco: Any,
    guild_id: int,
    *,
    guild_defaults: dict | None = None,
) -> int:
    """Resolve o cargo ignorado preservando fallbacks e tolerância a falhas do banco."""
    if guild_defaults is not None:
        try:
            return max(0, int((guild_defaults or {}).get("ignored_tts_role_id", 0) or 0))
        except Exception:
            return 0

    if banco is not None and hasattr(banco, "get_ignored_tts_role_id"):
        try:
            valor = banco.get_ignored_tts_role_id(guild_id)
            return max(0, int(valor or 0))
        except Exception:
            pass
    if banco is not None and hasattr(banco, "get_guild_tts_defaults"):
        try:
            defaults = banco.get_guild_tts_defaults(guild_id)
            return max(0, int((defaults or {}).get("ignored_tts_role_id", 0) or 0))
        except Exception:
            pass
    return 0


def cargo_ignorado_tts_ativo(
    banco: Any,
    guild_id: int,
    *,
    guild_defaults: dict | None = None,
    obter_id_cargo: ObterIdCargoIgnorado | None = None,
) -> bool:
    """Resolve a flag de ativação mantendo a migração suave do formato legado."""
    if obter_id_cargo is None:
        obter_id_cargo = lambda gid, *, guild_defaults=None: obter_id_cargo_ignorado_tts(
            banco,
            gid,
            guild_defaults=guild_defaults,
        )

    if guild_defaults is not None:
        if "ignored_tts_role_enabled" in (guild_defaults or {}):
            return bool((guild_defaults or {}).get("ignored_tts_role_enabled", False))
        return bool(obter_id_cargo(guild_id, guild_defaults=guild_defaults))

    if banco is not None and hasattr(banco, "get_ignored_tts_role_enabled"):
        try:
            return bool(banco.get_ignored_tts_role_enabled(guild_id))
        except Exception:
            pass
    if banco is not None and hasattr(banco, "get_guild_tts_defaults"):
        try:
            defaults = banco.get_guild_tts_defaults(guild_id) or {}
            if "ignored_tts_role_enabled" in defaults:
                return bool(defaults.get("ignored_tts_role_enabled", False))
            return bool(obter_id_cargo(guild_id, guild_defaults=defaults))
        except Exception:
            pass
    return bool(obter_id_cargo(guild_id))


def obter_cargo_ignorado_tts(
    guild: Any,
    *,
    guild_defaults: dict | None = None,
    obter_id_cargo: ObterIdCargoIgnorado,
) -> Any | None:
    if guild is None:
        return None
    role_id = obter_id_cargo(guild.id, guild_defaults=guild_defaults)
    if role_id <= 0:
        return None
    return guild.get_role(role_id)


def texto_cargo_ignorado_tts(
    bot: Any,
    guild_id: int,
    *,
    guild_defaults: dict | None = None,
    obter_id_cargo: ObterIdCargoIgnorado,
    cargo_ativo: CargoIgnoradoAtivo,
) -> str:
    role_id = obter_id_cargo(guild_id, guild_defaults=guild_defaults)
    enabled = cargo_ativo(guild_id, guild_defaults=guild_defaults)
    if role_id <= 0:
        return "desligado"
    guild = bot.get_guild(guild_id)
    role = guild.get_role(role_id) if guild is not None else None
    mention = role.mention if role is not None else f"<@&{role_id}>"
    if enabled:
        return f"{mention} · ligado"
    return f"desligado · {mention} salvo"


def membro_tem_cargo_ignorado_tts(
    member: Any,
    *,
    guild_defaults: dict | None = None,
    obter_id_cargo: ObterIdCargoIgnorado,
    cargo_ativo: CargoIgnoradoAtivo,
) -> bool:
    if member is None or member.guild is None:
        return False
    if not cargo_ativo(member.guild.id, guild_defaults=guild_defaults):
        return False
    ignored_role_id = obter_id_cargo(member.guild.id, guild_defaults=guild_defaults)
    if ignored_role_id <= 0:
        return False
    return any(
        int(getattr(role, "id", 0) or 0) == ignored_role_id
        for role in getattr(member, "roles", [])
    )


def sufixo_apelido_membro_tts(
    member: Any,
    *,
    guild_defaults: dict | None = None,
    membro_tem_cargo_ignorado: MembroTemCargoIgnorado,
) -> str:
    if member is None:
        return ""

    is_muted = False
    voice_state = getattr(member, "voice", None)
    if voice_state is not None:
        try:
            is_muted = bool(getattr(voice_state, "mute", False))
        except Exception:
            is_muted = False

    ignores_tts = membro_tem_cargo_ignorado(member, guild_defaults=guild_defaults)

    if is_muted and ignores_tts:
        return " [ultra-censurado]"
    if is_muted:
        return " [censurado]"
    if ignores_tts:
        return " [bot ignora]"
    return ""
