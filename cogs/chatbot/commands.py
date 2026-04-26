"""Slash commands do chatbot.

Organização:
- `/chatbot profile <acao> [profile?]` — acao=criar|listar|editar|apagar|ativar|desativar
- `/chatbot memoria` — reseta toda a memória do servidor
- `/chatbotadmin master <acao>` — acao=ver|editar|transferir (management guild only)
- `/chatbotadmin reset_global` — apaga toda memória cross-guild (management guild only)
- Todos exigem permissão Manage Guild (staff).
- `/reset` — qualquer membro, limpa a memória PESSOAL dele.

Em vez de subgroups (que ficam poluídos no autocomplete do Discord), usamos
comandos diretos com `app_commands.Choice` pra escolher a ação. Fica apenas 2
entradas em `/chatbot` em vez de 10 espalhadas por 3 subgroups.

Comandos de "operador do bot" (master prompt, reset cross-guild) ficam em um
grupo separado `/chatbotadmin` registrado só na MANAGEMENT_GUILD_ID. Eles não
poderiam ficar dentro de /chatbot porque discord.py exige que toda a Group
seja guild-restricted juntos — não dá pra restringir só alguns subcomandos.

Implementação como Mixin: a classe `ChatbotCommandsMixin` é herdada pelo
`ChatbotCog` em `cog.py`. Isso mantém o `ChatbotCog` como UM cog só (um único
extension do discord.py), mas divide a responsabilidade entre arquivos.

Todas as respostas são ephemeral (só quem rodou vê), exceto casos onde
a feedback faz sentido ser público (p.ex. ativar — server todo se beneficia
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
from .views import ConfirmView, MasterEditModal, ProfileCreateModal, ProfileEditModal

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

    Qualquer exceção aqui é silenciada — autocomplete nunca pode crashar
    (o Discord mostra "No options found" em vez de erro pro user).
    """
    try:
        cog = interaction.client.get_cog("Chatbot")
        if cog is None or interaction.guild is None:
            return []
        profiles = getattr(cog, "_profiles", None)
        if profiles is None:
            return []
        all_profiles = await profiles.list_profiles(interaction.guild.id)
    except Exception:
        log.exception("chatbot: falha no autocomplete")
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


def _safe_slash(func):
    """Decorator: captura qualquer exceção em um slash command e responde ao
    usuário com erro amigável em vez de deixar "aplicativo não respondeu".

    O Discord dá só 3 segundos pra responder uma interaction, e se o comando
    ainda não chamou `interaction.response.*`, o user vê "aplicativo não
    respondeu". Com esse wrapper:
      - Exceção antes de responder → tenta responder com erro.
      - Exceção depois de responder → tenta followup.
      - Exceção no followup → só loga. Nada mais a fazer.

    Uso: aplica em cima de `@chatbot.command(...)` nos handlers.
    """
    import functools

    @functools.wraps(func)
    async def wrapper(self, interaction: discord.Interaction, *args, **kwargs):
        try:
            return await func(self, interaction, *args, **kwargs)
        except Exception as exc:
            log.exception("chatbot: exceção em %s", func.__name__)
            err_msg = (
                "❌ Erro interno no comando. Já anotei nos logs, tenta de novo "
                "em alguns segundos."
            )
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(err_msg, ephemeral=True)
                else:
                    await interaction.followup.send(err_msg, ephemeral=True)
            except Exception:
                pass  # último recurso: se nem o aviso de erro funciona, desiste

    return wrapper


