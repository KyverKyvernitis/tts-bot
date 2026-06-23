from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from copy import deepcopy
from typing import Any

import discord
from discord.ext import commands

from .models import MAX_CONNECTIONS_PER_GUILD, ROLE_ICON_UPDATE_DELAY_SECONDS, normalize_hex, now_iso
from .renderer import normalize_original_icon, recolor_role_icon
from .storage import RoleIconStorage

log = logging.getLogger(__name__)


class RoleIconUserError(Exception):
    pass


def member_label(member: discord.Member | None, user_id: int) -> str:
    if member is None:
        return f"usuário `{int(user_id)}`" if int(user_id or 0) else "aguardando membro"
    return member.mention


def role_label(role: discord.Role | None, role_id: int) -> str:
    if role is None:
        return f"cargo `{int(role_id)}`"
    return role.mention


def _trim(text: str, limit: int = 3900) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


class RoleIconsCog(commands.Cog):
    """Conecta a cor escolhida por um usuário ao ícone de um cargo."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.storage = RoleIconStorage(bot)
        self._pending_tasks: dict[tuple[int, int], asyncio.Task] = {}
        self._latest_colors: dict[tuple[int, int], str | None] = {}
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}

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

    def _guild_supports_role_icons(self, guild: discord.Guild) -> bool:
        features = {str(item).upper() for item in (getattr(guild, "features", None) or [])}
        if "ROLE_ICONS" in features:
            return True
        return int(getattr(guild, "premium_tier", 0) or 0) >= 2

    async def _resolve_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        if int(user_id or 0) <= 0:
            return None
        member = guild.get_member(int(user_id))
        if member is not None:
            return member
        with contextlib.suppress(Exception):
            return await guild.fetch_member(int(user_id))
        return None

    def _check_role_manageable(self, guild: discord.Guild, role: discord.Role) -> None:
        if role.is_default():
            raise RoleIconUserError("Não dá para conectar o @everyone.")
        if bool(getattr(role, "managed", False)):
            raise RoleIconUserError("Esse cargo é gerenciado por integração.")
        me = guild.me or (guild.get_member(self.bot.user.id) if self.bot.user else None)
        perms = getattr(me, "guild_permissions", None)
        if me is None or not bool(getattr(perms, "manage_roles", False)):
            raise RoleIconUserError("O bot precisa de Gerenciar Cargos.")
        if role >= me.top_role:
            raise RoleIconUserError("Esse cargo precisa ficar abaixo do cargo do bot.")

    async def _fetch_role_icon_bytes(self, role: discord.Role) -> bytes:
        display_icon = getattr(role, "display_icon", None) or getattr(role, "icon", None)
        if display_icon is None:
            raise RoleIconUserError("Esse cargo não tem ícone base.")
        if isinstance(display_icon, str):
            raise RoleIconUserError("Ícone de emoji não pode ser recolorido. Use imagem no cargo.")
        read = getattr(display_icon, "read", None)
        if not callable(read):
            raise RoleIconUserError("Não consegui ler o ícone desse cargo.")
        raw = await read()
        if not raw:
            raise RoleIconUserError("Não consegui baixar o ícone desse cargo.")
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

    def _connection_by_role(self, cfg: dict[str, Any], role_id: int) -> dict[str, Any] | None:
        idx = self._connection_index_by_role(cfg, int(role_id))
        if idx < 0:
            return None
        return dict((cfg.get("connections") or [])[idx])

    def _role_members(self, role: discord.Role) -> list[discord.Member]:
        return [member for member in getattr(role, "members", []) or [] if not getattr(member, "bot", False)]

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

    async def _update_connection_fields(self, guild_id: int, role_id: int, **fields: Any) -> dict[str, Any] | None:
        cfg = self._get_config(int(guild_id))
        idx = self._connection_index_by_role(cfg, int(role_id))
        if idx < 0:
            return None
        updated = dict(cfg["connections"][idx])
        updated.update(fields)
        updated["updated_at"] = now_iso()
        cfg["connections"][idx] = updated
        await self._save_config(int(guild_id), cfg)
        return updated

    async def _create_connection(self, guild: discord.Guild, role: discord.Role) -> tuple[dict[str, Any], str]:
        if not self._guild_supports_role_icons(guild):
            raise RoleIconUserError("Este servidor não parece ter ícones de cargo.")
        self._check_role_manageable(guild, role)
        cfg = self._get_config(guild.id)
        connections = list(cfg.get("connections") or [])
        if self._connection_index_by_role(cfg, role.id) >= 0:
            raise RoleIconUserError("Esse cargo já está conectado.")
        if len(connections) >= MAX_CONNECTIONS_PER_GUILD:
            raise RoleIconUserError(f"Limite atingido: {MAX_CONNECTIONS_PER_GUILD}/10.")
        members = self._role_members(role)
        if len(members) > 1:
            raise RoleIconUserError("Esse cargo precisa ter no máximo 1 membro.")
        target = members[0] if members else None
        connection = {
            "id": str(int(role.id)),
            "user_id": int(target.id) if target else 0,
            "role_id": int(role.id),
            "enabled": True,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "last_status": "Ativo." if target else "Aguardando membro.",
        }
        connection = await self._capture_original_icon(guild, connection)
        if target is None:
            connection["last_status"] = "Aguardando membro."
        connections.append(connection)
        cfg["connections"] = connections
        await self._save_config(guild.id, cfg)
        if target is not None:
            color_hex = await self.get_member_color_hex(target)
            await self.apply_connection(guild, connection, color_hex, force=True)
            return connection, f"Cargo conectado a {target.mention}."
        return connection, "Cargo conectado. Aguardando 1 membro."

    async def _remove_connection_by_role(self, guild_id: int, role_id: int) -> dict[str, Any] | None:
        cfg = self._get_config(int(guild_id))
        idx = self._connection_index_by_role(cfg, int(role_id))
        if idx < 0:
            return None
        connection = dict(cfg["connections"][idx])
        cfg["connections"] = [item for item in cfg.get("connections") or [] if int(item.get("role_id") or 0) != int(role_id)]
        await self._save_config(int(guild_id), cfg)
        return connection

    async def _toggle_role(self, guild: discord.Guild, role: discord.Role) -> tuple[str, dict[str, Any] | None, bool]:
        cfg = self._get_config(guild.id)
        existing = self._connection_by_role(cfg, role.id)
        if existing is not None:
            removed = await self._remove_connection_by_role(guild.id, role.id)
            return "Conexão removida.", removed, False
        connection, notice = await self._create_connection(guild, role)
        return notice, connection, True

    async def apply_connection(self, guild: discord.Guild, connection: dict[str, Any], color_hex: str | None, *, force: bool = False) -> bool:
        connection = deepcopy(connection)
        role_id = int(connection.get("role_id") or 0)
        role = guild.get_role(role_id)
        if role is None:
            await self._update_connection_fields(guild.id, role_id, last_status="Cargo não encontrado.")
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
                status = "Ícone base restaurado."
            digest = hashlib.sha256(target).hexdigest()
            if not force and str(connection.get("last_rendered_hash") or "") == digest:
                await self._update_connection_fields(guild.id, role_id, last_status=status)
                return True
            await role.edit(display_icon=target, reason="Ícone de cargo conectado à cor do usuário")
            if clean_color:
                self.storage.write_rendered_icon(guild.id, role_id, target)
            await self._update_connection_fields(
                guild.id,
                role_id,
                last_color=clean_color or "",
                last_rendered_hash=digest,
                last_status=status,
                last_updated_at=now_iso(),
            )
            return True
        except RoleIconUserError as exc:
            await self._update_connection_fields(guild.id, role_id, last_status=str(exc))
            return False
        except discord.Forbidden:
            await self._update_connection_fields(guild.id, role_id, last_status="Sem permissão para editar o cargo.")
            return False
        except Exception as exc:
            log.exception("[role_icons] falha ao aplicar ícone gid=%s role=%s", guild.id, role_id)
            await self._update_connection_fields(guild.id, role_id, last_status=f"Falha ao aplicar: {type(exc).__name__}.")
            return False

    async def apply_for_member(self, guild: discord.Guild, user_id: int, color_hex: str | None, *, force: bool = False) -> int:
        cfg = self._get_config(guild.id)
        targets = [
            item for item in (cfg.get("connections") or [])
            if bool(item.get("enabled", True)) and int(item.get("user_id") or 0) == int(user_id)
        ]
        applied = 0
        for connection in targets:
            role_id = int(connection.get("role_id") or 0)
            role = guild.get_role(role_id)
            if role is None:
                await self._update_connection_fields(guild.id, role_id, last_status="Cargo não encontrado.")
                continue
            members = self._role_members(role)
            if len(members) != 1 or int(members[0].id) != int(user_id):
                status = "Pausado: alvo fora do cargo." if not members else "Conflito: cargo com membro diferente ou extra."
                await self._update_connection_fields(guild.id, role_id, last_status=status)
                continue
            if await self.apply_connection(guild, connection, color_hex, force=force):
                applied += 1
        return applied

    def schedule_member_update(self, member: discord.Member, color_hex: str | None) -> None:
        guild = member.guild
        cfg = self._get_config(guild.id)
        if not any(
            bool(item.get("enabled", True)) and int(item.get("user_id") or 0) == int(member.id)
            for item in (cfg.get("connections") or [])
        ):
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

    async def _sync_role_target(self, guild: discord.Guild, role_id: int) -> None:
        cfg = self._get_config(guild.id)
        connection = self._connection_by_role(cfg, int(role_id))
        if connection is None or not bool(connection.get("enabled", True)):
            return
        role = guild.get_role(int(role_id))
        if role is None:
            await self._update_connection_fields(guild.id, role_id, last_status="Cargo não encontrado.")
            return
        members = self._role_members(role)
        current_user_id = int(connection.get("user_id") or 0)
        if current_user_id <= 0:
            if len(members) == 1:
                target = members[0]
                updated = await self._update_connection_fields(
                    guild.id,
                    role_id,
                    user_id=int(target.id),
                    id=str(int(role_id)),
                    last_status="Ativo.",
                )
                color_hex = await self.get_member_color_hex(target)
                await self.apply_connection(guild, updated or connection, color_hex, force=True)
            elif len(members) == 0:
                await self._update_connection_fields(guild.id, role_id, last_status="Aguardando membro.")
            else:
                await self._update_connection_fields(guild.id, role_id, last_status="Conflito: mais de 1 membro.")
            return
        if len(members) == 1 and int(members[0].id) == current_user_id:
            await self._update_connection_fields(guild.id, role_id, last_status="Ativo.")
            color_hex = await self.get_member_color_hex(members[0])
            self.schedule_member_update(members[0], color_hex)
        elif len(members) == 0:
            await self._update_connection_fields(guild.id, role_id, last_status="Pausado: alvo fora do cargo.")
        else:
            await self._update_connection_fields(guild.id, role_id, last_status="Conflito: cargo com membro diferente ou extra.")

    @commands.Cog.listener("on_member_color_changed")
    async def _on_member_color_changed(self, member: discord.Member, color_hex: str | None = None):
        if isinstance(member, discord.Member):
            self.schedule_member_update(member, color_hex)

    @commands.Cog.listener("on_member_update")
    async def _on_member_update(self, before: discord.Member, after: discord.Member):
        if before.guild is None or after.guild is None:
            return
        before_ids = {int(role.id) for role in getattr(before, "roles", []) or []}
        after_ids = {int(role.id) for role in getattr(after, "roles", []) or []}
        changed = before_ids.symmetric_difference(after_ids)
        if not changed:
            return
        cfg = self._get_config(after.guild.id)
        connected = {int(item.get("role_id") or 0) for item in (cfg.get("connections") or [])}
        for role_id in changed & connected:
            await self._sync_role_target(after.guild, role_id)

    def _make_view(self, title: str, lines: list[str] | str, *, ok: bool = True) -> discord.ui.LayoutView:
        body = lines if isinstance(lines, str) else "\n".join(str(line) for line in lines if str(line).strip())
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim(f"## {title}\n{body}".rstrip())),
            accent_color=discord.Color.green() if ok else discord.Color.red(),
        ))
        return view

    async def _send_view(self, ctx: commands.Context, title: str, lines: list[str] | str, *, ok: bool = True) -> None:
        await ctx.send(view=self._make_view(title, lines, ok=ok), allowed_mentions=discord.AllowedMentions.none())

    async def _list_connections(self, ctx: commands.Context) -> None:
        cfg = self._get_config(ctx.guild.id)
        connections = list(cfg.get("connections") or [])
        if not connections:
            await self._send_view(
                ctx,
                "Ícones conectados",
                [
                    "Nenhum cargo conectado.",
                    "Use `_roleicon @cargo` para ligar.",
                ],
                ok=True,
            )
            return
        lines: list[str] = [f"Conexões: `{len(connections)}/{MAX_CONNECTIONS_PER_GUILD}`"]
        for index, conn in enumerate(connections, start=1):
            role_id = int(conn.get("role_id") or 0)
            user_id = int(conn.get("user_id") or 0)
            role = ctx.guild.get_role(role_id)
            member = await self._resolve_member(ctx.guild, user_id)
            status = str(conn.get("last_status") or "-")[:140]
            lines.extend([
                "",
                f"**{index}.** {role_label(role, role_id)}",
                f"Alvo: {member_label(member, user_id)}",
                f"Status: {status}",
            ])
        await self._send_view(ctx, "Ícones conectados", lines, ok=True)

    @commands.command(name="roleicon")
    @commands.guild_only()
    async def roleicon_command(self, ctx: commands.Context, role: discord.Role = None):
        member = ctx.author if isinstance(ctx.author, discord.Member) else None
        allowed = bool(
            member
            and (getattr(member.guild_permissions, "manage_roles", False) or getattr(member.guild_permissions, "administrator", False))
        )
        if not allowed:
            await self._send_view(ctx, "Role icon", "Você precisa de Gerenciar Cargos.", ok=False)
            return
        if role is None:
            await self._list_connections(ctx)
            return
        try:
            notice, connection, connected = await self._toggle_role(ctx.guild, role)
        except RoleIconUserError as exc:
            await self._send_view(ctx, "Role icon", str(exc), ok=False)
            return
        except Exception as exc:
            log.exception("[role_icons] comando falhou gid=%s role=%s", ctx.guild.id if ctx.guild else None, role.id if role else None)
            await self._send_view(ctx, "Role icon", f"Falha: {type(exc).__name__}.", ok=False)
            return
        if connection:
            user_id = int(connection.get("user_id") or 0)
            target = await self._resolve_member(ctx.guild, user_id)
            status = str(connection.get("last_status") or ("Ativo." if connected else "Removido."))
            lines = [
                notice,
                "",
                f"Cargo: {role.mention}",
                f"Alvo: {member_label(target, user_id)}" if connected else "Alvo: removido",
                f"Status: {status}",
            ]
        else:
            lines = [notice, "", f"Cargo: {role.mention}"]
        await self._send_view(ctx, "Role icon", lines, ok=True)

    @roleicon_command.error
    async def roleicon_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.BadArgument):
            await self._send_view(ctx, "Role icon", "Cargo inválido. Use `_roleicon @cargo`.", ok=False)
            return
        if isinstance(error, commands.TooManyArguments):
            await self._send_view(ctx, "Role icon", "Use apenas `_roleicon` ou `_roleicon @cargo`.", ok=False)
            return
        log.exception("[role_icons] erro no comando", exc_info=error)
        await self._send_view(ctx, "Role icon", f"Falha: {type(error).__name__}.", ok=False)


async def setup(bot):
    await bot.add_cog(RoleIconsCog(bot))
