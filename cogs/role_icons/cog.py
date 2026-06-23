from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
from copy import deepcopy
from typing import Any

import discord
from discord.ext import commands

from utility.interaction_safety import safe_send_interaction_message

from .models import MAX_CONNECTIONS_PER_GUILD, ROLE_ICON_UPDATE_DELAY_SECONDS, normalize_hex, now_iso, sanitize_config
from .renderer import normalize_original_icon, recolor_role_icon
from .storage import RoleIconStorage

log = logging.getLogger(__name__)
_ID_RE = re.compile(r"\d{15,25}")


class RoleIconUserError(Exception):
    pass


def parse_discord_id(value: str | int | None) -> int:
    if isinstance(value, int):
        return int(value)
    match = _ID_RE.search(str(value or ""))
    return int(match.group(0)) if match else 0


def member_label(member: discord.Member | None, user_id: int) -> str:
    if member is None:
        return f"usuário {int(user_id)}"
    return str(getattr(member, "display_name", None) or getattr(member, "name", None) or member.id)


def role_label(role: discord.Role | None, role_id: int) -> str:
    if role is None:
        return f"cargo {int(role_id)}"
    return str(role.name or role.id)


class RoleIconsCog(commands.Cog):
    """Conecta a cor escolhida por um usuário ao ícone de um cargo."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.storage = RoleIconStorage(bot)
        self._pending_tasks: dict[tuple[int, int], asyncio.Task] = {}
        self._latest_colors: dict[tuple[int, int], str | None] = {}
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._active_panels: dict[tuple[int, int], int] = {}

    @property
    def db(self):
        return getattr(self.bot, "settings_db", None)

    def cog_unload(self):
        for task in list(self._pending_tasks.values()):
            task.cancel()
        self._pending_tasks.clear()
        self._latest_colors.clear()

    def _get_config(self, guild_id: int) -> dict[str, Any]:
        return self.storage.get_config(int(guild_id))

    async def _save_config(self, guild_id: int, config: dict[str, Any]) -> dict[str, Any]:
        return await self.storage.save_config(int(guild_id), config)

    def _known_guild_ids(self) -> set[int]:
        ids = {int(getattr(guild, "id", 0) or 0) for guild in getattr(self.bot, "guilds", []) if getattr(guild, "id", 0)}
        db = self.db
        if db is not None and hasattr(db, "guild_cache"):
            ids.update(int(gid) for gid in getattr(db, "guild_cache", {}).keys() if int(gid or 0))
        return ids

    def _guild_supports_role_icons(self, guild: discord.Guild) -> bool:
        features = {str(item).upper() for item in (getattr(guild, "features", None) or [])}
        if "ROLE_ICONS" in features:
            return True
        # Alguns servidores liberam o recurso por nível de boost antes do feature
        # aparecer no cache. Nesse caso deixamos a API ser a validação final.
        return int(getattr(guild, "premium_tier", 0) or 0) >= 2

    async def _resolve_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        member = guild.get_member(int(user_id))
        if member is not None:
            return member
        with contextlib.suppress(Exception):
            return await guild.fetch_member(int(user_id))
        return None

    def _check_role_manageable(self, guild: discord.Guild, role: discord.Role) -> None:
        if role.is_default():
            raise RoleIconUserError("Não dá para conectar o cargo @everyone.")
        if bool(getattr(role, "managed", False)):
            raise RoleIconUserError("Esse cargo é gerenciado por integração e não pode ser editado.")
        me = guild.me or (guild.get_member(self.bot.user.id) if self.bot.user else None)
        perms = getattr(me, "guild_permissions", None)
        if me is None or not bool(getattr(perms, "manage_roles", False)):
            raise RoleIconUserError("O bot precisa da permissão Gerenciar Cargos.")
        if role >= me.top_role:
            raise RoleIconUserError("Esse cargo precisa ficar abaixo do cargo do bot.")

    async def _fetch_role_icon_bytes(self, role: discord.Role) -> bytes:
        display_icon = getattr(role, "display_icon", None) or getattr(role, "icon", None)
        if display_icon is None:
            raise RoleIconUserError("Esse cargo não tem ícone de imagem para usar como base.")
        if isinstance(display_icon, str):
            raise RoleIconUserError("Ícone de emoji unicode não pode ser recolorido. Use uma imagem no cargo.")
        read = getattr(display_icon, "read", None)
        if not callable(read):
            raise RoleIconUserError("Não consegui ler o ícone atual desse cargo.")
        raw = await read()
        if not raw:
            raise RoleIconUserError("Não consegui baixar o ícone atual desse cargo.")
        return raw

    async def _capture_original_icon(self, guild: discord.Guild, connection: dict[str, Any]) -> dict[str, Any]:
        role_id = int(connection.get("role_id") or 0)
        role = guild.get_role(role_id)
        if role is None:
            raise RoleIconUserError("Cargo não encontrado.")
        self._check_role_manageable(guild, role)
        raw = await self._fetch_role_icon_bytes(role)
        normalized = await normalize_original_icon(raw)
        rel_path, digest = self.storage.write_original_icon(guild.id, role.id, normalized)
        updated = dict(connection)
        updated["original_icon_path"] = rel_path
        updated["original_icon_hash"] = digest
        updated["last_status"] = "Ícone base salvo."
        updated["updated_at"] = now_iso()
        return updated

    def _connection_index_by_role(self, cfg: dict[str, Any], role_id: int) -> int:
        for idx, item in enumerate(cfg.get("connections") or []):
            if int(item.get("role_id") or 0) == int(role_id):
                return idx
        return -1

    def _connection_index_by_id(self, cfg: dict[str, Any], connection_id: str) -> int:
        for idx, item in enumerate(cfg.get("connections") or []):
            if str(item.get("id") or "") == str(connection_id):
                return idx
        return -1

    async def add_connection(self, guild: discord.Guild, *, user_id: int, role_id: int) -> dict[str, Any]:
        if not self._guild_supports_role_icons(guild):
            raise RoleIconUserError("Este servidor não parece ter suporte a ícones de cargo.")
        cfg = self._get_config(guild.id)
        connections = list(cfg.get("connections") or [])
        if len(connections) >= MAX_CONNECTIONS_PER_GUILD:
            raise RoleIconUserError(f"Limite atingido: {MAX_CONNECTIONS_PER_GUILD} cargos conectados.")
        if self._connection_index_by_role(cfg, role_id) >= 0:
            raise RoleIconUserError("Esse cargo já está conectado.")
        member = await self._resolve_member(guild, user_id)
        if member is None:
            raise RoleIconUserError("Usuário não encontrado neste servidor.")
        role = guild.get_role(int(role_id))
        if role is None:
            raise RoleIconUserError("Cargo não encontrado.")
        self._check_role_manageable(guild, role)
        connection = {
            "id": f"{int(user_id)}:{int(role_id)}",
            "user_id": int(user_id),
            "role_id": int(role_id),
            "enabled": True,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        connection = await self._capture_original_icon(guild, connection)
        connections.append(connection)
        cfg["connections"] = connections
        await self._save_config(guild.id, cfg)
        color_hex = await self.get_member_color_hex(member)
        if color_hex:
            await self.apply_connection(guild, connection, color_hex, force=True)
        return connection

    async def edit_connection(self, guild: discord.Guild, connection_id: str, *, user_id: int, role_id: int) -> dict[str, Any]:
        cfg = self._get_config(guild.id)
        idx = self._connection_index_by_id(cfg, connection_id)
        if idx < 0:
            raise RoleIconUserError("Conexão não encontrada.")
        duplicate_idx = self._connection_index_by_role(cfg, role_id)
        if duplicate_idx >= 0 and duplicate_idx != idx:
            raise RoleIconUserError("Esse cargo já está conectado.")
        member = await self._resolve_member(guild, user_id)
        if member is None:
            raise RoleIconUserError("Usuário não encontrado neste servidor.")
        role = guild.get_role(int(role_id))
        if role is None:
            raise RoleIconUserError("Cargo não encontrado.")
        self._check_role_manageable(guild, role)
        connection = dict((cfg.get("connections") or [])[idx])
        role_changed = int(connection.get("role_id") or 0) != int(role_id)
        connection["user_id"] = int(user_id)
        connection["role_id"] = int(role_id)
        connection["id"] = f"{int(user_id)}:{int(role_id)}"
        connection["updated_at"] = now_iso()
        if role_changed or not connection.get("original_icon_path"):
            connection = await self._capture_original_icon(guild, connection)
        cfg["connections"][idx] = connection
        await self._save_config(guild.id, cfg)
        return connection

    async def remove_connection(self, guild_id: int, connection_id: str) -> bool:
        cfg = self._get_config(int(guild_id))
        before = len(cfg.get("connections") or [])
        cfg["connections"] = [item for item in (cfg.get("connections") or []) if str(item.get("id") or "") != str(connection_id)]
        if len(cfg["connections"]) == before:
            return False
        await self._save_config(int(guild_id), cfg)
        return True

    async def toggle_connection(self, guild_id: int, connection_id: str) -> bool:
        cfg = self._get_config(int(guild_id))
        idx = self._connection_index_by_id(cfg, connection_id)
        if idx < 0:
            raise RoleIconUserError("Conexão não encontrada.")
        cfg["connections"][idx]["enabled"] = not bool(cfg["connections"][idx].get("enabled", True))
        cfg["connections"][idx]["updated_at"] = now_iso()
        await self._save_config(int(guild_id), cfg)
        return bool(cfg["connections"][idx].get("enabled"))

    async def recapture_connection_base(self, guild: discord.Guild, connection_id: str) -> dict[str, Any]:
        cfg = self._get_config(guild.id)
        idx = self._connection_index_by_id(cfg, connection_id)
        if idx < 0:
            raise RoleIconUserError("Conexão não encontrada.")
        connection = await self._capture_original_icon(guild, dict(cfg["connections"][idx]))
        cfg["connections"][idx] = connection
        await self._save_config(guild.id, cfg)
        return connection

    async def get_member_color_hex(self, member: discord.Member | None) -> str | None:
        if member is None or member.guild is None:
            return None
        color_cog = self.bot.get_cog("ColorRolesCog")
        if color_cog is None:
            return None
        getter = getattr(color_cog, "_member_current_color_slot", None)
        if not callable(getter):
            return None
        try:
            _, slot = getter(member.guild, member)
        except Exception:
            return None
        if not slot:
            return None
        return normalize_hex(str(slot.get("role_hex") or slot.get("text_hex") or ""))

    async def _update_connection_fields(self, guild_id: int, connection_id: str, **fields: Any) -> None:
        cfg = self._get_config(int(guild_id))
        idx = self._connection_index_by_id(cfg, connection_id)
        if idx < 0:
            return
        updated = dict(cfg["connections"][idx])
        updated.update(fields)
        updated["updated_at"] = now_iso()
        cfg["connections"][idx] = updated
        await self._save_config(int(guild_id), cfg)

    async def apply_connection(self, guild: discord.Guild, connection: dict[str, Any], color_hex: str | None, *, force: bool = False) -> bool:
        connection = deepcopy(connection)
        connection_id = str(connection.get("id") or f"{connection.get('user_id')}:{connection.get('role_id')}")
        role_id = int(connection.get("role_id") or 0)
        role = guild.get_role(role_id)
        if role is None:
            await self._update_connection_fields(guild.id, connection_id, last_status="Cargo não encontrado.")
            return False
        try:
            self._check_role_manageable(guild, role)
            original = self.storage.read_original_icon(connection, guild.id)
            clean_color = normalize_hex(color_hex)
            if clean_color:
                target = await recolor_role_icon(original, clean_color)
                status = f"Aplicado em {clean_color}."
            else:
                target = original
                status = "Restaurado para o ícone base."
            digest = hashlib.sha256(target).hexdigest()
            if not force and str(connection.get("last_rendered_hash") or "") == digest:
                return True
            await role.edit(display_icon=target, reason="Ícone de cargo conectado à cor do usuário")
            if clean_color:
                self.storage.write_rendered_icon(guild.id, role_id, target)
            await self._update_connection_fields(
                guild.id,
                connection_id,
                last_color=clean_color or "",
                last_rendered_hash=digest,
                last_status=status,
                last_updated_at=now_iso(),
            )
            return True
        except RoleIconUserError as exc:
            await self._update_connection_fields(guild.id, connection_id, last_status=str(exc))
            return False
        except discord.Forbidden:
            await self._update_connection_fields(guild.id, connection_id, last_status="Sem permissão para editar o cargo.")
            return False
        except Exception as exc:
            log.exception("[role_icons] falha ao aplicar ícone gid=%s role=%s", guild.id, role_id)
            await self._update_connection_fields(guild.id, connection_id, last_status=f"Falha ao aplicar: {type(exc).__name__}.")
            return False

    async def apply_for_member(self, guild: discord.Guild, user_id: int, color_hex: str | None, *, force: bool = False) -> int:
        cfg = self._get_config(guild.id)
        targets = [item for item in (cfg.get("connections") or []) if bool(item.get("enabled", True)) and int(item.get("user_id") or 0) == int(user_id)]
        applied = 0
        for connection in targets:
            if await self.apply_connection(guild, connection, color_hex, force=force):
                applied += 1
        return applied

    async def build_preview_file(self, guild: discord.Guild, connection: dict[str, Any]) -> discord.File:
        member = await self._resolve_member(guild, int(connection.get("user_id") or 0))
        color_hex = await self.get_member_color_hex(member) if member is not None else None
        original = self.storage.read_original_icon(connection, guild.id)
        if color_hex:
            data = await recolor_role_icon(original, color_hex)
            suffix = color_hex.replace("#", "")
        else:
            data = original
            suffix = "base"
        return discord.File(fp=__import__("io").BytesIO(data), filename=f"role-icon-preview-{suffix}.png")

    def schedule_member_update(self, member: discord.Member, color_hex: str | None) -> None:
        guild = member.guild
        cfg = self._get_config(guild.id)
        if not any(bool(item.get("enabled", True)) and int(item.get("user_id") or 0) == int(member.id) for item in (cfg.get("connections") or [])):
            return
        key = (int(guild.id), int(member.id))
        clean_color = normalize_hex(color_hex)
        self._latest_colors[key] = clean_color
        old = self._pending_tasks.pop(key, None)
        if old is not None:
            old.cancel()
        self._pending_tasks[key] = asyncio.create_task(self._debounced_member_update(member, clean_color))

    async def _debounced_member_update(self, member: discord.Member, color_hex: str | None) -> None:
        key = (int(member.guild.id), int(member.id))
        try:
            await asyncio.sleep(ROLE_ICON_UPDATE_DELAY_SECONDS)
            if self._latest_colors.get(key) != color_hex:
                return
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                await self.apply_for_member(member.guild, member.id, color_hex, force=False)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("[role_icons] update pendente falhou gid=%s user=%s", member.guild.id, member.id)
        finally:
            if self._pending_tasks.get(key) is asyncio.current_task():
                self._pending_tasks.pop(key, None)
            self._latest_colors.pop(key, None)

    @commands.Cog.listener("on_member_color_changed")
    async def _on_member_color_changed(self, member: discord.Member, color_hex: str | None = None):
        if isinstance(member, discord.Member):
            self.schedule_member_update(member, color_hex)

    @commands.command(name="roleicons", aliases=["roleicon", "cargoicone", "cargoicones", "iconescargo"])
    @commands.guild_only()
    async def roleicons_command(self, ctx: commands.Context):
        member = ctx.author if isinstance(ctx.author, discord.Member) else None
        if member is None or not bool(getattr(member.guild_permissions, "manage_roles", False) or getattr(member.guild_permissions, "administrator", False)):
            await ctx.send("Você precisa de Gerenciar Cargos para abrir esse painel.")
            return
        key = (int(ctx.guild.id), int(ctx.author.id))
        old_id = self._active_panels.get(key)
        if old_id:
            with contextlib.suppress(Exception):
                old_msg = await ctx.channel.fetch_message(old_id)
                await old_msg.delete()
        from .panel import RoleIconPanelView
        view = RoleIconPanelView(self, guild_id=ctx.guild.id, owner_id=ctx.author.id)
        try:
            msg = await ctx.send(view=view)
        except Exception as exc:
            log.exception("[role_icons] falha ao abrir painel")
            await ctx.send(f"não consegui abrir o painel ({type(exc).__name__}).")
            return
        view.message = msg
        self._active_panels[key] = int(msg.id)
        with contextlib.suppress(Exception):
            await ctx.message.delete()

    async def reply_error(self, interaction: discord.Interaction, text: str) -> None:
        await safe_send_interaction_message(interaction, str(text or "Erro."), ephemeral=True, log=log, label="role_icons")
