"""Launcher público do TTS.

Concentra a apresentação compacta de Edge/gTTS e as ações do launcher sem
conhecer síntese, fila, streaming, Worker, Termux ou APK. A classe canônica usa
nomes em português; ``ui.py`` mantém a fachada legada.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Type

import discord
import config

from ..ttv import resolve_preferences, voice_label

from ..utils.embed import human_language_name, human_voice_name
from .controles_paineis import BotaoLancadorPublicoTTS
from .modais_simples import ModalApelidoFalado
from .modais_vozes_online import ModalConfiguracaoEdge, ModalConfiguracaoGTTS, ModalConfiguracaoTeto
from .operacoes_painel import DESCRICAO_LANCADOR_TTS, enviar_modal_configuracao_com_fallback
from .visoes_layout import VisaoLayoutBaseTTS


EnviadorModalFallback = Callable[..., Awaitable[None]]


class VisaoLancadorPublicoTTS(VisaoLayoutBaseTTS):
    def __init__(
        self,
        cog: "TTSVoice",
        id_dono: int,
        id_servidor: int,
        *,
        duracao: float = 300,
        classe_botao: Type[discord.ui.Button] = BotaoLancadorPublicoTTS,
        classe_modal_edge: Type[discord.ui.Modal] = ModalConfiguracaoEdge,
        classe_modal_gtts: Type[discord.ui.Modal] = ModalConfiguracaoGTTS,
        classe_modal_teto: Type[discord.ui.Modal] = ModalConfiguracaoTeto,
        classe_modal_apelido: Type[discord.ui.Modal] = ModalApelidoFalado,
        enviar_modal_fallback: EnviadorModalFallback = enviar_modal_configuracao_com_fallback,
        descricao_lancador: str = DESCRICAO_LANCADOR_TTS,
        id_usuario_alvo: int | None = None,
        nome_usuario_alvo: str | None = None,
    ):
        super().__init__(
            cog,
            id_dono,
            id_servidor,
            timeout=duracao,
            target_user_id=id_usuario_alvo,
            target_user_name=nome_usuario_alvo,
        )
        self.panel_kind = "launcher"
        self._classe_botao = classe_botao
        self._classe_modal_edge = classe_modal_edge
        self._classe_modal_gtts = classe_modal_gtts
        self._classe_modal_teto = classe_modal_teto
        self._classe_modal_apelido = classe_modal_apelido
        self._enviar_modal_fallback = enviar_modal_fallback
        self._descricao_lancador = str(descricao_lancador)
        self._guild_defaults, self._user_settings = self._carregar_configuracoes_lancador()
        self._reconstruir_itens()

    def painel_componentes_v2(self) -> bool:
        return bool(
            hasattr(discord.ui, "LayoutView")
            and isinstance(self, getattr(discord.ui, "LayoutView"))
            and hasattr(discord.ui, "Container")
            and hasattr(discord.ui, "TextDisplay")
            and hasattr(discord.ui, "ActionRow")
            and hasattr(discord.ui, "Section")
        )

    def _carregar_configuracoes_lancador(self) -> tuple[dict, dict]:
        db = self.cog._get_db()
        padroes_servidor: dict = {}
        configuracoes_usuario: dict = {}
        if db is None:
            return padroes_servidor, configuracoes_usuario
        try:
            if hasattr(db, "get_guild_tts_defaults"):
                padroes_servidor = dict(db.get_guild_tts_defaults(self.guild_id) or {})
        except Exception as erro:
            print(f"[tts_panel] falha ao carregar padrões do launcher: {erro!r}")
        id_configurado = int(self.target_user_id or self.owner_id or 0)
        if id_configurado > 0:
            try:
                if hasattr(db, "get_user_tts"):
                    configuracoes_usuario = dict(db.get_user_tts(self.guild_id, id_configurado) or {})
            except Exception as erro:
                print(f"[tts_panel] falha ao carregar ajustes pessoais do launcher: {erro!r}")
        return padroes_servidor, configuracoes_usuario

    def _apelido_falado_ativo(self) -> bool:
        return bool((self._guild_defaults or {}).get("announce_author", False))

    def _teto_disponivel(self) -> bool:
        # Visibilidade do bloco segue a conexão; a amostra verifica o renderer.
        online = getattr(self.cog, "_tts_phone_worker_online_for_ui", None)
        if callable(online):
            try:
                return bool(online())
            except Exception:
                return False
        checker = getattr(self.cog, "_ttv_voice_status", None)
        if callable(checker):
            try:
                return bool(checker().get("online"))
            except Exception:
                return False
        return False

    def _preferencias_ttv(self) -> dict:
        default = getattr(config, "TTS_TETO_DEFAULT_PITCH_SEMITONES", 0.0)
        try:
            return resolve_preferences(self._user_settings, default_pitch=default)
        except ValueError:
            # Exibir um ID inválido como indisponível, sem trocar a seleção salva.
            settings = dict(self._user_settings or {})
            voice_id = str(settings.pop("ttv_voice_id", ""))
            preferences = resolve_preferences(settings, default_pitch=default)
            preferences["ttv_voice_id"] = voice_id
            return preferences

    def _tom_teto_atual(self) -> str:
        return self._preferencias_ttv()["ttv_pitch_semitones"]

    def _resumo_ttv(self) -> str:
        preferences = self._preferencias_ttv()
        try:
            voice = voice_label(preferences["ttv_voice_id"])
        except ValueError:
            voice = "Voz indisponível"
        pitch = float(preferences["ttv_pitch_semitones"])
        rate = preferences["ttv_speech_rate"]
        parts = [f"Voz: {self._codigo(voice)}"]
        if pitch != 0:
            tone = f"{pitch:+g}st".replace(".", ",")
            parts.append(f"Tom: {self._codigo(tone)}")
        if rate != 1:
            speed = f"{rate * 100:g}%".replace(".", ",")
            parts.append(f"Velocidade: {self._codigo(speed)}")
        return " · ".join(parts)

    @staticmethod
    def _separador():
        try:
            return discord.ui.Separator(visible=True)
        except TypeError:
            return discord.ui.Separator()

    def _texto_introducao(self) -> str:
        titulo = "TTS"
        if self.target_user_id and self.target_user_name:
            titulo = f"TTS de {self.target_user_name}"
        return f"### {titulo}\n{self._descricao_lancador}"

    def _alvo_interacao(self, interaction: discord.Interaction) -> tuple[int, str]:
        if self.target_user_id:
            nome = str(self.target_user_name or "").strip()
            if not nome:
                member = getattr(getattr(interaction, "guild", None), "get_member", lambda _id: None)(self.target_user_id)
                nome = self.cog._member_panel_name(member)
            return int(self.target_user_id), nome
        usuario = interaction.user
        return int(usuario.id), self.cog._member_panel_name(usuario)

    def _painel_de_outro_usuario(self) -> bool:
        return bool(self.target_user_id and int(self.target_user_id) != int(self.owner_id or 0))

    @staticmethod
    def _limpar_configuracao(valor: object) -> str:
        return str(valor or "").strip()

    def _configuracao_servidor(self, chave: str, fallback: str) -> str:
        return self._limpar_configuracao((self._guild_defaults or {}).get(chave)) or str(fallback or "")

    def _configuracao_normalizada(self, chave: str, valor: object) -> str:
        texto = self._limpar_configuracao(valor)
        if chave == "rate":
            normalizado = self.cog._normalize_rate_value(texto)
            return str(normalizado or texto).strip().lower()
        if chave == "pitch":
            normalizado = self.cog._normalize_pitch_value(texto)
            return str(normalizado or texto).strip().lower()
        if chave == "language":
            return texto.replace("_", "-").lower()
        if chave == "voice":
            return texto.lower()
        return texto

    def _diferenca_pessoal(self, chave: str, fallback: str) -> bool:
        pessoal = self._limpar_configuracao((self._user_settings or {}).get(chave))
        if not pessoal:
            return False
        servidor = self._configuracao_servidor(chave, fallback)
        return self._configuracao_normalizada(chave, pessoal) != self._configuracao_normalizada(chave, servidor)

    @staticmethod
    def _codigo(valor: object) -> str:
        texto = str(valor or "").strip().replace("`", "")
        return f"`{texto}`" if texto else ""

    def _resumo_edge(self) -> str:
        partes: list[str] = []
        voz_padrao = str(getattr(config, "EDGE_TTS_VOICE", "pt-BR-FranciscaNeural") or "pt-BR-FranciscaNeural")
        if self._diferenca_pessoal("voice", voz_padrao):
            partes.append(f"Voz: {self._codigo(human_voice_name(self._user_settings.get('voice')))}")
        if self._diferenca_pessoal("rate", "+0%"):
            partes.append(f"Velocidade: {self._codigo(self.cog._normalize_rate_value(self._user_settings.get('rate')) or self._user_settings.get('rate'))}")
        if self._diferenca_pessoal("pitch", "+0Hz"):
            partes.append(f"Tom: {self._codigo(self.cog._normalize_pitch_value(self._user_settings.get('pitch')) or self._user_settings.get('pitch'))}")
        return " · ".join(parte for parte in partes if parte)

    def _resumo_gtts(self) -> str:
        if not self._diferenca_pessoal("language", "pt-br"):
            return ""
        idioma = human_language_name(self._user_settings.get("language"))
        return f"Idioma: {self._codigo(idioma)}"

    def _texto_motor(self, *, motor: str) -> str:
        if motor == "edge":
            prefixo = self._configuracao_servidor("edge_prefix", ",")
            linhas = [
                "**Edge**",
                f"Voz mais personalizável (é mais lenta) · Prefixo {self._codigo(prefixo)}",
            ]
            resumo = self._resumo_edge()
        elif motor == "teto":
            prefixo = self._configuracao_servidor("teto_prefix", "'")
            linhas = [
                "**TTV (TextToVocaloid)**",
                f"Voz da vocaloid escolhida · Prefixo {self._codigo(prefixo)}",
            ]
            resumo = self._resumo_ttv()
        else:
            prefixo = self._configuracao_servidor("gtts_prefix", ".")
            linhas = [
                "**gTTS**",
                f"Voz mais simples (é mais rápida) · Prefixo {self._codigo(prefixo)}",
            ]
            resumo = self._resumo_gtts()
        if resumo:
            linhas.append(f"-# {resumo}")
        return "\n".join(linhas)

    def _criar_botao(self, *, acao: str, rotulo: str, emoji: str | None = None) -> discord.ui.Button:
        try:
            return self._classe_botao(acao=acao, rotulo=rotulo, emoji=emoji)
        except TypeError:
            return self._classe_botao(action=acao, label=rotulo, emoji=emoji)

    def _reconstruir_itens(self) -> None:
        try:
            self.clear_items()
        except Exception:
            pass

        apelido_ativo = self._apelido_falado_ativo()
        if self.painel_componentes_v2():
            botao_edge = self._criar_botao(acao="edge", rotulo="Configurar")
            botao_gtts = self._criar_botao(acao="gtts", rotulo="Configurar")
            container = discord.ui.Container(
                discord.ui.TextDisplay(self._texto_introducao()),
                self._separador(),
                discord.ui.Section(discord.ui.TextDisplay(self._texto_motor(motor="edge")), accessory=botao_edge),
                self._separador(),
                discord.ui.Section(discord.ui.TextDisplay(self._texto_motor(motor="gtts")), accessory=botao_gtts),
            )
            if apelido_ativo:
                botao_apelido = self._criar_botao(acao="spoken_name", rotulo="Alterar")
                container.add_item(self._separador())
                container.add_item(discord.ui.Section(
                    discord.ui.TextDisplay("**Apelido falado**\nEscolha o nome anunciado antes das suas mensagens"),
                    accessory=botao_apelido,
                ))
            if self._teto_disponivel():
                botao_teto = self._criar_botao(acao="teto", rotulo="Configurar")
                container.add_item(self._separador())
                container.add_item(discord.ui.Section(
                    discord.ui.TextDisplay(self._texto_motor(motor="teto")),
                    accessory=botao_teto,
                ))
            if not self._painel_de_outro_usuario():
                prefixo_bot = self._configuracao_servidor(
                    "bot_prefix",
                    str(getattr(config, "BOT_PREFIX", getattr(config, "PREFIX", "_")) or "_"),
                )
                container.add_item(self._separador())
                container.add_item(discord.ui.TextDisplay(
                    f"-# Dica: {self._codigo(prefixo_bot + 'advanced')} ajusta Edge e gTTS."
                ))
            self.add_item(container)
            return

        self.add_item(self._criar_botao(acao="edge", rotulo="Configurar Edge"))
        self.add_item(self._criar_botao(acao="gtts", rotulo="Configurar gTTS"))
        if apelido_ativo:
            self.add_item(self._criar_botao(acao="spoken_name", rotulo="Alterar apelido"))
        if self._teto_disponivel():
            self.add_item(self._criar_botao(acao="teto", rotulo="Configurar TTV"))

    async def _abrir_acao(self, interaction: discord.Interaction, acao: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Esse painel só pode ser usado dentro de um servidor.", ephemeral=True)
            return

        id_alvo, nome_alvo = self._alvo_interacao(interaction)
        mensagem_painel = getattr(interaction, "message", None)
        if acao == "edge":
            await self._enviar_modal_fallback(
                interaction,
                lambda: self._classe_modal_edge(
                    self.cog,
                    mensagem_painel,
                    server=False,
                    target_user_id=id_alvo,
                    target_user_name=nome_alvo,
                ),
                lambda: self._classe_modal_edge(
                    self.cog,
                    mensagem_painel,
                    server=False,
                    target_user_id=id_alvo,
                    target_user_name=nome_alvo,
                    force_text_fallback=True,
                ),
                context="public-edge",
            )
            return

        if acao == "gtts":
            await self._enviar_modal_fallback(
                interaction,
                lambda: self._classe_modal_gtts(
                    self.cog,
                    mensagem_painel,
                    server=False,
                    target_user_id=id_alvo,
                    target_user_name=nome_alvo,
                ),
                lambda: self._classe_modal_gtts(
                    self.cog,
                    mensagem_painel,
                    server=False,
                    target_user_id=id_alvo,
                    target_user_name=nome_alvo,
                    force_text_fallback=True,
                ),
                context="public-gtts",
            )
            return

        if acao == "teto":
            if not self._teto_disponivel():
                await interaction.response.send_message("O worker está offline. Abra o painel novamente quando ele voltar.", ephemeral=True)
                return
            def criar_modal(*, fallback=False):
                modal = self._classe_modal_teto(
                    self.cog, mensagem_painel, server=False,
                    target_user_id=id_alvo, target_user_name=nome_alvo,
                    force_text_fallback=fallback,
                )
                modal.owner_id = int(interaction.user.id)
                modal.guild_id = int(interaction.guild.id)
                return modal
            await self._enviar_modal_fallback(
                interaction, lambda: criar_modal(), lambda: criar_modal(fallback=True), context="public-ttv",
            )
            return

        if acao == "spoken_name" and self._apelido_falado_ativo():
            valor_atual = self.cog._get_saved_spoken_name(interaction.guild.id, id_alvo)
            await interaction.response.send_modal(
                self._classe_modal_apelido(
                    self.cog,
                    mensagem_painel,
                    target_user_id=id_alvo,
                    target_user_name=nome_alvo,
                    current_value=valor_atual,
                )
            )
            return

        await interaction.response.send_message("Opção indisponível.", ephemeral=True)

    async def _abrir_meu_tts(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Comando indisponível", "Esse painel só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return
        nome_alvo = self.cog._member_panel_name(interaction.user)
        embed = await self.cog._build_settings_embed(
            interaction.guild.id,
            interaction.user.id,
            server=False,
            panel_kind="user",
            target_user_name=nome_alvo,
            viewer_user_id=interaction.user.id,
        )
        visao = self.cog._build_panel_view(
            interaction.user.id,
            interaction.guild.id,
            server=False,
            target_user_id=interaction.user.id,
            target_user_name=nome_alvo,
        )
        mensagem = await self.cog._respond(interaction, embed=embed, view=visao, ephemeral=True)
        visao.message = mensagem

    async def _abrir_tts_servidor(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Comando indisponível", "Esse painel só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return
        if not getattr(getattr(interaction.user, "guild_permissions", None), "kick_members", False):
            await interaction.response.send_message(
                embed=self.cog._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para abrir o painel do servidor.", ok=False),
                ephemeral=True,
            )
            return
        embed = await self.cog._build_settings_embed(
            interaction.guild.id,
            interaction.user.id,
            server=True,
            panel_kind="server",
            viewer_user_id=interaction.user.id,
        )
        visao = self.cog._build_panel_view(interaction.user.id, interaction.guild.id, server=True)
        mensagem = await self.cog._respond(interaction, embed=embed, view=visao, ephemeral=True)
        visao.message = mensagem

    async def _abrir_ajuda(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=self.cog._make_embed(
                "Ajuda do TTS",
                "Edge, gTTS e TTV (TextToVocaloid) são modos de voz. O prefixo é só o símbolo digitado antes da frase.\n\n"
                "Exemplos:\n"
                "• `,bom dia` usa Edge.\n"
                "• `.bom dia` usa gTTS.\n"
                "• `'bom dia` usa a vocaloid escolhida no TTV. A leitura precisa estar disponível.",
                ok=True,
            ),
            ephemeral=True,
        )

    # Fachadas internas legadas. Mantidas para os componentes existentes e
    # para tornar a migração incremental sem alterar contratos observáveis.
    def is_components_v2_panel(self) -> bool:
        return self.painel_componentes_v2()

    def _load_launcher_settings(self) -> tuple[dict, dict]:
        return self._carregar_configuracoes_lancador()

    def _spoken_name_enabled(self) -> bool:
        return self._apelido_falado_ativo()

    @staticmethod
    def _separator():
        return VisaoLancadorPublicoTTS._separador()

    def _intro_text(self) -> str:
        return self._texto_introducao()

    @staticmethod
    def _clean_setting(value: object) -> str:
        return VisaoLancadorPublicoTTS._limpar_configuracao(value)

    def _server_setting(self, key: str, fallback: str) -> str:
        return self._configuracao_servidor(key, fallback)

    def _normalized_setting(self, key: str, value: object) -> str:
        return self._configuracao_normalizada(key, value)

    def _is_personal_difference(self, key: str, fallback: str) -> bool:
        return self._diferenca_pessoal(key, fallback)

    @staticmethod
    def _code(value: object) -> str:
        return VisaoLancadorPublicoTTS._codigo(value)

    def _edge_summary(self) -> str:
        return self._resumo_edge()

    def _gtts_summary(self) -> str:
        return self._resumo_gtts()

    def _engine_text(self, *, engine: str) -> str:
        return self._texto_motor(motor=engine)

    def _rebuild_items(self) -> None:
        self._reconstruir_itens()

    async def _open_action(self, interaction: discord.Interaction, action: str) -> None:
        await self._abrir_acao(interaction, action)

    async def _open_my_tts(self, interaction: discord.Interaction):
        await self._abrir_meu_tts(interaction)

    async def _open_server_tts(self, interaction: discord.Interaction):
        await self._abrir_tts_servidor(interaction)

    async def _open_help(self, interaction: discord.Interaction):
        await self._abrir_ajuda(interaction)
