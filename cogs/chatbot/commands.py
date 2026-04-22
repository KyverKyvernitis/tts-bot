"""Slash commands do chatbot.

Organização:
- `/chatbot criar`, `/chatbot editar <profile>`, `/chatbot apagar <profile>`,
  `/chatbot listar`, `/chatbot ativar <profile>`, `/chatbot reset_server`
  — todos exigem permissão Manage Guild (staff).
- `/reset` — qualquer membro, limpa a memória PESSOAL dele.

Implementação como Mixin: a classe `ChatbotCommandsMixin` é herdada pelo
`ChatbotCog` em `cog.py`. Isso mantém o `ChatbotCog` como UM cog só (um único
extension do discord.py), mas divide a responsabilidade entre arquivos.

Todas as respostas são ephemeral (só quem rodou vê), exceto casos onde
a feedback faz sentido ser público (p.ex. `ativar` — server todo se beneficia
de saber que mudou o profile).
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from . import constants as C
from .profiles import ChatbotProfile
from .views import ConfirmView, ProfileCreateModal, ProfileEditModal

log = logging.getLogger(__name__)


# Decorador que checa permissão Manage Guild (pra comandos de gerenciamento).
# Usa app_commands.default_permissions que esconde o comando pra não-staff
# na UI do Discord, MAIS um check em runtime (defense in depth).
_STAFF_PERMS = app_commands.default_permissions(manage_guild=True)


async def _staff_check(interaction: discord.Interaction) -> bool:
    """Check adicional em runtime — pra casos onde o Discord mostra o comando
    mesmo sem permissão (admins do server, ou se o default_permissions não
    sincronizou). Retorna True se user tem Manage Guild OU é owner do server."""
    member = interaction.user
    guild = interaction.guild
    if guild is None:
        return False
    if not isinstance(member, discord.Member):
        return False
    if member.id == guild.owner_id:
        return True
    perms = member.guild_permissions
    return perms.manage_guild or perms.administrator


async def _profile_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Autocomplete de profile_id → mostra o NOME do profile, salva o ID.

    Acessa a instância da cog via interaction.client — precisa que o cog
    esteja carregado com self.bot.get_cog("Chatbot") disponível.
    """
    cog = interaction.client.get_cog("Chatbot")
    if cog is None or interaction.guild is None:
        return []
    profiles = getattr(cog, "_profiles", None)
    if profiles is None:
        return []
    try:
        all_profiles = await profiles.list_profiles(interaction.guild.id)
    except Exception:
        return []

    current_lower = (current or "").lower()
    out: list[app_commands.Choice[str]] = []
    for p in all_profiles:
        label = p.name
        if p.active:
            label = f"⭐ {label}"  # indicador do ativo
        if current_lower and current_lower not in p.name.lower() and current_lower not in p.profile_id.lower():
            continue
        out.append(app_commands.Choice(name=label[:100], value=p.profile_id))
        if len(out) >= 25:  # limite do Discord
            break
    return out


