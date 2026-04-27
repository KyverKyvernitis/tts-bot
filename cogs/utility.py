import inspect
import os
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

import config
from .tts.aliases import format_prefixed_aliases, matches_prefixed_command
from .tts.utils.app_commands import fetch_root_command_ids_cached, slash_mention


HELP_EXPIRE_AFTER_SECONDS = 180.0
HELP_DISPATCH_TIMEOUT_SECONDS = 86400.0

HEALTH_COMMAND_GUILD_ID = 927002914449424404
HEALTH_COMMAND_GUILD = discord.Object(id=HEALTH_COMMAND_GUILD_ID)


from dataclasses import dataclass


@dataclass
class HelpPage:
    """Página do help. `body` é markdown que vai dentro de um TextDisplay; o
    Discord cuida da renderização. `title` aparece como heading com ##."""
    title: str
    body: str
    accent: discord.Color


class _HelpPageJumpModal(discord.ui.Modal, title="Ir para página"):
    """Modal acionado pelo botão central do paginator. O usuário digita o
    número da página e o paginator muda de página direto, sem ter que clicar
    várias vezes em prev/next."""

    page_input = discord.ui.TextInput(
        label="Número da página",
        placeholder="Ex: 5",
        required=True,
        max_length=2,
    )

    def __init__(self, view: "HelpPaginatorView"):
        super().__init__(timeout=60)
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.page_input.value or "").strip()
        try:
            target = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "Digite só o número da página.",
                ephemeral=True,
            )
            return
        total = len(self.view_ref.pages)
        if target < 1 or target > total:
            await interaction.response.send_message(
                f"Página inválida. Use 1 a {total}.",
                ephemeral=True,
            )
            return
        self.view_ref.page_index = target - 1
        self.view_ref._rebuild_layout()
        await interaction.response.edit_message(view=self.view_ref)


