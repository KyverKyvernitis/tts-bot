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


class HelpPaginatorView(discord.ui.View):
    def __init__(self, cog: "Utility", *, owner_id: int, pages: list[discord.Embed], command_mention: str, prefix_hint: str, timeout: float = 180):
        requested_timeout = max(1.0, float(timeout or HELP_EXPIRE_AFTER_SECONDS))
        dispatch_timeout = max(requested_timeout, HELP_DISPATCH_TIMEOUT_SECONDS)
        super().__init__(timeout=dispatch_timeout)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.pages = pages
        self.command_mention = str(command_mention or "`/help`")
        self.prefix_hint = str(prefix_hint or "`help`")
        self.page_index = 0
        self.message: discord.Message | None = None
        self.expires_at_monotonic = time.monotonic() + requested_timeout
        self._refresh_buttons()

    def _is_expired(self) -> bool:
        return time.monotonic() >= self.expires_at_monotonic

    def _refresh_buttons(self) -> None:
        total = max(1, len(self.pages))
        at_start = self.page_index <= 0
        at_end = self.page_index >= total - 1

        self.first_button.disabled = at_start
        self.prev_button.disabled = at_start
        self.page_button.label = f"{self.page_index + 1}/{total}"
        self.page_button.disabled = True
        self.next_button.disabled = at_end
        self.last_button.disabled = at_end

        self.clear_items()
        self.add_item(self.first_button)
        self.add_item(self.prev_button)
        self.add_item(self.page_button)
        self.add_item(self.next_button)
        self.add_item(self.last_button)

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

    @discord.ui.button(emoji="⏪", style=discord.ButtonStyle.secondary, row=0)
    async def first_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page_index = 0
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page_index], view=self)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, row=0)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page_index > 0:
            self.page_index -= 1
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page_index], view=self)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.primary, disabled=True, row=0)
    async def page_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, row=0)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page_index < len(self.pages) - 1:
            self.page_index += 1
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page_index], view=self)

    @discord.ui.button(emoji="⏩", style=discord.ButtonStyle.secondary, row=0)
    async def last_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page_index = max(0, len(self.pages) - 1)
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page_index], view=self)


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

    def _build_help_embeds(self, *, guild: discord.Guild | None, prefixes: dict[str, str], root_ids: dict[str, int]) -> list[discord.Embed]:
        bot_prefix = prefixes["bot_prefix"]
        gtts_prefix = prefixes["gtts_prefix"]
        edge_prefix = prefixes["edge_prefix"]
        gcloud_prefix = prefixes["gcloud_prefix"]

        help_slash = slash_mention(root_ids, root="help", path="help")
        ping_slash = slash_mention(root_ids, root="ping", path="ping")
        tts_menu_slash = slash_mention(root_ids, root="tts", path="tts menu")
        tts_status_slash = slash_mention(root_ids, root="tts", path="tts status")
        tts_user_slash = slash_mention(root_ids, root="tts", path="tts usuario")
        tts_server_menu_slash = slash_mention(root_ids, root="tts", path="tts server menu")

        # Mentions adicionais pra chatbot, toggles, cores e economia.
        # slash_mention retorna `</nome:0>` se o root não estiver no cache;
        # quando o comando é de outra árvore, fallback é o texto literal.
        chatbot_profile_slash = slash_mention(root_ids, root="chatbot", path="chatbot profile")
        chatbot_memoria_slash = slash_mention(root_ids, root="chatbot", path="chatbot memoria")
        imagem_slash = slash_mention(root_ids, root="imagem", path="imagem")
        chatbot_reset_slash = slash_mention(root_ids, root="reset", path="reset")
        toggle_menu_slash = slash_mention(root_ids, root="toggle_menu", path="toggle_menu")
        economia_slash = slash_mention(root_ids, root="economia", path="economia")

        prefix_help = format_prefixed_aliases(bot_prefix, "help")
        prefix_panel = format_prefixed_aliases(bot_prefix, "panel_user")
        prefix_server_panel = format_prefixed_aliases(bot_prefix, "panel_server")
        prefix_join = format_prefixed_aliases(bot_prefix, "join")
        prefix_leave = format_prefixed_aliases(bot_prefix, "leave")
        prefix_clear = format_prefixed_aliases(bot_prefix, "clear")
        prefix_reset = f"{format_prefixed_aliases(bot_prefix, 'reset')} `@usuário`"
        prefix_set_lang = f"{format_prefixed_aliases(bot_prefix, 'set_lang')} `pt`"
        prefix_color = format_prefixed_aliases(bot_prefix, "color")
        prefix_coloredit = format_prefixed_aliases(bot_prefix, "coloredit")

        pages: list[discord.Embed] = []

        overview = discord.Embed(
            title="📘 Central de ajuda do TTS",
            description=(
                f"Tudo que importa no bot, separado por categoria e com exemplos práticos pra você achar o comando certo sem enrolação."
            ),
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        overview.add_field(
            name="🧭 Navegação",
            value=(
                "**Página 1** • visão geral\n"
                "**Página 2** • atalhos de fala por prefixo\n"
                "**Página 3** • comandos do usuário\n"
                "**Página 4** • comandos de servidor\n"
                "**Página 5** • chatbot e imagens\n"
                "**Página 6** • cores e outros\n"
                "**Página 7** • utilidades\n"
                "**Página 8** • economia\n"
                "**Página 9** • jogos"
            ),
            inline=False,
        )
        overview.add_field(
            name="⚙️ Prefixos ativos",
            value=(
                f"**Bot:** `{bot_prefix}`\n"
                f"**gTTS:** `{gtts_prefix}`\n"
                f"**Edge:** `{edge_prefix}`\n"
                f"**Google Cloud:** `{gcloud_prefix}`"
            ),
            inline=True,
        )
        overview.add_field(
            name="✨ Começo rápido",
            value=(
                f"{help_slash} ou {prefix_help}\n"
                f"{tts_menu_slash} ou {prefix_panel}\n"
                f"`{edge_prefix}oi, cheguei na call`\n"
                f"`{gtts_prefix}teste de voz`"
            ),
            inline=True,
        )
        pages.append(overview)

        speech_page = discord.Embed(
            title="🎙️ Atalhos de fala por prefixo",
            description="Esses não são painéis; são os prefixos que fazem o bot falar a mensagem diretamente.",
            color=discord.Color.purple(),
            timestamp=discord.utils.utcnow(),
        )
        speech_page.add_field(
            name="🌐 gTTS",
            value=(
                f"**Prefixo:** `{gtts_prefix}`\n"
                f"**Exemplo:** `{gtts_prefix}olá, tudo bem?`\n"
                "**Uso:** fala a mensagem usando o modo gTTS."
            ),
            inline=False,
        )
        speech_page.add_field(
            name="🗣️ Edge",
            value=(
                f"**Prefixo:** `{edge_prefix}`\n"
                f"**Exemplo:** `{edge_prefix}essa frase vai no edge`\n"
                "**Uso:** fala a mensagem usando o modo Edge."
            ),
            inline=False,
        )
        speech_page.add_field(
            name="☁️ Google Cloud",
            value=(
                f"**Prefixo:** `{gcloud_prefix}`\n"
                f"**Exemplo:** `{gcloud_prefix}essa frase vai no google cloud`\n"
                "**Uso:** fala a mensagem usando o modo Google Cloud."
            ),
            inline=False,
        )
        speech_page.add_field(
            name="📝 Observação",
            value="As vozes, idiomas e ajustes usados nesses prefixos podem mudar conforme o painel do usuário ou o painel do servidor.",
            inline=False,
        )
        pages.append(speech_page)

        user_page = discord.Embed(
            title="👤 Comandos do usuário",
            description="Painéis pessoais, status e ajustes voltados para cada membro.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        user_page.add_field(
            name="🟢 Painel pessoal",
            value=(
                f"**Slash:** {tts_menu_slash}\n"
                f"**Prefixo:** {prefix_panel}\n"
                "**Uso:** abre o painel principal do seu TTS com botões e menus."
            ),
            inline=False,
        )
        user_page.add_field(
            name="🏠 Status do TTS",
            value=(
                f"**Slash:** {tts_status_slash}\n"
                "**Uso:** ver o próprio status, mostrar o de outro usuário ou copiar a configuração dele.\n"
                "**Exemplos:** `acao=self`, `acao=show_other`, `acao=copy_other`."
            ),
            inline=False,
        )
        user_page.add_field(
            name="🔒 Gerenciar um usuário",
            value=(
                f"**Slash:** {tts_user_slash}\n"
                "**Uso:** abrir o painel de outro usuário, trocar o apelido falado ou resetar as configurações dele.\n"
                "**Ações:** `panel`, `spoken_name`, `reset`."
            ),
            inline=False,
        )
        user_page.add_field(
            name="💡 Dica",
            value="O painel pessoal é o atalho mais completo quando você quer mexer em voz, idioma, velocidade, tom e apelido falado sem decorar comando.",
            inline=False,
        )
        pages.append(user_page)

        server_page = discord.Embed(
            title="🏠 Comandos de servidor e moderação",
            description="Ferramentas para administrar o comportamento do TTS no servidor.",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        server_page.add_field(
            name="🔒 Painel do servidor",
            value=(
                f"**Slash:** {tts_server_menu_slash}\n"
                f"**Prefixo:** {prefix_server_panel}\n"
                "**Uso:** abre o painel com os padrões do servidor, como prefixos, engine padrão e configurações globais."
            ),
            inline=False,
        )
        server_page.add_field(
            name="🏠 Controle de conexão",
            value=(
                f"**Entrar na call:** {prefix_join}\n"
                f"**Sair da call:** {prefix_leave}\n"
                f"**Limpar fila:** {prefix_clear}"
            ),
            inline=False,
        )
        server_page.add_field(
            name="🔒 Administração rápida por prefixo",
            value=(
                f"**Resetar um usuário:** {prefix_reset}\n"
                f"**Trocar idioma pessoal do gTTS:** {prefix_set_lang}"
            ),
            inline=False,
        )
        server_page.add_field(
            name="🛡️ Permissão",
            value="Os itens marcados com 🔒 dependem da permissão `Expulsar Membros`.",
            inline=False,
        )
        pages.append(server_page)

        # --- Página: chatbot e imagens ----------------------------------------
        chatbot_page = discord.Embed(
            title="🤖 Chatbot e imagens",
            description=(
                "Personagens conversacionais (profiles), geração de imagem e "
                "memória do chatbot. Configurações de profile exigem permissão "
                "Manage Server."
            ),
            color=discord.Color.fuchsia(),
            timestamp=discord.utils.utcnow(),
        )
        chatbot_page.add_field(
            name="🎭 Profiles",
            value=(
                f"**Slash:** {chatbot_profile_slash}\n"
                "**Uso:** criar, listar, editar, apagar, ativar ou desativar profiles "
                "(personagens) do chatbot no servidor.\n"
                "**Ações:** `Criar`, `Listar`, `Editar`, `Apagar`, `Ativar`, `Desativar`.\n"
                "Apenas um profile fica ativo por servidor — ele responde a menções "
                "e replies. Outros profiles podem ser invocados temporariamente "
                "via `@nome` em uma mensagem."
            ),
            inline=False,
        )
        chatbot_page.add_field(
            name="🖼️ Geração de imagem",
            value=(
                f"**Slash:** {imagem_slash} `prompt`\n"
                "**Uso:** gera uma imagem a partir do texto. Em canais NSFW "
                "(em servidores onde a feature está habilitada), o gerador "
                "aceita conteúdo adulto; senão fica em modo SFW e recusa "
                "esses pedidos.\n"
                "**Dica:** seja específico no prompt — \"floresta de pinheiros "
                "ao amanhecer, neblina, fotografia\" rende melhor que \"árvore\"."
            ),
            inline=False,
        )
        chatbot_page.add_field(
            name="🧹 Memória",
            value=(
                f"**Reset pessoal:** {chatbot_reset_slash}\n"
                "Limpa a sua conversa pessoal com o profile ativo (qualquer membro "
                "pode rodar pra si mesmo).\n"
                f"\n**Reset do servidor:** {chatbot_memoria_slash}\n"
                "Apaga toda a memória do chatbot no servidor — pessoal de cada "
                "membro + coletiva, todos os profiles. Operação irreversível, "
                "exige Manage Server."
            ),
            inline=False,
        )
        chatbot_page.add_field(
            name="💡 Como conversar",
            value=(
                "• Mencione o profile ativo (`@bot`) ou responda a uma mensagem "
                "dele pra continuar a conversa.\n"
                "• Use `@nome do profile` pra invocar **outro** profile "
                "temporariamente (sem trocar o ativo).\n"
                "• Anexe imagens/áudios — o profile entende e reage."
            ),
            inline=False,
        )
        pages.append(chatbot_page)

        # --- Página: cores e outros -------------------------------------------
        others_page = discord.Embed(
            title="🎨 Cores, toggles e outros",
            description=(
                "Customizações pessoais e ajustes rápidos por servidor."
            ),
            color=discord.Color.magenta(),
            timestamp=discord.utils.utcnow(),
        )
        others_page.add_field(
            name="🌈 Cores personalizadas",
            value=(
                f"**Pedir uma cor:** {prefix_color} `#hex` ou nome\n"
                f"**Editar a sua cor:** {prefix_coloredit} `#hex`\n"
                "**Uso:** cria/atualiza um cargo só seu com a cor escolhida. "
                "Útil pra destacar seu nome no chat sem depender da staff."
            ),
            inline=False,
        )
        others_page.add_field(
            name="🎛️ Toggles do TTS",
            value=(
                f"**Slash:** {toggle_menu_slash}\n"
                "**Uso:** painel guiado pros toggles de TTS — auto-join, "
                "ignore-list, filtros e mais. Mais rápido que decorar "
                "comandos individuais."
            ),
            inline=False,
        )
        others_page.add_field(
            name="🏠 Painel completo do servidor",
            value=(
                f"**Slash:** {tts_server_menu_slash}\n"
                f"**Prefixo:** {prefix_server_panel}\n"
                "**Uso:** painel mestre do servidor. Define prefixos, engine "
                "padrão, permissões. Exige Manage Server."
            ),
            inline=False,
        )
        pages.append(others_page)

        utility_page = discord.Embed(
            title="🧰 Utilidades",
            description="Comandos gerais do bot para consulta rápida.",
            color=discord.Color.teal(),
            timestamp=discord.utils.utcnow(),
        )
        utility_page.add_field(
            name="🏓 Ping",
            value=(
                f"**Slash:** {ping_slash}\n"
                "**Uso:** mostra latência, uptime, uso de recursos e status geral do bot."
            ),
            inline=False,
        )
        utility_page.add_field(
            name="❓ Help",
            value=(
                f"**Slash:** {help_slash}\n"
                f"**Prefixo:** {prefix_help}\n"
                "**Uso:** abre esta central de ajuda com paginação por botão."
            ),
            inline=False,
        )
        utility_page.add_field(
            name="🚀 Dica final",
            value=(
                f"Para configurar quase tudo sem decorar sintaxe, começa por {tts_menu_slash} ou {prefix_panel}."
            ),
            inline=False,
        )
        pages.append(utility_page)

        fichas_page = discord.Embed(
            title="🪙 Economia",
            description="Saldo, daily, recarga, ranking e atalhos da economia do servidor.",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        fichas_page.add_field(
            name="📌 Seus atalhos principais",
            value=(
                f"**Saldo:** `{bot_prefix}ficha` ou só `ficha`\n"
                f"**Extrato:** `extrato` (10 últimas movimentações)\n"
                f"**Daily:** `{bot_prefix}daily` ou só `daily`\n"
                "**Recarga:** `recarga`\n"
                f"**Ranking:** `{bot_prefix}rank` ou só `rank`\n"
                "**Pagar alguém:** `pay @usuário valor`\n"
                "**Mendigar:** `mendigar valor` ou `mendigar valor @usuário`"
            ),
            inline=False,
        )
        fichas_page.add_field(
            name="🎁 Daily",
            value=(
                f"**Comando:** `{bot_prefix}daily`\n"
                "**Uso:** resgata fichas normais, +10 fichas bônus e libera os giros extras do dia. As fichas bônus saem antes das normais."
            ),
            inline=False,
        )
        fichas_page.add_field(
            name="🔋 Recarga",
            value=(
                "**Trigger:** `recarga`\n"
                "**Uso:** restaura seu saldo para **100** quando ele estiver abaixo de **15**.\n"
                "**Observação:** a recarga tem cooldown próprio."
            ),
            inline=False,
        )
        fichas_page.add_field(
            name="🏆 Ranking e administração",
            value=(
                f"**Ranking:** `{bot_prefix}rank`\n"
                f"**Resetar um usuário:** `{bot_prefix}resetficha @usuário`\n"
                f"**Resetar o servidor:** `{bot_prefix}resetfichasservidor`\n"
                "Os resets são voltados para staff."
            ),
            inline=False,
        )
        fichas_page.add_field(
            name="⚙️ Configuração (staff)",
            value=(
                f"**Slash:** {economia_slash}\n"
                "**Uso:** ativa/desativa a economia no servidor, define cargo "
                "staff específico, gerencia roles que recebem features extras. "
                "Exige permissão Expulsar Membros ou cargo staff configurado."
            ),
            inline=False,
        )
        pages.append(fichas_page)

        jogos_page = discord.Embed(
            title="🎮 Jogos",
            description="Apostas rápidas, lobbies e jogos de mesa. Triggers são palavras digitadas sozinhas no chat.",
            color=discord.Color.dark_magenta(),
            timestamp=discord.utils.utcnow(),
        )
        jogos_page.add_field(
            name="🎰 Apostas rápidas",
            value=(
                "**Trigger:** `roleta`\n"
                "**Trigger:** `carta` ou `cartas`\n"
                "**Uso:** faz uma rodada rápida. Se faltar saldo, o jogo avisa antes de te jogar no vermelho."
            ),
            inline=False,
        )
        jogos_page.add_field(
            name="🔫 Rodadas e lobbies",
            value=(
                "**Trigger:** `buckshot`\n"
                "**Trigger:** `alvo`\n"
                "**Trigger:** `corrida`\n"
                "**Uso:** abre uma rodada para entrar e disputar o prêmio no fim. Quem estiver no vermelho precisa confirmar antes de entrar."
            ),
            inline=False,
        )
        jogos_page.add_field(
            name="🃏 Poker e Truco",
            value=(
                "**Trigger:** `poker` — abre a mesa de poker com entrada própria.\n"
                "**Trigger:** `truco @usuário` — desafio de truco 1v1.\n"
                "**Trigger:** `truco2` — truco em duplas (2v2)."
            ),
            inline=False,
        )
        jogos_page.add_field(
            name="🦹 Roubo",
            value=(
                "**Trigger:** `roubar @usuário` ou `rob @usuário`\n"
                "**Uso:** tenta roubar parte do saldo do alvo. Tem chance de "
                "falhar e tem janela com cooldown."
            ),
            inline=False,
        )
        jogos_page.add_field(
            name="💸 Atalhos úteis",
            value=(
                "**Pagar:** `pay @usuário valor`\n"
                "**Pedir:** `mendigar valor` ou `mendigar valor @usuário`\n"
                "**Encerrar buckshot:** `atirar`\n"
                "**Disparar no alvo:** `disparar`"
            ),
            inline=False,
        )
        pages.append(jogos_page)

        if self.bot.user and self.bot.user.display_avatar:
            avatar_url = self.bot.user.display_avatar.url
            for index, embed in enumerate(pages, start=1):
                embed.set_thumbnail(url=avatar_url)
                embed.set_footer(text=f"Página {index}/{len(pages)} • Use os botões abaixo para navegar")

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
        pages = self._build_help_embeds(guild=guild, prefixes=prefixes, root_ids=root_ids)
        view = HelpPaginatorView(
            self,
            owner_id=owner.id,
            pages=pages,
            command_mention=slash_mention(root_ids, root="help", path="help"),
            prefix_hint=f"`{prefixes['bot_prefix']}help`",
        )

        if interaction is not None:
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=pages[0], view=view, ephemeral=ephemeral)
                try:
                    view.message = await interaction.original_response()
                except Exception:
                    pass
            else:
                view.message = await interaction.followup.send(embed=pages[0], view=view, ephemeral=ephemeral)
            return

        view.message = await responder.send(embed=pages[0], view=view)

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