class ChatbotCommandsMixin:
    """Mixin que adiciona todos os slash commands ao cog.

    A classe que herda DEVE ter os atributos: self.bot, self._profiles,
    self._memory, self._webhooks — todos preenchidos em cog_load. Os
    comandos validam isso e respondem com erro se o cog não estiver pronto.
    """

    # O grupo de comandos /chatbot. Declarado como class-level — discord.py
    # automaticamente registra os subcomandos definidos com decorator aqui.
    chatbot = app_commands.Group(
        name="chatbot",
        description="Gerenciamento do chatbot do servidor",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    # --- Verificação de estado do cog -----------------------------------------

    def _require_ready(self, interaction: discord.Interaction) -> bool:
        """Retorna True se cog está pronto. Caso contrário responde e retorna False."""
        profiles = getattr(self, "_profiles", None)
        memory = getattr(self, "_memory", None)
        if profiles is None or memory is None:
            # A interaction.response pode já ter sido usada em contextos raros;
            # tentamos responder como followup se for o caso.
            try:
                if not interaction.response.is_done():
                    # NOTA: não awaited — é só pra sinalizar defensivamente.
                    # O caller vai ver False e simplesmente retornar.
                    pass
            except Exception:
                pass
            return False
        return True

    # --- /chatbot criar -------------------------------------------------------

    @chatbot.command(name="criar", description="Cria um novo profile de chatbot")
    async def chatbot_criar(self, interaction: discord.Interaction):
        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode criar profiles.", ephemeral=True
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto. Tenta de novo em alguns segundos.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        # Checa limite antes de abrir o modal — UX melhor do que deixar criar
        # e falhar depois.
        count = await self._profiles.count_profiles(guild.id)
        if count >= C.MAX_PROFILES_PER_GUILD:
            await interaction.response.send_message(
                f"❌ O servidor já atingiu o limite de {C.MAX_PROFILES_PER_GUILD} "
                f"profiles. Apague algum antes de criar outro com `/chatbot apagar`.",
                ephemeral=True,
            )
            return

        modal = ProfileCreateModal(store=self._profiles)
        await interaction.response.send_modal(modal)

    # --- /chatbot editar <profile> --------------------------------------------

    @chatbot.command(name="editar", description="Edita um profile existente")
    @app_commands.describe(profile="Profile para editar")
    @app_commands.autocomplete(profile=_profile_autocomplete)
    async def chatbot_editar(self, interaction: discord.Interaction, profile: str):
        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode editar profiles.", ephemeral=True
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto. Tenta de novo em alguns segundos.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        prof = await self._profiles.get_profile(guild.id, profile)
        if prof is None:
            await interaction.response.send_message(
                "Profile não encontrado. Use autocomplete para escolher.",
                ephemeral=True,
            )
            return

        modal = ProfileEditModal(store=self._profiles, profile=prof)
        await interaction.response.send_modal(modal)

    # --- /chatbot apagar <profile> --------------------------------------------

    @chatbot.command(name="apagar", description="Apaga um profile (não volta)")
    @app_commands.describe(profile="Profile para apagar")
    @app_commands.autocomplete(profile=_profile_autocomplete)
    async def chatbot_apagar(self, interaction: discord.Interaction, profile: str):
        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode apagar profiles.", ephemeral=True
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto. Tenta de novo em alguns segundos.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        prof = await self._profiles.get_profile(guild.id, profile)
        if prof is None:
            await interaction.response.send_message(
                "Profile não encontrado.", ephemeral=True
            )
            return

        view = ConfirmView(
            requester_id=interaction.user.id,
            prompt=f"Apagar **{discord.utils.escape_markdown(prof.name)}**? Essa ação é irreversível.",
            confirm_label="Apagar",
        )
        await interaction.response.send_message(
            view.prompt, view=view, ephemeral=True
        )
        await view.wait()
        if view.result is not True:
            return

        ok = await self._profiles.delete_profile(guild.id, profile)
        if not ok:
            await interaction.followup.send(
                "❌ Profile não encontrado (pode ter sido apagado por outro admin).",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"🗑️ Profile **{discord.utils.escape_markdown(prof.name)}** apagado.",
            ephemeral=True,
        )

    # --- /chatbot listar ------------------------------------------------------

    @chatbot.command(name="listar", description="Lista todos os profiles do servidor")
    async def chatbot_listar(self, interaction: discord.Interaction):
        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode ver a lista.", ephemeral=True
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        profiles = await self._profiles.list_profiles(guild.id)
        if not profiles:
            await interaction.response.send_message(
                f"Nenhum profile ainda. Use `/chatbot criar` para o primeiro "
                f"(limite: {C.MAX_PROFILES_PER_GUILD} por servidor).",
                ephemeral=True,
            )
            return

        lines = [
            f"**Profiles do servidor** ({len(profiles)}/{C.MAX_PROFILES_PER_GUILD})"
        ]
        for p in profiles:
            status = "⭐ ativo" if p.active else "inativo"
            prompt_preview = (p.system_prompt or "").strip().replace("\n", " ")
            if len(prompt_preview) > 80:
                prompt_preview = prompt_preview[:77] + "..."
            lines.append(
                f"\n**{discord.utils.escape_markdown(p.name)}** — {status}\n"
                f"`ID: {p.profile_id}` · temp `{p.temperature:.2f}` · memória `{p.history_size}`\n"
                f"> {discord.utils.escape_markdown(prompt_preview) or '(sem prompt)'}"
            )

        text = "\n".join(lines)
        await interaction.response.send_message(text[:2000], ephemeral=True)

    # --- /chatbot ativar <profile> --------------------------------------------

    @chatbot.command(name="ativar", description="Escolhe o profile ativo do servidor")
    @app_commands.describe(profile="Profile para ativar (substitui o atual)")
    @app_commands.autocomplete(profile=_profile_autocomplete)
    async def chatbot_ativar(self, interaction: discord.Interaction, profile: str):
        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode mudar o profile ativo.",
                ephemeral=True,
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        activated = await self._profiles.set_active_profile(guild.id, profile)
        if activated is None:
            await interaction.response.send_message(
                "Profile não encontrado.", ephemeral=True
            )
            return

        # Resposta PÚBLICA (não ephemeral) — o server todo se beneficia de saber
        # qual profile está ativo agora.
        await interaction.response.send_message(
            f"⭐ Profile ativo do chatbot agora é **"
            f"{discord.utils.escape_markdown(activated.name)}**.\n"
            f"-# Mencione o bot no início de uma mensagem ou responda a ele "
            f"para conversar."
        )

    # --- /chatbot desativar ---------------------------------------------------

    @chatbot.command(name="desativar", description="Desativa o chatbot (remove profile ativo)")
    async def chatbot_desativar(self, interaction: discord.Interaction):
        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode desativar.", ephemeral=True
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        count = await self._profiles.deactivate_all(guild.id)
        if count == 0:
            await interaction.response.send_message(
                "Nenhum profile estava ativo.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "🚫 Chatbot desativado. Menções e replies não serão respondidos "
            "até reativar com `/chatbot ativar`.",
            ephemeral=False,
        )

    # --- /chatbot reset_server ------------------------------------------------

    @chatbot.command(name="reset_server", description="Apaga TODA a memória do chatbot neste servidor")
    async def chatbot_reset_server(self, interaction: discord.Interaction):
        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode resetar memória do servidor.",
                ephemeral=True,
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        view = ConfirmView(
            requester_id=interaction.user.id,
            prompt=(
                "⚠️ Isso apaga **toda** a memória do chatbot neste servidor — "
                "pessoal de cada membro + coletiva. Operação irreversível. "
                "Confirma?"
            ),
            confirm_label="Apagar tudo",
        )
        await interaction.response.send_message(
            view.prompt, view=view, ephemeral=True
        )
        await view.wait()
        if view.result is not True:
            return

        count = await self._memory.clear_all_guild_memory(guild.id)
        await interaction.followup.send(
            f"🧹 Memória do chatbot resetada. ({count} registros removidos)",
            ephemeral=True,
        )

    # --- /reset (sem /chatbot) — qualquer membro ------------------------------

    @app_commands.command(
        name="reset",
        description="Reseta sua memória pessoal com o chatbot",
    )
    async def reset(self, interaction: discord.Interaction):
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        deleted = await self._memory.clear_user_history(guild.id, interaction.user.id)
        if deleted:
            msg = (
                "✅ Sua memória pessoal com o chatbot foi resetada. "
                "Ele vai começar do zero com você.\n"
                "-# A memória coletiva do servidor não foi afetada."
            )
        else:
            msg = "Você ainda não tinha memória salva com o chatbot."
        await interaction.response.send_message(msg, ephemeral=True)
