import inspect
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

import config


class HelpPaginatorView(discord.ui.View):
    def __init__(self, cog: "Utility", *, owner_id: int, pages: list[discord.Embed], timeout: float = 180):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.pages = pages
        self.page_index = 0
        self.message: discord.Message | None = None
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        total = max(1, len(self.pages))
        at_start = self.page_index <= 0
        at_end = self.page_index >= total - 1

        self.prev_button.disabled = at_start
        self.next_button.disabled = at_end
        self.page_button.label = f"{self.page_index + 1}/{total}"

        self.clear_items()
        if not at_start:
            self.add_item(self.first_button)
        self.add_item(self.prev_button)
        self.add_item(self.page_button)
        self.add_item(self.next_button)
        if not at_end:
            self.add_item(self.last_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
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
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(label="◀️◀️", style=discord.ButtonStyle.secondary)
    async def first_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page_index = 0
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page_index], view=self)

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page_index > 0:
            self.page_index -= 1
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page_index], view=self)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.primary, disabled=True)
    async def page_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page_index < len(self.pages) - 1:
            self.page_index += 1
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page_index], view=self)

    @discord.ui.button(label="▶️▶️", style=discord.ButtonStyle.secondary)
    async def last_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page_index = max(0, len(self.pages) - 1)
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page_index], view=self)


class Utility(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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

    async def _fetch_root_command_ids(self, guild: discord.Guild | None) -> dict[str, int]:
        command_ids: dict[str, int] = {}
        fetch_targets: list[discord.Guild | None] = []
        if guild is not None:
            fetch_targets.append(guild)
        fetch_targets.append(None)

        for target in fetch_targets:
            try:
                commands_list = await self.bot.tree.fetch_commands(guild=target)
            except Exception:
                continue
            for cmd in commands_list:
                name = str(getattr(cmd, "name", "") or "").strip()
                cmd_id = getattr(cmd, "id", None)
                if not name or not cmd_id or name in command_ids:
                    continue
                command_ids[name] = int(cmd_id)
        return command_ids

    def _slash_mention(self, root_ids: dict[str, int], *, root: str, path: str) -> str:
        cmd_id = root_ids.get(root)
        if cmd_id:
            return f"</{path}:{cmd_id}>"
        return f"`/{path}`"

    def _build_help_embeds(self, *, guild: discord.Guild | None, prefixes: dict[str, str], root_ids: dict[str, int]) -> list[discord.Embed]:
        bot_prefix = prefixes["bot_prefix"]
        gtts_prefix = prefixes["gtts_prefix"]
        edge_prefix = prefixes["edge_prefix"]
        gcloud_prefix = prefixes["gcloud_prefix"]

        help_slash = self._slash_mention(root_ids, root="help", path="help")
        ping_slash = self._slash_mention(root_ids, root="ping", path="ping")
        tts_menu_slash = self._slash_mention(root_ids, root="tts", path="tts menu")
        tts_status_slash = self._slash_mention(root_ids, root="tts", path="tts status")
        tts_user_slash = self._slash_mention(root_ids, root="tts", path="tts usuario")
        tts_server_menu_slash = self._slash_mention(root_ids, root="tts", path="tts server menu")

        guild_name = getattr(guild, "name", None) or "este servidor"
        prefix_help = f"`{bot_prefix}help`"
        prefix_panel = f"`{bot_prefix}panel`"
        prefix_server_panel = f"`{bot_prefix}panel_server`"
        prefix_join = f"`{bot_prefix}join`"
        prefix_leave = f"`{bot_prefix}leave`"
        prefix_clear = f"`{bot_prefix}clear`"
        prefix_reset = f"`{bot_prefix}reset @usuário`"
        prefix_set_lang = f"`{bot_prefix}set lang pt`"

        pages: list[discord.Embed] = []

        overview = discord.Embed(
            title="📘 Central de ajuda do TTS",
            description=(
                f"Guia rápido com os principais comandos do bot, separado por categoria e com exemplos de uso em {guild_name}."
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
                "**Página 5** • utilidades"
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
        overview.add_field(
            name="🟢 Legenda",
            value=(
                "🟢 livre para usar\n"
                "🏠 funciona no servidor\n"
                "🔒 exige `Expulsar Membros`"
            ),
            inline=False,
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
        root_ids = await self._fetch_root_command_ids(guild)
        pages = self._build_help_embeds(guild=guild, prefixes=prefixes, root_ids=root_ids)
        view = HelpPaginatorView(self, owner_id=owner.id, pages=pages)

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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.content:
            return

        prefixes = await self._get_prefix_data(message.guild)
        bot_prefix = prefixes["bot_prefix"]
        lowered = message.content.strip().lower()
        if lowered != f"{bot_prefix}help":
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