class ChatbotCommandsMixin:
    """Mixin que adiciona todos os slash commands ao cog.

    A classe que herda DEVE ter os atributos: self.bot, self._profiles,
    self._memory, self._webhooks — todos preenchidos em cog_load. Os
    comandos validam isso e respondem com erro se o cog não estiver pronto.
    """

    # O grupo raiz /chatbot + subgrupos. Declarados como class-level —
    # discord.py automaticamente registra os subcomandos definidos com
    # decorator neles.
    #
    # Discord limita a 2 níveis de nesting: `/root subgroup command` é o
    # Discord tem um limite de 25 subcomandos por grupo, mas UX sofre muito antes.
    # Ao invés de subgroups aninhados (/chatbot profile criar, /chatbot master ver),
    # usamos comandos DIRETOS no grupo /chatbot, com as ações diferentes expostas
    # como Choice. Isso dá autocomplete enxuto (3 entradas em vez de 10) e URLs
    # mais curtas pra chamar.
    chatbot = app_commands.Group(
        name="chatbot",
        description="Gerenciamento do chatbot do servidor",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    # Grupo separado pra comandos cross-guild / "operador do bot" — só fica
    # registrado e visível na MANAGEMENT_GUILD. discord.py não permite restringir
    # subcomandos individuais de um Group por guild ("child commands cannot have
    # default guilds set"), então a única forma de ter comando guild-restricted
    # é criar um Group inteiro restrito. Por isso esses comandos NÃO ficam
    # dentro de /chatbot — usar /chatbot teria forçado todo o grupo a virar
    # guild-only, escondendo /chatbot profile do resto dos servers.
    chatbot_admin = app_commands.Group(
        name="chatbotadmin",
        description="Operações administrativas do bot (management guild only)",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_ids=[C.MANAGEMENT_GUILD_ID],
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

    async def _do_profile_criar(self, interaction: discord.Interaction):
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

        # IMPORTANTE: abrir o modal PRIMEIRO (antes de ir ao Mongo).
        # Discord dá 3s pra responder a interaction; se formos ao Mongo antes
        # do send_modal e demorar >3s, aparece "aplicativo não respondeu".
        # A checagem de limite é feita lá no on_submit do modal (também dentro
        # do próprio create_profile, defensivamente).
        modal = ProfileCreateModal(store=self._profiles, guild_limit=C.MAX_PROFILES_PER_GUILD)
        await interaction.response.send_modal(modal)

    # --- /chatbot editar <profile> --------------------------------------------

    async def _do_profile_editar(self, interaction: discord.Interaction, profile: str):
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

    async def _do_profile_apagar(self, interaction: discord.Interaction, profile: str):
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

        # Limpa as memórias órfãs daquele profile (pessoal + coletiva).
        # Fire-and-forget: se falhar, o `clear_all_guild_memory` do reset_server
        # pega depois. Não bloqueia a resposta ao admin.
        memory_count = 0
        try:
            memory_count = await self._memory.clear_profile_memory(
                guild.id, profile
            )
        except Exception:
            log.exception("chatbot: falha ao limpar memória do profile apagado")

        msg_tail = f" ({memory_count} memórias removidas)" if memory_count else ""
        await interaction.followup.send(
            f"🗑️ Profile **{discord.utils.escape_markdown(prof.name)}** "
            f"apagado.{msg_tail}",
            ephemeral=True,
        )

    # --- /chatbot listar ------------------------------------------------------

    async def _do_profile_listar(self, interaction: discord.Interaction):
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
                f"Nenhum profile ainda. Use `/chatbot profile criar` para o primeiro "
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

        # Nota sobre comportamento variável por canal
        lines.append(
            "\n-# 💡 Comportamento varia por canal: em canais com age-restriction "
            "os profiles têm mais liberdade; em canais normais, tom é controlado. "
            "Proibições absolutas (menores, crimes reais) sempre valem."
        )

        text = "\n".join(lines)
        await interaction.response.send_message(text[:2000], ephemeral=True)

    # --- /chatbot ativar <profile> --------------------------------------------

    async def _do_profile_ativar(self, interaction: discord.Interaction, profile: str):
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

        # Resposta PÚBLICA (não ephemeral) — o server todo se beneficia de
        # saber qual profile está ativo agora.
        # Embed pra ficar visualmente agradável e mostrar o avatar do profile.
        safe_name = discord.utils.escape_markdown(activated.name)
        embed = discord.Embed(
            title=f"⭐ Chatbot ativo: {activated.name}",
            description=(
                f"Mencione o bot no início de uma mensagem "
                f"(ex: {interaction.client.user.mention} oi) ou responda a "
                f"uma mensagem do **{safe_name}** para conversar."
            ),
            color=discord.Color.blurple(),
        )
        if activated.avatar_url:
            try:
                embed.set_thumbnail(url=activated.avatar_url)
            except Exception:
                pass  # URL inválida — só ignora o thumbnail
        await interaction.response.send_message(embed=embed)

    # --- /chatbot desativar ---------------------------------------------------

    async def _do_profile_desativar(self, interaction: discord.Interaction):
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
            "até reativar com `/chatbot profile acao:Ativar`.",
            ephemeral=False,
        )

    # --- /chatbot reset_server ------------------------------------------------

    async def _do_memoria_reset_server(self, interaction: discord.Interaction):
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

    # --- /chatbot master ------------------------------------------------------
    # Comandos de staff, mas com check adicional: só funcionam se a pessoa
    # está no config_guild_id atual. Quem controla o master prompt é o dono
    # do bot, e ele define qual server tem essa "autoridade".

    async def _master_check(
        self, interaction: discord.Interaction
    ) -> Optional[object]:
        """Valida que o comando pode rodar aqui.

        Retorna a MasterConfig atual se tudo ok (e o caller prossegue),
        ou None se falhou (e já respondeu com erro ao user).

        Checks:
          1. Cog pronto (master store inicializado)
          2. Usuário é staff NESTE server
          3. Este server é o config_guild_id atual
        """
        master = getattr(self, "_master", None)
        if master is None:
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return None

        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode mexer no prompt mestre.",
                ephemeral=True,
            )
            return None

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return None

        cfg = await master.get()
        if int(guild.id) != int(cfg.config_guild_id):
            await interaction.response.send_message(
                "❌ Este servidor não é o **config server** do bot. "
                f"O prompt mestre só pode ser editado no servidor de "
                f"configuração (ID atualmente: `{cfg.config_guild_id}`).\n"
                f"-# Pra transferir a config pra este server, rode "
                f"`/chatbotadmin master acao:Transferir` a partir do server atual.",
                ephemeral=True,
            )
            return None

        return cfg

    async def _do_master_ver(self, interaction: discord.Interaction):
        cfg = await self._master_check(interaction)
        if cfg is None:
            return

        import datetime as _dt
        updated_str = "nunca"
        if cfg.updated_at > 0:
            updated_str = _dt.datetime.fromtimestamp(
                cfg.updated_at
            ).strftime("%Y-%m-%d %H:%M")

        # Se muito longo, usa code block numa mensagem só. Se ultra longo,
        # anexaria arquivo — mas como limite é 4000 chars e Discord aceita
        # 2000 na mensagem, talvez precise truncar.
        content = cfg.prompt or "(vazio — usando default)"
        preview = content[:1700]
        if len(content) > 1700:
            preview += "\n... (truncado no preview)"

        await interaction.response.send_message(
            f"**Prompt mestre atual** ({len(content)} chars, "
            f"última edição: {updated_str}):\n```\n{preview}\n```",
            ephemeral=True,
        )

    async def _do_master_editar(self, interaction: discord.Interaction):
        cfg = await self._master_check(interaction)
        if cfg is None:
            return

        modal = MasterEditModal(
            master_store=self._master,
            current_content=cfg.prompt,
        )
        await interaction.response.send_modal(modal)

    async def _do_master_transferir(
        self, interaction: discord.Interaction,
    ):
        # IMPORTANTE: este comando faz check invertido. O user precisa estar
        # NO config_guild_id ATUAL (não no novo) pra poder transferir.
        # Isso previne hijack: ninguém pode "pegar" o bot criando um server
        # e rodando o comando lá.
        master = getattr(self, "_master", None)
        if master is None:
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return

        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode transferir a config.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        cfg = await master.get()

        # Edge case: se o config_guild_id atual for inválido (ex: server não
        # existe mais), aceita a transferência de qualquer server com staff.
        # Permite "recuperação" caso o config original seja perdido.
        current_config_server = self.bot.get_guild(int(cfg.config_guild_id))
        if current_config_server is None:
            # Config server atual é inacessível — libera transferência
            pass
        elif int(guild.id) != int(cfg.config_guild_id):
            await interaction.response.send_message(
                f"❌ Você precisa estar **no config server atual** pra "
                f"transferir a autoridade. Config atual: "
                f"`{cfg.config_guild_id}` (`{current_config_server.name}`).\n"
                f"-# Rode o comando lá pra mudar a config pra este servidor.",
                ephemeral=True,
            )
            return
        # Se já é o mesmo, operação é no-op mas avisamos
        if int(guild.id) == int(cfg.config_guild_id):
            await interaction.response.send_message(
                "Este servidor **já é** o config server. Nada a fazer.",
                ephemeral=True,
            )
            return

        # Confirmação: ação destrutiva (perda de autoridade do server antigo)
        view = ConfirmView(
            requester_id=interaction.user.id,
            prompt=(
                f"⚠️ Transferir autoridade do prompt mestre pra **{guild.name}** "
                f"(`{guild.id}`)? O server antigo (`{cfg.config_guild_id}`) "
                f"perderá o acesso ao `/chatbotadmin master`."
            ),
            confirm_label="Transferir",
        )
        await interaction.response.send_message(
            view.prompt, view=view, ephemeral=True,
        )
        await view.wait()
        if view.result is not True:
            return

        new_cfg = await master.set_config_guild(
            guild.id,
            updated_by=interaction.user.id,
        )
        await interaction.followup.send(
            f"✅ Config server atualizado pra **{guild.name}** "
            f"(`{new_cfg.config_guild_id}`).",
            ephemeral=True,
        )

    # =========================================================================
    # Entrypoints diretos — evita o sufixo `acao` nos comandos.
    # =========================================================================

    # --- /chatbot profile <acao> [profile?] -----------------------------------
    # Um comando só que cobre as 6 ações de profile. Profile é opcional na UI
    # porque "criar", "listar" e "desativar" não precisam dele; o runtime valida
    # que "editar", "apagar" e "ativar" receberam um profile.
    @chatbot.command(
        name="profile",
        description="Criar, listar, editar, apagar, ativar ou desativar profiles",
    )
    @app_commands.choices(
        acao=[
            app_commands.Choice(name="Criar novo profile", value="criar"),
            app_commands.Choice(name="Listar todos os profiles", value="listar"),
            app_commands.Choice(name="Editar um profile", value="editar"),
            app_commands.Choice(name="Apagar um profile", value="apagar"),
            app_commands.Choice(name="Ativar um profile no servidor", value="ativar"),
            app_commands.Choice(name="Desativar o chatbot no servidor", value="desativar"),
        ]
    )
    @app_commands.autocomplete(profile=_profile_autocomplete)
    @_safe_slash
    async def chatbot_profile(
        self,
        interaction: discord.Interaction,
        acao: app_commands.Choice[str],
        profile: Optional[str] = None,
    ):
        action = acao.value
        # Valida que as ações que exigem profile receberam um.
        if action in ("editar", "apagar", "ativar") and not profile:
            await interaction.response.send_message(
                f"❌ A ação **{acao.name}** exige que você escolha um profile "
                f"no campo `profile`.",
                ephemeral=True,
            )
            return
        if action == "criar":
            await self._do_profile_criar(interaction)
        elif action == "listar":
            await self._do_profile_listar(interaction)
        elif action == "desativar":
            await self._do_profile_desativar(interaction)
        elif action == "editar":
            await self._do_profile_editar(interaction, profile)  # type: ignore[arg-type]
        elif action == "apagar":
            await self._do_profile_apagar(interaction, profile)  # type: ignore[arg-type]
        elif action == "ativar":
            await self._do_profile_ativar(interaction, profile)  # type: ignore[arg-type]

    # --- /chatbot memoria -----------------------------------------------------
    # Antes era um subgroup com 1 só comando (`reset_server`). Agora é comando
    # direto porque não faz sentido subgroup pra 1 ação.
    @chatbot.command(
        name="memoria",
        description="Resetar toda a memória do servidor (destrutivo)",
    )
    @_safe_slash
    async def chatbot_memoria(self, interaction: discord.Interaction):
        await self._do_memoria_reset_server(interaction)

    # --- /chatbotadmin master <acao> ------------------------------------------
    # Era /chatbot master, mas movido pro grupo `chatbotadmin` (guild-restrict)
    # porque só faz sentido na management guild — o comando edita o prompt
    # mestre global do bot, não tem por que aparecer pra staff de outras guilds.
    @chatbot_admin.command(
        name="master",
        description="Ver, editar ou transferir o prompt mestre global",
    )
    @app_commands.choices(
        acao=[
            app_commands.Choice(name="Ver prompt mestre", value="ver"),
            app_commands.Choice(name="Editar prompt mestre", value="editar"),
            app_commands.Choice(name="Transferir config pra este servidor", value="transferir"),
        ]
    )
    @_safe_slash
    async def chatbotadmin_master(
        self,
        interaction: discord.Interaction,
        acao: app_commands.Choice[str],
    ):
        if acao.value == "ver":
            await self._do_master_ver(interaction)
        elif acao.value == "editar":
            await self._do_master_editar(interaction)
        elif acao.value == "transferir":
            await self._do_master_transferir(interaction)

    # --- /chatbotadmin reset_global -------------------------------------------
    # Apaga TODA memória do chatbot — todas as guilds, todos os profiles,
    # pessoal e coletiva. Só registrado na management guild via guild_ids
    # do Group, então o autocomplete não mostra esse comando em outros servers.
    # Mantemos confirmação dupla porque é destrutivo e cross-guild.
    @chatbot_admin.command(
        name="reset_global",
        description="Apagar TODA a memória do chatbot em TODAS as guilds (irreversível)",
    )
    @_safe_slash
    async def chatbotadmin_reset_global(self, interaction: discord.Interaction):
        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode rodar isso.",
                ephemeral=True,
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return

        view = ConfirmView(
            requester_id=interaction.user.id,
            prompt=(
                "🚨 **RESET GLOBAL** 🚨\n\n"
                "Isso apaga **toda** a memória do chatbot em **todas as guilds** "
                "onde o bot está presente — pessoal de cada membro + coletiva, "
                "todos os profiles. Operação **irreversível**.\n\n"
                "Confirma?"
            ),
            confirm_label="Apagar tudo (cross-guild)",
        )
        await interaction.response.send_message(
            view.prompt, view=view, ephemeral=True
        )
        await view.wait()
        if view.result is not True:
            return

        count = await self._memory.clear_all_memory_everywhere()
        log.warning(
            "chatbot: reset_global executado | requester=%s registros_apagados=%s",
            interaction.user.id, count,
        )
        await interaction.followup.send(
            f"🧹 Memória global do chatbot apagada. ({count} registros removidos "
            f"em todas as guilds)",
            ephemeral=True,
        )

    # --- /imagem <prompt> — gera imagem via Gemini (qualquer membro) ---------
    # Top-level (não em /chatbot) porque /chatbot é staff-only via
    # default_permissions. Imagegen é liberado pra qualquer membro.

    @app_commands.command(
        name="imagem",
        description="Gera uma imagem a partir da descrição (usa o profile ativo)",
    )
    @app_commands.describe(
        prompt="Descrição da imagem que você quer gerar (ex: 'gato siamês surfando')",
    )
    @_safe_slash
    async def imagem(
        self,
        interaction: discord.Interaction,
        prompt: str,
    ):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando só funciona em servidor.", ephemeral=True
            )
            return
        if self._profiles is None or self._webhooks is None:
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return

        # Rate-limit por usuário — mesma lógica do chat normal
        if self._is_user_on_cooldown(guild.id, interaction.user.id):
            await interaction.response.send_message(
                "⌛ Espera um pouco antes de pedir outra imagem.", ephemeral=True
            )
            return
        self._apply_user_cooldown(guild.id, interaction.user.id)

        # Pega profile ativo do server pra dar identidade à imagem
        active = await self._profiles.get_active_profile(guild.id)
        if active is None:
            await interaction.response.send_message(
                "Não há profile ativo neste servidor. A staff precisa ativar "
                "um com `/chatbot profile acao:Ativar` antes.",
                ephemeral=True,
            )
            return

        # Imagegen demora, então defer a interaction pra não estourar 3s
        await interaction.response.defer(thinking=True)

        from . import imagegen as _imagegen
        import io as _io

        try:
            # NSFW só vale se: (a) canal é age-restricted no Discord, E
            # (b) a guild está na allowlist de NSFW (constants.nsfw_enabled_for_guild).
            # Fora da allowlist, força channel_is_nsfw=False — o imagegen vai
            # tratar como SFW e recusar prompts adultos com a mesma mensagem
            # genérica que daria pra um canal não-NSFW qualquer.
            channel_nsfw_flag = bool(getattr(interaction.channel, "nsfw", False))
            effective_nsfw = channel_nsfw_flag and C.nsfw_enabled_for_guild(guild.id)
            generated = await _imagegen.generate_image(
                self._session,
                prompt=prompt.strip()[:1000],
                channel_is_nsfw=effective_nsfw,
            )
        except Exception:
            log.exception("chatbot: /imagem falhou")
            generated = None

        if generated is None or not generated.ok or generated.image is None:
            if generated is None:
                err_msg = (
                    "🖼️ Não consegui gerar a imagem agora. "
                    "Tenta reescrever o pedido ou espera um pouco."
                )
            else:
                err_msg = _imagegen.build_image_failure_message(generated)
            await interaction.followup.send(
                err_msg,
                ephemeral=True,
            )
            return

        # Envia via webhook (com identidade do profile ativo) no canal onde
        # foi invocado, e confirma na interaction
        ext = "png" if "png" in (generated.image.mime_type or "") else "jpg"
        safe_name = "".join(c for c in active.name if c.isalnum())[:20] or "image"
        filename = f"{safe_name}.{ext}"

        channel = interaction.channel
        if not isinstance(
            channel,
            (discord.TextChannel, discord.VoiceChannel,
             discord.StageChannel, discord.Thread),
        ):
            # Canal esquisito — manda só pela interaction
            file = discord.File(_io.BytesIO(generated.image.data), filename=filename)
            await interaction.followup.send(
                content=f"🖼️ Imagem gerada para: *{prompt[:200]}*",
                file=file,
            )
            return

        file = discord.File(_io.BytesIO(generated.image.data), filename=filename)
        caption = f"🖼️ Imagem gerada para: *{prompt[:200]}*"
        sent = await self._webhooks.send_as_profile(
            channel=channel,
            profile_name=active.name,
            avatar_url=active.avatar_url,
            content=caption[:1900],
            files=[file],
        )
        if sent is None:
            # Fallback: manda direto na interaction
            file2 = discord.File(_io.BytesIO(generated.image.data), filename=filename)
            await interaction.followup.send(
                content=caption[:1900],
                file=file2,
            )
            return
        # Deu certo via webhook — confirma na interaction sem duplicar
        await interaction.followup.send(
            f"✅ Imagem enviada no canal.",
            ephemeral=True,
        )

    # --- /reset (sem /chatbot) — qualquer membro ------------------------------

    @app_commands.command(
        name="reset",
        description="Reseta sua memória pessoal com o chatbot",
    )
    @_safe_slash
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

        # Apaga memória do user com TODOS os profiles do server (sem
        # profile_id = wildcard). É o que o user espera: "esquece tudo
        # que sabe sobre mim aqui".
        count = await self._memory.clear_user_history(
            guild.id, interaction.user.id
        )
        if count > 0:
            msg = (
                f"✅ Sua memória pessoal com o chatbot foi resetada "
                f"({count} registros). O bot vai começar do zero com você.\n"
                f"-# A memória coletiva do servidor não foi afetada."
            )
        else:
            msg = "Você ainda não tinha memória salva com o chatbot."
        await interaction.response.send_message(msg, ephemeral=True)