class HelpPaginatorView(discord.ui.LayoutView):
    """Paginator do help em Components V2. Usa Container + TextDisplay no lugar
    de Embed pra ter layout mais limpo. O botão central abre um modal de
    seleção rápida em vez de só mostrar o número."""

    def __init__(
        self,
        cog: "Utility",
        *,
        owner_id: int,
        pages: list[HelpPage],
        command_mention: str,
        prefix_hint: str,
        bot_avatar_url: str | None = None,
        timeout: float = 180,
    ):
        requested_timeout = max(1.0, float(timeout or HELP_EXPIRE_AFTER_SECONDS))
        dispatch_timeout = max(requested_timeout, HELP_DISPATCH_TIMEOUT_SECONDS)
        super().__init__(timeout=dispatch_timeout)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.pages = pages
        self.command_mention = str(command_mention or "`/help`")
        self.prefix_hint = str(prefix_hint or "`help`")
        self.bot_avatar_url = bot_avatar_url
        self.page_index = 0
        self.message: discord.Message | None = None
        self.expires_at_monotonic = time.monotonic() + requested_timeout
        self._rebuild_layout()

    def _rebuild_layout(self) -> None:
        """Reconstrói os componentes V2 com a página atual. Chamado a cada
        navegação porque a estrutura inteira do Container muda."""
        # Limpa items existentes do LayoutView e reanexa.
        for item in list(self.children):
            self.remove_item(item)

        page = self.pages[self.page_index]
        total = max(1, len(self.pages))
        at_start = self.page_index <= 0
        at_end = self.page_index >= total - 1

        # Cabeçalho: linha fina de breadcrumb + título destacado. O índice da
        # página fica em cima ao invés de no fim, aí o user já sabe onde tá
        # antes mesmo de ler o corpo.
        header_text = (
            f"-# 📖 Central de ajuda · página **{self.page_index + 1}** de **{total}**\n"
            f"## {page.title}"
        )

        # Rodapé curto com a dica do paginator. Vai abaixo do corpo, antes dos
        # botões — fica claro pra quê serve cada controle sem ter que clicar.
        footer_hint = (
            f"-# ⏪ início · ◀ voltar `{self.page_index + 1}/{total}` pular "
            f"▶ · fim ⏩"
        )

        # Linha de botões: ⏪ ◀ N/Total ▶ ⏩. O botão central abre o modal de
        # jump em vez de ser puramente decorativo.
        first = discord.ui.Button(
            emoji="⏪", style=discord.ButtonStyle.secondary, disabled=at_start,
        )
        first.callback = self._go_first

        prev_b = discord.ui.Button(
            emoji="◀️", style=discord.ButtonStyle.secondary, disabled=at_start,
        )
        prev_b.callback = self._go_prev

        jump = discord.ui.Button(
            label=f"{self.page_index + 1}/{total}",
            style=discord.ButtonStyle.primary,
        )
        jump.callback = self._open_jump_modal

        next_b = discord.ui.Button(
            emoji="▶️", style=discord.ButtonStyle.secondary, disabled=at_end,
        )
        next_b.callback = self._go_next

        last = discord.ui.Button(
            emoji="⏩", style=discord.ButtonStyle.secondary, disabled=at_end,
        )
        last.callback = self._go_last

        action_row = discord.ui.ActionRow(first, prev_b, jump, next_b, last)

        # Layout: cabeçalho · corpo · rodapé · botões. As Separators visuais
        # quebram a leitura sem precisar enfeitar o markdown do corpo.
        container = discord.ui.Container(
            discord.ui.TextDisplay(header_text),
            discord.ui.Separator(),
            discord.ui.TextDisplay(page.body),
            discord.ui.Separator(),
            discord.ui.TextDisplay(footer_hint),
            action_row,
            accent_color=page.accent,
        )
        self.add_item(container)

    def _is_expired(self) -> bool:
        return time.monotonic() >= self.expires_at_monotonic

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._is_expired():
            message = (
                "Essa central de ajuda já expirou porque ficou aberta por tempo demais.\n\n"
                f"Para abrir tudo de novo, use {self.command_mention} novamente"
                f" — ou, se preferir, {self.prefix_hint}."
            )
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except Exception:
                pass
            return False

        if int(getattr(getattr(interaction, "user", None), "id", 0) or 0) == self.owner_id:
            return True
        message = "Só quem abriu esse help pode trocar de página."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            pass
        return False

    async def on_timeout(self) -> None:
        pass

    async def _go_first(self, interaction: discord.Interaction):
        self.page_index = 0
        self._rebuild_layout()
        await interaction.response.edit_message(view=self)

    async def _go_prev(self, interaction: discord.Interaction):
        if self.page_index > 0:
            self.page_index -= 1
        self._rebuild_layout()
        await interaction.response.edit_message(view=self)

    async def _open_jump_modal(self, interaction: discord.Interaction):
        # Botão central: em vez de ser desabilitado, agora abre modal com
        # input pra digitar a página. Mais rápido que apertar prev/next várias
        # vezes pra chegar na página 8.
        await interaction.response.send_modal(_HelpPageJumpModal(self))

    async def _go_next(self, interaction: discord.Interaction):
        if self.page_index < len(self.pages) - 1:
            self.page_index += 1
        self._rebuild_layout()
        await interaction.response.edit_message(view=self)

    async def _go_last(self, interaction: discord.Interaction):
        self.page_index = max(0, len(self.pages) - 1)
        self._rebuild_layout()
        await interaction.response.edit_message(view=self)


