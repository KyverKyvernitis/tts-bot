from __future__ import annotations

import time

import discord


async def fetch_root_command_ids_cached(bot, cache: dict[object, tuple[float, dict[str, int]]], guild: discord.Guild | None = None, *, ttl_seconds: float = 600.0, include_global_fallback: bool = False) -> dict[str, int]:
    """Busca e memoriza IDs de comandos raiz para montar menções slash com baixo custo."""
    cache_key = int(guild.id) if guild is not None else 0
    now = time.monotonic()
    cached = cache.get(cache_key)
    if cached is not None:
        expires_at, command_ids = cached
        if now < expires_at:
            return dict(command_ids)

    command_ids: dict[str, int] = {}
    fetch_targets: list[discord.Guild | None] = [guild] if guild is not None else [None]
    if include_global_fallback and guild is not None:
        fetch_targets.append(None)

    for target in fetch_targets:
        fetch_kwargs = {"guild": target} if target is not None else {}
        try:
            commands_list = await bot.tree.fetch_commands(**fetch_kwargs)
        except Exception:
            continue
        for cmd in commands_list:
            name = str(getattr(cmd, "name", "") or "").strip()
            cmd_id = getattr(cmd, "id", None)
            if not name or not cmd_id or name in command_ids:
                continue
            command_ids[name] = int(cmd_id)

    if command_ids:
        cache[cache_key] = (now + ttl_seconds, dict(command_ids))
    elif cached is not None:
        return dict(cached[1])
    return command_ids


def slash_mention(root_ids: dict[str, int], *, root: str, path: str) -> str:
    cmd_id = root_ids.get(root)
    if cmd_id:
        return f"</{path}:{cmd_id}>"
    return f"`/{path}`"