class Utility(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._app_command_id_cache: dict[object, tuple[float, dict[str, int]]] = {}

    def _get_db(self):
        return getattr(self.bot, "settings_db", None)

    async def _maybe_await(self, value: Any):
        if inspect.isawaitable(value):
            return await value
        return value

    async def _get_prefix_data(self, guild: discord.Guild | None) -> dict[str, str]:
        defaults = {
            "bot_prefix": str(getattr(config, "BOT_PREFIX", getattr(config, "PREFIX", "_")) or "_"),
            "gtts_prefix": ".",
            "edge_prefix": ",",
            "gcloud_prefix": str(getattr(config, "GOOGLE_CLOUD_TTS_PREFIX", "'") or "'"),
        }
        if guild is None:
            return defaults

        db = self._get_db()
        if db is None or not hasattr(db, "get_guild_tts_defaults"):
            return defaults

        try:
            guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(guild.id))
        except Exception:
            guild_defaults = {}

        guild_defaults = guild_defaults or {}
        defaults["bot_prefix"] = str(guild_defaults.get("bot_prefix", defaults["bot_prefix"]) or defaults["bot_prefix"])
        defaults["gtts_prefix"] = str(guild_defaults.get("gtts_prefix", guild_defaults.get("tts_prefix", defaults["gtts_prefix"])) or defaults["gtts_prefix"])
        defaults["edge_prefix"] = str(guild_defaults.get("edge_prefix", defaults["edge_prefix"]) or defaults["edge_prefix"])
        defaults["gcloud_prefix"] = str(guild_defaults.get("gcloud_prefix", defaults["gcloud_prefix"]) or defaults["gcloud_prefix"])
        return defaults

    async def _fetch_root_command_ids_cached(self, guild: discord.Guild | None) -> dict[str, int]:
        return await fetch_root_command_ids_cached(
            self.bot,
            self._app_command_id_cache,
            guild,
            ttl_seconds=600.0,
            include_global_fallback=True,
        )

    def _build_help_pages(self, *, guild: discord.Guild | None, prefixes: dict[str, str], root_ids: dict[str, int]) -> list[HelpPage]:
        bot_prefix = prefixes["bot_prefix"]
        gtts_prefix = prefixes["gtts_prefix"]
        edge_prefix = prefixes["edge_prefix"]
        gcloud_prefix = prefixes["gcloud_prefix"]

        # Slash mentions: cada um vira `</nome:ID>` no Discord (clicável). Quando
        # o root não está no cache (comando guild-only fora da guild correta,
        # por exemplo), o helper retorna texto literal `/path`.
        help_slash = slash_mention(root_ids, root="help", path="help")
        ping_slash = slash_mention(root_ids, root="ping", path="ping")
        tts_menu_slash = slash_mention(root_ids, root="tts", path="tts menu")
        tts_status_slash = slash_mention(root_ids, root="tts", path="tts status")
        tts_user_slash = slash_mention(root_ids, root="tts", path="tts usuario")
        tts_server_menu_slash = slash_mention(root_ids, root="tts", path="tts server menu")
        chatbot_profile_slash = slash_mention(root_ids, root="chatbot", path="chatbot profile")
        chatbot_memoria_slash = slash_mention(root_ids, root="chatbot", path="chatbot memoria")
        imagem_slash = slash_mention(root_ids, root="imagem", path="imagem")
        chatbot_reset_slash = slash_mention(root_ids, root="reset", path="reset")
        economia_slash = slash_mention(root_ids, root="economia", path="economia")

        prefix_help = format_prefixed_aliases(bot_prefix, "help")
        prefix_panel = format_prefixed_aliases(bot_prefix, "panel_user")
        prefix_server_panel = format_prefixed_aliases(bot_prefix, "panel_server")
        prefix_join = format_prefixed_aliases(bot_prefix, "join")
        prefix_leave = format_prefixed_aliases(bot_prefix, "leave")
        prefix_clear = format_prefixed_aliases(bot_prefix, "clear")
        prefix_reset = format_prefixed_aliases(bot_prefix, "reset")
        prefix_set_lang = format_prefixed_aliases(bot_prefix, "set_lang")
        prefix_color = format_prefixed_aliases(bot_prefix, "color")
        prefix_coloredit = format_prefixed_aliases(bot_prefix, "coloredit")

        pages: list[HelpPage] = []

        # === Página 1 — Visão geral ===========================================
        nav = (
            "**1.** Visão geral e começo rápido\n"
            "**2.** Atalhos de fala\n"
            "**3.** Comandos do usuário\n"
            "**4.** Comandos do servidor\n"
            "**5.** Chatbot e imagens\n"
            "**6.** Cores\n"
            "**7.** Utilidades\n"
            "**8.** Economia\n"
            "**9.** Jogos"
        )
        prefixes_text = (
            f"**Bot:** `{bot_prefix}`  ·  **gTTS:** `{gtts_prefix}`  ·  "
            f"**Edge:** `{edge_prefix}`  ·  **gcloud:** `{gcloud_prefix}`"
        )
        quick_start = (
            f"› {help_slash} ou {prefix_help} — abre esta central\n"
            f"› {tts_menu_slash} ou {prefix_panel} — painel pessoal de TTS\n"
            f"› `{edge_prefix}oi cheguei na call` — fala usando Edge\n"
            f"› `{gtts_prefix}teste de voz` — fala usando gTTS"
        )
        pages.append(HelpPage(
            title="📚 Central de ajuda",
            body=(
                "Bem-vindo ao painel de ajuda do bot. Os comandos estão separados "
                "por categoria; use os botões abaixo pra navegar e o número no centro "
                "abre uma seleção rápida de página.\n\n"
                f"### Navegação\n{nav}\n\n"
                f"### Prefixos ativos neste servidor\n{prefixes_text}\n\n"
                f"### Começo rápido\n{quick_start}"
            ),
            accent=discord.Color.blurple(),
        ))

        # === Página 2 — Atalhos de fala =======================================
        pages.append(HelpPage(
            title="🔊 Atalhos de fala",
            body=(
                "Não são painéis: são **prefixos de fala**. Você manda a mensagem "
                "começando com o prefixo e o bot fala.\n\n"
                f"### gTTS — `{gtts_prefix}`\n"
                f"Exemplo: `{gtts_prefix}olá tudo bem`\n"
                "Voz padrão da web, leve, funciona em qualquer cenário.\n\n"
                f"### Edge — `{edge_prefix}`\n"
                f"Exemplo: `{edge_prefix}essa frase vai no edge`\n"
                "Vozes neurais da Microsoft. Mais natural, com várias opções por idioma.\n\n"
                f"### Google Cloud — `{gcloud_prefix}`\n"
                f"Exemplo: `{gcloud_prefix}essa fala usa o google cloud`\n"
                "Vozes premium do Google. Qualidade alta, requer config no servidor.\n\n"
                "Voz, idioma, velocidade e tom usados nesses prefixos seguem o "
                f"painel pessoal ({tts_menu_slash}) ou o painel do servidor."
            ),
            accent=discord.Color.purple(),
        ))

        # === Página 3 — Comandos do usuário ===================================
        pages.append(HelpPage(
            title="👤 Comandos do usuário",
            body=(
                "Painéis pessoais e atalhos voltados para cada membro.\n\n"
                f"### Painel pessoal\n"
                f"{tts_menu_slash} ou {prefix_panel}\n"
                "Abre o painel principal do seu TTS — botões e menus para mexer "
                "em voz, idioma, velocidade, tom e apelido falado sem precisar decorar comando.\n\n"
                f"### Status do TTS\n"
                f"{tts_status_slash}\n"
                "Vê o seu status, mostra o de outro usuário ou copia a config "
                "dele pra você. Ações: `self`, `show_other`, `copy_other`.\n\n"
                f"### Gerenciar outro usuário\n"
                f"{tts_user_slash}\n"
                "Abre o painel de outro usuário, troca o apelido falado dele "
                "ou reseta as configs. Ações: `panel`, `spoken_name`, `reset`."
            ),
            accent=discord.Color.green(),
        ))

        # === Página 4 — Comandos de servidor ==================================
        pages.append(HelpPage(
            title="🛡️ Comandos do servidor",
            body=(
                "Área de staff. Comandos marcados com 🔒 dependem da permissão "
                "**Expulsar membros**.\n\n"
                f"### 🔒 Painel do servidor\n"
                f"{tts_server_menu_slash} ou {prefix_server_panel}\n"
                "Define os padrões do servidor — prefixos, engine padrão, permissões e configs globais.\n\n"
                "### Conexão\n"
                f"Entrar na call: {prefix_join}\n"
                f"Sair da call: {prefix_leave}\n"
                f"Limpar fila: {prefix_clear}\n\n"
                "### Atalhos rápidos\n"
                f"🔒 Resetar um usuário: {prefix_reset} `@usuário`\n"
                f"Trocar idioma pessoal do gTTS: {prefix_set_lang} `pt`"
            ),
            accent=discord.Color.gold(),
        ))

        # === Página 5 — Chatbot e imagens =====================================
        pages.append(HelpPage(
            title="🤖 Chatbot e imagens",
            body=(
                "Profiles conversacionais, geração de imagem e memória do chatbot. "
                "Profiles exigem **Manage Server**.\n\n"
                f"### Profiles\n"
                f"{chatbot_profile_slash}\n"
                "Cria, lista, edita, apaga, ativa ou desativa profiles do chatbot no servidor. "
                "Ações: `Criar`, `Listar`, `Editar`, `Apagar`, `Ativar`, `Desativar`.\n"
                "Só um profile fica ativo por servidor. Outros profiles podem ser invocados "
                "temporariamente com `@nome` em uma mensagem.\n\n"
                f"### Geração de imagem\n"
                f"{imagem_slash} `prompt`\n"
                "Gera uma imagem a partir do texto. Quanto mais específico o prompt, melhor.\n\n"
                f"### Memória\n"
                f"Reset pessoal: {chatbot_reset_slash}\n"
                "Limpa só a sua conversa pessoal com o profile ativo.\n\n"
                f"Reset do servidor: {chatbot_memoria_slash}\n"
                "Apaga toda a memória do chatbot no servidor — pessoal de cada membro "
                "mais a coletiva, todos os profiles. Irreversível, exige Manage Server.\n\n"
                "### Como conversar\n"
                "Mencione (`@bot`) ou responda a uma mensagem dele para continuar a conversa. "
                "Use `@nome do profile` para invocar outro profile temporariamente sem trocar o ativo. "
                "Imagens e áudios anexados são entendidos pelo profile."
            ),
            accent=discord.Color.fuchsia(),
        ))

        # === Página 6 — Cores =================================================
        pages.append(HelpPage(
            title="🎨 Cores",
            body=(
                "Painel de cargos coloridos do servidor. Comandos exigem **Administrador**.\n\n"
                f"### Publicar o painel\n"
                f"{prefix_color}\n"
                "Posta o painel público de cores no canal atual. Os membros clicam na cor "
                "que quiserem e ganham o cargo correspondente. Substitui o painel anterior se já existir um.\n\n"
                f"### Editar as cores disponíveis\n"
                f"{prefix_coloredit}\n"
                "Abre um editor interativo para adicionar, remover ou trocar cores do painel — "
                "escolhe nome, hex e ícone de cada uma. As mudanças aplicam no painel publicado automaticamente.\n\n"
                "Cooldown curto entre publicações para evitar spam de painéis."
            ),
            accent=discord.Color.from_rgb(255, 105, 180),
        ))

        # === Página 7 — Utilidades ============================================
        pages.append(HelpPage(
            title="🧰 Utilidades",
            body=(
                "Comandos gerais e diagnósticos rápidos.\n\n"
                f"### Ping\n"
                f"{ping_slash}\n"
                "Latência, uptime, uso de recursos e status geral do bot.\n\n"
                f"### Help\n"
                f"{help_slash} ou {prefix_help}\n"
                "Abre esta central de ajuda. O número no centro do paginator "
                "abre um modal para pular direto para qualquer página."
            ),
            accent=discord.Color.teal(),
        ))

        # === Página 8 — Economia ==============================================
        pages.append(HelpPage(
            title="🪙 Economia",
            body=(
                "Saldo, daily, recarga, pagamentos e ranking. Triggers como `ficha` ou `daily` "
                "funcionam digitando a palavra sozinha no chat, sem prefixo.\n\n"
                "### Atalhos do dia a dia\n"
                f"Saldo: `{bot_prefix}ficha` ou `ficha`\n"
                "Extrato: `extrato` — 10 últimas movimentações\n"
                f"Daily: `{bot_prefix}daily` ou `daily`\n"
                "Recarga: `recarga` — volta saldo para 100 quando está abaixo de 15\n"
                f"Ranking: `{bot_prefix}rank` ou `rank`\n"
                "Pagar: `pay @usuário valor`\n"
                "Mendigar: `mendigar valor` ou `mendigar valor @usuário`\n\n"
                "### Como funciona o saldo\n"
                "Fichas bônus saem antes das normais nas apostas, e ganhos quitam dívida primeiro "
                "antes de voltar para o saldo. O daily dá fichas normais, fichas bônus e libera "
                "os giros extras do dia.\n\n"
                "### Painel de staff\n"
                f"`{bot_prefix}painelficha` (alias: `fichapainel`, `adminficha`)\n"
                "Painel completo para ajustar saldo, resetar usuário ou resetar o servidor inteiro. "
                "Qualquer staff com permissão **Expulsar membros** pode usar todos os botões.\n\n"
                "### Configuração\n"
                f"{economia_slash}\n"
                "Ativa/desativa a economia, define cargo staff e gerencia roles que recebem features extras."
            ),
            accent=discord.Color.orange(),
        ))

        # === Página 9 — Jogos =================================================
        pages.append(HelpPage(
            title="🎮 Jogos",
            body=(
                "Triggers são palavras digitadas sozinhas no chat. Se faltar saldo, "
                "o jogo avisa antes de te jogar no vermelho.\n\n"
                "### Apostas rápidas\n"
                "`roleta` — aposta com jackpot\n"
                "`carta` ou `cartas` — saque rápido de cartas\n\n"
                "### Lobbies com botão para entrar\n"
                "`buckshot` — rodada de sobrevivência\n"
                "`alvo` — disputa de mira\n"
                "`corrida` — corrida de cavalos\n\n"
                "### Mesas\n"
                "`poker` — mesa de poker com entrada própria\n"
                "`truco @usuário` — desafio de truco 1v1\n"
                "`truco2` — truco em duplas 2v2\n\n"
                "### Roubo\n"
                "`roubar @usuário` — alias `rob @usuário`\n"
                "Tenta roubar parte do saldo do alvo. Pode falhar; tem janela com cooldown."
            ),
            accent=discord.Color.dark_magenta(),
        ))

        return pages


    async def _send_help_response(
        self,
        *,
        guild: discord.Guild | None,
        owner: discord.abc.User,
        responder: discord.abc.Messageable,
        interaction: discord.Interaction | None = None,
        ephemeral: bool = False,
    ):
        prefixes = await self._get_prefix_data(guild)
        root_ids = await self._fetch_root_command_ids_cached(guild)
        pages = self._build_help_pages(guild=guild, prefixes=prefixes, root_ids=root_ids)
        avatar_url = None
        if self.bot.user and self.bot.user.display_avatar:
            avatar_url = self.bot.user.display_avatar.url
        view = HelpPaginatorView(
            self,
            owner_id=owner.id,
            pages=pages,
            command_mention=slash_mention(root_ids, root="help", path="help"),
            prefix_hint=f"`{prefixes['bot_prefix']}help`",
            bot_avatar_url=avatar_url,
        )

        # Components V2: o conteúdo (heading + corpo + botões) vai TODO dentro
        # do LayoutView. Não passa `embed=`, não passa `content=` — só `view=`.
        # A flag `components_v2=True` é exigida pelo Discord pra aceitar o
        # layout. discord.py 2.5+ sabe setar isso automaticamente quando o
        # view é uma LayoutView, mas mantemos explícito por clareza.
        if interaction is not None:
            if not interaction.response.is_done():
                await interaction.response.send_message(view=view, ephemeral=ephemeral)
                try:
                    view.message = await interaction.original_response()
                except Exception:
                    pass
            else:
                view.message = await interaction.followup.send(view=view, ephemeral=ephemeral)
            return

        view.message = await responder.send(view=view)

    def _format_bool_badge(self, value: bool, *, ok_label: str = "OK", bad_label: str = "Falha") -> str:
        return f"🟢 {ok_label}" if bool(value) else f"🔴 {bad_label}"

    def _format_duration(self, total_seconds: float | int | None) -> str:
        try:
            total = int(float(total_seconds or 0))
        except Exception:
            total = 0
        days, rem = divmod(max(0, total), 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if seconds or not parts:
            parts.append(f"{seconds}s")
        return " ".join(parts)

    def _format_ms(self, value: Any) -> str:
        try:
            return f"{float(value):.2f} ms"
        except Exception:
            return "n/a"

    def _format_bytes_human(self, value: int | float | None) -> str:
        try:
            size = float(value or 0)
        except Exception:
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB"]
        idx = 0
        while size >= 1024.0 and idx < len(units) - 1:
            size /= 1024.0
            idx += 1
        if idx == 0:
            return f"{int(size)} {units[idx]}"
        return f"{size:.2f} {units[idx]}"

    def _build_health_embeds(self) -> list[discord.Embed]:
        snapshot = {}
        get_snapshot = getattr(self.bot, "get_health_snapshot", None)
        if callable(get_snapshot):
            try:
                snapshot = get_snapshot() or {}
            except Exception:
                snapshot = {}

        tts_metrics = dict(snapshot.get("tts_metrics") or {})
        engine_metrics = dict(tts_metrics.get("engines") or {})

        tmp_root = os.path.join(os.getcwd(), "tmp_audio")
        runtime_dir = os.path.join(tmp_root, "runtime")
        cache_dir = os.path.join(tmp_root, "cache")
        credentials_dir = os.path.join(tmp_root, "credentials")

        def _dir_stats(path: str) -> tuple[int, int]:
            total_bytes = 0
            total_files = 0
            try:
                for entry in os.scandir(path):
                    if not entry.is_file():
                        continue
                    total_files += 1
                    try:
                        total_bytes += int(entry.stat().st_size)
                    except Exception:
                        pass
            except Exception:
                return 0, 0
            return total_files, total_bytes

        runtime_files, runtime_bytes = _dir_stats(runtime_dir)
        cache_files, cache_bytes = _dir_stats(cache_dir)
        cred_files, cred_bytes = _dir_stats(credentials_dir)
        total_tmp_bytes = runtime_bytes + cache_bytes + cred_bytes

        healthy = bool(snapshot.get("healthy"))
        starting = bool(snapshot.get("starting"))
        color = discord.Color.green() if healthy else (discord.Color.gold() if starting else discord.Color.red())

        cache_hits = int(tts_metrics.get("cache_hits", 0) or 0)
        cache_misses = int(tts_metrics.get("cache_misses", 0) or 0)
        cache_stores = int(tts_metrics.get("cache_stores", 0) or 0)
        total_cache_lookups = cache_hits + cache_misses
        cache_hit_rate = (cache_hits / total_cache_lookups * 100.0) if total_cache_lookups else 0.0

        title_badge = "🩺" if healthy else ("⏳" if starting else "🚨")
        summary = discord.Embed(
            title=f"{title_badge} Saúde geral do bot",
            description=(
                "Painel rápido de saúde, latência, fila, cache e engines do bot. "
                "Use isto para bater o olho e entender se está tudo estável."
            ),
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        summary.add_field(
            name="Estado do bot",
            value=(
                f"**Status:** `{snapshot.get('status', 'unknown')}`\n"
                f"**Healthy:** {self._format_bool_badge(snapshot.get('healthy'), ok_label='saudável', bad_label='com problema')}\n"
                f"**Inicializando:** {'🟡 sim' if starting else '⚪ não'}\n"
                f"**Discord pronto:** {self._format_bool_badge(snapshot.get('discord_ready'), ok_label='pronto', bad_label='não pronto')}\n"
                f"**Conexão fechada:** {'🔴 sim' if snapshot.get('discord_closed') else '🟢 não'}\n"
                f"**MongoDB:** {self._format_bool_badge(snapshot.get('mongo_ok'), ok_label='ok', bad_label='offline')}"
            ),
            inline=False,
        )
        summary.add_field(
            name="Tempo e rede",
            value=(
                f"**Uptime:** `{self._format_duration(snapshot.get('uptime_seconds'))}`\n"
                f"**Latência:** `{snapshot.get('latency_ms', 'n/a')} ms`\n"
                f"**Guilds:** `{snapshot.get('guild_count', len(self.bot.guilds))}`\n"
                f"**Voice clients:** `{len(getattr(self.bot, 'voice_clients', []) or [])}`"
            ),
            inline=True,
        )
        summary.add_field(
            name="Fila e despacho",
            value=(
                f"**Na fila agora:** `{int(tts_metrics.get('queued_items_current', 0) or 0)}`\n"
                f"**Enfileiradas:** `{int(tts_metrics.get('queue_enqueued', 0) or 0)}`\n"
                f"**Deduplicadas:** `{int(tts_metrics.get('queue_deduplicated', 0) or 0)}`\n"
                f"**Descartadas:** `{int(tts_metrics.get('queue_dropped', 0) or 0)}`\n"
                f"**Espera média:** `{self._format_ms(tts_metrics.get('avg_queue_wait_ms'))}`\n"
                f"**Despacho médio:** `{self._format_ms(tts_metrics.get('avg_dispatch_ms'))}`"
            ),
            inline=True,
        )
        summary.add_field(
            name="Cache e armazenamento",
            value=(
                f"**Hits:** `{cache_hits}`\n"
                f"**Misses:** `{cache_misses}`\n"
                f"**Stores:** `{cache_stores}`\n"
                f"**Hit rate:** `{cache_hit_rate:.1f}%`\n"
                f"**tmp_audio:** `{self._format_bytes_human(total_tmp_bytes)}`\n"
                f"**Runtime / Cache / Cred:** `{runtime_files}` / `{cache_files}` / `{cred_files}`"
            ),
            inline=False,
        )
        if self.bot.user and self.bot.user.display_avatar:
            summary.set_thumbnail(url=self.bot.user.display_avatar.url)
        summary.set_footer(text="Painel global • não é limitado ao servidor atual")

        engines = discord.Embed(
            title="⚙️ Engines e synth",
            description="Indicadores por engine para detectar lentidão, falhas repetidas e efetividade da cache.",
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        if engine_metrics:
            for engine_name, data in sorted(engine_metrics.items()):
                engines.add_field(
                    name=f"{engine_name.upper()}",
                    value=(
                        f"**Synths:** `{int(data.get('synth_count', 0) or 0)}`\n"
                        f"**Falhas:** `{int(data.get('synth_failures', 0) or 0)}`\n"
                        f"**Consecutivas:** `{int(data.get('consecutive_failures', 0) or 0)}`\n"
                        f"**Hits / Misses:** `{int(data.get('cache_hits', 0) or 0)}` / `{int(data.get('cache_misses', 0) or 0)}`\n"
                        f"**Média synth:** `{self._format_ms(data.get('avg_synth_ms'))}`\n"
                        f"**Última synth:** `{self._format_ms(data.get('last_synth_ms'))}`\n"
                        f"**Slow alerts:** `{int(data.get('slow_alerts', 0) or 0)}`\n"
                        f"**Último erro:** `{str(data.get('last_error') or 'nenhum')[:90]}`"
                    ),
                    inline=False,
                )
        else:
            engines.add_field(name="Sem dados", value="Ainda não há métricas suficientes de engine para mostrar aqui.", inline=False)
        if self.bot.user and self.bot.user.display_avatar:
            engines.set_thumbnail(url=self.bot.user.display_avatar.url)
        engines.set_footer(text="Métricas globais do TTS")
        return [summary, engines]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.content:
            return

        prefixes = await self._get_prefix_data(message.guild)
        bot_prefix = prefixes["bot_prefix"]
        if not matches_prefixed_command(message.content, bot_prefix, kind="help"):
            return

        await self._send_help_response(
            guild=message.guild,
            owner=message.author,
            responder=message.channel,
        )

    @app_commands.command(name="help", description="Mostra a central de ajuda com todos os comandos principais do bot")
    async def help_command(self, interaction: discord.Interaction):
        await self._send_help_response(
            guild=interaction.guild,
            owner=interaction.user,
            responder=interaction.channel,
            interaction=interaction,
            ephemeral=True,
        )

    @app_commands.command(name="health", description="Mostra a saúde geral do bot, fila, cache e engines")
    @app_commands.guilds(HEALTH_COMMAND_GUILD)
    async def health(self, interaction: discord.Interaction):
        embeds = self._build_health_embeds()
        await interaction.response.send_message(embeds=embeds, ephemeral=False)

    @app_commands.command(name="ping", description="Mostra o status atual e a latência do bot")
    async def ping(self, interaction: discord.Interaction):
        import os
        import time

        try:
            import psutil
        except Exception:
            psutil = None

        start = time.perf_counter()
        await interaction.response.defer(ephemeral=True)

        ws_ping = round(self.bot.latency * 1000)
        response_ping = round((time.perf_counter() - start) * 1000)

        now = discord.utils.utcnow()
        started_at = getattr(self.bot, "started_at", None)
        if started_at is None:
            uptime_text = "Desconhecido"
        else:
            delta = now - started_at
            total_seconds = int(delta.total_seconds())
            days, rem = divmod(total_seconds, 86400)
            hours, rem = divmod(rem, 3600)
            minutes, seconds = divmod(rem, 60)
            parts = []
            if days:
                parts.append(f"{days}d")
            if hours:
                parts.append(f"{hours}h")
            if minutes:
                parts.append(f"{minutes}m")
            if seconds or not parts:
                parts.append(f"{seconds}s")
            uptime_text = " ".join(parts)

        shard_id = getattr(interaction.guild, "shard_id", None)
        shard_text = str(shard_id) if shard_id is not None else "Único"
        db = getattr(self.bot, "settings_db", None)
        db_status = "🟢 Online" if db is not None else "🔴 Offline"

        memory_mb = 0.0
        cpu_percent = 0.0
        if psutil is not None:
            process = psutil.Process(os.getpid())
            memory_mb = process.memory_info().rss / 1024 / 1024
            cpu_percent = psutil.cpu_percent(interval=None)

        if ws_ping < 120:
            status_text, color = "🟢 Excelente", discord.Color.green()
        elif ws_ping < 250:
            status_text, color = "🟡 Boa", discord.Color.gold()
        elif ws_ping < 400:
            status_text, color = "🟠 Instável", discord.Color.orange()
        else:
            status_text, color = "🔴 Alta", discord.Color.red()

        embed = discord.Embed(title="🏓 Pong!", description="Status atual do bot em tempo real.", color=color)
        embed.add_field(name="Latência WebSocket", value=f"`{ws_ping}ms`", inline=True)
        embed.add_field(name="Resposta do comando", value=f"`{response_ping}ms`", inline=True)
        embed.add_field(name="Status geral", value=status_text, inline=True)
        embed.add_field(name="Uptime", value=f"`{uptime_text}`", inline=True)
        embed.add_field(name="Banco de dados", value=db_status, inline=True)
        embed.add_field(name="Shard", value=f"`{shard_text}`", inline=True)
        embed.add_field(name="Uso de memória", value=f"`{memory_mb:.2f} MB`", inline=True)
        embed.add_field(name="Uso de CPU", value=f"`{cpu_percent:.1f}%`", inline=True)
        embed.add_field(name="Servidores", value=f"`{len(self.bot.guilds)}`", inline=True)
        if self.bot.user and self.bot.user.display_avatar:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.set_footer(text="Atualizado no momento do comando")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
